import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from smart_tapo.config import Config
from smart_tapo.desktop import DesktopApp


class SaveBudgetTests(unittest.TestCase):
    def test_unchanged_root_save_can_proceed_while_passive_history_is_loading(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            app=self.make_app(Path(tmp))
            app.history_panel=SimpleNamespace(pending=True)
            app.refueling_panel=SimpleNamespace(is_running=True)
            self.assertTrue(app.save_settings())
            app.store.save.assert_called_once()

    def test_close_cancels_pending_storage_estimate(self):
        app=DesktopApp.__new__(DesktopApp)
        app.closing=False;app.busy=False;app._active=Mock(return_value=False)
        app.root=Mock();app._budget_timer='budget-pending'
        for name in ('updates_panel','startup_panel','webhook_panel','history_panel','refueling_panel',
                     'audio_panel','offline_audio_panel','notify_test_panel','operation_stop','controller'):
            setattr(app,name,Mock())
        app.audio_panel.is_running=False
        app.close()
        app.root.after_cancel.assert_called_with('budget-pending')

    def make_app(self, root):
        app = DesktopApp.__new__(DesktopApp)
        app.config = replace(Config(), data_dir=root/'data', baseline_path=root/'reference.png',
                             capture_backend='rtsp', capture_interval_seconds=.2,
                             max_image_storage_mb=512)
        app.config.baseline_path.write_bytes(b'x' * 1_760_000)
        from types import SimpleNamespace
        app.controller=SimpleNamespace(video_storage_mode='legacy_spool')
        app._active = Mock(return_value=False)
        app._audio_active = Mock(return_value=False)
        app.busy = False
        app._candidate = Mock(side_effect=lambda: app.config)
        app.notify_key = Mock(); app.notify_key.get.return_value = ''
        app.store = Mock(); app.status = Mock(); app._error = Mock()
        app._refresh_calibration = Mock()
        return app

    def test_normal_save_raises_insufficient_quota_from_actual_file_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            original = app.config
            self.assertTrue(app.save_settings())
            expected = math.ceil((math.ceil(330/.2)+1)*1_760_000*1.25/1_000_000)
            self.assertGreaterEqual(app.config.max_image_storage_mb, expected)
            self.assertEqual(replace(app.config, max_image_storage_mb=512), original)
            self.assertEqual(app._candidate(), app.config)

    def test_candidate_after_save_stays_equal_without_metadata_io_even_when_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            del app._candidate
            app.form = {}
            for key in ('camera_id','rtsp_url','stuck_consecutive_frames','deposit_history_hours','capture_interval_seconds'):
                var = Mock(); var.get.return_value = str(getattr(app.config,key))
                app.form[key] = var
            app.notify = Mock(); app.notify.get.return_value = app.config.notify_enabled
            self.assertTrue(app.save_settings())
            app._active.return_value = True
            app._audio_active.return_value = True
            with patch('smart_tapo.storage_budget.estimate_storage', side_effect=AssertionError('unexpected scan')), \
                 patch('os.scandir', side_effect=AssertionError('unexpected directory IO')), \
                 patch.object(Path,'stat', side_effect=AssertionError('unexpected metadata IO')):
                for _ in range(20): self.assertEqual(app._candidate(), app.config)

    def test_insufficient_disk_does_not_save_config_or_credentials(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            before = app.config
            with patch('smart_tapo.storage_budget.shutil.disk_usage',return_value=SimpleNamespace(free=100)):
                self.assertFalse(app.save_settings())
            self.assertEqual(app.config,before)
            app.store.save.assert_not_called()
            app.store.save_notify_key.assert_not_called()
            self.assertIn('พื้นที่ดิสก์ไม่พอ',app._error.call_args.args[0])

    def test_save_is_blocked_before_estimation_while_active_busy_or_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            for guard in ('_active', '_audio_active', 'busy'):
                if guard == 'busy': app.busy = True
                else: getattr(app, guard).return_value = True
                self.assertFalse(app.save_settings())
                app.store.save.assert_not_called()
                if guard == 'busy': app.busy = False
                else: getattr(app, guard).return_value = False
