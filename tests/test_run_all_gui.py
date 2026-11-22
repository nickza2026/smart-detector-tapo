"""Real offscreen button/coordinator/API panel, owned profile, fake process IO."""
from dataclasses import replace
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.update_business import business_state


class Controller:
    video_storage_mode='ram'
    def __init__(self,events):self.events=events;self.running=set();self.latest=None
    def is_running(self,role):return role in self.running
    def snapshot(self):return {r:dict(state='running' if r in self.running else 'stopped') for r in ('capture','worker')}
    def drain_messages(self):return []
    def start_capture(self,config):self.events.append('capture');self.running.add('capture');return True
    def start_worker(self,*args):self.events.append('worker');self.running.add('worker');return True
    def stop(self,role):self.running.discard(role)
    def stop_all(self):self.running.clear();self.latest=None
    def tracking_snapshot(self):return self.latest
    def close(self):assert not self.running


class Audio:
    def __init__(self,events):self.events=events;self.is_running=False
    def start(self,*args,**kwargs):self.events.append('audio');self.is_running=True
    def stop(self):self.is_running=False
    def close(self):self.stop()
    def poll(self):return []


class RunAllGuiTests(unittest.TestCase):
    def setUp(self):
        from smart_tapo.smoke_window import install
        install()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.events=[];self.controller=Controller(self.events)
        self.root=tk.Tk();self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.store.save(replace(self.store.load_or_create(),capture_backend='rtsp',
            refueling_api_enabled=True,refueling_api_url='https://example.invalid/api'))
        with patch.object(self.store,'load_notify_key',return_value=''):
            self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.cleanup_app)
        self.audio=Audio(self.events);self.app.audio_panel.listener_panel.runtime=self.audio
        self.app.webhook_panel.sender.poll=Mock()  # No DB dispatch or HTTP executor.
        real_start=self.app.webhook_panel.sender.start
        def start(*args,**kwargs):self.events.append('api');return real_start(*args,**kwargs)
        self.app.webhook_panel.sender.start=start
        self.enterContext(patch.object(self.store,'load_refueling_key',return_value='synthetic-key-only'))
        self.enterContext(patch('smart_tapo.desktop_run_all.is_ready',return_value=True))
        self.errors=self.enterContext(patch('smart_tapo.desktop.messagebox.showerror'))
        self.confirm=self.enterContext(patch('smart_tapo.desktop_run_all.messagebox.askyesno',return_value=True))
        self.app.notebook.select(self.app.control);self.root.deiconify()
        self.root.update_idletasks();self.root.update()

    def cleanup_app(self):
        self.app.closing=True
        if self.app.tracking_popup is not None:self.app.tracking_popup.close()
        for name in ('health_alerts','updates_panel','startup_panel','webhook_panel','history_panel',
                     'refueling_panel','audio_panel','offline_audio_panel','notify_test_panel'):
            getattr(self.app,name).close()
        self.controller.stop_all();self.controller.close()
        for timer in self.root.tk.call('after','info'):self.root.after_cancel(timer)
        self.root.destroy()

    def test_button_starts_all_with_prepared_profile_one_consent_api_last_and_stop(self):
        self.assertEqual(self.events,[])
        self.assertGreater(self.app.run_all_button.winfo_width(),1)
        self.assertEqual(self.app.audio_panel.listener_panel.profile_path.get(),'')
        self.app.run_all_button.invoke()
        self.assertEqual(self.events,['capture','worker','audio','api'],self.app.status.get())
        self.confirm.assert_called_once();self.errors.assert_not_called()
        self.assertTrue(Path(self.app.audio_panel.listener_panel.profile_path.get()).is_file())
        self.assertIsNotNone(self.app.webhook_panel.sender.scope)
        self.app.run_all_button.invoke()
        self.assertEqual(self.events,['capture','worker','audio','api'])
        self.confirm.assert_called_once()
        self.app.stop_all()
        self.assertFalse(self.controller.running);self.assertFalse(self.audio.is_running)
        self.assertIsNone(self.app.webhook_panel.sender.scope)

    def test_stop_all_during_confirmation_cancels_even_previously_stopped(self):
        self.app.stop_all()
        def confirm(*args,**kwargs):
            self.assertIsNone(business_state(self.app))
            self.app.run_all_button.invoke()  # Reentrant press is disabled.
            self.app.stop_all();return True
        self.confirm.side_effect=confirm
        self.app.run_all_button.invoke()
        self.assertEqual(self.events,[])
        self.assertIn('สถานะเปลี่ยน',self.app.status.get())
        self.assertFalse(self.app._run_all_starting)

    def test_main_tracking_view_projects_same_frame_without_calibration_mutation(self):
        import time
        from types import SimpleNamespace
        from PIL import Image
        from smart_tapo.desktop_tracking import make_packet
        original=self.app.config;selection=self.app.selection;reference=self.app.reference_image
        now=time.monotonic()
        packet=make_packet(Image.new('RGB',original.expected_size,'blue'),
            SimpleNamespace(session_id='synthetic',sequence=1,captured_monotonic=now-.2),
            SimpleNamespace(view_corners=(50,60,100,60,100,120,50,120)),
            generation=2,observed_monotonic=now-.1,safe=True)
        self.controller.latest=packet
        self.app.open_tracking_view();popup=self.app.tracking_popup
        popup.window.update_idletasks();popup.poll()
        self.assertTrue(popup.window.winfo_ismapped())
        types=[popup.canvas.type(i) for i in popup.canvas.find_all()]
        self.assertEqual(types,['image','polygon'])
        self.assertIs(self.app.config,original);self.assertEqual(self.app.selection,selection)
        self.assertIs(self.app.reference_image,reference);self.assertEqual(self.events,[])
        self.controller.latest=dict(packet,sequence=2,alignment_ok=False,corners=(),reason='anchors_do_not_surround_slot')
        popup.poll()
        self.assertEqual([popup.canvas.type(i) for i in popup.canvas.find_all()],['image'])
        self.assertIn('UNKNOWN',popup.label['text'])
        self.app.stop_all();popup.poll()
        self.assertFalse(popup.canvas.find_all())
