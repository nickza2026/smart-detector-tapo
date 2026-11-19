"""Local, per-attempt Notify diagnostics in the explicitly selected data root."""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
import sqlite3
import stat
import time

RETENTION_SECONDS = 30 * 86400
MAX_BODY_BYTES = 65536
LOG = logging.getLogger(__name__)
APPLICATION_ID = 0x53544E48
_PRIVATE = re.compile(r'authorization|cookie|password|secret|token|jwt|api.?key|ack.?url|capability|credential', re.I)


class NotifyHistoryError(RuntimeError):
    """Fixed safe message only; never embed SQL, paths, requests or responses."""


class _ObjectPairs(list):
    """Preserve duplicate JSON fields: this is a diagnostic, not a parser result."""


def redact(text, key):
    text = str(text)
    # Remove the known credential even when echoed in a JSON string or URL.
    from urllib.parse import quote, quote_plus
    for value in sorted({key, json.dumps(key)[1:-1], quote(key, safe=''), quote_plus(key)}, key=len, reverse=True):
        if value:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|jwt|password|secret|ack[_-]?token)\s*[=:]\s*)[^\s&<>"\']+',
                  r'\1[REDACTED]', text)
    return text


def body_text(raw, key):
    """Keep full bounded body, masking named capabilities and echoed keys."""
    if raw is None:
        return None
    text = raw[:MAX_BODY_BYTES].decode('utf-8', errors='replace')
    try:
        data = json.loads(text, object_pairs_hook=_ObjectPairs)
        def clean(value, depth=0):
            if depth > 40:
                return '[REDACTED: nesting limit]'
            if isinstance(value, _ObjectPairs):
                return _ObjectPairs((redact(name,key), '[REDACTED]' if _PRIVATE.search(name) else clean(item, depth+1))
                                    for name, item in value)
            if isinstance(value, list):
                return [clean(item, depth+1) for item in value]
            if isinstance(value, str):
                return redact(value, key)
            return value
        def encode(value):
            if isinstance(value, _ObjectPairs):
                return '{'+','.join(json.dumps(name,ensure_ascii=False)+':'+encode(item) for name,item in value)+'}'
            if isinstance(value, list):
                return '['+','.join(encode(item) for item in value)+']'
            return json.dumps(value,ensure_ascii=False)
        return encode(clean(data))
    except (ValueError, RecursionError):
        if text.lstrip().startswith(('{','[')):
            return '[Body omitted: invalid or excessively nested JSON could not be safely redacted]'
        text = redact(text,key)
        # Non-JSON proxy errors can contain headers or HTML form credentials.
        text = re.sub(r'(?im)(authorization|set-cookie|cookie)\s*:[^\r\n]*', r'\1: [REDACTED]', text)
        text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|jwt|password|secret|ack[_-]?token)\s*[=:]\s*)[^\s&<>"\']+',
                      r'\1[REDACTED]', text)
        return text


