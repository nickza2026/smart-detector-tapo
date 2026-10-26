"""Durable emergency transport. SQL is exclusively called by the worker thread.

Injected clients implement send(message, retry, expire, tag), receipt(receipt),
and cancel(receipt), returning Result. No transport method accesses SQLite.
"""
from dataclasses import dataclass
import json
import logging
import os
import re
import uuid
from urllib import parse, request, error

LOG = logging.getLogger(__name__)
UNRESOLVED = ('sending', 'active', 'cancel_pending', 'ambiguous')


@dataclass(frozen=True)
class Result:
    state: str
    receipt: str = ''


class PushoverClient:
    """Bounded, deliberately conservative Pushover API adapter."""
    def _call(self, path, fields, post=True):
        token = os.environ.get('PUSHOVER_TOKEN')
        if not token:
            return {'status': 0} if path == 'messages.json' else None
        fields = dict(fields, token=token)
        url = 'https://api.pushover.net/1/' + path
        body = parse.urlencode(fields).encode() if post else None
        if not post:
            url += '?' + parse.urlencode(fields)
        try:
            with request.urlopen(url, data=body, timeout=10) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except error.HTTPError as exc:
            # Only a structured explicit provider rejection is definitive.
            if 400 <= exc.code < 500:
                try:
                    raw = exc.read(65537)
                    value = json.loads(raw) if len(raw) <= 65536 else None
                    if isinstance(value, dict) and type(value.get('status')) is int and value['status'] == 0:
                        return value
                except Exception:
                    pass
                finally:
                    exc.close()
            else:
                exc.close()
            return None
        except Exception:
            return None

    def send(self, message, retry, expire, tag):
        user = os.environ.get('PUSHOVER_USER')
        if not user:
            return Result('failed')
        value = self._call('messages.json', dict(user=user, message=message, priority=2,
                                               retry=max(30, retry), expire=min(10800, max(1, expire)), tags=tag))
        if value and type(value.get('status')) is int and value['status'] == 0:
            return Result('failed')
        receipt = value.get('receipt') if value else None
        if (value and type(value.get('status')) is int and value['status'] == 1
                and isinstance(receipt, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', receipt)):
            return Result('active', receipt)
        return Result('ambiguous')

    def receipt(self, receipt):
        value = self._call('receipts/' + parse.quote(receipt, safe='') + '.json', {}, False)
        if (not value or type(value.get('status')) is not int or value['status'] != 1
                or type(value.get('acknowledged')) is not int or value['acknowledged'] not in (0, 1)
                or type(value.get('expired')) is not int or value['expired'] not in (0, 1)):
            return Result('unknown')
        return Result('acknowledged' if value['acknowledged'] else 'expired' if value['expired'] else 'active')

    def cancel(self, receipt):
        value = self._call('receipts/' + parse.quote(receipt, safe='') + '/cancel.json', {})
        return Result('cancelled' if value and type(value.get('status')) is int and value['status'] == 1 else 'unknown')


class EmergencyManager:
    """Single writer, at most one future; call service even when no frames arrive."""
    def __init__(self, db, config, executor, clock, client=None):
        self.db, self.config, self.executor, self.clock = db, config, executor, clock
        self.client = client or PushoverClient()
        self.pending = None
        db.execute('''CREATE TABLE IF NOT EXISTS emergency_notifications(
            id TEXT PRIMARY KEY,event TEXT,state TEXT,receipt TEXT,tag TEXT,
            created REAL,expires_at REAL,next_check REAL)''')
        db.execute('CREATE INDEX IF NOT EXISTS emergency_event_idx ON emergency_notifications(event,created DESC,id DESC)')
        with db:
            recovered = db.execute("UPDATE emergency_notifications SET state='ambiguous' WHERE state='sending' AND (receipt IS NULL OR receipt='')")
            if recovered.rowcount:
                LOG.info('Emergency state: %s', 'ambiguous')
            db.execute("UPDATE emergency_notifications SET state='active' WHERE state='sending' AND receipt IS NOT NULL AND receipt<>''")

    def blocked(self, event=None):
        return bool(self.db.execute("""SELECT 1 FROM emergency_notifications
            WHERE state IN ('sending','active','cancel_pending','ambiguous')
            OR (event=? AND state IN ('acknowledged','expired','cancelled'))
            OR (event=? AND state='shadow' AND ?=0) LIMIT 1""",
            (event,event,int(self.config.notify_enabled))).fetchone())

    def transition(self, identity, state, **fields):
        self.db.execute('UPDATE emergency_notifications SET state=?' + ''.join(',' + key + '=?' for key in fields) + ' WHERE id=?', (state, *fields.values(), identity))
        LOG.info('Emergency state: %s', state)

    def start(self, event, message):
        now = self.clock()
        if self.blocked(event) or self.pending:
            return
        last = self.db.execute('SELECT next_check FROM emergency_notifications WHERE event=? ORDER BY created DESC,id DESC LIMIT 1', (event,)).fetchone()
        if last and now < last['next_check']:
            return
        identity, tag = uuid.uuid4().hex, uuid.uuid4().hex
        expire = min(10800, max(1, getattr(self.config, 'emergency_expire_seconds', 10800)))
        state = 'sending' if self.config.notify_enabled else 'shadow'
        with self.db:
            self.db.execute('INSERT INTO emergency_notifications VALUES(?,?,?,?,?,?,?,?)', (identity, event, state, None, tag, now, now + expire, now))
            LOG.info('Emergency state: %s', state)
        if state == 'sending':
            self.pending = (self.executor.submit(self.client.send, message, max(30, getattr(self.config, 'emergency_retry_seconds', 60)), expire, tag), identity, 'send')

    def drain(self, wait=False):
        if not self.pending:
            return
        future, identity, operation = self.pending
        if not wait and not future.done():
            return
        try:
            result = future.result()
            if not isinstance(result, Result):
                result = Result('unknown')
        except Exception:
            result = Result('unknown')
        self.pending = None
        row = self.db.execute('SELECT * FROM emergency_notifications WHERE id=?', (identity,)).fetchone()
        now = self.clock()
        delay = max(5, getattr(self.config, 'emergency_poll_seconds', 10))
        with self.db:
            if operation == 'send':
                state = result.state if result.state in ('active', 'failed') else 'ambiguous'
                if state == 'active' and not result.receipt:
                    state = 'ambiguous'
                closed = self.db.execute('SELECT closed FROM events WHERE id=?', (row['event'],)).fetchone()
                if state == 'active' and closed and closed['closed'] is not None:
                    state = 'cancel_pending'
                self.transition(identity, state, receipt=result.receipt or None, next_check=now + (60 if state == 'failed' else 0))
            elif operation == 'cancel':
                self.transition(identity, 'cancelled' if result.state == 'cancelled' else 'cancel_pending', next_check=now + delay)
            else:
                state = result.state if result.state in ('acknowledged', 'expired') else row['state']
                self.transition(identity, state, next_check=now + delay)

    def service(self, busy=False):
        self.drain()
        with self.db:
            self.db.execute("""UPDATE emergency_notifications SET state='cancel_pending',next_check=0
                WHERE state='active' AND event IN (SELECT id FROM events WHERE closed IS NOT NULL)""")
        if busy or self.pending or not self.config.notify_enabled:
            return
        row = self.db.execute("SELECT * FROM emergency_notifications WHERE state IN ('active','cancel_pending') AND receipt IS NOT NULL AND next_check<=? ORDER BY next_check,created,id LIMIT 1", (self.clock(),)).fetchone()
        if row:
            operation = 'cancel' if row['state'] == 'cancel_pending' else 'receipt'
            self.pending = (self.executor.submit(getattr(self.client, operation), row['receipt']), row['id'], operation)
