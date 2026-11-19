"""Owned temporary SQLite + fake HTTP only. Never contact a provider."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from smart_tapo.notify_api import NotifyTarget, AlertMessage, NotifyResult, send_alert, deliver_alert
from smart_tapo.notify_history import NotifyHistory, NotifyHistoryError, RETENTION_SECONDS, body_text


class Response:
    def __init__(self, code, body, headers=None):
        self.code,self.headers = code,headers or {}
        self.stream = io.BytesIO(body)
    def read(self, size):return self.stream.read(size)
    def close(self):self.stream.close()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = [2_000_000_000.]
        self.history = NotifyHistory(self.root/'chosen-data',clock=lambda:self.now[0])
        self.key = 'SYNTHETIC-APPLICATION-KEY-DO-NOT-STORE'
        self.target = NotifyTarget('https://notify.invalid',self.key,history=self.history,channel='manual_test')
        self.alert = AlertMessage('alert_1','same-event-1','TEST', 'payload '+self.key,'critical')
        self.cancel = threading.Event()
        self.guard = patch('smart_tapo.notify_api.request.build_opener',side_effect=AssertionError('HTTP forbidden'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def send(self, code, body, headers=None):
        class Opener:
            def open(inner, request, timeout):return Response(code,body,headers)
        return send_alert(self.target,self.alert,self.cancel,opener=Opener())

    def test_actual_transport_records_success_and_failure_without_secrets(self):
        accepted=dict(alert_id='alert_1',status='provider_accepted',importance='critical',
            acknowledged=False,delivery_verified=False,expires_at='2099-01-01T00:00:00Z',
            ack_url='https://private.invalid/token',api_key=self.key)
        self.assertEqual(self.send(202,json.dumps(accepted).encode()).state,'success')
        failed=json.dumps(dict(error=dict(code='unauthorized',message='echo '+self.key),
                               token='different-token-private')).encode()
        self.assertEqual(self.send(401,failed,{'Set-Cookie':'private-cookie','Content-Type':'application/json'}).state,'failed')
        rows=self.history.list_page()
        self.assertEqual([r['http_status'] for r in rows],[401,202])
        self.assertEqual([r['state'] for r in rows],['failed','success'])
        self.assertNotIn('request_body',rows[0])
        detail=self.history.detail(rows[0]['id'])
        text=json.dumps(detail)
        for secret in (self.key,'different-token-private','private-cookie'):
            self.assertNotIn(secret,text)
        self.assertEqual(detail['url'],'https://notify.invalid/v1/alerts')
        self.assertIn('critical',detail['request_body'])
        self.assertIn('unauthorized',detail['response_body'])
        self.assertEqual(detail['request_headers']['Authorization'],'[REDACTED]')
        from smart_tapo.diagnostics import APP_VERSION
        self.assertEqual(detail['request_headers']['User-Agent'],'SmartTapo/'+APP_VERSION.removeprefix('v'))
        self.assertEqual(detail['request_headers']['Accept'],'application/json')
        for path in self.history.root.glob('notify-history.sqlite3*'):
            self.assertNotIn(self.key.encode(),path.read_bytes())
        accepted_detail=self.history.detail(rows[1]['id'])
        self.assertNotIn('https://private.invalid',accepted_detail['response_body'])

    def test_retry_records_each_attempt_not_only_final_outcome(self):
        calls=[]
        class Cancel:
            def is_set(inner):return False
            def wait(inner, seconds):return False
        class Opener:
            def open(inner, request, timeout):
                calls.append(request)
                return Response(503,b'{"error":{"code":"database_unavailable"}}')
        cancel=Cancel()
        result=deliver_alert(self.target,self.alert,cancel,
            sender=lambda t,a,c:send_alert(t,a,c,opener=Opener()),attempts=3)
        self.assertEqual(result.code,'retry_budget_exhausted')
        self.assertEqual(len(self.history.list_page()),3)
        self.assertTrue(result.history_saved)
        self.assertTrue(all(req.data==self.alert._body for req in calls))

    def test_cancel_before_open_does_not_create_history_or_send(self):
        self.cancel.set()
        self.assertEqual(send_alert(self.target,self.alert,self.cancel).code,'cancelled')
        self.assertFalse(self.history.path.exists())

    def test_network_failure_is_recorded_without_raw_exception(self):
        class Opener:
            def open(inner,*args,**kwargs):raise TimeoutError(self.key)
        result=send_alert(self.target,self.alert,self.cancel,opener=Opener())
        self.assertEqual(result.code,'network_unknown')
        detail=self.history.detail(self.history.list_page()[0]['id'])
        self.assertIsNone(detail['http_status'])
        self.assertIsNone(detail['response_body'])
        self.assertNotIn(self.key,json.dumps(detail))

    def test_missing_finish_after_process_exit_remains_unknown_not_success(self):
        identifier=self.history.begin(self.target,self.alert,'manual_test')
        reopened=NotifyHistory(self.history.root,clock=lambda:self.now[0])
        detail=reopened.detail(identifier)
        self.assertEqual((detail['state'],detail['code']),('ambiguous','outcome_unknown'))

    def test_30_day_visibility_and_batched_physical_cleanup_all_states(self):
        self.send(401,b'{"error":{"code":"unauthorized"}}')
        identifier=self.history.list_page()[0]['id']
        self.now[0]+=RETENTION_SECONDS-1
        self.assertIsNotNone(self.history.detail(identifier))
        self.now[0]+=2
        self.assertEqual(self.history.list_page(),[])
        self.assertIsNone(self.history.detail(identifier))
        self.history.prune()
        with closing(sqlite3.connect(self.history.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notify_attempts').fetchone()[0],0)

    def test_pages_are_bounded_and_details_are_lazy(self):
        for _ in range(103):self.history.begin(self.target,self.alert,'manual_test')
        first=self.history.list_page()
        second=self.history.list_page(before_id=first[-1]['id'])
        self.assertEqual((len(first),len(second)),(100,3))
        self.assertFalse({r['id'] for r in first}&{r['id'] for r in second})
        with self.assertRaises(ValueError):self.history.list_page(limit=101)

    def test_concurrent_instances_do_not_share_connections_or_lose_rows(self):
        self.history.begin(self.target,self.alert,'manual_test')
        def write(_):
            store=NotifyHistory(self.history.root,clock=lambda:self.now[0])
            return store.begin(self.target,self.alert,'health')
        with ThreadPoolExecutor(max_workers=3) as executor:
            ids=list(executor.map(write,range(12)))
        self.assertEqual(len(set(ids)),12)
        self.assertEqual(len(self.history.list_page()),13)

    def test_writer_waits_for_short_real_sqlite_contention_without_dropping_attempt(self):
        self.history.begin(self.target,self.alert,'manual_test')
        blocker=sqlite3.connect(self.history.path)
        blocker.execute('BEGIN IMMEDIATE')
        entered,finished=threading.Event(),threading.Event()
        original_connect=sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            def execute(inner,sql,*args,**kwargs):
                entered.set()
                return super().execute(sql,*args,**kwargs)
        def connect(*args,**kwargs):
            return original_connect(*args,**kwargs,factory=ObservedConnection)
        def write():
            try:return self.history.begin(self.target,self.alert,'health')
            finally:finished.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with patch('smart_tapo.notify_history.sqlite3.connect',side_effect=connect):
                    future=executor.submit(write)
                    self.assertTrue(entered.wait(2))
                    prematurely_finished=finished.wait(.3)
            finally:
                blocker.rollback();blocker.close()
            self.assertFalse(prematurely_finished,'An ordinary short writer lock must not lose this attempt')
            self.assertIsInstance(future.result(timeout=3),int)
        self.assertEqual(len(self.history.list_page()),2)

    def test_persistently_locked_writer_still_fails_within_bounded_wait(self):
        self.history.begin(self.target,self.alert,'manual_test')
        blocker=sqlite3.connect(self.history.path)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future=executor.submit(self.history.begin,self.target,self.alert,'health')
                with self.assertRaises(NotifyHistoryError):future.result(timeout=3)
        finally:
            blocker.rollback();blocker.close()
        self.assertEqual(len(self.history.list_page()),1)

    def test_storage_failure_does_not_mask_acceptance_or_create_extra_send(self):
        accepted=json.dumps(dict(alert_id='alert_1',status='provider_accepted',importance='critical',
            acknowledged=False,delivery_verified=False,expires_at='2099-01-01T00:00:00Z')).encode()
        with patch.object(self.history,'begin',side_effect=NotifyHistoryError('fixed safe error')):
            result=self.send(202,accepted)
        self.assertEqual(result.state,'success')
        self.assertFalse(result.history_saved)

    def test_relative_root_never_falls_back_to_cwd(self):
        store=NotifyHistory(Path('not-an-authorized-global-root'))
        with self.assertRaises(NotifyHistoryError):store.begin(self.target,self.alert,'manual_test')

    def test_escaped_key_and_nested_capability_are_redacted(self):
        encoded=''.join('\\u%04x'%ord(c) for c in self.key)
        raw=('{"message":"'+encoded+'","nested":{"capability":"PRIVATE-CAPABILITY"}}').encode()
        text=body_text(raw,self.key)
        self.assertNotIn(self.key,text)
        self.assertNotIn('PRIVATE-CAPABILITY',text)

    def test_duplicate_response_fields_are_preserved_for_diagnostics(self):
        text=body_text(b'{"state":"first","state":"second","token":"hidden"}',self.key)
        self.assertIn('"state":"first"',text)
        self.assertIn('"state":"second"',text)
        self.assertNotIn('hidden',text)

    def test_non_json_bearer_token_and_cookie_are_redacted(self):
        raw=b'proxy: Bearer OTHER-SECRET\nSet-Cookie: cookie-private\ntoken=hidden-token debug=visible'
        text=body_text(raw,self.key)
        for secret in ('OTHER-SECRET','cookie-private','hidden-token'):
            self.assertNotIn(secret,text)
        self.assertIn('debug=visible',text)

    def test_credential_text_matching_json_field_cannot_disable_secret_detection(self):
        for key in ('token','secret','api_key'):
            text=body_text(b'{"token":"SYNTHETIC-CAPABILITY-OTHER-THAN-KEY"}',key)
            self.assertNotIn('SYNTHETIC-CAPABILITY-OTHER-THAN-KEY',text)

    def test_oversized_body_is_explicitly_truncated(self):
        self.send(401,b'x'*65537)
        detail=self.history.detail(self.history.list_page()[0]['id'])
        self.assertTrue(detail['response_truncated'])
        self.assertEqual(len(detail['response_body']),65536)

    def test_other_database_is_not_adopted_or_modified(self):
        self.history.root.mkdir()
        with closing(sqlite3.connect(self.history.path)) as db:
            db.execute('CREATE TABLE unrelated(id INTEGER)');db.commit()
        with self.assertRaises(NotifyHistoryError):self.history.begin(self.target,self.alert,'notify')
        with closing(sqlite3.connect(self.history.path)) as db:
            self.assertEqual(db.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall(),[('unrelated',)])

    def test_stuck_and_health_paths_bind_shared_history_without_changing_importance(self):
        from types import SimpleNamespace
        from smart_tapo.worker import notify_sender
        from smart_tapo.desktop_health_alerts import send_health
        class Opener:
            def open(inner,*args,**kwargs):return Response(401,b'{"error":{"code":"unauthorized"}}')
        real_send=send_alert
        def fake_send(t,a,c):return real_send(t,a,c,opener=Opener())
        def fake_deliver(t,a,c):return deliver_alert(t,a,c,sender=fake_send)
        config=SimpleNamespace(data_dir=self.history.root,notify_api_url='https://notify.invalid')
        with patch.dict('os.environ',{'NOTIFY_API_KEY':self.key}), \
             patch('smart_tapo.worker.deliver_alert',side_effect=fake_deliver):
            self.assertEqual(notify_sender(config,self.cancel,history=self.history)('synthetic stuck','event_2'),'failed')
        notice=SimpleNamespace(kind='recovered',scope='a'*32,serial=1)
        with patch('smart_tapo.desktop_health_alerts.send_alert',side_effect=fake_send), \
             patch('smart_tapo.desktop_health_alerts.format_notice',return_value='synthetic recovery'):
            self.assertEqual(send_health(self.key,'https://notify.invalid',notice,self.cancel,'critical',history=self.history),'failed')
        rows=self.history.list_page()
        self.assertEqual([(r['channel'],r['importance']) for r in rows],[('health','normal'),('stuck','critical')])
