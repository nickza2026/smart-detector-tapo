"""Single-writer visual occupancy history and notification worker.

PRESENT is only a candidate object, not a verified banknote classification.
Emergency receipts and acknowledgement are intentionally unsupported.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import socket
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import parse, request

from PIL import Image

from smart_tapo.frames import atomic_bytes, read_frame

LOG = logging.getLogger(__name__)


def pushover_send(message):
    """Priority-one fallback only; never log provider bodies or credentials."""
    token, user = os.environ.get('PUSHOVER_TOKEN'), os.environ.get('PUSHOVER_USER')
    if not token or not user:
        return 'failed'
    body = parse.urlencode(dict(token=token, user=user, message=message, priority=1)).encode()
    try:
        with request.urlopen('https://api.pushover.net/1/messages.json', data=body, timeout=10) as response:
            payload = json.loads(response.read(65536))
            return 'success' if payload.get('status') == 1 else 'failed'
    except (TimeoutError, socket.timeout):
        return 'ambiguous'
    except Exception:
        return 'failed'


class Worker:
    def __init__(self, config, detector=None, sender=None, clock=time.time):
        self.config = config
        self.clock = clock
        if detector is None:
            from smart_tapo.vision import Detector
            detector = Detector(config)
        self.detector = detector
        self.sender = sender or pushover_send
        if config.data_dir.is_symlink() or any((config.data_dir / name).is_symlink() for name in ('worker.sqlite3', 'worker.sqlite3-wal', 'worker.sqlite3-shm')):
            raise ValueError('Worker data and database must not be symlinks')
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(config.data_dir / 'worker.sqlite3'))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS frames(id TEXT PRIMARY KEY, captured REAL, marker TEXT, image TEXT);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, kind TEXT, started REAL, closed REAL,
                last_success REAL, retry_at REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS evidence(event TEXT, role TEXT, path TEXT, PRIMARY KEY(event,role));
            CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,event TEXT,created REAL,status TEXT);
            CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT);
            CREATE INDEX IF NOT EXISTS frames_captured_idx ON frames(captured);
            CREATE INDEX IF NOT EXISTS events_closed_idx ON events(closed);
            CREATE INDEX IF NOT EXISTS events_started_idx ON events(started DESC,id DESC);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(events)')}
        for column in ('last_present', 'first_clear'):
            if column not in columns:
                self.db.execute('ALTER TABLE events ADD COLUMN ' + column + ' REAL')
        owner = self.db.execute("SELECT value FROM settings WHERE name='camera_id'").fetchone()
        if owner and owner['value'] != config.camera_id:
            self.db.close()
            raise ValueError('Use a separate data_dir for each camera; this database belongs to another camera')
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('camera_id',?)", (config.camera_id,))
        # Crash during transport is ambiguous, never silently called successful/retried at startup.
        with self.db:
            self.db.execute("UPDATE notifications SET status='ambiguous' WHERE status='pending'")
            self.db.execute('UPDATE events SET retry_at=? WHERE closed IS NULL', (clock() + 60,))
            self.db.execute("UPDATE events SET kind='incomplete',closed=? WHERE closed IS NULL AND kind='candidate'", (clock(),))
        self.count = self.empty = self.live_count = 0
        self.previous = None
        self.view_anchor = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.last_cleanup = float('-inf')

    def active(self):
        return self.db.execute('SELECT * FROM events WHERE closed IS NULL ORDER BY started DESC LIMIT 1').fetchone()

    def snapshot(self, event, role, image, observation=None, *, full_frame=False):
        existing = self.db.execute('SELECT 1 FROM evidence WHERE event=? AND role=?', (event, role)).fetchone()
        if existing:
            return
        path = self.config.data_dir / 'events' / self.config.camera_id / (event + '-' + role + '.png')
        base = self.config.data_dir.absolute()
        if any(p.is_symlink() for p in (path, *path.parents) if p == base or base in p.parents):
            raise ValueError('Evidence path must not contain symlinks')
        encoded = b'' if full_frame else getattr(observation, 'roi_png', b'')
        if not encoded:
            image = Path(image)
            if full_frame:
                root = self.config.data_dir / 'frames' / self.config.camera_id
                if (image.resolve().parent != root.resolve()
                        or any(p.is_symlink() for p in (image, *image.parents)
                               if p == base or base in p.parents)):
                    raise ValueError('Full evidence source must be in the managed spool')
            with Image.open(image) as source:
                if full_frame and (source.format != 'PNG' or source.size != self.config.expected_size):
                    raise ValueError('Invalid full screenshot')
                source.load()
                captured = source if full_frame else source.crop(self.config.roi.box())
                content = io.BytesIO()
                captured.save(content, format='PNG')
            encoded = content.getvalue()
        atomic_bytes(path, encoded)
        self.db.execute('INSERT INTO evidence VALUES(?,?,?)', (event, role, str(path)))

    def drain(self, wait=False):
        if self.pending is None:
            return
        future, notification, event = self.pending
        if not wait and not future.done():
            return
        try:
            result = future.result()
            if result not in ('success', 'failed', 'ambiguous'):
                result = 'failed'
        except Exception:
            result = 'ambiguous'
        now = self.clock()
        with self.db:
            self.db.execute('UPDATE notifications SET status=? WHERE id=?', (result, notification))
            if result == 'success':
                self.db.execute('UPDATE events SET last_success=?,retry_at=0 WHERE id=?', (now, event))
            else:
                self.db.execute('UPDATE events SET retry_at=? WHERE id=?', (now + 60, event))
        self.pending = None

    def process(self, frame, image, marker):
        self.drain()
        if self.db.execute('SELECT 1 FROM frames WHERE id=?', (frame.frame_id,)).fetchone():
            return False
        now = self.clock()
        live = 0 <= now - frame.captured_at <= self.config.max_live_age_seconds
        previous = self.previous
        continuity = previous is None or (
            frame.session_id == previous.session_id and frame.sequence == previous.sequence + 1
            and 0 < frame.captured_monotonic - previous.captured_monotonic <= self.config.max_frame_gap_seconds
            and 0 < frame.captured_at - previous.captured_at <= self.config.max_frame_gap_seconds)
        valid = frame.captured_at <= now and frame.fresh and frame.health == 'healthy'
        observation = None
        try:
            observation = self.detector.observe(image) if valid else None
            label = observation.label if observation is not None else 'UNKNOWN'
        except Exception:
            LOG.warning('Frame observation unavailable')
            label = 'UNKNOWN'
        if label not in ('PRESENT', 'EMPTY'):
            label = 'UNKNOWN'
        corners = getattr(observation, 'view_corners', ())
        if label == 'UNKNOWN':
            self.view_anchor = None
            reason = getattr(observation, 'alignment_reason', '')
            if reason:
                LOG.info('Alignment unavailable: %s; consecutive proof reset', reason)
        elif corners:
            if self.view_anchor is not None:
                shift = max(math.hypot(corners[i] - self.view_anchor[i], corners[i+1] - self.view_anchor[i+1])
                            for i in range(0, 8, 2))
                if shift >= self.config.alignment_reset_pixels:
                    continuity = False
                    LOG.info('Camera view changed: resetting consecutive proof')
            if self.view_anchor is None or not continuity:
                self.view_anchor = corners
        # A gap's current frame starts a new proof, but cannot extend the old proof.
        if not continuity:
            self.count = self.empty = self.live_count = 0
        self.live_count = self.live_count + 1 if live and valid and label == 'PRESENT' else 0
        self.previous = frame
        send = None
        with self.db:
            self.db.execute('INSERT INTO frames VALUES(?,?,?,?)', (frame.frame_id, frame.captured_at, str(marker), str(image)))
            event = self.active()
            if not continuity and event and event['kind'] != 'stuck':
                self.db.execute("UPDATE events SET kind='incomplete',closed=? WHERE id=?", (frame.captured_at, event['id']))
                event = None
            if label == 'UNKNOWN':
                self.count = self.empty = 0
                if event:
                    self.db.execute('UPDATE events SET first_clear=NULL WHERE id=?', (event['id'],))
                if event and event['kind'] != 'stuck':
                    self.db.execute("UPDATE events SET kind='incomplete',closed=? WHERE id=?", (frame.captured_at, event['id']))
            elif label == 'EMPTY':
                self.count = 0
                self.empty += 1
                if event and self.empty == 1:
                    self.db.execute('UPDATE events SET first_clear=? WHERE id=?', (frame.captured_at, event['id']))
                if event and self.empty >= self.config.clear_consecutive_frames:
                    self.snapshot(event['id'], 'clear', image, observation)
                    kind = 'stuck' if event['kind'] == 'stuck' else 'normal_visual'
                    self.db.execute('UPDATE events SET kind=?,closed=? WHERE id=?', (kind, frame.captured_at, event['id']))
            else:
                self.empty = 0
                self.count += 1
                if event is None:
                    event_id = uuid.uuid4().hex
                    self.db.execute("INSERT INTO events(id,kind,started) VALUES(?,'candidate',?)", (event_id, frame.captured_at))
                    self.snapshot(event_id, 'first', image, observation)
                    self.snapshot(event_id, 'first_full', image, full_frame=True)
                    event = self.active()
                self.db.execute('UPDATE events SET last_present=?,first_clear=NULL WHERE id=?', (frame.captured_at, event['id']))
                if self.count >= self.config.stuck_consecutive_frames:
                    self.db.execute("UPDATE events SET kind='stuck' WHERE id=?", (event['id'],))
                    self.snapshot(event['id'], 'threshold', image, observation)
                    due = event['last_success'] is None or now - event['last_success'] >= max(60, self.config.reminder_interval_seconds)
                    if (self.live_count >= self.config.stuck_consecutive_frames and due
                            and now >= event['retry_at'] and self.pending is None):
                        notification = uuid.uuid4().hex
                        status = 'pending' if self.config.notify_enabled else 'shadow'
                        self.db.execute('INSERT INTO notifications VALUES(?,?,?,?)', (notification, event['id'], now, status))
                        if status == 'shadow':
                            self.db.execute('UPDATE events SET last_success=? WHERE id=?', (now, event['id']))
                        else:
                            send = (notification, event['id'])
        if send:
            self.pending = (self.executor.submit(self.sender, 'Suspected banknote/object stuck: ' + self.config.camera_id), *send)
        return True

    def scan(self):
        self.drain()
        frames = []
        for marker in (self.config.data_dir / 'frames' / self.config.camera_id).glob('*.ready.json'):
            identity = marker.name[:-len('.ready.json')]
            if self.db.execute('SELECT 1 FROM frames WHERE id=?', (identity,)).fetchone():
                continue
            try:
                frame, image = read_frame(marker, self.config)
                frames.append((frame.captured_at, frame.sequence, frame.frame_id, frame, image, marker))
            except (ValueError, OSError, TypeError):
                self.count = self.empty = self.live_count = 0
                LOG.warning('Invalid frame marker; proof reset')
        for _, _, _, frame, image, marker in sorted(frames):
            self.process(frame, image, marker)
        now = self.clock()
        if now - self.last_cleanup >= self.config.cleanup_interval_seconds:
            self.cleanup()
            self.last_cleanup = now

    def unlink_owned(self, path, root):
        path, root = Path(path), Path(root)
        base = self.config.data_dir.absolute()
        unsafe_parent = any(p.is_symlink() for p in (root, *root.parents) if p == base or base in p.parents)
        if unsafe_parent or path.is_symlink() or path.resolve().parent != root.resolve():
            LOG.warning('Unsafe cleanup path skipped')
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            LOG.warning('Unable to unlink worker-owned file')
            return False

    def cleanup(self):
        now = self.clock()
        root = self.config.data_dir / 'frames' / self.config.camera_id
        with self.db:
            for row in self.db.execute('SELECT * FROM frames WHERE captured<? AND image IS NOT NULL', (now - self.config.rolling_frames_minutes * 60,)).fetchall():
                # Only processed DB-owned files; never pending markers, source PNGs or temporary files.
                if self.unlink_owned(row['marker'], root) and self.unlink_owned(row['image'], root):
                    # Keep lightweight deduplication IDs after deleting the image.
                    self.db.execute('UPDATE frames SET marker=NULL,image=NULL WHERE id=?', (row['id'],))
            self.db.execute('DELETE FROM frames WHERE image IS NULL AND captured<?', (now - self.config.event_history_days * 86400,))
            for event in self.db.execute('SELECT * FROM events WHERE closed IS NOT NULL').fetchall():
                pending = self.db.execute("SELECT 1 FROM notifications WHERE event=? AND status IN ('pending','failed','ambiguous')", (event['id'],)).fetchone()
                if pending:
                    continue
                normal = event['kind'] == 'normal_visual'
                ttl = (self.config.deposit_history_hours * 3600 if normal else
                       self.config.alert_evidence_days * 86400 if event['kind'] == 'stuck' else
                       self.config.non_alert_images_minutes * 60)
                if now - event['closed'] > ttl:
                    for row in self.db.execute('SELECT * FROM evidence WHERE event=?', (event['id'],)).fetchall():
                        if self.unlink_owned(row['path'], self.config.data_dir / 'events' / self.config.camera_id):
                            self.db.execute('DELETE FROM evidence WHERE event=? AND role=?', (event['id'], row['role']))
                history_ttl = ttl if normal else self.config.event_history_days * 86400
                if now - event['closed'] > history_ttl and not self.db.execute('SELECT 1 FROM evidence WHERE event=?', (event['id'],)).fetchone():
                    self.db.execute('DELETE FROM notifications WHERE event=?', (event['id'],))
                    self.db.execute('DELETE FROM events WHERE id=?', (event['id'],))

    def close(self):
        self.drain(wait=True)
        self.executor.shutdown(wait=True)
        self.db.close()
