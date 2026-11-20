"""Real offscreen Tk with fake roles, clock, credentials and Pushover executor."""
from dataclasses import replace
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.desktop_health_alerts import HealthAlertSupervisor
from tests.test_desktop_gui import FakeController
from tests.test_desktop_health_alerts import FakeClock,HeldExecutor


class Controller(FakeController):
    def __init__(self,clock):
        super().__init__();self.clock=clock;self.expected=False;self.healthy=False
    def health_snapshot(self):
        return dict(expected=self.expected,healthy=self.healthy,
            reason='ok' if self.healthy else 'vision_worker_failed',
            observed_at=self.clock.now,generation=1)
    def stop(self,role):self.expected=False;self.active=False
    def stop_all(self):super().stop_all();self.expected=False


class HealthGuiTests(unittest.TestCase):
    def setUp(self):
        from smart_tapo.smoke_window import install
        install()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=FakeClock();self.clock.now=10
        self.executor=HeldExecutor();self.calls=[]
        with patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor',return_value=self.executor):
            self.supervisor=HealthAlertSupervisor(lambda t,u,n,c,i:self.calls.append(n) or 'success',self.clock)
        self.addCleanup(self.supervisor.close)
        self.root=tk.Tk()
        self.addCleanup(lambda:self.root.destroy() if self.root.tk.call('info','commands',self.root._w) else None)
        store=DesktopStore(Path(self.temp.name)/'config.json')
        store.save(replace(store.load_or_create(),notify_enabled=True))
        self.controller=Controller(self.clock)
        self.base=store.load_or_create().notify_api_url
        with patch.object(store,'load_notify_key',return_value='fake-key'),patch(
                'smart_tapo.desktop_health_alerts.HealthAlertSupervisor',return_value=self.supervisor):
            self.app=DesktopApp(self.root,store,self.controller)
        self.addCleanup(self.cleanup_app)
        self.app.notebook.select(self.app.settings)
        self.root.deiconify()
        self.root.update_idletasks()
        self.root.update()

    def cleanup_app(self):
        self.app.closing=True
        self.supervisor.close()
        for name in ('updates_panel','startup_panel','webhook_panel','history_panel',
                     'refueling_panel','audio_panel','offline_audio_panel','notify_test_panel'):
            getattr(self.app,name).close()
        self.controller.stop_all();self.controller.close()
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)

    def test_fault_then_recovery_and_settings_render_with_only_fake_delivery(self):
        self.assertTrue(self.app.health_notify.get())
        self.assertEqual(float(self.app.form['health_failure_seconds'].get()),30)
        self.assertEqual(float(self.app.form['health_repeat_seconds'].get()),900)
        self.assertGreater(self.app.settings_canvas.winfo_width(),1)
        self.assertEqual(self.app._health_credentials,('fake-key',self.base))
        self.controller.expected=True
        self.app._poll_health();self.clock.now=40;self.app._poll_health()
        self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls],['fault'])
        self.controller.healthy=True;self.clock.now=41
        self.app._poll_health();self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls],['fault'])
        for stamp in range(42,72):
            self.clock.now=stamp
            self.app._poll_health();self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls],['fault','recovered'])
        self.app.notify_key.set('unconfirmed-new-key')
        self.app._poll_health()
        self.assertEqual(self.app._health_credentials,('fake-key',self.base))

    def test_failed_roles_still_offer_stop_to_disarm_health(self):
        self.controller.expected=True
        self.root.after_cancel(self.app.timer)
        self.app._poll()
        self.assertFalse(self.app.stop_buttons['worker'].instate(['disabled']))
        self.app.stop_buttons['worker'].invoke()
        self.clock.now=40;self.app._poll_health();self.executor.run_next()
        self.assertEqual(self.calls,[])
        self.root.after_cancel(self.app.timer)

    def test_terminal_audio_still_offers_manual_stop_and_disarms(self):
        panel=self.app.audio_panel.listener_panel
        panel._health_expected=True;panel._watching=False
        panel._buttons()
        self.assertFalse(panel.stop_button.instate(['disabled']))
        panel.stop_button.invoke()
        self.assertFalse(panel.health_snapshot()['expected'])
