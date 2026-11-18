"""Actual typed runtime snapshots through the pure alert engine, without IO."""
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from smart_tapo.health_alerts import HealthMonitor
from tests.test_runtime_health_signals import audio, vision
from tests.test_desktop_health_alerts import FakeClock, HeldExecutor
from smart_tapo.desktop_health_alerts import HealthAlertSupervisor


class HealthPipelineTests(unittest.TestCase):
    def supervisor(self):
        self.clock=FakeClock();self.clock.now=10
        self.executor=HeldExecutor();self.calls=[]
        patcher=patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor',return_value=self.executor)
        patcher.start();self.addCleanup(patcher.stop)
        self.sender=HealthAlertSupervisor(lambda t,u,n,c,i:self.calls.append(n) or 'success',self.clock)
        self.addCleanup(self.sender.close)
        self.config=dict(notify_enabled=True,health_notify_enabled=True,health_failure_seconds=30,
                         health_repeat_seconds=900,camera_id='cam',data_dir='synthetic',rtsp_url='synthetic')
        return self.sender

    def test_terminal_storage_cause_survives_without_more_progress_and_recovers_once(self):
        sender=self.supervisor();source=audio()
        source._accept_health(dict(kind='error',code='storage_corrupt'),10)
        sender.poll(self.config,('fake-token','fake-user'),{'audio':source.health_snapshot(10)})
        self.clock.now=40
        sender.poll(self.config,('fake-token','fake-user'),{'audio':source.health_snapshot(40)})
        self.executor.run_next()
        self.assertEqual([(n.kind,n.reason) for n in self.calls],[('fault','audio_storage')])
        self.clock.now=42
        source._accept_health(dict(kind='listener_progress',heartbeat=42,pcm_started_at=41,
            pcm_session='a'*32,audio_frames=100,audio_rms=0.,waiting_audio=False,stage='persisted'),42)
        sender.poll(self.config,('fake-token','fake-user'),{'audio':source.health_snapshot(42)})
        self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls],['fault'])
        for stamp in range(43,73):
            self.clock.now=stamp
            source._accept_health(dict(kind='listener_progress',heartbeat=stamp,pcm_started_at=stamp-.1,
                pcm_session='a'*32,audio_frames=100*(stamp-41),audio_rms=0.,waiting_audio=False,stage='persisted'),stamp)
            sender.poll(self.config,('fake-token','fake-user'),{'audio':source.health_snapshot(stamp)})
            self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls],['fault','recovered'])

    def test_unexpected_worker_exit_does_not_wait_for_stale_good_stamp_to_expire(self):
        sender=self.supervisor();source=vision()
        source._accept_health(dict(sequence=1,session='synthetic',observed_at=10,
            captured_at=10,healthy=True,reason='unknown'),10)
        sender.poll(self.config,('fake-token','fake-user'),{'vision':source.health_snapshot(10)})
        source._roles['worker']['state']='failed';self.clock.now=11
        sender.poll(self.config,('fake-token','fake-user'),{'vision':source.health_snapshot(11)})
        self.clock.now=41
        sender.poll(self.config,('fake-token','fake-user'),{'vision':source.health_snapshot(41)})
        self.executor.run_next()
        self.assertEqual([(n.kind,n.reason) for n in self.calls],[('fault','vision_worker_failed')])

    def test_manual_stop_role_cancels_queued_fault_before_next_gui_tick(self):
        from smart_tapo.desktop import DesktopApp
        sender=self.supervisor();source=vision()
        source._frame_channel=Mock()
        for record in source._roles.values():record['stop']=Mock()
        app=DesktopApp.__new__(DesktopApp)
        app.health_alerts=sender;app.controller=source;app.closing=False
        app.config=self.config;app._health_credentials=('fake-token','fake-user')
        app.audio_panel=SimpleNamespace(listener_panel=audio())
        app.audio_panel.listener_panel._health_expected=False
        app._poll_health()
        self.clock.now=40
        app._poll_health()
        app.stop_role('worker')
        self.executor.run_next()
        self.assertEqual(self.calls,[])

    def test_advancing_quiet_pcm_never_becomes_an_alert(self):
        source=audio();monitor=HealthMonitor();monitor.arm('audio',10)
        for stamp in range(10,51):
            source._accept_health(dict(kind='listener_progress',heartbeat=stamp,pcm_started_at=10,
                pcm_session='a'*32,audio_frames=100*(stamp-9),audio_rms=0.,
                waiting_audio=False,stage='persisted'),stamp)
            state=source.health_snapshot(stamp)
            self.assertTrue(state['healthy'])
            monitor.observe('audio',state['healthy'],state['reason'],stamp)
            self.assertEqual(monitor.take_due(stamp),[])

    def test_fresh_trusted_empty_frames_never_become_an_alert(self):
        source=vision();monitor=HealthMonitor();monitor.arm('vision',10)
        for stamp in range(10,51):
            source._accept_health(dict(sequence=stamp,session='synthetic',observed_at=stamp,
                captured_at=stamp,healthy=True,reason='unknown'),stamp)
            state=source.health_snapshot(stamp)
            self.assertTrue(state['healthy'])
            monitor.observe('vision',state['healthy'],state['reason'],stamp)
            self.assertEqual(monitor.take_due(stamp),[])
