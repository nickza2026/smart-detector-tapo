"""No devices, UI windows, camera, endpoint, provider or customer data."""
from dataclasses import replace
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from smart_tapo.config import Config
from smart_tapo.desktop import DesktopApp


class ParallelStartTests(unittest.TestCase):
    def test_recovery_statuses_are_visible_and_auditable_without_arbitrary_text(self):
        from smart_tapo.audit_log import valid_detail
        from smart_tapo.desktop_controller import _detection_hint
        for reason in ('navigation_recovery','navigation_unavailable','clock_epoch_changed'):
            detail=('UNKNOWN',0,10,0,reason)
            self.assertTrue(valid_detail('detection',detail))
            self.assertIsNotNone(_detection_hint(detail))
        self.assertIsNone(_detection_hint(('UNKNOWN',0,10,0,'private-unvalidated-text')))
    def app(self, directory):
        config=replace(Config(),data_dir=Path(directory),adb_serial='synthetic-device')
        return SimpleNamespace(busy=False,config=config,_audio_active=lambda:True,
            store=SimpleNamespace(root=Path(directory)),
            _active=lambda:False,save_settings=Mock(return_value=False),_candidate=lambda:config,
            _calibration_pending=lambda:False,controller=Mock(),notify_key=Mock(get=lambda:''),
            notebook=Mock(),control=object(),calibration=object(),_error=Mock())

    def test_capture_and_worker_can_start_after_audio_without_resaving_live_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(directory)
            with patch('smart_tapo.desktop.is_ready',return_value=True):
                DesktopApp.start_role(app,'capture')
                DesktopApp.start_role(app,'worker')
            app.controller.start_capture.assert_called_once_with(app.config)
            app.controller.start_worker.assert_called_once_with(app.config,'')
            app.save_settings.assert_not_called()
            app._error.assert_not_called()

    def test_unsaved_settings_remain_blocked_during_parallel_run(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(directory)
            app._candidate=lambda:replace(app.config,camera_name='different-camera')
            DesktopApp.start_role(app,'capture')
            app.controller.start_capture.assert_not_called()
            app.save_settings.assert_not_called()
            app._error.assert_called_once()

    def test_stop_all_still_stops_both_roles_and_audio(self):
        app=SimpleNamespace(controller=Mock(),audio_panel=Mock(),webhook_panel=Mock(),
                            startup_panel=Mock(),operation_stop=Mock())
        DesktopApp.stop_all(app)
        app.startup_panel.stop.assert_called_once()
        app.operation_stop.set.assert_called_once()
        app.controller.stop_all.assert_called_once()
        app.audio_panel.stop.assert_called_once()
        app.webhook_panel.stop.assert_called_once()


if __name__=='__main__':unittest.main()
