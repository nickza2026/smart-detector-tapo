"""Single-writer visual occupancy history and notification worker.

PRESENT is only a candidate object, not a verified banknote classification.
Emergency receipts and acknowledgement are intentionally unsupported.
"""
from __future__ import annotations

import io
import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, deque
from contextlib import ExitStack, contextmanager
from enum import Enum
from pathlib import Path

from PIL import Image
from smart_tapo.clock_epoch import current_clock_epoch

from smart_tapo.capture import coordination
from smart_tapo.capture.coordination import navigation_lock as ui_lock
from smart_tapo.frames import atomic_bytes, read_frame
from smart_tapo.diagnostics import ALIGNMENT_HINTS, observation_error_code
from smart_tapo.notify_api import AlertMessage, NotifyTarget, deliver_alert
from smart_tapo.passage import RETAIN_SECONDS as PASSAGE_RETAIN_SECONDS, parse_passages

LOG = logging.getLogger(__name__)


NOTIFY_TITLE = 'SmartTapo: สงสัยธนบัตร/วัตถุค้าง'


def notify_sender(config, cancel, *, history=None):
    """Notify API sender bound to config; key comes only from NOTIFY_API_KEY.

    Stuck objects are always critical (alarm until acknowledged on the phone).
    sender(message, notification_id) retries one immutable event (same alert ID
    and Idempotency-Key). success is provider acceptance, never phone delivery.
    """
    from smart_tapo.notify_history import NotifyHistory
    history = history if history is not None else NotifyHistory(config.data_dir)
    def send(message, notification):
        try:
            target = NotifyTarget(config.notify_api_url, os.environ.get('NOTIFY_API_KEY', ''),
                                  history=history, channel='stuck')
            alert = AlertMessage(notification, 'smarttapo:stuck:' + notification, NOTIFY_TITLE,
                                 message, 'critical')
        except ValueError:
            return 'failed'
        return deliver_alert(target, alert, cancel).state
    return send


class ProcessOutcome(Enum):
    DEFERRED = 'navigation_busy'


