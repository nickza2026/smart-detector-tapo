from __future__ import annotations

import tempfile
import subprocess
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from smart_tapo.desktop import DesktopApp, main
from smart_tapo.desktop_calibration import is_ready
from smart_tapo.desktop_storage import DesktopStore
from tests.test_alignment_regression import textured_reference


class FakeController:
    def __init__(self):
        self.active=False
        self.worker_calls=0
        self.stops=0
    def is_running(self,role):return self.active
    def snapshot(self):return {r:{'state':'running' if self.active else 'stopped','pid':None} for r in ('capture','worker')}
    def drain_messages(self):return []
    def start_worker(self,*args):self.worker_calls+=1
    def stop_all(self):self.stops+=1;self.active=False


class DesktopGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.controller=FakeController()
        self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.root.after_cancel,self.app.timer)
        self.errors=self.enterContext(patch('smart_tapo.desktop.messagebox.showerror'))
        self.confirm=self.enterContext(patch('smart_tapo.desktop.messagebox.askyesno',return_value=True))

    def calibrate(self):
        self.app.load_image(textured_reference(self.app.config))
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())

    def test_auto_config_and_missing_reference_are_guided_without_cli(self):
        self.assertTrue(self.store.config_path.exists())
        self.assertFalse(self.app.config.notify_enabled)
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.assertEqual(self.app.notebook.select(),str(self.app.calibration))
        self.errors.assert_called_once()

    def test_empty_confirmation_is_required_then_camera_change_invalidates_it(self):
        self.app.load_image(textured_reference(self.app.config))
        self.assertFalse(self.app.save_reference())
        self.assertFalse(self.app.config.baseline_path.exists())
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())
        self.assertTrue(is_ready(self.app.config,self.store.root))
        self.app.form['camera_name'].set('changed-camera')
        self.assertTrue(self.app.save_settings())
        self.assertFalse(is_ready(self.app.config,self.store.root))

    def test_partial_reference_save_cannot_silently_start_worker(self):
        self.calibrate()
        image=textured_reference(self.app.config)
        image.putpixel((350,280),(255,255,255))
        self.app.load_image(image)
        self.app.confirm_empty.set(True)
        with patch('smart_tapo.desktop.mark_ready',side_effect=OSError('simulated-full-disk')):
            self.assertFalse(self.app.save_reference())
        self.assertFalse(is_ready(self.app.config,self.store.root))
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)

    def test_featureless_reference_is_rejected_in_setup_not_silently_ready(self):
        from PIL import Image
        self.app.load_image(Image.new('RGB',self.app.config.expected_size,'gray'))
        self.app.confirm_empty.set(True)
        self.assertFalse(self.app.save_reference())
        self.assertFalse(self.app.config.baseline_path.exists())
        self.assertFalse(is_ready(self.app.config,self.store.root))

    def test_numeric_input_error_does_not_echo_arbitrary_pasted_text(self):
        self.app.form['stuck_consecutive_frames'].set('synthetic-private-input')
        self.assertFalse(self.app.save_settings())
        self.assertNotIn('synthetic-private-input',self.app.status.get())

    def test_credentials_are_not_added_to_plain_config(self):
        self.app.token.set('synthetic-private-token');self.app.user.set('synthetic-private-user')
        with patch.object(self.store,'save_credentials') as saved:
            self.assertTrue(self.app.save_settings())
            saved.assert_called_once_with('synthetic-private-token','synthetic-private-user')
        self.assertNotIn('synthetic-private',self.store.config_path.read_text(encoding='utf-8'))

    def test_selection_stays_in_video_and_requires_confirmation_again(self):
        self.calibrate()
        self.app._drag_begin(SimpleNamespace(x=20,y=20))
        self.app._drag_end(SimpleNamespace(x=3000,y=3000))
        roi=self.app.selection;video=self.app.config.video
        self.assertGreaterEqual(roi.x,video.x)
        self.assertLessEqual(roi.x+roi.width,video.x+video.width)
        self.assertLessEqual(roi.y+roi.height,video.y+video.height)
        self.assertFalse(self.app.confirm_empty.get())

    def test_close_requests_graceful_stop_and_cancels_camera_setup(self):
        self.controller.active=True
        self.app.close()
        self.assertEqual(self.controller.stops,1)
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.operation_stop.is_set())

    def test_device_discovery_hides_console_and_still_lists_devices(self):
        result=subprocess.CompletedProcess([],0,b'List of devices attached\nemulator-5554\tdevice\n',b'')
        with patch('smart_tapo.desktop.hidden_console_options',return_value={'creationflags':0x08000000}), \
             patch('smart_tapo.desktop.subprocess.run',return_value=result) as runner, \
             patch.object(self.app,'_async',side_effect=lambda work,done:done(work())):
            self.app.find_devices()
        runner.assert_called_once_with([self.app.config.adb_path,'devices'],capture_output=True,
                                       timeout=15,check=True,creationflags=0x08000000)
        self.assertEqual(self.app.form['adb_serial'].get(),'emulator-5554')

    def test_unexpected_window_failure_still_stops_managed_jobs(self):
        fake_root=Mock()
        fake_root.mainloop.side_effect=RuntimeError('synthetic-window-failure')
        self.controller.active=True
        with patch('sys.argv',['SmartTapo']), patch('smart_tapo.desktop.tk.Tk',return_value=fake_root), \
             patch('smart_tapo.desktop.DesktopStore',return_value=self.store), \
             patch('smart_tapo.desktop.DesktopApp',return_value=self.app):
            self.assertEqual(main(),1)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.stops,1)


if __name__=='__main__':unittest.main()
