"""Production RTSP startup: synthetic profiles and owned temporary registration only."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.startup import bind, manage_entry, ENTRY, validate_binding
from tests.test_alignment_regression import textured_reference


class DesktopStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from smart_tapo.smoke_window import install
        install()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk(); self.root.withdraw(); self.addCleanup(self.root.destroy)
        self.store = DesktopStore(Path(self.temp.name)/'config.json')
        self.controller = Mock()
        self.controller.is_running.return_value = False
        self.controller.snapshot.return_value = {role: {'state':'stopped','pid':None} for role in ('capture','worker')}
        self.controller.drain_messages.return_value = []
        self.app = DesktopApp(self.root, self.store, self.controller)
        self.addCleanup(lambda: self.root.after_cancel(self.app.timer))
        self.panel = self.app.startup_panel
        self.profile = Path(self.temp.name)/'profile.json'; self.profile.write_text('{}')
        self.profile_loader = patch('smart_tapo.announcement_profile.load_announcement_profile', return_value=SimpleNamespace(fingerprint='synthetic-fingerprint'))
        self.profile_loader.start(); self.addCleanup(self.profile_loader.stop)
        self.app.audio_panel.listener_panel.profile_path.set(str(self.profile))

    def ready(self, audio=False):
        app = self.app
        app.load_image(textured_reference(app.config)); app.confirm_empty.set(True)
        self.assertTrue(app.save_reference())
        selected = replace(app.config, windows_startup_audio=audio)
        app.config = replace(selected, windows_startup_enabled=True, windows_startup_binding=bind(selected, str(self.profile)))
        return json.loads(app.config.windows_startup_binding)

    def test_construct_is_off_no_registration_or_listening(self):
        self.assertFalse(self.panel.enabled.get()); self.assertFalse(self.panel.audio.get())
        self.assertFalse(self.panel.is_running); self.assertFalse(self.app.audio_panel.is_running)
        self.panel.poll()
        self.controller.start_capture.assert_not_called(); self.controller.start_worker.assert_not_called()
        self.assertFalse(hasattr(self.app.audio_panel, 'probe'))

    def test_missing_calibration_pauses_without_adb_or_listener(self):
        self.app.config = replace(self.app.config, windows_startup_enabled=True, windows_startup_binding=bind(self.app.config))
        with patch('smart_tapo.capture.Adb') as adb, patch.object(self.app.audio_panel.listener_panel.runtime, 'start') as audio:
            self.panel.begin(); adb.assert_not_called(); audio.assert_not_called()
        self.controller.start_capture.assert_not_called()
        self.assertIn('พัก startup', self.panel.status.get())

    def test_source_dev_enable_never_registers_python(self):
        self.panel.enabled.set(True)
        with patch('smart_tapo.desktop_startup.manage_entry') as register, patch('smart_tapo.desktop_startup.sys.frozen', False, create=True):
            self.panel.save(); register.assert_not_called()
        self.assertIn('EXE', self.panel.status.get())

    def test_missing_profile_is_actionable_without_registration(self):
        self.ready(); old = self.app.config
        self.panel.enabled.set(True); self.panel.audio.set(True)
        self.app.audio_panel.listener_panel.profile_path.set(str(self.profile.with_name('missing.json')))
        with patch('smart_tapo.desktop_startup.sys.frozen', True, create=True), patch('smart_tapo.desktop_startup.manage_entry') as registration:
            self.panel.save(); registration.assert_not_called()
        self.assertEqual(self.app.config, old)
        self.assertIn('โปรไฟล์', self.panel.status.get())

    def registration_context(self, answer=True):
        from contextlib import ExitStack
        stack = ExitStack()
        exe = Path(self.temp.name)/'Installed SmartTapo.exe'; exe.touch()
        stack.enter_context(patch('smart_tapo.desktop_startup.sys.frozen', True, create=True))
        stack.enter_context(patch('smart_tapo.desktop_startup.sys.executable', str(exe)))
        stack.enter_context(patch('smart_tapo.desktop_startup.messagebox.askyesno', return_value=answer))
        def register(enabled, executable=None, config=None):
            return manage_entry(enabled, executable, config, folder=self.temp.name)
        stack.enter_context(patch('smart_tapo.desktop_startup.manage_entry', side_effect=register))
        return stack

    def test_explicit_enable_binds_once_and_disable_only_owned_temp_entry(self):
        self.ready(); self.panel.enabled.set(True); self.panel.audio.set(True)
        with self.registration_context(), patch('smart_tapo.capture.Adb') as adb:
            self.panel.save(); adb.assert_not_called()
            saved = validate_binding(self.app.config)
            self.assertEqual(saved['schema'], 'rtsp-av-startup-v2')
            self.assertEqual(saved['audio_source'], 'rtsp')
            for key in ('device','android_identity','adb_serial','camera_name'): self.assertNotIn(key, saved)
            self.assertTrue((Path(self.temp.name)/ENTRY).exists())
            self.assertFalse(self.app.audio_panel.is_running)
            self.controller.start_capture.assert_not_called(); self.controller.start_worker.assert_not_called()
            self.panel.enabled.set(False); self.panel.save()
            self.assertFalse((Path(self.temp.name)/ENTRY).exists())
            self.assertFalse(self.app.config.windows_startup_enabled)
            self.assertTrue((Path(self.temp.name)/'Installed SmartTapo.exe').exists())

    def test_foreign_registration_is_not_overwritten_and_config_rolls_back(self):
        self.ready(); old = self.app.config; self.panel.enabled.set(True)
        foreign = Path(self.temp.name)/ENTRY; foreign.write_text('foreign owner', encoding='utf-16')
        with self.registration_context(): self.panel.save()
        self.assertEqual(self.app.config, old)
        self.assertEqual(foreign.read_text(encoding='utf-16'), 'foreign owner')

    def test_cancel_confirmation_does_not_register_or_change_config(self):
        self.ready(); old = self.app.config; self.panel.enabled.set(True)
        with self.registration_context(answer=False): self.panel.save()
        self.assertEqual(self.app.config, old)
        self.assertFalse((Path(self.temp.name)/ENTRY).exists())

    def test_provider_failure_redacted_without_registration(self):
        self.ready(); old = self.app.config; self.panel.enabled.set(True); self.panel.audio.set(True)
        with patch('smart_tapo.desktop_startup.sys.frozen', True, create=True), patch('smart_tapo.desktop_startup.bind', side_effect=ValueError('UserSecrets rtsp://private token')), patch('smart_tapo.desktop_startup.manage_entry') as registration:
            self.panel.save(); registration.assert_not_called()
        self.assertEqual(self.app.config, old)
        for secret in ('UserSecrets','rtsp://private','token'): self.assertNotIn(secret, self.panel.status.get())

    def test_optin_starts_roles_once_without_adb_or_webhook_arm(self):
        self.ready()
        with patch('smart_tapo.capture.Adb') as adb, patch.object(self.app.webhook_panel, 'start') as webhook:
            self.panel.begin(); self.panel.begin(); self.panel.poll(); self.panel.poll()
            adb.assert_not_called(); webhook.assert_not_called()
        self.controller.start_capture.assert_called_once(); self.controller.start_worker.assert_called_once()
        self.assertFalse(self.app.audio_panel.is_running)

    def test_refused_video_role_not_reported_started_audio_independent(self):
        self.ready(audio=True); self.controller.start_capture.return_value = False
        with patch.object(self.app.audio_panel.listener_panel, 'start_bound') as audio:
            self.panel.begin(); audio.assert_called_once()
        self.controller.start_worker.assert_not_called()
        self.assertIn('ไม่สำเร็จ', self.panel.status.get())
        self.controller.stop_all.assert_not_called()

    def test_audio_failure_does_not_kill_video(self):
        self.ready(audio=True)
        with patch.object(self.app.audio_panel.listener_panel, 'start_bound', side_effect=RuntimeError('UserSecrets')):
            self.panel.begin()
        self.controller.start_capture.assert_called_once(); self.controller.start_worker.assert_called_once()
        self.controller.stop_all.assert_not_called()
        self.assertNotIn('UserSecrets', self.app.audio_panel.listener_panel.status.get())

    def test_saved_profile_fingerprint_passed_without_per_boot_consent(self):
        saved = self.ready(audio=True)
        with patch.object(self.app.audio_panel.listener_panel, 'start_bound') as audio:
            self.panel.begin()
            audio.assert_called_once_with(self.app.config, saved['profile'], expected_fingerprint=saved['fingerprint'])
        self.assertFalse(self.app.audio_panel.listener_panel.consent.get())

    def test_changed_source_or_legacy_consent_cannot_open_other_stream(self):
        self.ready(audio=True); original = self.app.config
        for change in ('url','legacy'):
            if change == 'url':
                self.app.config = replace(original, rtsp_url='rtsp://127.0.0.1:8554/changed')
                self.app.form['rtsp_url'].set(self.app.config.rtsp_url)
            else:
                self.app.form['rtsp_url'].set(original.rtsp_url)
                old = json.loads(original.windows_startup_binding); old['schema'] = 'rtsp-startup-v1'; old['device'] = {'endpoint_id':'legacy'}
                self.app.config = replace(original, windows_startup_binding=json.dumps(old))
            with patch.object(self.app.audio_panel.listener_panel, 'start_bound') as audio:
                self.panel.begin(); audio.assert_not_called()
            self.controller.start_capture.assert_not_called()

    def test_profile_modified_missing_or_new_selection_blocks_startup(self):
        self.ready(audio=True)
        for change in ('digest','missing','selection'):
            if change == 'digest': self.profile.write_text('{"changed":true}')
            elif change == 'missing': self.profile.unlink()
            else: self.app.audio_panel.listener_panel.profile_path.set('other.json')
            with patch.object(self.app.audio_panel.listener_panel, 'start_bound') as audio:
                self.panel.begin(); audio.assert_not_called()
            self.controller.start_capture.assert_not_called()

    def test_stop_cancels_every_role_and_poll_has_no_late_start(self):
        self.ready(audio=True)
        with patch.object(self.app.audio_panel, 'stop') as audio, patch.object(self.app.webhook_panel, 'stop') as webhook:
            self.app.stop_all(); audio.assert_called_once(); webhook.assert_called_once()
        self.controller.stop_all.assert_called_once()
        self.panel.poll(); self.panel.poll()
        self.controller.start_capture.assert_not_called(); self.controller.start_worker.assert_not_called()
        self.assertFalse(self.panel.is_running)

    def test_prepopulate_profile_without_starting_or_waiving_legacy_consent(self):
        saved = self.ready(audio=True); saved['schema'] = 'rtsp-startup-v1'
        self.store.save(replace(self.app.config, windows_startup_binding=json.dumps(saved)))
        other_root = tk.Toplevel(self.root); other = DesktopApp(other_root, self.store, self.controller)
        try:
            self.assertEqual(other.audio_panel.listener_panel.profile_path.get(), saved['profile'])
            self.assertFalse(other.audio_panel.is_running); self.assertFalse(other.startup_panel.is_running)
            self.assertFalse(other.audio_panel.listener_panel.consent.get())
            other.startup_panel.begin(); self.controller.start_capture.assert_not_called()
        finally:
            other_root.after_cancel(other.timer); other_root.destroy()
