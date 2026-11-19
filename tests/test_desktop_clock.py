import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image

from smart_tapo.config import Config,Rect
from smart_tapo.desktop_clock import ClockRegionEditor,display_region
from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.desktop_calibration import is_ready
from tests.test_desktop_gui import FakeController
from tests.test_alignment_regression import textured_reference


class ClockRegionTests(unittest.TestCase):
    def test_coordinate_mapping_uses_actual_rendered_size_and_clamps_both_axes(self):
        self.assertEqual(display_region((25,30),(75,40),(720,1280),(180,320)),Rect(100,120,200,40))
        self.assertEqual(display_region((190,400),(-5,-2),(720,1280),(180,320)),Rect(0,0,720,1280))
        with self.assertRaises(ValueError):display_region((1,1),(1,1),(720,1280),(180,320))


class ClockGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.controller=FakeController();self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.root.after_cancel,self.app.timer)
        self.errors=self.enterContext(patch('smart_tapo.desktop.messagebox.showerror'))

    def calibrate(self):
        self.app.load_image(textured_reference(self.app.config));self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())

    def test_clock_save_changes_only_clock_without_baseline_or_bank_reconfirmation(self):
        self.calibrate();original=self.app.config
        before=original.baseline_path.read_bytes()
        region=Rect(0,174,280,21)  # Synthetic requested region, not a VPS preset.
        self.assertTrue(self.app._save_clock_roi(original,region))
        self.assertEqual(self.store.load_or_create(),replace(original,clock_roi=region))
        self.assertEqual(original.baseline_path.read_bytes(),before)
        self.assertTrue(is_ready(self.app.config,self.store.root))
        self.assertFalse(self.app._save_clock_roi(original,Rect(1,174,280,21)))

    def test_save_rejects_running_changed_device_unsaved_form_and_invalid_region(self):
        original=self.app.config;before=self.store.config_path.read_bytes()
        self.controller.active=True
        self.assertFalse(self.app._save_clock_roi(original,Rect(0,174,280,21)))
        self.controller.active=False
        self.app.form['rtsp_url'].set('rtsp://127.0.0.1:8554/unsaved-other-camera')
        self.assertFalse(self.app._save_clock_roi(original,Rect(0,174,280,21)))
        self.app.form['rtsp_url'].set(original.rtsp_url)
        self.assertFalse(self.app._save_clock_roi(original,Rect(0,1275,280,21)))
        self.assertEqual(self.store.config_path.read_bytes(),before)

    def test_recorded_roi_preset_is_unconfirmed_preview_and_blocks_worker(self):
        self.calibrate();before=self.store.config_path.read_bytes();baseline=self.app.config.baseline_path.read_bytes()
        self.assertTrue(self.app.use_recorded_roi())
        self.assertEqual(self.app.selection,Rect(249,272,100,80))
        self.assertFalse(self.app.confirm_empty.get())
        self.assertEqual(self.store.config_path.read_bytes(),before)
        self.assertEqual(self.app.config.baseline_path.read_bytes(),baseline)
        self.app.start_role('worker');self.assertEqual(self.controller.worker_calls,0)

    def test_editor_never_saves_on_open_or_drag_and_clears_confirmation_on_redrag(self):
        callback=Mock(return_value=True)
        editor=ClockRegionEditor(self.root,Image.new('RGB',self.app.config.expected_size),self.app.config,callback)
        editor.pack();self.root.update_idletasks()
        callback.assert_not_called();self.assertFalse(editor.save())
        editor._begin(SimpleNamespace(x=0,y=0));editor._move(SimpleNamespace(x=50,y=10))
        callback.assert_not_called();self.assertFalse(editor.save())
        editor.confirm.set(True);self.assertTrue(editor.save());callback.assert_called_once()
        editor._move(SimpleNamespace(x=60,y=20));self.assertFalse(editor.confirm.get())
