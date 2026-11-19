"""Local-only refueling journal and immutable two-stage delivery outbox.

All database mutations reload the aggregate inside BEGIN IMMEDIATE.  This class
never performs network IO and never removes source images or legacy history.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import shutil
import time
import uuid
import warnings

from PIL import Image

from .refueling import ConfirmedAnnouncement, RefuelingDecision, RefuelingEngine, SessionUpdate
from .refueling_contract import ERROR_CODES,validate_event
from .storage_diagnostics import StorageCameraMismatchError, StoragePathUnsafeError, StorageMetadataBusyError, StorageSchemaError, guard_evidence

TTL_SECONDS = 72 * 60 * 60
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_IMAGES = 64
MAX_TOTAL_MEDIA_BYTES = 512*1024*1024
_KEY = re.compile(r'[A-Za-z0-9_.:-]{1,128}', re.ASCII)
_IMAGE = re.compile(r'[0-9a-f]{32}\.(?:png|jpg)', re.ASCII)


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError('Invalid source or destination key')
    return value


def _timestamp(value):
    if (type(value) not in (int, float) or value<0 or value>253402300799
            or not math.isfinite(value)):
        raise ValueError('Invalid timestamp')
    return float(value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe(path, *, component='path'):
    """Reject links/reparse points (including ancestors), not merely escapes."""
    path = Path(os.path.abspath(path))
    for depth, part in reversed(tuple(enumerate((path, *path.parents)))):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        # Windows can return zero links while an ephemeral SQLite leaf is
        # being unlinked. Validate its current state ONCE; never accept zero,
        # retry real hardlinks/reparse points, or extend this to DB/ancestors.
        if (depth == 0 and component in ('journal', 'wal', 'shm')
                and stat.S_ISREG(info.st_mode) and info.st_nlink == 0
                and not getattr(info, 'st_file_attributes', 0) & 0x400
                and not getattr(info, 'st_reparse_tag', 0)):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise StoragePathUnsafeError('Unsafe linked path',
                guard=guard_evidence('linked_path', component, depth, info))
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            if (depth == 0 and component in ('journal', 'wal', 'shm') and info.st_nlink == 0
                    and not getattr(info, 'st_reparse_tag', 0)):
                raise StorageMetadataBusyError('Ephemeral metadata changing; operation refused',
                    guard=guard_evidence('non_single_link', component, depth, info))
            raise StoragePathUnsafeError('Unsafe hardlinked file',
                guard=guard_evidence('non_single_link', component, depth, info))
    return path


class RefuelingStore:
    def __init__(self, directory, camera_id, *, clock=time.time, source_root=None,
                 busy_timeout_seconds=10.):
        if (type(busy_timeout_seconds) not in (int, float)
                or not math.isfinite(busy_timeout_seconds) or not 0 <= busy_timeout_seconds <= 10):
            raise ValueError('Invalid SQLite busy timeout')
        RefuelingEngine(camera_id)  # validate before creating anything
        self.camera_id = camera_id
        self.clock = clock
        self.directory = _safe(directory, component='directory')
        if self.directory == self.directory.anchor or self.directory == Path(self.directory.anchor):
            raise StoragePathUnsafeError('Filesystem root is not a store',
                guard=guard_evidence('filesystem_root', 'directory', 0))
        self.source_root = _safe(source_root if source_root is not None else self.directory,
                                 component='source_root')
        self.media = self.directory / 'media'
        self.db_path = self.directory / 'refueling.sqlite3'
        _safe(self.directory, component='directory').mkdir(parents=True, exist_ok=True)
        _safe(self.media, component='media').mkdir(exist_ok=True)
        self._guard()
        self._db = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=busy_timeout_seconds)
        self._db.row_factory = sqlite3.Row
        try:
            self._validate_existing_schema()
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA secure_delete=ON')
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,created REAL NOT NULL,data TEXT NOT NULL,
                    started_at REAL NOT NULL,last_ready_at REAL NOT NULL,ended_at REAL,
                    ready_count INTEGER NOT NULL CHECK(ready_count>=1),revision INTEGER NOT NULL CHECK(revision>=ready_count),
                    has_issue INTEGER NOT NULL CHECK(has_issue IN (0,1)));
                CREATE TABLE IF NOT EXISTS observations(source_key TEXT PRIMARY KEY,created REAL NOT NULL,session_id TEXT,reason TEXT,
                    kind TEXT NOT NULL,started_at REAL NOT NULL,ended_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS partial_audio_members(source_key TEXT PRIMARY KEY REFERENCES observations(source_key) ON DELETE CASCADE,
                    group_id TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS partial_audio_group ON partial_audio_members(group_id);
                CREATE TABLE IF NOT EXISTS issues(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,first_detected_at REAL NOT NULL,original_deadline_at REAL,
                    PRIMARY KEY(session_id,code));
                CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    source_key TEXT,extension TEXT NOT NULL,sha256 TEXT NOT NULL,content_type TEXT NOT NULL,byte_size INTEGER NOT NULL,captured_at REAL NOT NULL,
                    UNIQUE(session_id,source_key));
                CREATE TABLE IF NOT EXISTS jobs(event_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,payload BLOB NOT NULL,created REAL NOT NULL,expires_at REAL NOT NULL,
                    destination_key TEXT,status TEXT NOT NULL DEFAULT 'queued',retry_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,error_code TEXT,UNIQUE(session_id,revision));
                CREATE TABLE IF NOT EXISTS media_gc(id TEXT NOT NULL,extension TEXT NOT NULL,byte_size INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(id,extension));
                CREATE TABLE IF NOT EXISTS file_intents(id TEXT PRIMARY KEY,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS uploads(image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    destination_key TEXT NOT NULL,PRIMARY KEY(image_id,destination_key));
                CREATE INDEX IF NOT EXISTS sessions_created ON sessions(created);
                CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at DESC,id DESC);
                DROP INDEX IF EXISTS one_open_round;
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_round ON sessions((1))
                    WHERE ended_at IS NULL AND json_extract(data,'$.source_interrupted_at') IS NULL;
                CREATE INDEX IF NOT EXISTS observations_session ON observations(session_id);
                CREATE INDEX IF NOT EXISTS observations_created ON observations(created);
                CREATE INDEX IF NOT EXISTS images_session ON images(session_id);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(destination_key,status,retry_at,created);
                CREATE INDEX IF NOT EXISTS intents_created ON file_intents(created);
            ''')
            with self._transaction():
                owner = self._meta('camera_id')
                if owner is not None and owner != camera_id:
                    raise StorageCameraMismatchError('Store belongs to another camera')
                self._set_meta('camera_id', camera_id)
                if self._meta('checkpoint') is None:
                    self._save_engine(RefuelingEngine(camera_id))
        except BaseException:
            self._db.close()
            raise

    def _validate_existing_schema(self):
        """Fail closed before DDL on known incompatible tables; never migrate.

        Missing tables retain the existing CREATE IF NOT EXISTS behavior. Exact
        column order matters because journal writes use positional INSERTs.
        """
        expected = {
            'meta': 'key value',
            'sessions': 'id created data started_at last_ready_at ended_at ready_count revision has_issue',
            'observations': 'source_key created session_id reason kind started_at ended_at',
            'partial_audio_members': 'source_key group_id created',
            'issues': 'session_id code first_detected_at original_deadline_at',
            'images': 'id session_id source_key extension sha256 content_type byte_size captured_at',
            'jobs': 'event_id session_id revision payload created expires_at destination_key status retry_at attempts error_code',
            'media_gc': 'id extension byte_size',
            'file_intents': 'id created',
            'uploads': 'image_id destination_key',
        }
        objects = dict(self._db.execute('SELECT name,type FROM sqlite_master'))
        for table, columns in expected.items():
            if table not in objects:
                continue
            if objects[table] != 'table':
                raise StorageSchemaError('Unsupported store schema')
            # Table names come exclusively from the constant map above.
            actual = [row[1] for row in self._db.execute(f'PRAGMA table_info({table})')]
            if actual != columns.split():
                raise StorageSchemaError('Unsupported store schema')

    def _guard(self):
        for component, path in (('directory', self.directory), ('media', self.media),
                     ('database', self.db_path), ('journal', Path(str(self.db_path) + '-journal')),
                     ('wal', Path(str(self.db_path) + '-wal')), ('shm', Path(str(self.db_path) + '-shm'))):
            _safe(path, component=component)

    @contextmanager
    def _transaction(self):
        self._guard()
        self._db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self._guard()
            self._db.execute('COMMIT')
        except BaseException:
            self._db.execute('ROLLBACK')
            raise

    def _meta(self, key):
        row = self._db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, key, value):
        self._db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, _json(value)))

    def _engine(self):
        engine = RefuelingEngine(self.camera_id)
        engine.restore_checkpoint(self._meta('checkpoint'))
        return engine

    def _save_engine(self, engine):
        self._set_meta('checkpoint', engine.checkpoint())

    def _manifest(self, session_id):
        return [dict(row) for row in self._db.execute(
            'SELECT id,sha256,content_type,byte_size,captured_at FROM images WHERE session_id=? ORDER BY rowid', (session_id,))]

    def _persist_update(self, update, now):
        session = update.session
        self._db.execute('''INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         data=excluded.data,started_at=excluded.started_at,last_ready_at=excluded.last_ready_at,
                         ended_at=excluded.ended_at,ready_count=excluded.ready_count,revision=excluded.revision,has_issue=excluded.has_issue''',
                         (session.id,now,_json(asdict(session)),session.started_at,session.last_ready_at,session.ended_at,
                          session.ready_count,session.revision,int(session.has_issue)))
        created = self._db.execute('SELECT created FROM sessions WHERE id=?', (session.id,)).fetchone()[0]
        kinds = {'started': 'started', 'completed': 'completed', 'ready_repeated': 'attention_required',
                 'completion_missing': 'attention_required', 'audio_source_interrupted': 'attention_required', 'evidence': 'updated'}
        issues = [{'code': code, 'detected_at': _iso(at)} for code, at in
                  (('ready_prompt_repeated', session.repeated_ready_at), ('completion_not_detected', session.missing_end_at),
                   ('audio_source_interrupted', session.source_interrupted_at)) if at is not None]
        for code,at in (('ready_prompt_repeated',session.repeated_ready_at),('completion_not_detected',session.missing_end_at),
                        ('audio_source_interrupted',session.source_interrupted_at)):
            if at is not None:
                self._db.execute('INSERT OR IGNORE INTO issues VALUES (?,?,?,?)',
                                 (session.id,code,at,session.completion_deadline if code=='completion_not_detected' else None))
        manifest = self._manifest(session.id)
        for image in manifest:
            image['captured_at'] = _iso(image['captured_at'])
        payload = {'schema_version': 1, 'event_id': update.event_key,
                   'event_type': 'refueling.' + kinds[update.kind], 'occurred_at': _iso(update.observed_at),
                   'session': {'id': session.id, 'camera_id': session.camera_id, 'revision': session.revision,
                               'state': session.state, 'started_at': _iso(session.started_at),
                               'last_ready_at': _iso(session.last_ready_at),
                               'ended_at': _iso(session.ended_at) if session.ended_at is not None else None,
                               'ready_count': session.ready_count, 'has_issue': session.has_issue, 'issues': issues,
                               'amount_baht': None, 'evidence': manifest}}
        body = _json(payload).encode('utf-8')
        validate_event(body,self.camera_id,update.event_key)
        self._db.execute('INSERT INTO jobs(event_id,session_id,revision,payload,created,expires_at) VALUES (?,?,?,?,?,?)',
                         (update.event_key, session.id, session.revision, body, now, created + TTL_SECONDS))

    def mark_audio_gap(self, at=None):
        """Atomically record source uncertainty; never infer a physical end."""
        now=_timestamp(self.clock())
        at=_timestamp(now if at is None else at)
        with self._transaction():
            # Even a second gap without a canonical active session invalidates
            # tentative acoustic continuity. Never bridge it in partial history.
            self._set_meta('partial_audio_checkpoint',None)
            self._set_meta('partial_audio_after',max(at,self._meta('partial_audio_after') or 0))
            engine=self._engine()
            updates=engine.interrupt(at)
            for update in updates:
                self._persist_update(update,now)
            if updates:
                self._set_meta('awaiting_end_after_gap',True)
            self._save_engine(engine)
            return RefuelingDecision(updates)

    def _partial_audio_group(self, announcement, gap, decision):
        """Associate new post-gap evidence, not sessions, updates, money or jobs."""
        if not gap:
            if self._meta('partial_audio_checkpoint') is not None:
                self._set_meta('partial_audio_checkpoint',None)
            return None
        if announcement.started_at < (self._meta('partial_audio_after') or 0):
            return None
        expected = ('awaiting_end_after_source_gap' if announcement.kind=='paid_ready'
                    else 'completion_without_start')
        if decision.ignored_reason != expected:
            return None
        candidate=RefuelingEngine(self.camera_id)
        record=self._meta('partial_audio_checkpoint')
        now=_timestamp(self.clock())
        if record is not None and _timestamp(record['created'])<=now-TTL_SECONDS:
            record=None
        if record is not None:
            candidate.restore_checkpoint(record['checkpoint'])
        if candidate.current is not None and announcement.started_at>candidate.current.completion_deadline:
            candidate=RefuelingEngine(self.camera_id)
            record=None
        if announcement.kind=='end' and candidate.current is None:
            self._set_meta('partial_audio_checkpoint',None)
            return None
        result=candidate.accept(announcement)
        if not result.updates:
            return None
        self._set_meta('partial_audio_checkpoint',
            {'created':record['created'] if record else now,'checkpoint':candidate.checkpoint()}
            if announcement.kind=='paid_ready' else None)
        return result.updates[-1].session.id

    def accept(self, source_key, announcement):
        _key(source_key)
        now = _timestamp(self.clock())
        with self._transaction():
            engine = self._engine()
            if not isinstance(announcement,ConfirmedAnnouncement):raise ValueError('Confirmed announcement required')
            if announcement.ended_at<=now-TTL_SECONDS:
                return RefuelingDecision(ignored_reason='expired_observation')
            if self._db.execute('SELECT 1 FROM observations WHERE source_key=?', (source_key,)).fetchone():
                return RefuelingDecision(ignored_reason='duplicate_source_key')
            gap=self._meta('awaiting_end_after_gap') is True
            awaiting=gap or self._meta('awaiting_end_after_expiry') is True
            if awaiting and engine.current is not None:raise ValueError('Invalid expired-round checkpoint')
            if awaiting and announcement.kind=='paid_ready':
                if announcement.started_at<engine.checkpoint()['processed_until']:
                    decision=RefuelingDecision(ignored_reason='duplicate_or_stale_observation')
                else:
                    engine.tick(announcement.started_at)
                    decision=RefuelingDecision(ignored_reason='awaiting_end_after_source_gap' if gap else 'expired_round_awaiting_completion')
            else:
                decision = engine.accept(announcement)
                if awaiting and decision.ignored_reason=='completion_without_start':
                    self._set_meta('awaiting_end_after_expiry',False)
                    self._set_meta('awaiting_end_after_gap',False)
            for update in decision.updates:
                self._persist_update(update, now)
            session_id = decision.updates[-1].session.id if decision.updates else (engine.current.id if engine.current else None)
            self._db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',
                             (source_key, now, session_id, decision.ignored_reason,
                              announcement.kind,announcement.started_at,announcement.ended_at))
            group=self._partial_audio_group(announcement,gap,decision)
            if group is not None:
                self._db.execute('INSERT INTO partial_audio_members VALUES (?,?,?)',(source_key,group,now))
            self._save_engine(engine)
            return decision

    def tick(self, processed_until):
        with self._transaction():
            engine = self._engine()
            updates = engine.tick(processed_until)
            for update in updates:
                self._persist_update(update, _timestamp(self.clock()))
            self._save_engine(engine)
            return updates

    def get_session(self, identifier):
        self._guard()
        row = self._db.execute('SELECT data,created FROM sessions WHERE id=?', (identifier,)).fetchone()
        if not row:
            return None
        data = json.loads(row['data'])
        data['created_at'] = row['created']
        data['evidence'] = self._manifest(identifier)
        data.setdefault('source_interrupted_at',None)
        data['has_issue'] = any(data[key] is not None for key in ('repeated_ready_at','missing_end_at','source_interrupted_at'))
        data['state'] = 'completed' if data['ended_at'] is not None else ('interrupted' if data['source_interrupted_at'] is not None else 'open')
        return data

    def list_sessions(self, limit=50):
        self._guard()
        limit = max(0, min(int(limit), 200))
        return [self.get_session(row[0]) for row in self._db.execute(
            'SELECT id FROM sessions ORDER BY started_at DESC,id DESC LIMIT ?', (limit,)).fetchall()]

    def list_history(self, limit=50):
        """UI-only projection; partial announcements never become business sessions."""
        self._guard()
        limit = max(0, min(int(limit), 200))
        if not limit:
            return []
        cutoff = _timestamp(self.clock()) - TTL_SECONDS
        rows = []
        for record in self._db.execute(
                'SELECT id FROM sessions WHERE created>? '
                'ORDER BY started_at DESC,id DESC LIMIT ?', (cutoff, limit)).fetchall():
            session = self.get_session(record['id'])
            if session is not None:
                rows.append(dict(session, row_type='session'))
        observations = self._db.execute(
            'SELECT o.source_key,o.reason,o.kind,o.started_at,o.ended_at,p.group_id FROM observations o '
            'LEFT JOIN partial_audio_members p ON p.source_key=o.source_key '
            'WHERE o.created>? AND o.ended_at>? '
            "AND o.kind IN ('paid_ready','end') "
            'AND (o.reason IS NOT NULL OR NOT EXISTS '
            '(SELECT 1 FROM sessions s WHERE s.id=o.session_id AND s.created>?)) '
            'ORDER BY o.started_at DESC,o.source_key DESC LIMIT ?',
            (cutoff, cutoff, cutoff, limit)).fetchall()
        groups=list({o['group_id'] for o in observations if o['group_id'] is not None})
        pairs={}
        if groups:
            # A bounded batch of selected groups, not one query per member.
            # Aggregation includes the whole group even when LIMIT selected End only.
            marks=','.join('?' for _ in groups)
            for pair in self._db.execute(
                    'SELECT p.group_id,MIN(CASE WHEN o.kind=\'paid_ready\' THEN o.started_at END) AS first_ready,'
                    'MAX(CASE WHEN o.kind=\'paid_ready\' THEN o.started_at END) AS last_ready,'
                    'SUM(o.kind=\'paid_ready\') AS ready_count,SUM(o.kind=\'end\') AS end_count,'
                    'MAX(CASE WHEN o.kind=\'end\' THEN o.started_at END) AS end_at '
                    'FROM partial_audio_members p JOIN observations o ON o.source_key=p.source_key '
                    f'WHERE p.group_id IN ({marks}) AND o.created>? AND o.ended_at>? GROUP BY p.group_id',
                    (*groups,cutoff,cutoff)).fetchall():
                if pair['ready_count'] and pair['end_count']==1:
                    pairs[pair['group_id']]=pair
        emitted=set()
        reasons = {'completion_without_start', 'awaiting_end_after_source_gap',
                   'expired_round_awaiting_completion', 'duplicate_or_stale_observation',
                   'overlapping_observation'}
        for observation in observations:
            group=observation['group_id']
            if group in pairs:
                if group in emitted:continue
                emitted.add(group);pair=pairs[group]
                rows.append(dict(id='partial:'+group,row_type='observation_pair',
                    observed_started_at=pair['first_ready'],observed_last_ready_at=pair['last_ready'],
                    observed_ready_count=pair['ready_count'],observed_end_at=pair['end_at'],evidence=[],
                    _history_tie=group))
                continue
            rows.append(dict(
                id='observation:' + hashlib.sha256(observation['source_key'].encode('ascii')).hexdigest(),
                row_type='observation', kind=observation['kind'],
                reason=observation['reason'] if observation['reason'] in reasons else 'unknown',
                observed_started_at=observation['started_at'], observed_ended_at=observation['ended_at'],
                evidence=[], _history_tie=observation['source_key']))
        rows.sort(key=lambda row: (
            row['observed_started_at'] if row['row_type'] != 'session' else row['started_at'],
            row['row_type'], row.get('_history_tie', row['id'])), reverse=True)
        result = rows[:limit]
        for row in result:
            row.pop('_history_tie', None)
        return result

    def _image_path(self, identifier, extension):
        name = identifier + '.' + extension
        if not _IMAGE.fullmatch(name):
            raise ValueError('Invalid owned image identifier')
        return _safe(self.media / name)

    def _image_dict(self, row):
        result = dict(row)
        result['path'] = str(self._image_path(row['id'], row['extension']))
        result.pop('extension', None)
        result.pop('source_key', None)
        result.pop('session_id', None)
        return result

    def _read_image(self, source):
        source = _safe(source)
        if not source.is_relative_to(self.source_root) or source == self.source_root:
            raise ValueError('Image outside source root')
        _safe(self.source_root)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
            raise ValueError('Invalid image size or file')
        with _safe(source).open('rb') as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('Image too large')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    fmt = image.format
                    if fmt not in ('PNG', 'JPEG') or image.width * image.height > MAX_IMAGE_PIXELS:
                        raise ValueError('Unsupported image')
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
        except Exception as exc:
            raise ValueError('Invalid image') from exc
        return data, ('png', 'image/png') if fmt == 'PNG' else ('jpg', 'image/jpeg')

    def add_evidence(self, session_id, source_path, captured_at, *, source_key=None):
        captured_at = _timestamp(captured_at)
        if source_key is not None:
            _key(source_key)
        created_paths = []
        identifier = uuid.uuid4().hex
        # Commit a non-personal ownership intent BEFORE creating any file. A
        # process crash can then never strand an untracked photo indefinitely.
        with self._transaction():
            row = self._db.execute('SELECT created FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None or row['created'] + TTL_SECONDS <= self.clock():
                raise ValueError('Unknown or expired session')
            if source_key is not None:
                existing = self._db.execute('SELECT * FROM images WHERE session_id=? AND source_key=?', (session_id, source_key)).fetchone()
                if existing:
                    return self._image_dict(existing)
            used=self._db.execute('SELECT coalesce(sum(byte_size),0) FROM images').fetchone()[0]
            used+=self._db.execute('SELECT coalesce(sum(byte_size),0) FROM media_gc').fetchone()[0]
            used+=self._db.execute('SELECT count(*) FROM file_intents').fetchone()[0]*MAX_IMAGE_BYTES
            if used+MAX_IMAGE_BYTES>MAX_TOTAL_MEDIA_BYTES or shutil.disk_usage(self.directory).free<MAX_IMAGE_BYTES+32*1024*1024:
                raise ValueError('Image storage quota reached')
            self._db.execute('INSERT INTO file_intents VALUES (?,?)', (identifier, row['created']))
        try:
            with self._transaction():
                engine = self._engine()
                row = self._db.execute('SELECT data,created FROM sessions WHERE id=?', (session_id,)).fetchone()
                if row is None or row['created'] + TTL_SECONDS <= self.clock():
                    raise ValueError('Unknown or expired session')
                if source_key is not None:
                    existing = self._db.execute('SELECT * FROM images WHERE session_id=? AND source_key=?', (session_id, source_key)).fetchone()
                    if existing:
                        self._db.execute('DELETE FROM file_intents WHERE id=?', (identifier,))
                        return self._image_dict(existing)
                if self._db.execute('SELECT count(*) FROM images WHERE session_id=?', (session_id,)).fetchone()[0] >= MAX_IMAGES:
                    raise ValueError('Too many images')
                data, (extension, content_type) = self._read_image(source_path)
                final = self._image_path(identifier, extension)
                temporary = _safe(self.media / (identifier + '.tmp'))
                with temporary.open('xb') as handle:
                    created_paths.append(temporary)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._guard()
                _safe(temporary)
                # Reserve exclusively so an unexpected collision never overwrites
                # an unrelated existing file. Publish only over our placeholder.
                with _safe(final).open('xb'):
                    created_paths.append(final)
                _safe(final)
                os.replace(temporary, final)
                digest = hashlib.sha256(data).hexdigest()
                self._db.execute('INSERT INTO images VALUES (?,?,?,?,?,?,?,?)',
                                 (identifier, session_id, source_key, extension, digest, content_type, len(data), captured_at))
                from .refueling import RefuelingSession
                session = replace(RefuelingSession(**json.loads(row['data'])), revision=json.loads(row['data'])['revision'] + 1)
                if engine.current is not None and engine.current.id == session_id:
                    snapshot = engine.checkpoint()
                    snapshot['current'] = asdict(session)
                    engine.restore_checkpoint(snapshot)
                self._persist_update(SessionUpdate('evidence', session, _timestamp(self.clock())), _timestamp(self.clock()))
                self._save_engine(engine)
                self._db.execute('DELETE FROM file_intents WHERE id=?', (identifier,))
                result = self._image_dict(self._db.execute('SELECT * FROM images WHERE id=?', (identifier,)).fetchone())
            return result
        except BaseException:
            for path in reversed(created_paths):
                try:
                    _safe(path).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            # Known rollback is not a crash: remove the reservation when no
            # owned staging/final file remains. Otherwise keep its GC intent.
            if all(not path.exists() for path in created_paths):
                with self._transaction():
                    self._db.execute('DELETE FROM file_intents WHERE id=?',(identifier,))
            raise

    def pending_jobs(self, destination_key, limit=8):
        _key(destination_key)
        now = _timestamp(self.clock())
        result = []
        with self._transaction():
            self._db.execute("UPDATE jobs SET destination_key=? WHERE destination_key IS NULL AND status='queued' AND expires_at>?", (destination_key, now))
            rows = self._db.execute("SELECT * FROM jobs WHERE destination_key=? AND status='queued' AND retry_at<=? AND expires_at>? ORDER BY retry_at,created,rowid LIMIT ?",
                                    (destination_key, now, now, max(0, min(int(limit), 100)))).fetchall()
            for row in rows:
                payload = bytes(row['payload'])
                manifest = json.loads(payload)['session']['evidence']
                images = []
                try:
                    for item in manifest:
                        image = self._db.execute('SELECT * FROM images WHERE id=? AND session_id=?', (item['id'], row['session_id'])).fetchone()
                        if image is None:
                            raise ValueError('Missing image')
                        image = self._image_dict(image)
                        with _safe(image['path']).open('rb') as handle:
                            data = handle.read(MAX_IMAGE_BYTES + 1)
                        if len(data) != item['byte_size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                            raise ValueError('Corrupt image')
                        image['captured_at'] = item['captured_at']
                        images.append(image)
                except (OSError, ValueError):
                    self._db.execute("UPDATE jobs SET status='failed',error_code='media_unavailable' WHERE event_id=?", (row['event_id'],))
                    continue
                job = {key: row[key] for key in ('event_id', 'session_id', 'revision', 'attempts', 'expires_at', 'destination_key')}
                job.update(payload=payload, images=images, metadata={})
                result.append(job)
        return result

    def rebind_unconfirmed(self, destination_key, *, expected_ids=None, consent=False):
        """Preview or explicitly rebind nonexpired unconfirmed jobs; never mutate facts/TTL.

        Snapshot IDs prevent confirmation from silently covering newly arriving jobs.
        A failed request may already have been accepted by its previous recipient.
        """
        _key(destination_key)
        with self._transaction():
            now = _timestamp(self.clock())
            rows = self._db.execute("SELECT event_id FROM jobs WHERE status IN ('queued','failed') AND expires_at>? ORDER BY event_id", (now,)).fetchall()
            identifiers = tuple(row[0] for row in rows)
            if not consent:
                return identifiers
            if expected_ids is None or tuple(expected_ids) != identifiers:
                raise ValueError('Queue changed; confirm again')
            for identifier in identifiers:
                # Receipts must not leak across destinations; even same-target recovery
                # revalidates media rather than relying on an ambiguous previous attempt.
                self._db.execute('DELETE FROM uploads WHERE image_id IN (SELECT i.id FROM images i JOIN jobs j ON i.session_id=j.session_id WHERE j.event_id=?) AND destination_key=?', (identifier, destination_key))
                self._db.execute("UPDATE jobs SET destination_key=?,status='queued',retry_at=0,error_code=NULL WHERE event_id=?", (destination_key, identifier))
            return len(identifiers)

    def mark_image_uploaded(self, image_id, destination_key):
        _key(destination_key)
        with self._transaction():
            # A receipt is meaningful only for an image bound to this destination.
            exists = self._db.execute('SELECT 1 FROM images i JOIN jobs j ON j.session_id=i.session_id WHERE i.id=? AND j.destination_key=?', (image_id, destination_key)).fetchone()
            if exists:
                self._db.execute('INSERT OR IGNORE INTO uploads VALUES (?,?)', (image_id, destination_key))

    def image_uploaded(self, image_id, destination_key):
        _key(destination_key)
        self._guard()
        return bool(self._db.execute('SELECT 1 FROM uploads WHERE image_id=? AND destination_key=?', (image_id, destination_key)).fetchone())

    def forget_image_uploads(self, destination_key):
        _key(destination_key)
        with self._transaction():
            self._db.execute('DELETE FROM uploads WHERE destination_key=?', (destination_key,))

    def mark_delivery(self, event_id, status, *, retry_at=None, error_code=None):
        if status not in ('delivered', 'retry', 'failed'):
            raise ValueError('Invalid delivery status')
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch('[a-z][a-z0-9_]{0,47}', error_code)):
            raise ValueError('Error code must be a safe constant')
        # Even an ASCII token can be a credential: persist only our vocabulary.
        if error_code is not None and error_code not in ERROR_CODES:
            error_code = 'delivery_error'
        retry_at = _timestamp(retry_at) if retry_at is not None else _timestamp(self.clock())
        with self._transaction():
            self._db.execute("UPDATE jobs SET status=?,retry_at=?,attempts=attempts+1,error_code=? WHERE event_id=? AND status='queued' AND destination_key IS NOT NULL AND expires_at>?",
                             ('queued' if status == 'retry' else status, retry_at, error_code, event_id, self.clock()))

    def delivery_counts(self):
        self._guard()
        result = {'queued': 0, 'delivered': 0, 'failed': 0, 'expired_before_delivery': self._meta('expired_before_delivery') or 0,
                  'last_cleanup_at': self._meta('last_cleanup_at'),
                  'awaiting_end_after_expiry':self._meta('awaiting_end_after_expiry') is True,
                  'awaiting_end_after_gap':self._meta('awaiting_end_after_gap') is True}
        for row in self._db.execute('SELECT status,count(*) FROM jobs GROUP BY status'):
            result[row[0]] = row[1]
        result['failed_reasons'] = dict(self._db.execute(
            "SELECT coalesce(error_code,'unknown'),count(*) FROM jobs WHERE status='failed' GROUP BY 1 ORDER BY 2 DESC"))
        return result

    def cleanup(self, now=None):
        now = _timestamp(self.clock() if now is None else now)
        deleted = failed = expired = sessions = 0
        with self._transaction():
            engine = self._engine()
            rows = self._db.execute('SELECT id FROM sessions WHERE created<=?', (now-TTL_SECONDS,)).fetchall()
            for row in rows:
                identifier = row[0]
                expired += self._db.execute("SELECT count(*) FROM jobs WHERE session_id=? AND status!='delivered'", (identifier,)).fetchone()[0]
                self._db.execute('INSERT OR IGNORE INTO media_gc SELECT id,extension,byte_size FROM images WHERE session_id=?', (identifier,))
                self._db.execute('DELETE FROM observations WHERE session_id=?', (identifier,))
                self._db.execute('DELETE FROM sessions WHERE id=?', (identifier,))
                if engine.current is not None and engine.current.id == identifier:
                    snapshot = engine.checkpoint()
                    snapshot.update(current=None, last_utterance=None)
                    engine.restore_checkpoint(snapshot)
                    # A non-personal control bit preserves the required round
                    # boundary without retaining expired IDs/content. Repeats
                    # cannot turn the same unfinished round into new payments.
                    self._set_meta('awaiting_end_after_expiry',True)
                sessions += 1
            self._db.execute('DELETE FROM observations WHERE created<=?', (now-TTL_SECONDS,))
            partial=self._meta('partial_audio_checkpoint')
            if partial and _timestamp(partial['created'])<=now-TTL_SECONDS:
                self._set_meta('partial_audio_checkpoint',None)
            # Drop old utterance even when the last round was completed.
            snapshot = engine.checkpoint()
            utterance = snapshot.get('last_utterance')
            if engine.current is None and utterance and utterance['ended_at'] <= now - TTL_SECONDS:
                snapshot.update(last_utterance=None)
                engine.restore_checkpoint(snapshot)
            self._save_engine(engine)
            self._set_meta('expired_before_delivery', (self._meta('expired_before_delivery') or 0) + expired)
            self._set_meta('last_cleanup_at', now)
            for extension in ('png', 'jpg', 'tmp'):
                self._db.execute('INSERT OR IGNORE INTO media_gc SELECT id,?,? FROM file_intents WHERE created<=?',
                                 (extension,MAX_IMAGE_BYTES,now-TTL_SECONDS))
            self._db.execute('DELETE FROM file_intents WHERE created<=?', (now-TTL_SECONDS,))
        # Delete only after committing removal of references. Failed deletions are
        # retried by the next explicit cleanup; the tombstone contains no payload.
        with self._transaction():
            for image in self._db.execute('SELECT id,extension FROM media_gc').fetchall():
                try:
                    if image['extension'] == 'tmp':
                        if not re.fullmatch('[0-9a-f]{32}', image['id']):
                            raise ValueError('Invalid owned temporary identifier')
                        path = _safe(self.media / (image['id'] + '.tmp'))
                    else:
                        path = self._image_path(image['id'], image['extension'])
                    self._guard()
                    existed = path.exists()
                    path.unlink(missing_ok=True)
                    self._db.execute('DELETE FROM media_gc WHERE id=? AND extension=?', (image['id'], image['extension']))
                    deleted += int(existed)
                except (OSError, ValueError):
                    failed += 1
        return {'sessions_deleted': sessions, 'media_deleted': deleted, 'media_delete_failed': failed,
                'expired_before_delivery': expired}

    def close(self):
        self._db.close()