class NotifyHistory:
    def __init__(self, data_dir, *, clock=time.time):
        # Lazy: construction never invents a global/cwd fallback or touches disk.
        self.root = Path(data_dir)
        self.path = self.root / 'notify-history.sqlite3'
        self.clock = clock
        self.last_error = False

    def _validate(self):
        from .storage_location import _check_ancestry
        if not self.root.is_absolute() or self.root == Path(self.root.anchor):
            raise NotifyHistoryError('ประวัติ Notify ต้องใช้โฟลเดอร์ข้อมูลที่กำหนดไว้')
        _check_ancestry(self.root)
        for name in ('notify-history.sqlite3', 'notify-history.sqlite3-wal', 'notify-history.sqlite3-shm'):
            path = self.root/name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1):
                raise NotifyHistoryError('ไฟล์ประวัติ Notify ไม่ปลอดภัย')

    @contextmanager
    def _connection(self, *, write=False):
        db = None
        try:
            self._validate()
            self.root.mkdir(parents=True, exist_ok=True)
            self._validate()
            # Independent health/manual/Worker writers can overlap. Allow a
            # bounded normal transaction to finish; keep UI readers short.
            db = sqlite3.connect(str(self.path), timeout=1.0 if write else .1)
            db.row_factory = sqlite3.Row
            application_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
            if application_id != APPLICATION_ID:
                if application_id != 0 or tables:
                    raise NotifyHistoryError('ไฟล์นี้ไม่ใช่ประวัติ Notify ของแอป')
                db.execute('PRAGMA application_id='+str(APPLICATION_ID))
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=64')
            db.execute('''CREATE TABLE IF NOT EXISTS notify_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                channel TEXT NOT NULL, alert_id TEXT NOT NULL, importance TEXT NOT NULL,
                url TEXT NOT NULL, request_headers TEXT NOT NULL, request_body TEXT NOT NULL,
                http_status INTEGER, response_headers TEXT NOT NULL DEFAULT '{}', response_body TEXT,
                response_truncated INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'ambiguous', code TEXT NOT NULL DEFAULT 'outcome_unknown',
                elapsed_ms INTEGER)''')
            db.execute('CREATE INDEX IF NOT EXISTS notify_created ON notify_attempts(created,id)')
            db.commit()
            deadline = time.monotonic()+.5
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            yield db
            db.commit()
            # A previously lost attempt cannot be recovered by a successful read.
        except Exception:
            if not self.last_error:
                LOG.warning('Notify request history unavailable; delivery is not suppressed')
            self.last_error = True
            raise NotifyHistoryError('อ่านหรือบันทึกประวัติ Notify ไม่สำเร็จ') from None
        finally:
            if db is not None:
                db.close()

    def begin(self, target, alert, channel):
        from .notify_api import client_headers
        headers = {**client_headers(), 'Content-Type': 'application/json; charset=utf-8',
                   'Idempotency-Key': redact(alert.idempotency_key, target.key),
                   'Authorization': '[REDACTED]'}
        with self._connection(write=True) as db:
            cursor = db.execute('''INSERT INTO notify_attempts
                (created,channel,alert_id,importance,url,request_headers,request_body)
                VALUES(?,?,?,?,?,?,?)''', (self.clock(), channel, redact(alert.alert_id, target.key),
                alert.importance, redact(target.base_url+'/v1/alerts', target.key),
                json.dumps(headers), body_text(alert._body, target.key)))
            return cursor.lastrowid

    def finish(self, identifier, result, status, raw, headers, elapsed_ms, key):
        # Explicit safe response-header subset; no cookies, capabilities or Location.
        safe_headers = {name: redact(str(headers.get(name, ''))[:512], key)
                        for name in ('Content-Type', 'Retry-After') if headers and headers.get(name)}
        with self._connection(write=True) as db:
            db.execute('''UPDATE notify_attempts SET state=?,code=?,http_status=?,
                response_headers=?,response_body=?,response_truncated=?,elapsed_ms=? WHERE id=?''',
                (result.state, result.code, status, json.dumps(safe_headers), body_text(raw,key),
                 bool(raw is not None and len(raw)>MAX_BODY_BYTES), elapsed_ms, identifier))

    def list_page(self, before_id=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid history page size')
        if before_id is not None and (type(before_id) is not int or before_id < 1):
            raise ValueError('Invalid history cursor')
        with self._connection() as db:
            return [dict(row) for row in db.execute('''SELECT id,created,channel,alert_id,importance,url,
                http_status,state,code,elapsed_ms FROM notify_attempts
                WHERE created>=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?''',
                (self.clock()-RETENTION_SECONDS,before_id,before_id,limit))]

    def detail(self, identifier):
        if type(identifier) is not int or identifier < 1:
            raise ValueError('Invalid history identifier')
        with self._connection() as db:
            row = db.execute('SELECT * FROM notify_attempts WHERE id=? AND created>=?',
                             (identifier,self.clock()-RETENTION_SECONDS)).fetchone()
            if row is None:
                return None
            result = dict(row)
            for name in ('request_headers','response_headers'):
                result[name] = json.loads(result[name])
            result['response_truncated'] = bool(result['response_truncated'])
            return result

    def prune(self):
        """Bounded maintenance batch; return True while more expired rows remain."""
        if not self.path.exists():
            return False
        cutoff = self.clock()-RETENTION_SECONDS
        with self._connection(write=True) as db:
            db.execute('''DELETE FROM notify_attempts WHERE id IN
                (SELECT id FROM notify_attempts WHERE created<? ORDER BY created,id LIMIT 256)''', (cutoff,))
            more = db.execute('SELECT 1 FROM notify_attempts WHERE created<? LIMIT 1',(cutoff,)).fetchone()
            db.execute('PRAGMA incremental_vacuum(128)')
            return bool(more)


class NotifyHistoryMaintenance:
    """One owned background cleanup, including idle periods with no deliveries."""
    def __init__(self, history):
        from concurrent.futures import ThreadPoolExecutor
        self.history = history
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='notify-history')
        self.future = None
        self.due = 0.
        self.closed = False

    @property
    def is_running(self):
        return self.future is not None and not self.future.done()

    def poll(self):
        if self.closed:
            return
        if self.future is not None and self.future.done():
            try:
                more = self.future.result()
            except Exception:
                more = False
            self.future = None
            self.due = time.monotonic()+(1 if more else 60)
        if self.future is None and time.monotonic()>=self.due:
            self.future = self.executor.submit(self.history.prune)

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
