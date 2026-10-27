"""Emergency tests use injected clients only; no live transport."""
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

from smart_tapo.emergency import EmergencyManager, PushoverClient, Result


class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        future = Future()
        self.calls.append((fn, args, future))
        return future


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE events(id TEXT,closed REAL)')
        self.db.execute("INSERT INTO events VALUES('e',NULL)")
        self.now = 100.
        self.config = SimpleNamespace(notify_enabled=True, emergency_poll_seconds=1)
        self.executor = Executor()
        self.client = SimpleNamespace(send=lambda *a: None, receipt=lambda *a: None, cancel=lambda *a: None)
        self.manager = EmergencyManager(self.db, self.config, self.executor, lambda: self.now, self.client)

    def tearDown(self):
        self.db.close()

    def row(self):
        return self.db.execute('SELECT * FROM emergency_notifications ORDER BY created DESC,id DESC').fetchone()

    def test_shadow_is_not_cloud_acceptance_and_does_not_block_later_arming(self):
        self.config.notify_enabled=False
        self.manager.start('e','test')
        self.assertEqual(self.row()['state'],'shadow')
        self.manager.start('e','test')
        self.assertEqual(self.db.execute('SELECT count(*) FROM emergency_notifications').fetchone()[0],1)
        self.assertEqual(self.executor.calls,[])
        self.config.notify_enabled=True
        self.assertFalse(self.manager.blocked('e'))
        self.manager.start('e','test')
        self.assertEqual(len(self.executor.calls),1)

    def complete(self, result):
        self.executor.calls[-1][2].set_result(result)
        self.manager.drain()

    def start(self):
        self.manager.start('e', 'test')

    def test_ack_suppresses_same_event_not_visual_state(self):
        self.start()
        self.complete(Result('active', 'receipt'))
        self.manager.service()
        self.complete(Result('acknowledged'))
        for _ in range(20):
            self.now += 10000
            self.start()
            self.manager.service()
        self.assertEqual(len(self.executor.calls), 2)
        self.assertEqual(self.row()['state'], 'acknowledged')
        self.assertIsNone(self.db.execute('SELECT closed FROM events').fetchone()[0])
        self.db.execute("INSERT INTO events VALUES('new',NULL)")
        self.manager.start('new', 'test')
        self.assertEqual(len(self.executor.calls), 3)

    def test_unknown_poll_backoff_and_expiry_not_ack(self):
        self.start()
        self.complete(Result('active', 'r'))
        self.manager.service()
        self.complete(Result('unknown'))
        self.assertEqual(self.row()['state'], 'active')
        self.now += 4
        self.manager.service()
        self.assertEqual(len(self.executor.calls), 2)
        self.now += 1
        self.manager.service()
        self.complete(Result('expired'))
        self.start()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertEqual(len(self.executor.calls), 3)

    def test_late_send_then_cancel_failure_restart(self):
        self.start()
        self.db.execute('UPDATE events SET closed=101')
        self.complete(Result('active', 'r'))
        self.assertEqual(self.row()['state'], 'cancel_pending')
        self.manager.service()
        self.complete(Result('unknown'))
        self.assertEqual(self.row()['state'], 'cancel_pending')
        self.manager = EmergencyManager(self.db, self.config, self.executor, lambda: self.now, self.client)
        self.now += 5
        self.manager.service()
        self.complete(Result('cancelled'))
        self.assertEqual(self.row()['state'], 'cancelled')
        self.start()
        self.assertEqual(len(self.executor.calls), 3)

    def test_crashed_send_is_ambiguous_globally(self):
        self.start()
        self.manager = EmergencyManager(self.db, self.config, self.executor, lambda: self.now, self.client)
        self.assertEqual(self.row()['state'], 'ambiguous')
        self.assertTrue(self.manager.blocked('other'))
        self.now += 999999
        self.start()
        self.manager.service()
        self.assertEqual(len(self.executor.calls), 1)

    def test_timeout_acceptance_never_retries(self):
        self.start()
        self.complete(Result('unknown'))
        self.now += 999999
        self.start()
        self.assertEqual(self.row()['state'], 'ambiguous')
        self.assertEqual(len(self.executor.calls), 1)

    def test_failed_retry_cooldown_new_tag(self):
        self.start()
        tag = self.row()['tag']
        self.complete(Result('failed'))
        self.start()
        self.assertEqual(len(self.executor.calls), 1)
        self.now += 60
        self.start()
        self.assertNotEqual(self.row()['tag'], tag)

    def test_disabled_recovery_and_shadow_zero_transport(self):
        self.start()
        self.complete(Result('active', 'r'))
        self.config.notify_enabled = False
        self.manager = EmergencyManager(self.db, self.config, self.executor, lambda: self.now, self.client)
        self.manager.service()
        self.db.execute('UPDATE events SET closed=1')
        self.manager.service()
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.row()['state'], 'cancel_pending')
        self.db.execute('DELETE FROM emergency_notifications')
        self.start()
        self.start()
        self.manager.service()
        self.assertEqual(self.row()['state'], 'shadow')
        self.assertEqual(len(self.executor.calls), 1)

    def test_active_restart_resumes_no_send(self):
        self.start()
        self.complete(Result('active', 'r'))
        self.manager = EmergencyManager(self.db, self.config, self.executor, lambda: self.now, self.client)
        self.start()
        self.manager.service()
        self.assertEqual(self.executor.calls[-1][0], self.client.receipt)


class WorkerEmergencyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_worker import WorkerTests
        from dataclasses import replace
        self.fixture = WorkerTests()
        self.fixture.setUp()
        self.worker = self.fixture.worker
        self.worker.config = replace(self.worker.config, notification_priority=2, notify_enabled=True,
                                     stuck_consecutive_frames=2)
        self.executor = Executor()
        self.worker.emergency.executor = self.executor
        self.worker.emergency.client = SimpleNamespace(send=lambda *a: None, receipt=lambda *a: None, cancel=lambda *a: None)

    def tearDown(self):
        if self.worker.emergency.pending and not self.worker.emergency.pending[0].done():
            self.worker.emergency.pending[0].set_result(Result('unknown'))
        self.fixture.tearDown()

    def test_priority_flip_ack_and_new_physical_event(self):
        from dataclasses import replace
        self.fixture.frame(1)
        self.fixture.frame(2)
        self.executor.calls[-1][2].set_result(Result('active', 'r'))
        self.worker.scan()
        self.executor.calls[-1][2].set_result(Result('acknowledged'))
        self.worker.scan()
        self.worker.config = replace(self.worker.config, notification_priority=1)
        for sequence in range(3, 20):
            self.fixture.frame(sequence)
        self.assertEqual(self.worker.active()['kind'], 'stuck')
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
        self.worker.config = replace(self.worker.config, notification_priority=2)
        self.fixture.frame(20, 'EMPTY')
        self.fixture.frame(21, 'EMPTY')
        self.fixture.frame(22)
        self.fixture.frame(23)
        self.assertEqual(len(self.executor.calls), 3)

    def test_cancel_pending_protects_evidence_until_provider_success(self):
        self.fixture.frame(1)
        self.fixture.frame(2)
        self.fixture.frame(3, 'EMPTY')
        self.fixture.frame(4, 'EMPTY')
        self.executor.calls[-1][2].set_result(Result('active', 'r'))
        self.worker.scan()
        self.executor.calls[-1][2].set_result(Result('unknown'))
        self.worker.emergency.drain()
        self.fixture.now += 99 * 86400
        self.worker.cleanup()
        self.assertTrue(self.worker.db.execute('SELECT 1 FROM evidence').fetchone())
        self.assertTrue(self.worker.db.execute('SELECT 1 FROM events').fetchone())

    def test_ambiguous_blocks_priority_one_after_restart(self):
        from dataclasses import replace
        self.fixture.frame(1)
        self.fixture.frame(2)
        self.executor.calls[-1][2].set_result(Result('ambiguous'))
        self.worker.emergency.drain()
        self.worker.config = replace(self.worker.config, notification_priority=1)
        for sequence in range(3, 15):
            self.fixture.frame(sequence)
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
        self.assertEqual(len(self.executor.calls), 1)


