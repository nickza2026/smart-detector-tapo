"""Owned real executor and blocking fake transport; no network/credentials IO."""
import threading
import unittest

from smart_tapo.desktop_health_alerts import HealthAlertSupervisor
from tests.test_desktop_health_alerts import FakeClock


class HealthAlertThreadTests(unittest.TestCase):
    def test_inflight_stop_scope_change_and_close_cannot_rearm(self):
        for action in ('stop','recipient','close'):
            with self.subTest(action=action):
                clock=FakeClock();entered=threading.Event();release=threading.Event();finished=threading.Event()
                calls=[]
                def transport(token,user,notice,cancel,importance):
                    calls.append((token,user,notice));entered.set()
                    release.wait(3);finished.set();return 'success'
                supervisor=HealthAlertSupervisor(transport,clock)
                config=dict(notify_enabled=True,health_notify_enabled=True,
                    health_failure_seconds=30,health_repeat_seconds=900)
                sample=dict(expected=True,healthy=False,reason='vision_alignment',observed_at=0,generation=1)
                try:
                    supervisor.poll(config,('fake-token','fake-user'),{'vision':sample})
                    clock.now=30;sample['observed_at']=30
                    supervisor.poll(config,('fake-token','fake-user'),{'vision':sample})
                    self.assertTrue(entered.wait(1))
                    future=supervisor._future
                    if action=='stop':
                        sample['expected']=False
                        supervisor.poll(config,('fake-token','fake-user'),{'vision':sample})
                    elif action=='recipient':
                        supervisor.poll(config,('new-fake-token','new-fake-user'),{})
                    else:
                        supervisor.close()
                    self.assertFalse(finished.is_set(),'Lifecycle action must not wait on fake HTTP')
                    release.set();future.result(timeout=3)
                    clock.now=31
                    supervisor.poll(config,('new-fake-token','new-fake-user'),{})
                    self.assertFalse(supervisor.is_running)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(calls[0][:2],('fake-token','fake-user'))
                finally:
                    release.set();supervisor.close()
