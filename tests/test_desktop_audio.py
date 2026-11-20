"""Real Tk UI with fake endpoints/PCM only; never records host audio."""
import multiprocessing
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from smart_tapo.audio import AudioProbe
from smart_tapo.config import Config
from smart_tapo.desktop_audio import AudioPanel
from tests.test_audio import DEVICE,FakeAPI,FakeAudio


class FakeProbe:
    def __init__(self):
        self.is_running=False
        self.calls=[]
        self.events=[]
    def start_list(self):self.calls.append(('list',));self.is_running=True
    def start_capture(self,device,directory=None,duration=10):
        self.calls.append(('capture',device,directory,duration));self.is_running=True
    def poll(self):
        events,self.events=self.events,[]
        if any(e['kind'] in ('devices','finished','error') for e in events):self.is_running=False
        return events
    def stop(self):self.calls.append(('stop',));self.is_running=False
    def close(self):self.calls.append(('close',));self.is_running=False


class AudioPanelTests(unittest.TestCase):
    def test_playback_blocks_probe_and_live_and_joins_main_stop_close(self):
        from unittest.mock import Mock
        original=self.panel.playback_panel.controller
        fake=Mock(is_running=True)
        self.panel.playback_panel.controller=fake
        try:
            self.assertTrue(self.panel.is_running)
            self.assertTrue(self.panel.listener_panel.other_running())
            self.panel.start(False)
            self.assertEqual(self.probe.calls,[])
            self.panel.stop()
            fake.stop.assert_called_once()
            self.panel.close()
            fake.close.assert_called_once()
        finally:
            self.panel.playback_panel.controller=original
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=tk.Tk();self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.config=Config(data_dir=Path(self.temporary.name)/'new-data')
        self.probe=FakeProbe()
        self.panel=AudioPanel(self.root,lambda:self.config,self.probe)
        self.panel.pack(fill='both',expand=True)

    def choose(self):
        self.panel.devices=[DEVICE]
        self.panel.device_box.configure(values=('synthetic output',))
        self.panel.device_box.current(0)
        self.panel.consent.set(True)

    def test_initialization_and_tab_visibility_do_not_start_audio_or_create_files(self):
        self.root.update_idletasks()
        self.assertEqual(self.probe.calls,[])
        self.assertFalse(self.config.data_dir.exists())
        self.assertEqual(str(self.panel.record_button['state']),'disabled')

    def test_devices_terminal_event_finishes_search_without_starting_audio(self):
        self.panel.refresh()
        self.probe.events=[{'kind':'devices','devices':[DEVICE]}]
        self.panel.poll()
        self.assertEqual(self.probe.calls,[('list',)])
        self.assertIn('เลือกอุปกรณ์',self.panel.status.get())
        self.assertEqual(self.panel.device_box.current(),-1)

    def test_consent_and_explicit_endpoint_are_required(self):
        self.panel.start(True)
        self.assertEqual(self.probe.calls,[])
        self.choose()
        self.panel.consent.set(False)
        self.panel.start(False)
        self.assertEqual(self.probe.calls,[])

    def test_monitor_has_no_output_directory(self):
        self.choose();self.panel.start(False)
        self.assertEqual(self.probe.calls[-1],('capture',DEVICE,None,10))
        self.assertFalse(self.config.data_dir.exists())

    def test_record_first_use_creates_safe_directory_and_complete_synthetic_wav(self):
        real=multiprocessing.get_context('spawn')
        class Process:
            exitcode=0
            def __init__(self,target,args,daemon):self.target,self.args=target,args
            def start(self):self.target(*self.args,api=FakeAPI(FakeAudio()))
            def is_alive(self):return False
            def join(self,timeout):pass
            def close(self):pass
        class Context:
            Pipe=staticmethod(real.Pipe)
            Queue=staticmethod(real.Queue)
            Event=staticmethod(real.Event)
        Context.Process=Process
        self.panel.probe=AudioProbe(context=Context())
        self.choose();self.panel.start(True)
        self.assertTrue((self.config.data_dir/'audio-samples').is_dir())
        self.panel.poll()
        self.assertIsNotNone(self.panel.last_file)
        self.assertEqual(self.panel.last_file.suffix,'.wav')
        self.assertFalse(list(self.panel.output_directory.glob('*.partial')))

    def test_silence_and_untrusted_driver_detail_are_not_coin_claims(self):
        self.probe.events=[{'kind':'level','rms_dbfs':None,'peak_dbfs':None,'elapsed':1,'clipped':False},
                           {'kind':'error','code':'audio_unavailable','message':'PRIVATE'}]
        self.panel.poll()
        self.assertIn('เงียบ',self.panel.level.get())
        self.assertNotIn('PRIVATE',self.panel.status.get())
        self.assertIn('ไม่ใช่ความมั่นใจ',self.panel.level.get())

    def test_close_and_stop_are_explicit_and_prevent_further_start(self):
        self.choose();self.panel.start(False);self.panel.stop();self.panel.close()
        count=len(self.probe.calls)
        self.panel.start(True);self.panel.refresh()
        self.assertEqual(len(self.probe.calls),count)

    def test_controls_and_privacy_text_fit_actual_minimum_desktop_tab(self):
        from smart_tapo.desktop import DesktopApp
        from smart_tapo.desktop_storage import DesktopStore
        from tests.test_desktop_gui import FakeController
        self.panel.destroy()
        from smart_tapo.desktop_rtsp_audio import RtspAudioPanel
        from tests.test_desktop_rtsp_audio import PureFakeRuntime
        with patch('smart_tapo.desktop.RtspAudioPanel',side_effect=lambda parent,getter,**kwargs:RtspAudioPanel(parent,getter,runtime=PureFakeRuntime(),**kwargs)):
            app=DesktopApp(self.root,DesktopStore(Path(self.temporary.name)/'config.json'),FakeController())
        try:
            app.notebook.select(app.audio_panel)
            self.root.geometry('1000x650')
            self.root.deiconify();self.root.update()
            panel=app.audio_panel
            for widget in panel.winfo_children():
                self.assertTrue(widget.winfo_ismapped(),str(widget))
                self.assertGreaterEqual(widget.winfo_height(),widget.winfo_reqheight())
            listener=panel.listener_panel
            for widget in (listener.path_entry,listener.browse_button,listener.prepared_button,
                           listener.create_button,listener.consent_box,listener.start_button,listener.stop_button):
                self.assertTrue(widget.winfo_ismapped(),str(widget))
                self.assertGreaterEqual(widget.winfo_width(),widget.winfo_reqwidth())
                self.assertGreaterEqual(widget.winfo_rooty(),panel.winfo_rooty())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),panel.winfo_rooty()+panel.winfo_height())
        finally:
            self.root.after_cancel(app.timer)
            app.history_panel.close()
            app.audio_panel.close()

