"""Queue diagnostics preserve only bounded metadata; no provider/user DB calls."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from smart_tapo.desktop_webhook import Sender, _QueueBoundary, _QueueFailure, _attempt
from smart_tapo.storage_diagnostics import StoragePathUnsafeError, guard_evidence


def unsafe():
    return StoragePathUnsafeError('PRIVATE/path/key/payload', guard=guard_evidence(
        'non_single_link', 'journal', 0, SimpleNamespace(st_mode=stat.S_IFREG,
        st_nlink=2, st_file_attributes=32, st_reparse_tag=0)))


class QueueGuardDiagnosticTests(unittest.TestCase):
    def test_dispatch_copies_exact_guard_and_allowlisted_operation(self):
        original = unsafe(); store = Mock()
        store.pending_jobs.side_effect = original
        with self.assertRaises(_QueueFailure) as caught:
            _QueueBoundary(store).pending_jobs('private-destination')
        failure = caught.exception
        self.assertEqual(getattr(failure, 'guard', None), original.guard)
        self.assertEqual(getattr(failure, 'operation', None), 'pending_jobs')
        original.guard['links'] = 999
        self.assertEqual(failure.guard['links'], 2)
        self.assertNotIn('PRIVATE', str(failure))

    def test_open_and_close_keep_safe_metadata(self):
        for phase in ('open', 'close'):
            with self.subTest(phase=phase):
                store = Mock(); store.pending_jobs.return_value = []
                if phase == 'close': store.close.side_effect = unsafe()
                factory = patch('smart_tapo.desktop_webhook.RefuelingStore',
                                side_effect=unsafe()) if phase == 'open' else patch(
                                'smart_tapo.desktop_webhook.RefuelingStore', return_value=store)
                with factory, self.assertRaises(_QueueFailure) as caught:
                    _attempt('unused', Mock(camera_id='cam'), threading.Event(), Mock())
                self.assertIsNotNone(getattr(caught.exception, 'guard', None))
                self.assertEqual(getattr(caught.exception, 'operation', None), phase)

    def test_guard_retry_status_has_context_without_raw_exception(self):
        store = Mock(); store.mark_delivery.side_effect = unsafe()
        with self.assertRaises(_QueueFailure) as caught:
            _QueueBoundary(store).mark_delivery('event', 'delivered')
        sender = Sender(); sender.scope = (Path('unused'), None)
        sender.future = Future(); sender.future.set_exception(caught.exception)
        try:
            sender.poll()
            self.assertIsNotNone(sender.scope)
            self.assertIn('15', sender.status)
            self.assertIn('component=journal', sender.status)
            self.assertIn('operation=mark_delivery', sender.status)
            self.assertNotIn('PRIVATE', sender.status)
            self.assertNotIn('ยืนยันรับแล้ว', sender.status)
        finally: sender.close()

    def test_unknown_operation_and_malformed_guard_cannot_leak(self):
        original = unsafe(); original.guard = dict(original.guard, path='PRIVATE')
        store = Mock(); store.PRIVATE.side_effect = original
        with self.assertRaises(_QueueFailure) as caught:
            _QueueBoundary(store).PRIVATE()
        failure = caught.exception
        self.assertIsNone(getattr(failure, 'guard', None))
        self.assertIsNone(getattr(failure, 'operation', None))
        self.assertNotIn('PRIVATE', str(failure))

    def test_actual_ack_then_local_mark_failure_is_not_claimed_unsent_or_delivered(self):
        from smart_tapo.refueling import ConfirmedAnnouncement
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling_webhook import WebhookTarget, DeliveryResult
        with tempfile.TemporaryDirectory() as temporary:
            target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cam')
            with_seed = RefuelingStore(temporary, 'cam')
            try:
                now = time.time(); with_seed.accept('ready', ConfirmedAnnouncement('paid_ready', now-6, now))
                expected = with_seed.pending_jobs(target.destination_key)[0]
                calls = []
                class Client:
                    def __init__(self): self.target = target
                    def send_event(self, payload, event_id):
                        calls.append((event_id, payload)); return DeliveryResult('delivered')
                with patch.object(RefuelingStore, 'mark_delivery', side_effect=unsafe()), self.assertRaises(_QueueFailure) as caught:
                    _attempt(temporary, target, threading.Event(), lambda *a: Client())
                self.assertEqual(calls, [(expected['event_id'], expected['payload'])])
                self.assertEqual(getattr(caught.exception, 'operation', None), 'mark_delivery')
                self.assertEqual(getattr(caught.exception, 'guard', None)['links'], 2)
                self.assertEqual(with_seed.delivery_counts()['delivered'], 0)
            finally: with_seed.close()
