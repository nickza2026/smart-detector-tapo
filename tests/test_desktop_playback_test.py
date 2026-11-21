import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from smart_tapo.desktop_playback_test import PlaybackTestPanel, smoke_playback_test
from tests.test_playback_test import device


class FakeController:
    is_running=False
    def __init__(self): self.starts=0; self.events=[]
    def start(self,*args,**kwargs): self.starts+=1; self.is_running=True
    def stop(self): self.is_running=False
    def close(self): self.stop()
    def poll(self): events,self.events=self.events,[]; return events


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk(); self.root.withdraw()
        self.config=SimpleNamespace(camera_id='synthetic',data_dir='unused',windows_startup_binding='',adb_serial='synthetic-device',notify_enabled=False)
        self.listener=SimpleNamespace(profile_path=tk.StringVar(self.root,value='synthetic-profile'),prepared_manifest='synthetic-manifest')
        self.controller=FakeController(); self.busy=False; self.device=device()
        self.panel=PlaybackTestPanel(self.root,lambda:self.config,lambda:self.device,self.listener,
                                     other_running=lambda:self.busy,controller=self.controller)
    def tearDown(self): self.panel.close(); self.root.destroy()

    def test_no_capture_on_construct_poll_or_decline(self):
        self.panel.poll(); self.assertEqual(self.controller.starts,0)
        with patch('smart_tapo.desktop_playback_test.messagebox.askyesno',return_value=False): self.panel.start()
        self.assertEqual(self.controller.starts,0)
        self.assertIn('unknown/unreceived',self.panel.status.get())

    def test_busy_consent_duplicate_and_selection_change(self):
        with patch('smart_tapo.desktop_playback_test.messagebox.askyesno',return_value=True):
            self.busy=True; self.panel.start(); self.assertEqual(self.controller.starts,0)
            self.busy=False; self.panel.start(); self.panel.start()
            self.assertEqual(self.controller.starts,1)
            self.device=dict(self.device,index=99); self.panel.poll()
            self.assertFalse(self.panel.is_running)
            self.panel.poll(); self.assertEqual(self.controller.starts,1)

    def test_fake_only_smoke(self):
        result=smoke_playback_test(self.root,self.panel,self.config)
        self.assertTrue(all(result.values()))
        self.assertIs(self.panel.controller,self.controller)


if __name__=='__main__': unittest.main()
