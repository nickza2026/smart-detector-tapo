"""Bounded rechecks under existing click consent; no UI clicks or real HTTP."""
from concurrent.futures import Future
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from smart_tapo.desktop_webhook import Sender, _QueueFailure
from smart_tapo.storage_diagnostics import StoragePathUnsafeError


class SenderRestartTests(unittest.TestCase):
    def sender(self):
        sender = Sender(); sender.executor.shutdown(); sender.executor = Mock()
        sender.scope = (Path('synthetic-unused'), None)
        self.addCleanup(sender.close)
        return sender

    def failed(self, sender, error, now):
        sender.future = Future(); sender.future.set_exception(error)
        with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=now): sender.poll()

    def due(self, sender, now):
        future = Future(); sender.executor.submit.return_value = future
        with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=now): sender.poll()
        return future

    def test_worker_failure_waits_fifteen_seconds_without_new_consent(self):
        sender = self.sender(); scope, stop = sender.scope, sender.stop_event
        self.failed(sender, RuntimeError('PRIVATE'), 100)
        self.assertIs(sender.scope, scope)
        self.assertIs(sender.stop_event, stop)
        self.assertEqual(sender.next_attempt, 115)
        self.due(sender, 114.999); sender.executor.submit.assert_not_called()
        future = self.due(sender, 115)
        self.assertIs(sender.future, future)
        self.assertEqual(sender.restart_attempts, 1)
        self.assertNotIn('PRIVATE', sender.status)

    def test_repeated_failures_stop_after_exactly_three_restarts(self):
        for error in (RuntimeError('PRIVATE'), _QueueFailure(StoragePathUnsafeError('PRIVATE'), 'queue_dispatch')):
            with self.subTest(error=type(error).__name__):
                sender = self.sender(); self.failed(sender, error, 0)
                for attempt in range(1, 4):
                    future = self.due(sender, attempt*15)
                    self.assertEqual(sender.executor.submit.call_count, attempt)
                    future.set_exception(error)
                    with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=attempt*15): sender.poll()
                self.assertIsNone(sender.scope)
                self.assertTrue(sender.stop_event.is_set())
                self.due(sender, 1000)
                self.assertEqual(sender.executor.submit.call_count, 3)

    def test_success_resets_budget_only_after_future_returns(self):
        sender = self.sender(); self.failed(sender, RuntimeError('PRIVATE'), 0)
        future = self.due(sender, 15)
        self.assertEqual(sender.restart_attempts, 1)
        self.due(sender, 100)
        self.assertEqual(sender.executor.submit.call_count, 1, 'Never overlap a pending future')
        future.set_result(None)
        with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=15): sender.poll()
        self.assertEqual(sender.restart_attempts, 0)

    def test_manual_stop_cancels_wait_and_does_not_resume_from_late_failure(self):
        sender = self.sender(); self.failed(sender, RuntimeError('PRIVATE'), 0)
        sender.stop(); self.due(sender, 15)
        sender.executor.submit.assert_not_called()
        self.failed(sender, RuntimeError('late PRIVATE'), 100)
        self.assertIsNone(sender.scope)
        sender.executor.submit.assert_not_called()

    def test_submission_error_is_safe_stop_not_untracked_duplicate_attempt(self):
        sender = self.sender(); sender.executor.submit.side_effect = RuntimeError('PRIVATE')
        with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=0): sender.poll()
        self.assertIsNone(sender.scope)
        self.assertTrue(sender.stop_event.is_set())
        self.assertNotIn('PRIVATE', sender.status)

    def test_idle_launch_never_starts_without_click_scope(self):
        sender = self.sender(); sender.scope = None
        self.due(sender, 1000); sender.executor.submit.assert_not_called()

    def test_real_hardlink_is_rechecked_three_times_but_never_opens_sql_or_client(self):
        import os, tempfile, hashlib
        from smart_tapo.desktop_webhook import _attempt
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling_webhook import WebhookTarget
        with tempfile.TemporaryDirectory() as temporary:
            store = RefuelingStore(Path(temporary)/'store', 'cam')
            path = store.db_path; store.close()
            os.link(path, Path(temporary)/'owned-alias')
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cam')
            sender = self.sender(); sender.scope = (path.parent, target)
            factory = Mock(side_effect=AssertionError('Must not reach HTTP client'))
            sender.client_factory = factory
            def submit(function, *args):
                future = Future()
                try: future.set_result(function(*args))
                except Exception as error: future.set_exception(error)
                return future
            sender.executor.submit.side_effect = submit
            with patch('smart_tapo.refueling_store.sqlite3.connect', side_effect=AssertionError('Unsafe DB must not open')):
                with self.assertRaises(_QueueFailure) as caught:
                    _attempt(path.parent, target, sender.stop_event, factory)
                self.assertEqual(caught.exception.guard['links'], 2)
                self.failed(sender, caught.exception, 0)
                for attempt in range(1, 4):
                    with patch('smart_tapo.desktop_webhook.time.monotonic', return_value=attempt*15):
                        sender.poll(); sender.poll()
            self.assertIsNone(sender.scope)
            self.assertEqual(sender.executor.submit.call_count, 3)
            factory.assert_not_called()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_editing_url_or_key_cancels_delayed_restart(self):
        from smart_tapo.smoke_window import install
        install()
        import tkinter as tk
        from smart_tapo.config import Config
        from smart_tapo.desktop_webhook import WebhookPanel
        root = tk.Tk(); root.withdraw()
        self.addCleanup(root.destroy)
        for name in ('url', 'key'):
            with self.subTest(field=name):
                config = Config(refueling_api_url='https://example.invalid', refueling_api_enabled=True)
                panel = WebhookPanel(root, lambda: config, lambda c: None, Mock())
                panel.sender.executor.shutdown(); panel.sender.executor = Mock()
                try:
                    panel.scope = (Path(config.data_dir).absolute(), config.camera_id, config.refueling_api_url)
                    panel.sender.scope = (Path('unused'), None)
                    self.failed(panel.sender, RuntimeError('PRIVATE'), 0)
                    getattr(panel, name).set('https://changed.invalid' if name == 'url' else 'PRIVATE-key')
                    self.assertIsNone(panel.sender.scope)
                    self.assertIsNone(panel.scope)
                    self.due(panel.sender, 1000)
                    panel.sender.executor.submit.assert_not_called()
                finally:
                    panel.close(); panel.destroy()
