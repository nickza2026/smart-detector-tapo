"""RTSP desktop migration and calibration; no network/audio/ADB calls."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from smart_tapo.config import Config, Rect
from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_rtsp import FULL_HD_ROI, reference_config
from smart_tapo.desktop_calibration import is_ready
from smart_tapo.desktop_storage import DesktopStore
from tests.test_desktop_gui import FakeController
from tests.test_alignment_regression import textured_reference


class ReferenceRoiTests(unittest.TestCase):
    def test_new_full_hd_source_starts_on_drop_path_and_needs_confirmation(self):
        config=replace(Config(),capture_backend='rtsp',expected_size=(720,1280))
        adopted=reference_config(config,Image.new('RGB',(1920,1080)))
        self.assertEqual(adopted.roi,FULL_HD_ROI)
        self.assertEqual(adopted.rtsp_source_binding,'')

    def test_other_geometry_keeps_centered_default(self):
        config=replace(Config(),capture_backend='rtsp',expected_size=(720,1280))
        adopted=reference_config(config,Image.new('RGB',(1280,720)))
        self.assertEqual(adopted.roi,Rect(590,310,100,100))


class DesktopRtspTests(unittest.TestCase):
    def test_invalid_form_during_snapshot_completion_is_safe_and_discards_image(self):
        expected=self.app.config
        self.app.form['rtsp_url'].set('not-a-url-SECRET')
        try:self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),expected)
        except Exception:self.fail('RTSP completion raised instead of discarding changed invalid form')
        self.assertIsNone(self.app.reference_image)
        self.assertNotIn('SECRET',self.app.status.get())

    def test_invalid_form_during_snapshot_completion_is_safe_and_discards_image(self):
        expected=self.app.config
        self.app.form['rtsp_url'].set('not-a-url-SECRET')
        try:self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),expected)
        except Exception:self.fail('RTSP completion raised instead of discarding changed invalid form')
        self.assertIsNone(self.app.reference_image)
        self.assertNotIn('SECRET',self.app.status.get())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.withdraw()
        self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.legacy=self.store.load_or_create()
        self.legacy.baseline_path.parent.mkdir(parents=True,exist_ok=True)
        textured_reference(self.legacy).save(self.legacy.baseline_path)
        self.before_config=self.store.config_path.read_bytes()
        self.before_image=self.legacy.baseline_path.read_bytes()
        self.controller=FakeController()
        self.errors=patch('smart_tapo.desktop.messagebox.showerror').start()
        self.addCleanup(patch.stopall)
        self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.close)

    def close(self):
        self.app.startup_panel.close();self.app.audio_panel.close()
        self.app.offline_audio_panel.close();self.app.webhook_panel.close()
        self.app.history_panel.close();self.app.refueling_panel.close();self.app.notify_test_panel.close()
        self.root.after_cancel(self.app.timer);self.root.destroy()

    def test_migration_does_not_overwrite_files_or_start_sources(self):
        self.assertEqual(self.app.config.capture_backend,'rtsp')
        self.assertEqual(self.app.form['rtsp_url'].get(),'rtsp://127.0.0.1:8554/tapo_c500')
        self.assertEqual(self.store.config_path.read_bytes(),self.before_config)
        self.assertEqual(self.legacy.baseline_path.read_bytes(),self.before_image)
        self.assertNotEqual(self.app.config.baseline_path,self.legacy.baseline_path)
        self.assertIsNone(self.app.reference_image)
        self.assertFalse(is_ready(self.app.config,self.store.root))
        self.assertFalse(self.controller.active)

    def test_image_and_audio_controls_are_rtsp_without_adb_settings(self):
        from smart_tapo.desktop_rtsp_audio import RtspAudioPanel
        self.assertIn('RTSP',self.app.capture_ref_button.cget('text'))
        self.assertIsInstance(self.app.audio_panel, RtspAudioPanel)
        self.assertFalse(hasattr(self.app, 'audio_connection_settings'))
        self.assertFalse(hasattr(self.app, 'device_picker'))
        for key in ('adb_serial', 'adb_path', 'memu_executable', 'camera_name'):
            self.assertNotIn(key, self.app.form)
        self.assertIn('RTSP', self.app.audio_panel.listener_panel.start_button.cget('text'))
        def children(widget):
            for child in widget.winfo_children():
                yield child
                yield from children(child)
        labels=[]
        for w in children(self.app.control):
            if 'text' in w.keys():labels.append(w.cget('text'))
        self.assertFalse(any('ADB' in value or 'MEmu' in value for value in labels))

    def test_snapshot_adopts_rtsp_dimensions_and_preserves_legacy_reference(self):
        self.assertTrue(self.app.save_settings())
        expected=self.app.config
        self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),expected)
        self.assertEqual(self.app.config.expected_size,(640,360))
        self.assertEqual(self.app.config.video,Rect(0,0,640,360))
        self.assertEqual(self.app.reference_image.size,(640,360))
        self.assertEqual(self.legacy.baseline_path.read_bytes(),self.before_image)
        backup=self.store.config_path.with_name('config.pre-rtsp-v30.json')
        self.assertEqual(backup.read_bytes(),self.before_config)
        self.assertFalse(is_ready(self.app.config,self.store.root))

    def test_url_edited_after_snapshot_cannot_be_confirmed_as_new_source(self):
        self.assertTrue(self.app.save_settings())
        self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),self.app.config)
        self.app.form['rtsp_url'].set('rtsp://127.0.0.1:8554/different')
        self.app.confirm_empty.set(True)
        self.assertFalse(self.app.save_reference())
        self.assertFalse(self.app.config.baseline_path.exists())
        self.assertIn('RTSP',self.app.status.get())

    def test_old_async_snapshot_is_discarded_after_form_source_change(self):
        expected=self.app.config
        self.app.form['rtsp_url'].set('rtsp://127.0.0.1:8554/different')
        self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),expected)
        self.assertIsNone(self.app.reference_image)

    def test_calibration_binds_video_source_not_audio_adb_settings(self):
        self.assertTrue(self.app.save_settings())
        self.app._rtsp_reference_loaded(Image.new('RGB',(640,360)),self.app.config)
        self.app.config=replace(self.app.config,alignment_enabled=False)
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())
        self.assertTrue(is_ready(self.app.config,self.store.root))
        config=replace(self.app.config,adb_serial='audio-only-device',camera_name='Audio name only')
        self.assertTrue(is_ready(config,self.store.root))
        self.assertFalse(is_ready(replace(config,rtsp_url='rtsp://127.0.0.1:8554/other'),self.store.root))