class Worker:
    def __init__(self, config, detector=None, sender=None, clock=time.time, health_observer=None, preview_observer=None):
        self.preview_observer = preview_observer
        self._preview_reported_at = float('-inf')
        self.health_observer = health_observer
        self._health_reported_at = float('-inf')
        self.config = config
        self.clock = clock
        if detector is None:
            from smart_tapo.vision import Detector
            detector = Detector(config)
        self.detector = detector
        self._send_cancel = threading.Event()
        from smart_tapo.notify_history import NotifyHistory
        self._notify_history = NotifyHistory(config.data_dir)
        self.sender = sender or notify_sender(config, self._send_cancel, history=self._notify_history)
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
        self.present_since = self.empty_since = self.live_since = None
        self.trusted_frame = None
        self.event_seen_at = None
        # Evidence for a full-rate passage that no sampled ROI frame opened.
        self.recent_frames = deque(maxlen=8)
        self._passage_covered_at = None
        self.proof_config = config
        self.previous = None
        self.navigation_generation = None
        self.view_anchor = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.last_cleanup = float('-inf')
        self._diagnostic_state = None
        self._diagnostic_at = float('-inf')
        # Rejection is not a processed/healthy frame. Remember only an unchanged
        # filesystem version so a legacy marker cannot reset every future scan.
        self._rejected_markers = OrderedDict()
        self._rejected_generation = bytes(32)
        self._memory_mode = False
        self._memory_generation = None
        self._memory_session = None
        self._memory_sequence = 0
        self._memory_received = float('-inf')

    def _reset_memory_proof(self):
        self.count = self.empty = self.live_count = 0
        self.present_since = self.empty_since = self.live_since = None
        self.previous = self.trusted_frame = self.view_anchor = None
        self._memory_discontinuity = True

    def memory_generation(self, generation):
        self._memory_mode = True
        if generation != self._memory_generation:
            self._reset_memory_proof()
            self._memory_generation = generation
            self._memory_session = None
            self._memory_sequence = 0
            self._memory_received = float('-inf')

    def process_memory(self, envelope, *, generation, generation_check=None, generation_guard=None,
                       fresh_after=float('-inf'), scope_check=None):
        from smart_tapo.ram_frames import MemoryFrame, binding
        from smart_tapo.capture.rtsp import valid_rtsp_proof
        from smart_tapo.vision import load_png
        self._memory_mode = True
        self.memory_generation(generation)
        if not isinstance(envelope, MemoryFrame) or envelope.generation != generation:
            return False
        started, expired = time.monotonic(), False
        try:
            frame = envelope.unpack(self.config)
            def scope_valid():
                scope = scope_check() if scope_check is not None else (generation, fresh_after)
                return scope[0] == generation and frame.captured_monotonic > max(fresh_after, scope[1])
            if not scope_valid():
                self._reset_memory_proof()
                return False
            if frame.captured_monotonic <= self._memory_received:
                return False
            if frame.session_id == self._memory_session and frame.sequence <= self._memory_sequence:
                return False
            if not valid_rtsp_proof(frame, self.config, envelope.png, now=self.clock()):
                raise ValueError('Invalid RAM provenance')
            current = load_png(io.BytesIO(envelope.png), self.config.expected_size)
            observation = self.detector.observe_image(current)
            if (not scope_valid() or envelope.binding != binding(self.config)
                    or not valid_rtsp_proof(frame, self.config, envelope.png, now=self.clock())
                    or (generation_check is not None and generation_check() != generation)):
                expired = True
                raise ValueError('Expired RAM provenance')
        except Exception:
            self._reset_memory_proof()
            if expired:
                LOG.warning('RAM frame expired during analysis',
                            extra={'analysis_seconds': round(time.monotonic() - started, 3)})
            else:
                LOG.warning('RAM frame rejected; new proof required')
            return False
        with ExitStack() as guard:
            if generation_guard is not None:
                guard.enter_context(generation_guard)
            if (not scope_valid() or (generation_check is not None and generation_check() != generation)
                    or not valid_rtsp_proof(frame, self.config, envelope.png, now=self.clock())):
                self._reset_memory_proof()
                return False
            self._memory_session = frame.session_id
            self._memory_sequence = frame.sequence
            self._memory_received = frame.captured_monotonic
            changed = self._memory_discontinuity
            self._memory_discontinuity = False
            self._preview_safe = False
            result = self._process_decision(frame, envelope.png, None, observation, None, changed)
        # Optional monitoring runs after the decision and outside the epoch lock.
        observer = getattr(self, 'preview_observer', None)
        if observer is not None:
            try:
                observed = time.monotonic()
                if observed - getattr(self, '_preview_reported_at', float('-inf')) >= 1:
                    self._preview_reported_at = observed
                    from smart_tapo.desktop_tracking import make_packet, validate_packet
                    packet = make_packet(current, frame, observation, generation=generation,
                                         observed_monotonic=observed, safe=self._preview_safe)
                    packet = validate_packet(packet, now=observed, max_age=self.config.max_live_age_seconds,
                                             size=self.config.expected_size, generation=generation)
                    if packet is not None:
                        observer(packet)
            except Exception:
                pass
        return result

    def active(self):
        return self.db.execute('SELECT * FROM events WHERE closed IS NULL ORDER BY started DESC LIMIT 1').fetchone()

    @contextmanager
    def _evidence_transaction(self):
        created=[]
        self._new_evidence_files=created
        try:
            with self.db:yield
        except Exception:
            # Roll back only files made by this failed transaction, never old
            # evidence, legacy spool or a path whose filesystem version changed.
            for path,version,digest in created:
                try:
                    info=path.lstat()
                    current=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
                    referenced=self.db.execute('SELECT 1 FROM evidence WHERE path=?',(str(path),)).fetchone()
                    if (not referenced and not path.is_symlink() and current==version
                            and hashlib.sha256(path.read_bytes()).digest()==digest):
                        path.unlink()
                except OSError:
                    LOG.warning('Unable to remove an aborted evidence file')
            raise
        finally:
            self._new_evidence_files=None

    def snapshot(self, event, role, image, observation=None, *, full_frame=False):
        existing = self.db.execute('SELECT 1 FROM evidence WHERE event=? AND role=?', (event, role)).fetchone()
        if existing:
            return
        path = self.config.data_dir / 'events' / self.config.camera_id / (event + '-' + role + '.png')
        base = self.config.data_dir.absolute()
        if any(p.is_symlink() for p in (path, *path.parents) if p == base or base in p.parents):
            raise ValueError('Evidence path must not contain symlinks')
        encoded = b'' if full_frame else getattr(observation, 'roi_png', b'')
        if not encoded and type(image) is bytes:
            from smart_tapo.vision import load_png
            source = load_png(io.BytesIO(image), self.config.expected_size)
            if full_frame:
                # RAM frames are stored (uncompressed) PNG; evidence must stay small.
                content = io.BytesIO()
                source.save(content, format='PNG', compress_level=1)
                encoded = content.getvalue()
            else:
                content = io.BytesIO()
                source.crop(self.config.roi.box()).save(content, format='PNG')
                encoded = content.getvalue()
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
        from smart_tapo.evidence_budget import admit_evidence
        admit_evidence(self.config, len(encoded))
        existed=path.exists()
        atomic_bytes(path, encoded)
        journal=getattr(self,'_new_evidence_files',None)
        if journal is not None and not existed:
            info=path.lstat()
            journal.append((path,(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns),
                            hashlib.sha256(encoded).digest()))
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
        LOG.info('Notification status: %s', result,
                 extra={'notification_context': {'event_id': event, 'notification_id': notification}})

    def _report_detection(self, label, reason, now, observation=None, frame=None, event_id=None, previous=None):
        threshold = self.config.stuck_consecutive_frames
        state = (label, reason, self.count >= threshold, self.live_count >= threshold)
        elapsed = now - self._diagnostic_at
        reached = self._diagnostic_state is not None and (
            (state[2] and not self._diagnostic_state[2])
            or (state[3] and not self._diagnostic_state[3]))
        # Refresh stable progress every five seconds; changing states at most
        # once a second, except a newly reached threshold must remain visible
        # even when one scan processes several frames. Never log private values.
        if (self._diagnostic_state is None or reached or elapsed < 0 or elapsed >= 5
                or (state != self._diagnostic_state and elapsed >= 1)):
            extra = {}
            if frame is not None:
                context = {'frame_id': frame.frame_id, 'session_id': frame.session_id,
                           'sequence': frame.sequence, 'captured_at': frame.captured_at,
                           'captured_monotonic': frame.captured_monotonic, 'fresh': frame.fresh}
                if event_id is not None:
                    context['event_id'] = event_id
                if reason == 'gap' and previous is not None:
                    context.update(previous_sequence=previous.sequence, previous_session_id=previous.session_id,
                                   previous_captured_at=previous.captured_at,
                                   previous_captured_monotonic=previous.captured_monotonic)
                extra = {'frame_context': context}
            LOG.info('Detection status: label=%s count=%d/%d live=%d reason=%s',
                     label, self.count, threshold, self.live_count, reason, extra=extra)
            if reason == 'alignment':
                raw = getattr(observation, 'alignment_reason', '')
                code = raw if type(raw) is str and raw in ALIGNMENT_HINTS else 'unclassified_alignment_failure'
                LOG.info('Alignment unavailable: %s; consecutive proof reset', code)
            if label in ('PRESENT', 'EMPTY') and observation is not None:
                score, ratio = getattr(observation, 'score', None), getattr(observation, 'ratio', None)
                if all(type(value) in (int, float) and math.isfinite(value) for value in (score, ratio)):
                    LOG.info('Visual quality: score=%.3f threshold=%.3f changed=%.4f threshold=%.4f',
                             score, self.config.difference_threshold, ratio, self.config.changed_pixel_ratio)
                area = getattr(observation, 'component_area', 0)
                if type(area) is int and area > 0:
                    LOG.info('Local occupancy: score=%.3f changed=%.4f component=%d',
                             observation.local_score, observation.local_ratio, area)
            self._diagnostic_state, self._diagnostic_at = state, now
            return True
        return False

    def _window_valid(self, frame, navigation):
        if frame.window_proof is None:
            return not getattr(self.config, 'clock_window_enabled', False)
        from smart_tapo.capture.clock_window import valid_window
        return valid_window(frame, self.config, navigation, self.clock(), time.monotonic())

    def process(self, frame, image, marker):
        self.drain()
        if not self._memory_mode and self.db.execute('SELECT 1 FROM frames WHERE id=?', (frame.frame_id,)).fetchone():
            return False
        observation = None
        reason = None
        if self.config.capture_backend == 'rtsp':
            from smart_tapo.capture.rtsp import valid_rtsp_proof
            # Only authenticated, source-bound RTSP markers bypass MEmu fences.
            # Audio navigation never acquires a lock or changes video continuity.
            if not valid_rtsp_proof(frame, self.config, image, now=self.clock()):
                reason = 'rtsp_source_unconfirmed'
            elif frame.fresh and frame.health == 'healthy':
                try:
                    observation = self.detector.observe(image)
                except Exception as error:
                    LOG.warning('Frame observation unavailable: stage=%s cause=%s',
                                'observe', observation_error_code(error))
                    reason = 'detector_error'
            # Expiry and image provenance are rechecked after slow inference.
            if not valid_rtsp_proof(frame, self.config, image, now=self.clock()):
                reason = 'rtsp_source_unconfirmed'
            return self._process_decision(frame, image, marker, observation, reason)
        try:
            before = coordination.navigation_snapshot(self.config)
        except (ValueError, OSError):
            before = None
        if before is None:
            reason = 'navigation_unavailable'
        elif before.recovering or frame.captured_monotonic < before.changed_monotonic:
            reason = 'navigation_recovery'
        elif frame.clock_epoch!=current_clock_epoch():
            reason='clock_epoch_changed'
        elif not self._window_valid(frame, before):
            reason = 'clock_window_unconfirmed'
        elif frame.captured_at <= self.clock() and frame.fresh and frame.health == 'healthy':
            # Inference may be expensive: do it outside the shared UI lock.
            try:
                observation = self.detector.observe(image)
            except Exception as error:
                LOG.warning('Frame observation unavailable: stage=%s cause=%s',
                            'observe', observation_error_code(error))
                reason = 'detector_error'
        with ExitStack() as stack:
            try:
                stack.enter_context(ui_lock(self.config.adb_serial, 5))
            except (ValueError, TimeoutError, OSError) as error:
                if isinstance(error, TimeoutError) or (
                        isinstance(error, ValueError) and str(error) == 'ui_dump_busy'):
                    # No synthetic observation and no consumed marker: the next
                    # normal poll retries this frame with a new snapshot/inference.
                    # One bounded lock attempt per poll, never a busy retry loop.
                    LOG.info('Frame decision deferred: navigation_busy',
                             extra={'frame_context': {'frame_id': frame.frame_id,
                                                      'reason': 'navigation_busy'}})
                    return ProcessOutcome.DEFERRED
                return self._process_decision(frame, image, marker, None, 'navigation_unavailable')
            try:
                current = coordination.navigation_snapshot(self.config)
            except (ValueError, OSError):
                current = None
            if current is None:
                reason = 'navigation_unavailable'
            elif frame.clock_epoch!=current_clock_epoch():
                reason='clock_epoch_changed'
            elif (before is None or current != before or current.recovering
                  or frame.captured_monotonic < current.changed_monotonic):
                reason = 'navigation_recovery'
            elif not self._window_valid(frame, current):
                reason = 'clock_window_unconfirmed'
            generation_changed = current is not None and current.generation != self.navigation_generation
            if generation_changed:
                self.count = self.empty = self.live_count = 0
                self.previous = self.view_anchor = None
                self.navigation_generation = current.generation
            # Commit AND enqueue under the same navigation lock. The provider
            # itself runs asynchronously; existing in-flight sends cannot be undone.
            return self._process_decision(frame, image, marker, observation, reason, generation_changed)

    def _process_decision(self, frame, image, marker, observation, safety_reason, generation_changed=False):
        self.drain()
        if not self._memory_mode and self.db.execute('SELECT 1 FROM frames WHERE id=?', (frame.frame_id,)).fetchone():
            return False
        now = self.clock()
        live = 0 <= now - frame.captured_at <= self.config.max_live_age_seconds
        previous = self.previous
        continuity = previous is None or (
            frame.session_id == previous.session_id and frame.sequence == previous.sequence + 1
            and 0 < frame.captured_monotonic - previous.captured_monotonic <= self.config.max_frame_gap_seconds
            and 0 < frame.captured_at - previous.captured_at <= self.config.max_frame_gap_seconds)
        if generation_changed or self.proof_config != self.config:
            continuity = False
        # Navigation, a changed calibration or a non-RTSP view generation may be a
        # different scene; an RTSP fence (overflow/timestamp fault) is the same source.
        identity_break = bool(safety_reason) or self.proof_config != self.config or (
            generation_changed and self.config.capture_backend != 'rtsp')
        self.proof_config = self.config
        trusted = self.trusted_frame
        if trusted is not None and not (
                trusted.session_id == frame.session_id
                and 0 < frame.captured_at - trusted.captured_at <= self.config.max_frame_gap_seconds
                and 0 < frame.captured_monotonic - trusted.captured_monotonic <= self.config.max_frame_gap_seconds):
            continuity = False
        valid = frame.captured_at <= now and frame.fresh and frame.health == 'healthy'
        reason = safety_reason or ('future' if frame.captured_at > now else
                  'unhealthy' if frame.health != 'healthy' else
                  'not_fresh' if not frame.fresh else 'ok')
        if safety_reason:
            valid = False
            observation = None
            continuity = False
        # A seconds-resolution clock can repeat at a faster capture cadence.
        # Consume such subframes without voting, inferring, extending timestamps
        # or dispatching. The expiry is anchored to the LAST trusted image, not
        # the last consumed subframe, so a frozen clock cannot refresh itself.
        if (self.config.capture_interval_seconds < 1 and not frame.fresh
                and not safety_reason and continuity and trusted is not None
                and frame.health == 'healthy' and live
                and 0 <= now - trusted.captured_at <= self.config.max_live_age_seconds
                and (self.count or self.empty)):
            if not self._memory_mode:
                with self.db:
                    self.db.execute('INSERT INTO frames VALUES(?,?,?,?)',
                                    (frame.frame_id, frame.captured_at, str(marker), str(image)))
            self.previous = frame
            self._report_detection('UNKNOWN', 'not_fresh', now, frame=frame)
            return True
        label = observation.label if observation is not None and valid else 'UNKNOWN'
        if label not in ('PRESENT', 'EMPTY'):
            label = 'UNKNOWN'
        corners = getattr(observation, 'view_corners', ())
        if label == 'UNKNOWN':
            self.view_anchor = None
            alignment_reason = getattr(observation, 'alignment_reason', '')
            if alignment_reason:
                reason = 'alignment'
            elif reason == 'ok':
                reason = 'unknown'
        elif corners:
            if self.view_anchor is not None:
                shift = max(math.hypot(corners[i] - self.view_anchor[i], corners[i+1] - self.view_anchor[i+1])
                            for i in range(0, 8, 2))
                if shift >= self.config.alignment_reset_pixels:
                    continuity = False
                    LOG.info('Camera view changed: resetting consecutive proof')
            if self.view_anchor is None or not continuity:
                self.view_anchor = corners
        if getattr(self, 'preview_observer', None) is not None:
            self._preview_safe = bool(live and valid and label in ('PRESENT', 'EMPTY') and corners)
        # Health uses the same revalidated decision, never occupancy scores.
        observer = getattr(self, 'health_observer', None)
        if observer is not None:
            try:
                producer = time.monotonic()
                if producer - getattr(self, '_health_reported_at', float('-inf')) >= 1:
                    self._health_reported_at = producer
                    observer(dict(observed_at=producer, captured_at=frame.captured_monotonic,
                        healthy=bool(live and valid and label in ('PRESENT', 'EMPTY')),
                        reason=('unknown' if live and valid and label in ('PRESENT', 'EMPTY') else
                                'vision_alignment' if reason == 'alignment' else 'vision_quality'),
                        sequence=frame.sequence, session=frame.session_id))
            except Exception:
                pass  # Monitoring (including its clock) cannot interrupt processing.
        self.trusted_frame = frame if label in ('PRESENT', 'EMPTY') else None
        # A gap's current frame starts a new proof, but cannot extend the old proof.
        if not continuity:
            self.count = self.empty = self.live_count = 0
        # Duration is captured-image time, never event age or processing time.
        # Counter resets also fence navigation, invalid markers and view changes.
        if not self.count or label != 'PRESENT': self.present_since = None
        if not self.empty or label != 'EMPTY': self.empty_since = None
        if not self.live_count or not (live and valid and label == 'PRESENT'): self.live_since = None
        if label == 'PRESENT' and self.present_since is None: self.present_since = frame.captured_at
        if label == 'EMPTY' and self.empty_since is None: self.empty_since = frame.captured_at
        if live and valid and label == 'PRESENT' and self.live_since is None: self.live_since = frame.captured_at
        present_elapsed = 0 if self.present_since is None else frame.captured_at - self.present_since
        empty_elapsed = 0 if self.empty_since is None else frame.captured_at - self.empty_since
        live_elapsed = 0 if self.live_since is None else frame.captured_at - self.live_since
        stuck_minimum = self.config.stuck_minimum_seconds
        clear_minimum = self.config.clear_minimum_seconds
        if (getattr(self.config, 'clock_window_enabled', False)
                or self.config.capture_backend == 'rtsp'):
            # Faster confirmed sampling must not shorten legacy nominal durations,
            # even for old manually edited subsecond configurations with zero minima.
            nominal = max(1., self.config.capture_interval_seconds)
            stuck_minimum = max(stuck_minimum, (self.config.stuck_consecutive_frames-1)*nominal)
            clear_minimum = max(clear_minimum, (self.config.clear_consecutive_frames-1)*nominal)
        stuck_duration = present_elapsed >= stuck_minimum
        live_duration = live_elapsed >= stuck_minimum
        clear_duration = empty_elapsed >= clear_minimum
        self.live_count = self.live_count + 1 if live and valid and label == 'PRESENT' else 0
        self.previous = frame
        send = None
        shadow = False
        deferred = None
        passages = ()
        if self.config.capture_backend == 'rtsp' and not safety_reason and isinstance(frame.source_proof, dict):
            passages = parse_passages(frame.source_proof.get('passages', []))
            self._check_passage_coverage(frame.captured_at)
        self.recent_frames.append((frame.captured_at, image, observation))
        with (self._evidence_transaction() if self._memory_mode else self.db):
            if not self._memory_mode:
                self.db.execute('INSERT INTO frames VALUES(?,?,?,?)', (frame.frame_id, frame.captured_at, str(marker), str(image)))
            self._reconcile_passages(passages)
            event = self.active()
            # A passing object can briefly make a frame unobservable (or a frame
            # can be dropped); only an unobserved span beyond the tolerance makes
            # the event unconfirmable. Consecutive stuck proof still restarts.
            tolerated = (event is not None and self.event_seen_at is not None and not identity_break
                         and 0 <= frame.captured_at - self.event_seen_at
                         <= getattr(self.config, 'event_gap_tolerance_seconds', 0))
            if not continuity and event and event['kind'] != 'stuck' and not tolerated:
                self._close_incomplete(event, frame.captured_at)
                event = None
            if label == 'UNKNOWN':
                self.count = self.empty = 0
                if event:
                    self.db.execute('UPDATE events SET first_clear=NULL WHERE id=?', (event['id'],))
                if event and event['kind'] != 'stuck' and not tolerated:
                    self._close_incomplete(event, frame.captured_at)
            elif label == 'EMPTY':
                self.count = 0
                self.empty += 1
                if event and self.empty == 1:
                    self.db.execute('UPDATE events SET first_clear=? WHERE id=?', (frame.captured_at, event['id']))
                if event and self.empty >= self.config.clear_consecutive_frames and clear_duration:
                    self.snapshot(event['id'], 'clear', image, observation)
                    kind = event['kind'] if event['kind'] in ('stuck', 'retracted') else 'normal_visual'
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
                if self.count >= self.config.stuck_consecutive_frames and stuck_duration:
                    self.db.execute("UPDATE events SET kind='stuck' WHERE id=?", (event['id'],))
                    self.snapshot(event['id'], 'threshold', image, observation)
                    due = event['last_success'] is None or now - event['last_success'] >= max(60, self.config.reminder_interval_seconds)
                    if (self.live_count >= self.config.stuck_consecutive_frames and live_duration and due
                            and now >= event['retry_at'] and self.pending is None):
                        notification = uuid.uuid4().hex
                        status = 'pending' if self.config.notify_enabled else 'shadow'
                        self.db.execute('INSERT INTO notifications VALUES(?,?,?,?)', (notification, event['id'], now, status))
                        if status == 'shadow':
                            self.db.execute('UPDATE events SET last_success=? WHERE id=?', (now, event['id']))
                            shadow = True
                        else:
                            send = (notification, event['id'])
                    else:
                        deferred = ('not_live' if self.live_count < self.config.stuck_consecutive_frames or not live_duration else
                                    'in_flight' if self.pending is not None else
                                    'retry_wait' if now < event['retry_at'] else 'cooldown')
            if self.active() is None:
                self.event_seen_at = None
            elif label in ('PRESENT', 'EMPTY'):
                self.event_seen_at = frame.captured_at
        if reason == 'ok':
            reason = 'stale' if not live else 'gap' if not continuity else 'ok'
        reported = self._report_detection(label, reason, now, observation, frame,
                                          event['id'] if event else None, previous)
        if reported and (self.config.stuck_minimum_seconds or self.config.clear_minimum_seconds):
            LOG.info('Continuous proof seconds: present=%.3f/%.3f live=%.3f/%.3f empty=%.3f/%.3f',
                     present_elapsed, self.config.stuck_minimum_seconds, live_elapsed,
                     self.config.stuck_minimum_seconds, empty_elapsed, self.config.clear_minimum_seconds)
        if deferred and reported:
            LOG.info('Notification deferred', extra={'notification_context': {'event_id': event['id'], 'reason': deferred}})
        if shadow:
            LOG.info('Notification status: %s', 'shadow',
                     extra={'notification_context': {'event_id': event['id'], 'notification_id': notification}})
        if send:
            self.pending = (self.executor.submit(self.sender, 'Suspected banknote/object stuck: ' + self.config.camera_id,
                                                 send[0]), *send)
            LOG.info('Notification status: %s', 'queued',
                     extra={'notification_context': {'event_id': send[1], 'notification_id': send[0]}})
        return True

    def _close_incomplete(self, event, closed):
        # A note already seen being pulled back stays hidden rather than unconfirmable.
        self.db.execute("UPDATE events SET kind=CASE WHEN kind='retracted' THEN kind ELSE 'incomplete' END,"
                        'closed=? WHERE id=?', (closed, event['id']))

    def _check_passage_coverage(self, captured_at):
        previous, self._passage_covered_at = self._passage_covered_at, captured_at
        if previous is not None and captured_at - previous > PASSAGE_RETAIN_SECONDS:
            LOG.warning('Passage verdicts may be lost',
                        extra={'passage_loss': dict(reason='worker_gap', since=float(previous))})

    def _reconcile_passages(self, passages):
        """Idempotent: every frame re-sends the last ~20 s of full-rate verdicts.

        Matching is by event start time only. Stuck events are never changed, and
        a pulled-back note that stays long enough still becomes stuck afterwards.
        """
        for passage in passages:
            window = (passage.started - 1, passage.ended + .5)
            if passage.verdict == 'retracted':
                self.db.execute("UPDATE events SET kind='retracted' WHERE kind IN ('candidate','normal_visual','incomplete') "
                                'AND started BETWEEN ? AND ?', window)
                continue
            overlap = 'started <= ? AND (closed IS NULL OR closed >= ?)'
            if self.db.execute('SELECT 1 FROM events WHERE id=? OR (' + overlap + ')',
                               (passage.id, window[1], window[0])).fetchone():
                self.db.execute("UPDATE events SET kind='normal_visual' WHERE kind='incomplete' AND " + overlap,
                                (window[1], window[0]))
                continue
            self.db.execute('INSERT INTO events(id,kind,started,closed,last_present,first_clear) '
                            "VALUES(?,'normal_visual',?,?,?,?)",
                            (passage.id, passage.started, passage.ended, passage.ended, passage.ended))
            middle = (passage.started + passage.ended) / 2
            nearby = [item for item in self.recent_frames if window[0] <= item[0] <= window[1]]
            if not nearby:
                continue
            _, image, observation = min(nearby, key=lambda item: abs(item[0] - middle))
            try:
                self.snapshot(passage.id, 'first', image, observation)
                self.snapshot(passage.id, 'first_full', image, full_frame=True)
            except (ValueError, OSError):
                LOG.warning('Passage evidence unavailable; event kept without images')

    def scan(self):
        self.drain()
        frames = []
        rejected_generation = bytearray(32)
        rejected_count = 0
        def note_rejection(marker, version):
            nonlocal rejected_count
            # Constant-size, order-independent set identity. Cache eviction is
            # only an I/O optimization, never renewed proof of a new input gap.
            digest = hashlib.sha256(repr((str(marker), version)).encode('utf-8')).digest()
            for index,value in enumerate(digest):rejected_generation[index] ^= value
            rejected_count += 1
        for marker in (self.config.data_dir / 'frames' / self.config.camera_id).glob('*.ready.json'):
            identity = marker.name[:-len('.ready.json')]
            if self.db.execute('SELECT 1 FROM frames WHERE id=?', (identity,)).fetchone():
                continue
            version = None
            try:
                stat = marker.lstat()
                version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
                           stat.st_ino, stat.st_mode, self.config.expected_size)
            except OSError:
                pass
            if version is not None and self._rejected_markers.get(marker) == version:
                note_rejection(marker, version)
                continue
            try:
                frame, image = read_frame(marker, self.config)
                self._rejected_markers.pop(marker, None)
                frames.append((frame.captured_at, frame.sequence, frame.frame_id, frame, image, marker))
            except (ValueError, OSError, TypeError):
                note_rejection(marker, version)
                if version is not None:
                    self._rejected_markers[marker] = version
                    self._rejected_markers.move_to_end(marker)
                    while len(self._rejected_markers) > 1024:
                        self._rejected_markers.popitem(last=False)
        if bytes(rejected_generation) != self._rejected_generation:
            self._rejected_generation = bytes(rejected_generation)
            if rejected_count:
                self.count = self.empty = self.live_count = 0
                self.trusted_frame = None
                LOG.warning('Rejected frame marker; new proof required')
        for _, _, _, frame, image, marker in sorted(frames):
            if self.process(frame, image, marker) is ProcessOutcome.DEFERRED:
                # Preserve spool ordering; later frames must not overtake a
                # decision that has not acquired its navigation fence yet.
                break
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
        try:
            self._notify_history.prune()
        except Exception:
            pass  # A diagnostic write/cleanup must not suppress occupancy work.
        root = self.config.data_dir / 'frames' / self.config.camera_id
        with self.db:
            if not self._memory_mode:
                for row in self.db.execute('SELECT * FROM frames WHERE captured<? AND image IS NOT NULL', (now - self.config.rolling_frames_minutes * 60,)).fetchall():
                    # Legacy spool only; RAM mode never touches old raw inputs.
                    if self.unlink_owned(row['marker'], root) and self.unlink_owned(row['image'], root):
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
        # Stop retry backoff; an already-open POST still finishes (ambiguous).
        self._send_cancel.set()
        self.drain(wait=True)
        self.executor.shutdown(wait=True)
        self.db.close()
