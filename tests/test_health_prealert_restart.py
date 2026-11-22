"""Per-send preflight contract; held worker/fake clock, no provider or device."""
import unittest
from unittest.mock import patch

from smart_tapo.desktop_health_alerts import HealthAlertSupervisor
from tests.test_health_autorecovery_gate import HeldExecutor


class PrealertRestartTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.calls = []
        self.outcome = 'success'
        self.executor = HeldExecutor()
        patcher = patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor', return_value=self.executor)
        patcher.start(); self.addCleanup(patcher.stop)
        self.s = HealthAlertSupervisor(self.sender, lambda: self.now)
        self.addCleanup(self.s.close)
        # Dynamic attribute also exercises the original implementation: RED is
        # an unwanted actual sender call, not an absent constructor/API error.
        self.s.require_audio_preflight = True
        self.config = dict(notify_enabled=True, health_notify_enabled=True,
                           health_failure_seconds=1, health_repeat_seconds=60)
        self.audio = dict(expected=True, healthy=False, generation=1,
                          reason='audio_listener_failed')
        self.samples = {'audio': self.audio}

    def sender(self, *args):
        self.calls.append(args[2])
        return self.outcome

    def poll(self, now):
        self.now = float(now)
        for sample in self.samples.values(): sample['observed_at'] = self.now
        self.s.poll(self.config, ('synthetic-key', 'https://notify.example'), self.samples)

    def first_request(self):
        self.poll(0); self.poll(1); self.executor.run_next()
        self.assertEqual(self.calls, [], 'Audio was sent without its fresh restart preflight')
        request = getattr(self.s, 'audio_preflight_request', None)
        self.assertIsNotNone(request)
        return request

    def failed(self, request, now):
        self.audio.update(prealert_request=request, prealert_status='failed')
        self.poll(now); self.executor.run_next()

    def test_fault_and_every_reminder_need_distinct_fresh_restart(self):
        first = self.first_request()
        self.failed(first, 2)
        self.assertEqual([n.kind for n in self.calls], ['fault'])
        self.poll(62); self.executor.run_next()
        self.assertEqual(len(self.calls), 1, 'Reminder reused an earlier restart permission')
        second = self.s.audio_preflight_request
        self.assertIsNotNone(second); self.assertIsNot(second, first)
        self.failed(second, 63)
        self.assertEqual([n.kind for n in self.calls], ['fault', 'reminder'])

    def test_provider_retry_needs_new_restart_but_keeps_notice_identity(self):
        self.outcome = 'failed'
        first = self.first_request()
        self.failed(first, 2)
        self.assertEqual(len(self.calls), 1)
        notice = self.calls[0]
        self.poll(32); self.executor.run_next()
        self.assertEqual(len(self.calls), 1)
        second = self.s.audio_preflight_request
        self.assertIsNot(second, first)
        # The old failure response cannot authorize this new HTTP attempt.
        self.poll(33); self.executor.run_next()
        self.assertEqual(len(self.calls), 1)
        self.failed(second, 34)
        self.assertEqual(len(self.calls), 2)
        self.assertIs(self.calls[1], notice)

    def test_unavailable_owner_deadline_does_not_slide_or_mute_forever(self):
        request = self.first_request()
        for now in range(2, 21):
            self.poll(now); self.executor.run_next()
            self.assertIs(self.s.audio_preflight_request, request)
            self.assertEqual(self.calls, [])
        self.poll(request.deadline); self.executor.run_next()
        self.assertEqual(len(self.calls), 1)

    def test_vision_is_delivered_while_audio_waits_for_restart(self):
        self.samples['vision'] = dict(expected=True, healthy=False, generation=1, reason='vision_worker_failed')
        self.poll(0); self.poll(1); self.executor.run_next()
        self.assertEqual([n.channel for n in self.calls], ['vision'])
        self.assertIsNotNone(self.s.audio_preflight_request)

    def queue_failed_request(self):
        request = self.first_request()
        self.audio.update(prealert_request=request, prealert_status='failed')
        self.poll(2)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_stop_invalidates_already_queued_permission(self):
        self.queue_failed_request()
        self.audio['expected'] = False
        self.poll(3); self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.s.audio_preflight_request)

    def test_close_invalidates_already_queued_permission(self):
        self.queue_failed_request()
        self.s.close(); self.executor.run_next()
        self.assertEqual(self.calls, [])

    def test_generation_change_invalidates_already_queued_permission(self):
        self.queue_failed_request()
        self.audio['generation'] += 1
        self.poll(3); self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.poll(4)
        self.assertIsNotNone(self.s.audio_preflight_request)

    def test_recipient_change_invalidates_already_queued_permission(self):
        self.queue_failed_request()
        self.now = 3
        self.s.poll(self.config, ('other-synthetic-key', 'https://notify.example'), self.samples)
        self.executor.run_next()
        self.assertEqual(self.calls, [])

    def test_fresh_health_invalidates_queued_fault(self):
        self.queue_failed_request()
        self.audio.update(healthy=True, reason='ok')
        self.poll(3); self.executor.run_next()
        self.assertEqual(self.calls, [])

    def test_stale_healthy_flag_cannot_renew_restart_deadline_forever(self):
        request = self.first_request()
        self.audio.update(healthy=True, reason='ok', observed_at=0)
        for now in (10, 20, request.deadline):
            self.now = now
            self.s.poll(self.config, ('synthetic-key', 'https://notify.example'), self.samples)
            self.executor.run_next()
        self.assertEqual(len(self.calls), 1)

    def test_recovered_notice_does_not_restart_a_healthy_producer(self):
        request = self.first_request()
        self.failed(request, 2)
        self.audio.update(healthy=True, reason='ok')
        for now in range(3, 34):
            self.poll(now); self.executor.run_next()
            self.assertIsNone(self.s.audio_preflight_request)
        self.assertEqual([n.kind for n in self.calls], ['fault', 'recovered'])


if __name__ == '__main__': unittest.main()