class TransportTests(unittest.TestCase):
    def test_http_rejection_and_server_error_close_responses(self):
        with patch.dict('os.environ',{'PUSHOVER_TOKEN':'test','PUSHOVER_USER':'test'}):
            for code,state in ((400,'failed'),(500,'ambiguous')):
                body=io.BytesIO(b'{"status":0}')
                error=HTTPError('https://api.pushover.net',code,'redacted',{},body)
                with patch('smart_tapo.emergency.request.urlopen',side_effect=error):
                    self.assertEqual(PushoverClient().send('test',60,100,'tag').state,state)
                self.assertTrue(body.closed)

    def call(self, response, operation):
        with patch.dict('os.environ', {'PUSHOVER_TOKEN': 'test-token', 'PUSHOVER_USER': 'test-user'}), patch('smart_tapo.emergency.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as opener:
            result = operation(PushoverClient())
            args, kwargs = opener.call_args
            return result, args[0], kwargs

    def test_full_send_payload(self):
        result, url, kwargs = self.call({'status': 1, 'receipt': 'abc'}, lambda c: c.send('test', 60, 10800, 'tag'))
        self.assertEqual(result, Result('active', 'abc'))
        self.assertEqual(url, 'https://api.pushover.net/1/messages.json')
        self.assertEqual(parse_qs(kwargs['data'].decode()), dict(token=['test-token'], user=['test-user'], message=['test'], priority=['2'], retry=['60'], expire=['10800'], tags=['tag']))
        self.assertEqual(kwargs['timeout'], 10)

    def test_receipt_query_cancel_post(self):
        result, url, kwargs = self.call({'status': 1, 'acknowledged': 1, 'expired': 0}, lambda c: c.receipt('abc'))
        self.assertEqual(result.state, 'acknowledged')
        self.assertEqual(urlsplit(url).path, '/1/receipts/abc.json')
        self.assertEqual(parse_qs(urlsplit(url).query), {'token': ['test-token']})
        self.assertIsNone(kwargs['data'])
        result, url, kwargs = self.call({'status': 1}, lambda c: c.cancel('abc'))
        self.assertTrue(url.endswith('/receipts/abc/cancel.json'))
        self.assertEqual(parse_qs(kwargs['data'].decode()), {'token': ['test-token']})
        self.assertEqual(result.state, 'cancelled')

    def test_malformed_fields_are_unknown_or_ambiguous(self):
        for response in ({'status': 1}, {'status': 1, 'acknowledged': '1', 'expired': 0}, {'status': 0}):
            result, _, _ = self.call(response, lambda c: c.receipt('abc'))
            self.assertEqual(result.state, 'unknown')
        result, _, _ = self.call({'status': 1}, lambda c: c.send('test', 60, 100, 'tag'))
        self.assertEqual(result.state, 'ambiguous')
        result, _, _ = self.call({'status': 0}, lambda c: c.send('test', 60, 100, 'tag'))
        self.assertEqual(result.state, 'failed')

    def test_timeout_oversized_and_non_object(self):
        with patch.dict('os.environ', {'PUSHOVER_TOKEN': 'test', 'PUSHOVER_USER': 'test'}):
            for response in (b' ' * 65537, b'[]', b'{'):
                with patch('smart_tapo.emergency.request.urlopen', return_value=io.BytesIO(response)):
                    self.assertEqual(PushoverClient().send('test', 60, 100, 'tag').state, 'ambiguous')
            with patch('smart_tapo.emergency.request.urlopen', side_effect=TimeoutError):
                self.assertEqual(PushoverClient().send('test', 60, 100, 'tag').state, 'ambiguous')


class DiskRecoveryTests(unittest.TestCase):
    def test_reopen_known_receipt_uses_worker_thread_only_for_transport(self):
        import pathlib
        main_thread=threading.get_ident()
        transport_threads=[]
        def send(*args):
            transport_threads.append(threading.get_ident())
            return Result('active','receipt-private')
        config=SimpleNamespace(notify_enabled=True,emergency_poll_seconds=10)
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'worker.sqlite3'
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            try:
                db.execute('CREATE TABLE events(id TEXT,closed REAL)')
                db.execute("INSERT INTO events VALUES('e',NULL)")
                with ThreadPoolExecutor(max_workers=1) as executor:
                    manager=EmergencyManager(db,config,executor,lambda:100.,SimpleNamespace(send=send))
                    manager.start('e','test')
                    manager.drain(wait=True)
            finally:db.close()
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            try:
                executor=Executor()
                client=SimpleNamespace(receipt=lambda r:Result('acknowledged'))
                manager=EmergencyManager(db,config,executor,lambda:200.,client)
                self.assertTrue(manager.blocked('e'))
                manager.service()
                self.assertEqual(executor.calls[0][0],client.receipt)
                self.assertEqual(executor.calls[0][1],('receipt-private',))
                executor.calls[0][2].set_result(Result('acknowledged'))
                manager.drain()
                self.assertTrue(manager.blocked('e'))
                self.assertNotEqual(transport_threads,[main_thread])
                self.assertEqual(db.execute('SELECT state FROM emergency_notifications').fetchone()[0],'acknowledged')
            finally:db.close()
