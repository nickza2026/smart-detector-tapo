"""Deterministic health state machine tests; no devices or transport."""
import unittest

from smart_tapo.health_alerts import HealthMonitor, Notice, format_notice


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.m = HealthMonitor()
        self.m.arm('vision', 0)

    def due(self, now=30):
        notices = self.m.take_due(now)
        self.assertEqual(len(notices), 1)
        return notices[0]

    def finish(self, notice, now, outcome='success'):
        self.assertTrue(self.m.mark_sending(notice))
        self.assertTrue(self.m.complete(notice, outcome, now))

    def healthy(self, now):
        self.m.observe('vision', True, 'vision_no_frames', now)

    def sustained_recovery(self, start=31):
        for now in range(start,start+30):
            self.healthy(now)
            self.assertEqual(self.m.take_due(now),[])
        self.healthy(start+30)

    def test_boundary_one_fault(self):
        self.assertEqual(self.m.take_due(29.999), [])
        n = self.due()
        self.assertEqual(n.kind, 'fault')
        self.finish(n, 30)
        self.assertEqual(self.m.take_due(31), [])

    def test_repeat_boundary(self):
        self.finish(self.due(), 30)
        self.assertEqual(self.m.take_due(929.99), [])
        self.assertEqual(self.due(930).kind, 'reminder')

    def test_long_gap_coalesces(self):
        self.finish(self.due(), 30)
        self.finish(self.due(1000000), 1000000)
        self.assertEqual(self.m.take_due(1000000), [])
        self.assertEqual(self.m.take_due(1000899), [])

    def test_recovered_once(self):
        self.finish(self.due(), 30)
        self.sustained_recovery()
        n = self.due(61)
        self.assertEqual(n.kind, 'recovered')
        self.finish(n, 61)
        self.assertEqual(self.m.take_due(10000), [])

    def test_healthy_before_delay(self):
        self.healthy(29)
        self.assertEqual(self.m.take_due(10000), [])

    def test_reason_change_preserves_start(self):
        self.m.observe('vision', False, 'vision_alignment', 29)
        self.assertEqual(self.due().reason, 'vision_alignment')

    def test_manual_stop_before_fault(self):
        self.m.disarm('vision')
        self.assertEqual(self.m.take_due(10000), [])

    def test_manual_stop_after_fault(self):
        self.finish(self.due(), 30)
        self.m.disarm('vision')
        self.assertEqual(self.m.take_due(10000), [])

    def test_startup_no_input(self):
        self.assertEqual(self.due().reason, 'starting')

    def test_unknown_is_not_healthy(self):
        self.m.observe('vision', True, 'unknown', 29)
        self.assertEqual(self.due().kind, 'fault')

    def test_channels_independent(self):
        self.m.arm('audio', 10)
        self.healthy(20)
        self.assertEqual(self.m.take_due(30), [])
        self.assertEqual(self.due(40).channel, 'audio')

    def test_failure_and_ambiguity_paced(self):
        for outcome in ('failed', 'ambiguous'):
            with self.subTest(outcome=outcome):
                self.setUp()
                n = self.due()
                self.finish(n, 30, outcome)
                self.assertEqual(self.m.take_due(30), [])
                self.assertEqual(self.m.take_due(59), [])
                self.assertIs(self.due(60), n)

    def test_three_attempts_then_repeat(self):
        n = self.due()
        self.finish(n, 30, 'failed')
        self.due(60)
        self.finish(n, 60, 'ambiguous')
        self.due(120)
        self.finish(n, 120, 'failed')
        self.assertEqual(self.m.take_due(1019), [])
        self.assertIs(self.due(1020), n)

    def test_failed_fault_no_recovery(self):
        n = self.due()
        self.finish(n, 30, 'failed')
        self.healthy(31)
        self.assertEqual(self.m.take_due(10000), [])

    def test_stale_scope_completion(self):
        n = self.due()
        self.m.mark_sending(n)
        self.m.disarm('vision')
        self.m.arm('vision', 31)
        self.assertFalse(self.m.complete(n, 'success', 32))
        self.assertFalse(self.m.is_current(n))
        self.assertEqual(self.due(61).kind, 'fault')

    def test_rearm_cancels(self):
        n = self.due()
        self.m.arm('vision', 30)
        self.assertFalse(self.m.mark_sending(n))
        self.assertEqual(self.m.take_due(59), [])

    def test_queued_fault_cancelled(self):
        n = self.due()
        self.healthy(31)
        self.assertFalse(self.m.mark_sending(n))
        self.assertEqual(self.m.take_due(31), [])

    def test_inflight_success_after_healthy(self):
        n = self.due()
        self.assertTrue(self.m.mark_sending(n))
        self.healthy(31)
        self.assertEqual(self.m.take_due(31), [])
        self.assertTrue(self.m.complete(n, 'success', 32))
        for now in range(32,61):
            self.healthy(now)
            self.assertEqual(self.m.take_due(now),[])
        self.healthy(61)
        recovery = self.due(61)
        self.assertEqual(recovery.kind, 'recovered')
        self.finish(recovery, 61)
        self.assertEqual(self.m.take_due(10000), [])

    def test_recovery_failed_bounded(self):
        self.finish(self.due(), 30)
        self.sustained_recovery()
        n = self.due(61)
        self.finish(n, 61, 'failed')
        self.assertEqual(self.m.take_due(90), [])
        self.assertIs(self.due(91), n)
        self.finish(n, 91, 'ambiguous')
        self.assertEqual(self.m.take_due(150), [])
        self.assertIs(self.due(151), n)
        self.finish(n, 151, 'failed')
        self.assertEqual(self.m.take_due(1050), [])
        self.assertIs(self.due(1051), n)
        self.finish(n, 1051)
        self.assertEqual(self.m.take_due(10000), [])

    def test_invalid_settings(self):
        for key, values in [('delay', [0, 3601, True, '30', None, float('nan'), float('inf')]),
                            ('repeat', [59, 86401, False, '900', float('-inf')])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        HealthMonitor(**{key: value})

    def test_invalid_times(self):
        for value in [True, None, '30', float('nan'), float('inf'), 10 ** 1000]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.m.take_due(value)

    def test_invalid_channel_and_health(self):
        for channel in ('other', None, [], True):
            with self.assertRaises(ValueError):
                self.m.arm(channel, 0)
        with self.assertRaises(ValueError):
            self.m.observe('vision', 1, 'unknown', 0)

    def test_out_of_order_observation(self):
        self.m.observe('vision', False, 'vision_quality', 20)
        self.assertFalse(self.m.observe('vision', True, 'vision_quality', 19))
        self.assertEqual(self.due().kind, 'fault')
        self.assertEqual(self.m.take_due(29), [])

    def test_out_of_order_completion(self):
        n = self.due()
        self.m.mark_sending(n)
        self.m.take_due(40)
        self.assertFalse(self.m.complete(n, 'success', 39))
        self.assertTrue(self.m.complete(n, 'success', 40))

    def test_fresh_health_resets_bad_period(self):
        self.healthy(20)
        self.m.observe('vision', False, 'vision_quality', 25)
        self.assertEqual(self.m.take_due(54), [])
        self.due(55)

    def test_untrusted_reason_never_formatted(self):
        self.m.observe('vision', False, 'https://secret/path', 1)
        n = self.due()
        self.assertEqual(n.reason, 'unknown')
        self.assertNotIn('secret', format_notice(n))
        with self.assertRaises(ValueError):
            format_notice(Notice('vision', 'fault', 'secret', 1, 1, 1))

    def test_claim_required_and_once(self):
        n = self.due()
        self.assertFalse(self.m.complete(n, 'success', 30))
        self.assertTrue(self.m.mark_sending(n))
        self.assertFalse(self.m.mark_sending(n))
        self.assertEqual(self.m.take_due(100), [])
        self.assertTrue(self.m.complete(n, 'success', 100))
        self.assertFalse(self.m.complete(n, 'success', 100))

    def test_no_automatic_arm(self):
        m = HealthMonitor()
        self.assertFalse(m.observe('audio', False, 'audio_no_pcm', 0))
        self.assertEqual(m.take_due(10000), [])


if __name__ == '__main__':
    unittest.main()
