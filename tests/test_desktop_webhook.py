import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_tapo.config import Config
from smart_tapo.desktop_storage import DesktopStore, DesktopStorageError
from smart_tapo.desktop_webhook import Sender
from smart_tapo.refueling_webhook import WebhookTarget, DeliveryResult


class DesktopWebhookTests(unittest.TestCase):
    def test_off_by_default_and_no_startup_io(self):
        sender = Sender(client_factory=lambda *args: self.fail('unexpected HTTP'))
        try:
            sender.poll()
            self.assertFalse(sender.busy)
            self.assertFalse(Config().refueling_api_enabled)
            with self.assertRaises(ValueError):
                sender.start('unused', WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1'))
        finally:
            sender.close()

    def test_settings_reject_insecure_or_credential_urls(self):
        for url in ('http://example.invalid', 'http://127.0.0.1', 'https://u:p@example.invalid',
                    'https://example.invalid/?token=value'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Config(refueling_api_url=url).validate()

    def test_key_is_separate_dpapi_not_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DesktopStore(Path(directory) / 'config.json')
            with patch('smart_tapo.desktop_storage._dpapi', return_value=b'encrypted') as protect:
                store.save_refueling_key('synthetic-key-only', 'https://example.invalid', 'cabinet-1')
                self.assertFalse(protect.call_args.kwargs.get('decrypt', False))
            store.save(Config(refueling_api_url='https://example.invalid'))
            self.assertNotIn('synthetic-key-only', store.config_path.read_text(encoding='utf-8'))
            self.assertEqual((Path(directory) / 'refueling-api.dat').read_bytes(), b'encrypted')
            with patch('smart_tapo.desktop_storage._dpapi', side_effect=Exception('synthetic-key-only')):
                with self.assertRaises(DesktopStorageError) as failure:
                    store.load_refueling_key('https://example.invalid', 'cabinet-1')
                self.assertNotIn('synthetic-key-only', str(failure.exception))

    def test_stop_owns_future_and_prevents_new_target_until_reaped(self):
        entered, release = threading.Event(), threading.Event()
        def attempt(*args):
            entered.set()
            release.wait(3)
            return DeliveryResult('delivered')
        sender = Sender()
        target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1')
        try:
            with patch('smart_tapo.desktop_webhook._attempt', side_effect=attempt):
                sender.start('unused', target, consent=True)
                sender.poll()
                self.assertTrue(entered.wait(1))
                sender.stop()
                with self.assertRaises(ValueError):
                    sender.start('another', target, consent=True)
                future = sender.future
                release.set()
                future.result(timeout=3)
                sender.poll()
                self.assertFalse(sender.busy)
                self.assertIsNone(sender.scope)
        finally:
            release.set()
            sender.close()

    def test_worker_closes_store_and_checks_stop_between_requests(self):
        from smart_tapo.desktop_webhook import _attempt
        stop = threading.Event()
        target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1')
        class Client:
            def __init__(self, destination, media):
                self.target = destination
            def upload_image(self, image):
                stop.set()
                return DeliveryResult('delivered')
            def send_event(self, *args):
                self.fail('must not send after stop')
        def dispatch(store, client):
            self.assertEqual(client.upload_image({}).status, 'delivered')
            self.assertEqual(client.send_event(b'', 'event').status, 'retry')
        with patch('smart_tapo.desktop_webhook.RefuelingStore') as store, patch(
                'smart_tapo.desktop_webhook.dispatch_one', side_effect=dispatch):
            _attempt('unused', target, stop, Client)
            store.return_value.close.assert_called_once()

    def test_attempt_log_is_optional_for_two_argument_client_factories(self):
        from smart_tapo.desktop_webhook import _attempt
        from smart_tapo.refueling_api_log import ApiAttemptLog
        target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1')
        class Client:
            def __init__(self, destination, media):
                self.target = destination
        with tempfile.TemporaryDirectory() as directory, \
                patch('smart_tapo.desktop_webhook.RefuelingStore'), \
                patch('smart_tapo.desktop_webhook.dispatch_one', return_value=DeliveryResult('delivered')):
            result = _attempt('unused', target, threading.Event(), Client, ApiAttemptLog(Path(directory) / 'api.jsonl'))
        self.assertEqual(result.status, 'delivered')

    def test_target_repr_hides_credentials(self):
        target = WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1')
        self.assertNotIn('synthetic-key-only', repr(target))


class RecoveryTests(unittest.TestCase):
    def test_wrong_key_recovery_preserves_facts_and_requires_explicit_consent(self):
        from smart_tapo.refueling import ConfirmedAnnouncement
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling_webhook import dispatch_one
        with tempfile.TemporaryDirectory() as directory:
            now = 1000.
            store = RefuelingStore(Path(directory)/'owned', 'cam1', clock=lambda:now)
            try:
                store.accept('ready', ConfirmedAnnouncement('paid_ready', 100, 106))
                bad = WebhookTarget('https://example.invalid/api', 'synthetic-wrong-key', 'cam1')
                good = WebhookTarget('https://example.invalid/api', 'synthetic-right-key', 'cam1')
                seen = []
                class Client:
                    def __init__(self, target): self.target = target
                    def send_event(self, payload, event_id):
                        seen.append((payload, event_id))
                        return DeliveryResult('failed', 'receiver_auth_rejected') if self.target == bad else DeliveryResult('delivered')
                self.assertEqual(dispatch_one(store, Client(bad), clock=lambda:now).error_code, 'receiver_auth_rejected')
                original = tuple(store._db.execute('SELECT event_id,payload,revision,created,expires_at FROM jobs').fetchone())
                self.assertIsNone(dispatch_one(store, Client(good), clock=lambda:now))
                preview = store.rebind_unconfirmed(good.destination_key)
                self.assertEqual(len(preview), 1)
                self.assertEqual(store.rebind_unconfirmed(good.destination_key, expected_ids=preview, consent=False), preview)
                self.assertIsNone(dispatch_one(store, Client(good), clock=lambda:now))
                with self.assertRaises(ValueError):
                    store.rebind_unconfirmed(good.destination_key, expected_ids=(), consent=True)
                self.assertEqual(store.rebind_unconfirmed(good.destination_key, expected_ids=preview, consent=True), 1)
                self.assertEqual(tuple(store._db.execute('SELECT event_id,payload,revision,created,expires_at FROM jobs').fetchone()), original)
                self.assertEqual(dispatch_one(store, Client(good), clock=lambda:now).status, 'delivered')
                self.assertEqual(seen[0], seen[1])
                self.assertEqual(store.rebind_unconfirmed(bad.destination_key), ())
                with self.assertRaises(ValueError):
                    RefuelingStore(Path(directory)/'owned', 'wrong-camera')
                # Expired unconfirmed jobs are never offered for recovery.
                store.accept('again', ConfirmedAnnouncement('paid_ready', 200, 206))
                now += 4*86400
                self.assertEqual(store.rebind_unconfirmed(good.destination_key), ())
            finally:
                store.close()


class WebhookLifecycleTests(unittest.TestCase):
    def test_main_finally_stops_webhook_after_mainloop_failure(self):
        from unittest.mock import MagicMock
        from smart_tapo import desktop
        with tempfile.TemporaryDirectory() as directory:
            app = MagicMock()
            root = MagicMock()
            root.mainloop.side_effect = RuntimeError('synthetic')
            with patch.object(desktop.tk, 'Tk', return_value=root), patch.object(
                    desktop, 'DesktopApp', return_value=app), patch.object(
                    desktop.messagebox, 'showerror'), patch('sys.argv', ['desktop', '--config', str(Path(directory)/'config.json')]):
                self.assertEqual(desktop.main(), 1)
            app.webhook_panel.close.assert_called_once()


    def test_smoke_finally_stops_webhook_on_exception(self):
        from unittest.mock import MagicMock
        from smart_tapo import desktop
        app, root = MagicMock(), MagicMock()
        app._active.return_value = False
        app.controller._roles = {}
        with patch.object(desktop.tk, 'Tk', return_value=root), patch.object(
                desktop, 'DesktopApp', return_value=app), patch.object(
                desktop, '_smoke_gui_flow', side_effect=RuntimeError('synthetic')), patch(
                'smart_tapo.smoke_window.install', return_value={}):
            with self.assertRaises(RuntimeError):
                desktop.smoke_gui(Path('unused-report'))
        app.webhook_panel.close.assert_called_once()

    def test_api_save_preserves_current_capture_fields(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from smart_tapo.desktop import DesktopApp
        current = Config(camera_id='current-camera', capture_interval_seconds=.5)
        holder = SimpleNamespace(config=current, store=MagicMock())
        draft = Config(refueling_api_url='https://example.invalid/api', refueling_api_enabled=True)
        DesktopApp._save_webhook_config(holder, draft)
        self.assertEqual(holder.config.camera_id, 'current-camera')
        self.assertEqual(holder.config.capture_interval_seconds, .5)
        self.assertTrue(holder.config.refueling_api_enabled)


class WebhookPanelTests(unittest.TestCase):
    def setUp(self):
        from smart_tapo.smoke_window import install
        install()  # Install BEFORE constructing any native window; never focus/show normally.
        import tkinter as tk
        from smart_tapo.desktop_webhook import WebhookPanel
        self.root = tk.Tk()
        self.root.geometry('900x500')
        self.root.deiconify()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = DesktopStore(Path(self.temp.name)/'config.json')
        self.config = Config(data_dir=Path(self.temp.name)/'data', refueling_api_url='https://example.invalid/api', refueling_api_enabled=True)
        self.panel = WebhookPanel(self.root, lambda:self.config, self.save_config, self.storage)
        self.panel.pack()
        self.storage.save_refueling_key('synthetic-key-only', self.config.refueling_api_url, self.config.camera_id)

    def save_config(self, config):
        self.storage.save(config)
        self.config = config

    def tearDown(self):
        self.panel.close()
        if self.panel.sender.future:
            self.panel.sender.future.result(timeout=5)
            self.panel.sender.poll()
        self.root.destroy()
        self.temp.cleanup()

    def arm(self):
        self.panel.consent.set(True)
        self.panel.start()

    def test_repeated_start_does_not_report_inactive_while_sender_remains_open(self):
        self.arm()
        self.panel.sender.next_attempt=float('inf')
        self.assertIsNotNone(self.panel.sender.scope)
        self.assertFalse(self.panel.consent.get())
        self.panel.start()
        self.assertIn('เปิดอยู่',self.panel.label.cget('text'))
        self.panel.poll()
        self.assertIn('เปิดอยู่',self.panel.label.cget('text'))
        self.assertIsNotNone(self.panel.sender.scope)

    def test_idle_poll_preserves_action_feedback(self):
        self.panel.start()  # Missing explicit queue consent, actual Tk label.
        message = self.panel.label.cget('text')
        self.assertIn('ยังไม่เริ่ม', message)
        self.panel.poll()
        self.assertEqual(self.panel.label.cget('text'), message)
        for action in ('save_ok', 'save_error', 'recover_error'):
            with self.subTest(action=action):
                if action == 'save_ok':
                    self.assertTrue(self.panel.save())
                elif action == 'save_error':
                    with patch.object(self.panel, 'save_config', side_effect=OSError('synthetic')):
                        self.assertFalse(self.panel.save())
                else:
                    self.panel.key.set('synthetic-unsaved')
                    self.panel.recover()
                message = self.panel.label.cget('text')
                for _ in range(3):
                    self.panel.poll()
                    self.assertEqual(self.panel.label.cget('text'), message)

    def test_save_after_stopped_scope_change_preserves_feedback(self):
        self.arm()
        self.panel.url.set('https://changed.invalid/api')
        self.assertTrue(self.panel.save())
        message = self.panel.label.cget('text')
        self.panel.poll()
        self.assertEqual(self.panel.label.cget('text'), message)
        self.assertIsNone(self.panel.sender.scope)
        self.assertFalse(self.panel.consent.get())

    def test_start_explains_unsaved_inputs_without_loading_key(self):
        from dataclasses import replace
        cases = (
            ('permission', 'สิทธิ์การส่งยังไม่ได้บันทึก'),
            ('consent', 'ยืนยันผูกคิว'),
            ('url', 'URL ที่แก้ไขยังไม่ได้บันทึก'),
            ('key', 'คีย์ที่กรอกยังไม่ได้บันทึก'),
        )
        original = self.config
        for kind, expected in cases:
            with self.subTest(kind=kind):
                self.config = replace(original, refueling_api_enabled=kind != 'permission')
                self.panel.url.set(original.refueling_api_url + ('/changed' if kind == 'url' else ''))
                self.panel.key.set('synthetic-unsaved' if kind == 'key' else '')
                self.panel.consent.set(kind != 'consent')
                with patch.object(self.storage, 'load_refueling_key') as load:
                    self.panel.start()
                    load.assert_not_called()
                self.panel.poll()
                self.assertIn(expected, self.panel.label.cget('text'))
                self.assertIsNone(self.panel.sender.scope)

    def test_key_load_failure_is_sticky_and_does_not_leak_exception(self):
        self.panel.consent.set(True)
        with patch.object(self.storage, 'load_refueling_key', side_effect=OSError('synthetic-secret-URL')):
            self.panel.start()
        self.panel.poll()
        message = self.panel.label.cget('text')
        self.assertIn('อ่านคีย์ไม่ได้', message)
        self.assertNotIn('synthetic-secret', message)
        self.assertIsNone(self.panel.sender.scope)

    def test_sender_progress_replaces_action_feedback(self):
        from concurrent.futures import Future
        self.panel.start()
        future = Future()
        future.set_result(DeliveryResult('failed', 'receiver_auth_rejected'))
        self.panel.sender.future = future
        self.panel.poll()
        self.assertIn('ปฏิเสธสิทธิ์', self.panel.label.cget('text'))
        self.assertIsNone(self.panel.sender.scope)

    def test_saved_key_rejects_camera_and_path_change_and_accepts_trailing_slash(self):
        self.assertEqual(self.storage.load_refueling_key(self.config.refueling_api_url+'/', self.config.camera_id), 'synthetic-key-only')
        for url, camera in ((self.config.refueling_api_url, 'other-camera'),
                            ('https://example.invalid/other-path', self.config.camera_id)):
            with self.assertRaises(DesktopStorageError):
                self.storage.load_refueling_key(url, camera)

    def test_actual_desktop_registers_api_tab_without_starting_sender(self):
        from smart_tapo.desktop import DesktopApp
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            app = DesktopApp(self.root, DesktopStore(Path(directory)/'config.json'), controller=MagicMock())
            try:
                self.assertIn('Refueling API v1', [app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()])
                self.assertFalse(app.webhook_panel.is_running)
                self.assertEqual(app.webhook_panel.key.get(), '')
            finally:
                self.root.after_cancel(app.timer)
                app.webhook_panel.close()
                app.history_panel.close()
                app.refueling_panel.close()
                app.audio_panel.close()

    def test_url_change_cannot_reuse_saved_secret(self):
        self.panel.url.set('https://different.invalid/api')
        self.assertTrue(self.panel.save())
        self.arm()
        self.assertIsNone(self.panel.sender.scope)

    def test_config_save_failure_never_pairs_new_key_with_old_url(self):
        self.panel.url.set('https://different.invalid/api')
        self.panel.key.set('synthetic-other-key')
        with patch.object(self.panel, 'save_config', side_effect=OSError('synthetic')):
            self.assertFalse(self.panel.save())
        self.panel.url.set(self.config.refueling_api_url)
        self.panel.key.set('')
        self.arm()
        scope = self.panel.sender.scope
        self.assertTrue(scope is None or scope[1].token != 'synthetic-other-key')

    def test_recovery_preview_cancel_and_explicit_confirm_never_autosends(self):
        from smart_tapo.refueling_store import RefuelingStore
        from smart_tapo.refueling import ConfirmedAnnouncement
        directory = self.config.data_dir/'refueling'
        old = WebhookTarget(self.config.refueling_api_url, 'synthetic-wrong-key', self.config.camera_id)
        with_store = RefuelingStore(directory, self.config.camera_id)
        import time
        now = time.time()
        with_store.accept('ready', ConfirmedAnnouncement('paid_ready', now-6, now))
        with_store.pending_jobs(old.destination_key)
        with_store.close()
        with patch('smart_tapo.desktop_webhook.messagebox.askyesno', return_value=False) as confirm:
            self.panel.recover()
            self.panel.recovery.result(timeout=5)
            self.panel.poll()
            self.assertIn('1 events', confirm.call_args.args[1])
            self.assertIn(self.config.camera_id, confirm.call_args.args[1])
            self.assertIn(self.config.refueling_api_url, confirm.call_args.args[1])
        check = RefuelingStore(directory, self.config.camera_id)
        self.assertEqual(check._db.execute('SELECT destination_key FROM jobs').fetchone()[0], old.destination_key)
        check.close()
        with patch('smart_tapo.desktop_webhook.messagebox.askyesno', return_value=True):
            self.panel.recover()
            self.panel.recovery.result(timeout=5)
            self.panel.poll()
            self.panel.recovery.result(timeout=5)
            self.panel.poll()
        self.assertIsNone(self.panel.sender.scope)
        self.assertFalse(self.panel.sender.busy)
        self.assertIn('ย้ายคิว', self.panel.label.cget('text'))
        current = WebhookTarget(self.config.refueling_api_url, 'synthetic-key-only', self.config.camera_id)
        check = RefuelingStore(directory, self.config.camera_id)
        self.assertEqual(check._db.execute('SELECT destination_key FROM jobs').fetchone()[0], current.destination_key)
        check.close()

    def test_live_disable_stops_and_prevents_start(self):
        self.arm()
        self.assertIsNotNone(self.panel.sender.scope)
        self.panel.enabled.set(False)
        self.assertIsNone(self.panel.sender.scope)
        self.arm()
        self.assertIsNone(self.panel.sender.scope)

    def test_tab_mask_save_scope_and_native_dpapi(self):
        from tkinter import ttk
        entries = [child for child in self.panel.winfo_children() if isinstance(child, ttk.Entry)]
        self.assertEqual(str(entries[1].cget('show')), '*')
        self.assertFalse(self.panel.sender.busy)
        self.assertEqual(self.storage.load_refueling_key(self.config.refueling_api_url, self.config.camera_id), 'synthetic-key-only')
        self.assertNotIn(b'synthetic-key-only', (self.storage.root/'refueling-api.dat').read_bytes())
        self.panel.key.set('synthetic-replacement-key')
        self.assertTrue(self.panel.save())
        self.assertEqual(self.panel.key.get(), '')
        self.assertEqual(self.storage.load_refueling_key(self.config.refueling_api_url, self.config.camera_id), 'synthetic-replacement-key')
        self.assertNotIn('synthetic-replacement-key', self.storage.config_path.read_text(encoding='utf-8'))
        self.arm()
        from dataclasses import replace
        self.config = replace(self.config, camera_id='other-camera')
        self.panel.poll()
        self.assertIsNone(self.panel.sender.scope)

    def test_close_inflight_and_auth_rejection(self):
        entered, release = threading.Event(), threading.Event()
        def attempt(*args):
            entered.set()
            release.wait(3)
            return DeliveryResult('failed', 'receiver_auth_rejected')
        with patch('smart_tapo.desktop_webhook._attempt', side_effect=attempt):
            self.arm()
            self.panel.poll()
            self.assertTrue(entered.wait(1))
            self.panel.stop()
            self.assertIn('กำลังหยุด', self.panel.label.cget('text'))
            self.panel.close()
            self.assertTrue(self.panel.sender.stop_event.is_set())
            release.set()
            self.panel.sender.future.result(timeout=3)
            self.panel.poll()
            self.assertFalse(self.panel.sender.busy)
            self.assertIsNone(self.panel.sender.scope)
            self.assertIn('ปฏิเสธสิทธิ์', self.panel.label.cget('text'))


if __name__ == '__main__':
    unittest.main()
