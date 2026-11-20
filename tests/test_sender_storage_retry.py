"""Sender-only regressions: synthetic failures, no network or user data."""
from concurrent.futures import Future
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from smart_tapo.desktop_webhook import Sender, _attempt


def busy():
    exc = sqlite3.OperationalError('SECRET/path')
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


class SenderStorageRetryTests(unittest.TestCase):
    def test_busy_open_retains_consent_and_paces_then_resets(self):
        sender = Sender()
        sender.scope = scope = (Path('synthetic-unused'), None)
        stop = sender.stop_event
        sender.executor.shutdown()
        sender.executor = Mock()
        try:
            for now, delay in [(0, 2), (2, 4), (6, 8), (14, 16), (30, 32), (62, 60), (122, 60)]:
                with patch('smart_tapo.desktop_webhook.RefuelingStore', side_effect=busy()):
                    future = Future()
                    try:
                        _attempt('unused', Mock(camera_id='cam'), stop, Mock())
                    except Exception as exc:
                        future.set_exception(exc)
                sender.future = future
                with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=now):
                    sender.poll()
                self.assertIs(sender.scope, scope)
                self.assertIs(sender.stop_event, stop)
                self.assertFalse(stop.is_set())
                self.assertEqual(sender.next_attempt, now + delay)
                self.assertNotIn('SECRET', sender.status)
                sender.executor.submit.assert_not_called()
            sender.future = Future()
            sender.future.set_result(None)
            with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=123):
                sender.poll()
            self.assertNotIn('storage_busy', sender.status)
            self.assertEqual(sender.storage_failures, 0)
        finally:
            sender.close()

    def test_unknown_transport_permission_stops_and_does_not_leak(self):
        sender = Sender()
        sender.scope = (Path('unused'), None)
        sender.future = Future()
        sender.future.set_exception(PermissionError('SECRET/path'))
        sender.poll()
        self.assertIsNone(sender.scope)
        self.assertNotIn('SECRET', sender.status)
        self.assertIn('sender_error', sender.status)
        sender.close()

    def test_actual_disposable_sqlite_lock_recovers(self):
        import tempfile
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling_webhook import WebhookTarget
        from smart_tapo.desktop_webhook import _QueueFailure
        target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cam')
        with tempfile.TemporaryDirectory() as directory:
            store = RefuelingStore(directory, 'cam')
            lock = sqlite3.connect(str(store.db_path), isolation_level=None)
            store.close()
            lock.execute('BEGIN EXCLUSIVE')
            factory = lambda *args: RefuelingStore(*args, busy_timeout_seconds=0)
            try:
                with patch('smart_tapo.desktop_webhook.RefuelingStore', side_effect=factory):
                    with self.assertRaises(_QueueFailure) as caught:
                        _attempt(directory, target, threading.Event(), Mock())
                self.assertEqual(caught.exception.code, 'storage_busy')
            finally:
                lock.rollback()
                lock.close()
            client = Mock(target=target)
            self.assertIsNone(_attempt(directory, target, threading.Event(), lambda *args: client))
            client.send_event.assert_not_called()

    def test_storage_boundary_and_close_preserve_original_failure(self):
        from smart_tapo.desktop_webhook import _QueueFailure
        target = Mock(camera_id='cam')
        store = Mock()
        store.cleanup.side_effect = busy()
        store.close.side_effect = PermissionError('SECRET')
        def dispatch(queue, client):
            queue.cleanup()
        with patch('smart_tapo.desktop_webhook.RefuelingStore', return_value=store), patch(
                'smart_tapo.desktop_webhook.dispatch_one', side_effect=dispatch):
            with self.assertRaises(_QueueFailure) as caught:
                _attempt('unused', target, threading.Event(), Mock())
        self.assertEqual((caught.exception.code, caught.exception.phase), ('storage_busy', 'queue_dispatch'))
        self.assertNotIn('SECRET', str(caught.exception))

    def test_permanent_storage_failures_stop(self):
        from smart_tapo.storage_diagnostics import StorageCameraMismatchError, StorageSchemaError
        corrupt = sqlite3.DatabaseError('SECRET')
        corrupt.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        for exc in [StorageCameraMismatchError('SECRET'),
                    StorageSchemaError('SECRET'), sqlite3.DatabaseError('SECRET'), corrupt]:
            sender = Sender()
            sender.scope = (Path('unused'), None)
            sender.future = Future()
            with patch('smart_tapo.desktop_webhook.RefuelingStore', side_effect=exc):
                try:
                    _attempt('unused', Mock(camera_id='cam'), threading.Event(), Mock())
                except Exception as failure:
                    sender.future.set_exception(failure)
            sender.poll()
            self.assertIsNone(sender.scope)
            self.assertNotIn('SECRET', sender.status)
            sender.close()

    def test_stopped_failure_does_not_rearm(self):
        sender = Sender()
        sender.scope = (Path('unused'), None)
        sender.future = Future()
        sender.future.set_exception(busy())
        sender.stop()
        sender.poll()
        self.assertIsNone(sender.scope)
        self.assertTrue(sender.stop_event.is_set())
        self.assertFalse(sender.busy)
        sender.close()

    def test_auth_or_firewall_stays_terminal_even_if_queue_mark_or_close_is_busy(self):
        from smart_tapo.refueling_webhook import DeliveryResult
        for code in ('receiver_auth_rejected','receiver_firewall_blocked'):
            for phase in ('mark','close'):
                with self.subTest(code=code,phase=phase):
                    store=Mock()
                    client=Mock(target=Mock())
                    client.send_event.return_value=DeliveryResult('failed',code)
                    if phase=='mark':store.mark_delivery.side_effect=busy()
                    else:store.close.side_effect=busy()
                    def dispatch(queue,transport):
                        result=transport.send_event(b'synthetic','event')
                        queue.mark_delivery('event','failed')
                        return result
                    with patch('smart_tapo.desktop_webhook.RefuelingStore',return_value=store),patch('smart_tapo.desktop_webhook.dispatch_one',side_effect=dispatch):
                        result=_attempt('unused',Mock(camera_id='cam'),threading.Event(),lambda *a:client)
                    self.assertEqual(result.error_code,code)

    def test_post_ack_busy_reuses_event_and_close_busy_never_resends_committed_event(self):
        self._assert_post_ack_stability(busy)

    def test_post_ack_ephemeral_metadata_busy_keeps_same_event_and_consent(self):
        from smart_tapo.storage_diagnostics import StorageMetadataBusyError
        def metadata_busy():
            return StorageMetadataBusyError(guard=dict(reason='non_single_link', component='journal',
                depth=0, mode=0o100600, links=0, attributes=32, reparse_tag=0))
        self._assert_post_ack_stability(metadata_busy)

    def _assert_post_ack_stability(self, error_factory):
        import tempfile,time
        from smart_tapo.refueling import ConfirmedAnnouncement
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling_webhook import WebhookTarget,DeliveryResult
        for phase in ('mark','close'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                target=WebhookTarget('https://example.invalid','synthetic-key-only','cam')
                seed=RefuelingStore(directory,'cam')
                now=time.time()
                seed.accept('ready',ConfirmedAnnouncement('paid_ready',now-6,now))
                original=seed.pending_jobs(target.destination_key)[0]
                calls=[];logical={}
                class Client:
                    def __init__(self):self.target=target
                    def send_event(self,payload,event_id):
                        calls.append((event_id,payload))
                        if event_id in logical:
                            if logical[event_id]!=payload:raise AssertionError('Idempotency changed')
                        logical[event_id]=payload
                        return DeliveryResult('delivered')
                real_mark,real_close=RefuelingStore.mark_delivery,RefuelingStore.close
                fail=[True]
                def mark(store,*a,**kw):
                    if phase=='mark' and fail[0]:
                        fail[0]=False;raise error_factory()
                    return real_mark(store,*a,**kw)
                def close(store):
                    real_close(store)
                    if phase=='close' and fail[0]:
                        fail[0]=False;raise error_factory()
                sender=Sender();sender.scope=scope=(Path(directory),target)
                sender.executor.shutdown();sender.executor=Mock()
                try:
                    with patch.object(RefuelingStore,'mark_delivery',mark),patch.object(RefuelingStore,'close',close):
                        sender.future=Future()
                        try:_attempt(directory,target,sender.stop_event,lambda *a:Client())
                        except Exception as exc:sender.future.set_exception(exc)
                    with patch('smart_tapo.desktop_webhook.time.monotonic',return_value=0):sender.poll()
                    self.assertIs(sender.scope,scope)
                    self.assertEqual(sender.storage_failures,1)
                    outcome=_attempt(directory,target,sender.stop_event,lambda *a:Client())
                    sender.future=Future();sender.future.set_result(outcome)
                    with patch('smart_tapo.desktop_webhook.time.monotonic',return_value=1):sender.poll()
                    self.assertEqual(sender.storage_failures,0)
                    self.assertIs(sender.scope,scope)
                    self.assertEqual(len(logical),1)
                    self.assertEqual(len(calls),2 if phase=='mark' else 1)
                    self.assertTrue(all(row==(original['event_id'],original['payload']) for row in calls))
                    self.assertEqual(seed.delivery_counts()['delivered'],1)
                    self.assertEqual(seed.pending_jobs(target.destination_key),[])
                finally:
                    sender.close();seed.close()
