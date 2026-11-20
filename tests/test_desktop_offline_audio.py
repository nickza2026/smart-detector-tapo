from concurrent.futures import Future
from pathlib import Path
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

from smart_tapo.desktop_offline_audio import OfflineAudioPanel
from smart_tapo.offline_audio import analyze_pcm
from smart_tapo.smoke_window import install
from tests.test_announcement_stream import profile


class OfflinePanelTests(unittest.TestCase):
    def test_long_file_progress_and_cancel_do_not_restore_late_result(self):
        self.completed_job()
        report=self.panel.future.result()
        pending=Future()
        self.panel.future=pending
        self.panel._progress_state=dict(processed_frames=80000,total_frames=800000,rate=8000)
        self.panel.poll()
        self.assertIn('10.0 / 100.0',self.panel.status.get())
        self.panel.stop()
        self.panel._progress_state=dict(processed_frames=160000,total_frames=800000,rate=8000)
        self.panel.poll()
        self.assertIn('ยกเลิก',self.panel.status.get())
        pending.set_result(report)
        self.panel.poll()
        self.assertIsNone(self.panel.report)
        self.assertEqual(self.panel.output.get('1.0','end').strip(),'')

    def setUp(self):
        install()  # Before Tk: real windows never focus or overlap the user.
        self.root=tk.Tk()
        self.tmp=tempfile.TemporaryDirectory()
        self.config=SimpleNamespace(camera_id='cam1',data_dir=Path(self.tmp.name),windows_startup_binding='')
        self.listener=SimpleNamespace(profile_path=tk.StringVar(master=self.root,value='selected.json'),prepared_manifest=Path('missing.json'))
        self.panel=OfflineAudioPanel(self.root,lambda:self.config,self.listener)
        self.panel.pack()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.cleanup_panel)

    def cleanup_panel(self):
        self.panel.close()
        if self.panel.future is not None:
            self.panel.future.result(timeout=10)
            self.panel.poll()

    def completed_job(self):
        self.panel.selection.set('synthetic.wav')
        self.panel.snapshot=self.panel._snapshot()
        self.panel.stop_event.clear()
        self.panel.future=Future()
        self.panel.executor=SimpleNamespace(shutdown=lambda **kwargs:None)
        self.panel.future.set_result(analyze_pcm(np.zeros((8000,1)),8000,profile()))

    def test_no_job_on_open_and_selected_profile_reused(self):
        self.assertFalse(self.panel.is_running)
        self.assertEqual(self.panel._profile(self.config),'selected.json')
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])
        self.listener.profile_path.set('')
        self.config.windows_startup_binding='{"camera_id":"cam1","profile":"saved.json"}'
        self.assertEqual(self.panel._profile(self.config),'saved.json')

    def test_completed_result_then_selection_clears_report(self):
        self.completed_job();self.panel.poll()
        self.assertIsNotNone(self.panel.report)
        self.panel.selection.set('other.wav')
        self.assertIsNone(self.panel.report)
        self.assertEqual(self.panel.output.get('1.0','end').strip(),'')

    def test_cancel_and_profile_change_discard_late_results(self):
        for action in (self.panel.stop,lambda:self.listener.profile_path.set('changed.json'),self.panel.close):
            self.panel.closing=False
            self.completed_job()
            action();self.panel.poll()
            self.assertFalse(self.panel.is_running)
            self.assertIsNone(self.panel.report)
            self.assertEqual(self.panel.output.get('1.0','end').strip(),'')

    def test_profile_selection_change_invalidates_completed_report(self):
        self.completed_job();self.panel.poll()
        self.listener.profile_path.set('changed.json');self.panel.poll()
        self.assertIsNone(self.panel.report)

    def test_error_never_keeps_old_success(self):
        self.completed_job();self.panel.poll()
        self.completed_job()
        future=Future();future.set_exception(ValueError('private path must not leak'))
        self.panel.future=future;self.panel.poll()
        self.assertIsNone(self.panel.report)
        self.assertNotIn('private',self.panel.status.get())

    def test_actual_synthetic_file_gui_smoke(self):
        from smart_tapo.desktop_offline_audio import smoke_offline_audio
        self.config.adb_serial='synthetic-device';self.config.notify_enabled=False
        flags=smoke_offline_audio(self.root,self.panel,self.config)
        self.assertTrue(all(flags.values()))
        self.assertEqual(self.listener.profile_path.get(),'selected.json')
        self.assertFalse(self.panel.is_running)

    def test_single_job_snapshot_and_async_worker(self):
        calls=[]
        self.panel.runner=lambda *args:(calls.append(args),analyze_pcm(np.zeros((8000,1)),8000,profile()))[1]
        self.panel.selection.set('synthetic.wav')
        self.panel.start();first=self.panel.future
        self.panel.start();self.assertIs(first,self.panel.future)
        first.result(timeout=10);self.panel.poll()
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1:3],('selected.json','cam1'))
        self.assertIsNotNone(self.panel.report)
