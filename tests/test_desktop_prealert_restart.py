"""Actual Tk audio owner + real supervisor with synthetic producer/sender."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from smart_tapo.desktop_health_alerts import HealthAlertSupervisor
from tests import test_desktop_rtsp_audio as audio_fixture
from tests import test_desktop_rtsp as desktop_fixture
from tests.test_health_autorecovery_gate import HeldExecutor


class DesktopPrealertTests(unittest.TestCase):
    setUp_fixture = audio_fixture.RtspControlsTests.setUp
    start_watch = audio_fixture.RtspControlsTests.start_watch

    def setUp(self):
        self.setUp_fixture()
        self.calls = []
        self.executor = HeldExecutor()
        patcher = patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor', return_value=self.executor)
        patcher.start(); self.addCleanup(patcher.stop)
        self.supervisor = HealthAlertSupervisor(self.sender, lambda: self.now, require_audio_preflight=True)
        self.addCleanup(self.supervisor.close)
        self.notify_config = dict(notify_enabled=True, health_notify_enabled=True,
                                  health_failure_seconds=1, health_repeat_seconds=60)
        self.start_watch()
        self.generation = self.listener._health_generation
        # Existing early reconnect budget is spent, but the continuing bound
        # watch remains authorized. No new checkbox consent is fabricated.
        self.listener._recovery.attempts = 3
        self.listener._recovery.fail(self.now)
        self.listener._watching = False
        self.listener._health_reason = 'audio_listener_failed'
        self.listener._health_good = None
        self.runtime.is_running = False
        self.tick(0)

    def sender(self, *args):
        self.calls.append(args[2])
        return 'success'

    def tick(self, seconds, *, run=False):
        self.now += seconds
        self.panel.poll()
        self.supervisor.poll(self.notify_config, ('synthetic-key', 'https://notify.example'),
                             {'audio': self.listener.health_snapshot()})
        self.listener.service_alert_recovery(self.supervisor.audio_preflight_request)
        if run: self.executor.run_next()

    def pcm(self, session='b'*32, frames=16000):
        self.runtime.events.append(dict(kind='listener_progress', waiting_audio=False, stage='persisted',
            heartbeat=self.now+1, pcm_started_at=self.listener._health_since,
            audio_frames=frames, pcm_session=session))

    def test_one_new_start_before_fault_and_before_each_reminder(self):
        self.tick(1, run=True)
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.calls[-1][0], self.runtime.calls[0][0])
        self.tick(8, run=True)
        self.assertEqual([n.kind for n in self.calls], ['fault'])
        self.tick(60, run=True)
        self.assertEqual(len(self.runtime.calls), 3)
        self.assertEqual(len(self.calls), 1)
        self.tick(8, run=True)
        self.assertEqual([n.kind for n in self.calls], ['fault', 'reminder'])
        self.assertEqual(self.listener._health_generation, self.generation)

    def test_new_persisted_pcm_cancels_fault_without_fake_recovery_or_budget_reset(self):
        self.tick(1)
        self.pcm(); self.tick(1, run=True)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.listener.health_snapshot()['healthy'])
        self.assertTrue(self.listener._watching)
        self.assertEqual(self.listener._health_generation, self.generation)
        self.assertEqual(self.listener._recovery.attempts, 3)
        for frame in range(2, 33):
            self.pcm(frames=16000*frame); self.tick(1, run=True)
        self.assertEqual(self.listener._recovery.attempts, 0)
        self.assertEqual(self.calls, [])

    def test_old_session_with_new_looking_timestamps_cannot_heal_probe(self):
        self.listener._health_pcm = ('a'*32, 100)
        self.tick(1)
        self.pcm(session='a'*32, frames=200); self.tick(1)
        self.assertFalse(self.listener.health_snapshot()['healthy'])
        self.tick(7, run=True)
        self.assertEqual(len(self.calls), 1)

    def test_running_flag_without_pcm_is_not_success(self):
        self.tick(1)
        self.assertTrue(self.runtime.is_running)
        self.tick(8, run=True)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.listener.health_snapshot()['healthy'])

    def test_stuck_old_child_does_not_overlap_and_cannot_mute_alert_forever(self):
        self.runtime.is_running = True
        self.runtime.stop = lambda: None
        self.tick(1)
        self.assertEqual(len(self.runtime.calls), 1)
        self.tick(8, run=True)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.calls), 1)

    def test_stop_before_probe_failure_cancels_and_never_restarts_again(self):
        self.tick(1)
        self.listener.stop()
        self.tick(100, run=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertFalse(self.listener._health_expected)

    def test_changed_source_while_reaping_revokes_authorization(self):
        self.runtime.is_running = True
        self.runtime.stop = lambda: None
        self.tick(1)
        self.config = replace(self.config, rtsp_url='rtsp://127.0.0.1:8554/other-synthetic')
        self.tick(1, run=True)
        self.tick(100, run=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertFalse(self.listener._health_expected)

    def test_success_then_immediate_failure_needs_new_ticket_not_three_extra_starts(self):
        self.tick(1)
        self.pcm(); self.tick(1)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='audio_timeout')]
        self.tick(1)
        self.assertEqual(len(self.runtime.calls), 2)
        self.tick(1, run=True)
        self.assertEqual(len(self.runtime.calls), 3)
        self.assertEqual(self.calls, [])

    def test_profile_edit_after_failed_probe_revokes_queued_send(self):
        self.tick(1)
        self.tick(8)
        self.profile.write_text('{"changed":true}')
        self.tick(1, run=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.listener._health_expected)

    def test_owned_stop_terminal_is_not_a_second_retry_or_new_probe_failure(self):
        self.runtime.is_running = True
        self.runtime.stop = lambda: None
        self.tick(1)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='finished'), dict(kind='error', code='worker_failed')]
        self.tick(1)
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertTrue(self.listener._prealert_pending())
        self.assertEqual(self.listener._recovery.attempts, 3)

    def test_late_owned_stop_error_after_send_cannot_erase_spent_budget(self):
        self.tick(1)
        self.tick(8, run=True)
        # HTTP claim consumed the ticket, but real stop/kill can reap later.
        self.listener.service_alert_recovery(None)
        self.runtime.events = [dict(kind='error', code='audio_timeout')]
        self.tick(1)
        self.assertEqual(self.listener._recovery.attempts, 3)

    def test_failed_stop_is_bounded_and_does_not_crash_health_poll(self):
        self.runtime.is_running = True
        def failed_stop(): raise OSError('synthetic-private-text')
        self.runtime.stop = failed_stop
        self.tick(1)
        self.runtime.stop = lambda: None
        self.tick(1, run=True)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('private', self.listener.status.get())


class DesktopPrealertWiringTests(unittest.TestCase):
    close = desktop_fixture.DesktopRtspTests.close

    def setUp(self):
        from smart_tapo.smoke_window import install
        install()
        desktop_fixture.DesktopRtspTests.setUp(self)
        self.addCleanup(self.app.health_alerts.close)

    def test_actual_desktop_opts_into_preflight(self):
        self.assertTrue(self.app.health_alerts.require_audio_preflight)

    def test_actual_desktop_poll_starts_probe_before_sender(self):
        now = [100.]
        executor = HeldExecutor()
        calls = []
        self.app.health_alerts.close()
        with patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor', return_value=executor):
            self.app.health_alerts = HealthAlertSupervisor(
                lambda *args: calls.append(args[2]) or 'success', lambda: now[0], require_audio_preflight=True)
        self.addCleanup(self.app.health_alerts.close)
        self.app.config = replace(self.app.config, rtsp_url='rtsp://127.0.0.1:8554/synthetic',
                                  notify_enabled=True, health_failure_seconds=1, health_repeat_seconds=60)
        self.app.form['rtsp_url'].set(self.app.config.rtsp_url)
        self.app._health_credentials = ('synthetic-key', 'https://notify.example')
        listener = self.app.audio_panel.listener_panel
        listener.clock = lambda: now[0]
        runtime = audio_fixture.PureFakeRuntime(); runtime.profile_fingerprint = 'frozen-fingerprint'
        listener.runtime = runtime
        profile = self.store.config_path.parent/'synthetic-profile.json'; profile.write_text('{}')
        listener.profile_path.set(str(profile))
        listener.start_bound(self.app.config, str(profile))
        listener._recovery.attempts = 3; listener._recovery.fail(now[0])
        listener._watching = False; runtime.is_running = False
        listener._health_reason = 'audio_listener_failed'
        self.app._poll_health()
        now[0] += 1; self.app._poll_health(); executor.run_next()
        self.assertEqual(len(runtime.calls), 2, 'Real desktop never serviced the restart ticket')
        self.assertEqual(calls, [])
        now[0] += 8; listener.poll(); self.app._poll_health(); executor.run_next()
        self.assertEqual([n.kind for n in calls], ['fault'])


if __name__ == '__main__': unittest.main()
