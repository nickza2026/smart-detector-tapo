"""Fake-clock and held-executor delivery checks; no network or provider calls."""
from concurrent.futures import Future
import unittest
from unittest.mock import patch
from smart_tapo.desktop_health_alerts import HealthAlertSupervisor


class HeldExecutor:
    def __init__(self): self.jobs = []
    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future
    def run_next(self):
        if self.jobs:
            future, fn, args = self.jobs.pop(0)
            if future.set_running_or_notify_cancel(): future.set_result(fn(*args))
    def shutdown(self, **kwargs):
        for future, _, _ in self.jobs: future.cancel()


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls = []
        self.hook = None
        self.executor = HeldExecutor()
        mocked = patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor', return_value=self.executor)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.s = HealthAlertSupervisor(self.sender, lambda: self.now)
        self.addCleanup(self.s.close)
        self.config = dict(notify_enabled=True, health_notify_enabled=True,
                           health_failure_seconds=30, health_repeat_seconds=900)
        self.samples = {'audio': dict(expected=True, healthy=False, generation=1, reason='audio_no_pcm')}

    def sender(self, token, user, notice, cancel, importance):
        self.calls.append(notice)
        if self.hook: self.hook(cancel)
        return 'success'

    def poll(self, now):
        self.now = now
        for sample in self.samples.values(): sample['observed_at'] = now
        self.s.poll(self.config, ('fake-token', 'fake-user'), self.samples)

    def test_recovery_blocks_fault_then_exhaustion_sends_once(self):
        self.samples['audio']['recovery_pending'] = True
        self.poll(0); self.poll(30); self.executor.run_next()
        self.assertEqual(self.calls, [])
        notice = self.s._notices['audio']
        self.samples['audio']['recovery_pending'] = False
        self.poll(45); self.executor.run_next()
        self.assertEqual(self.calls, [notice])
        self.assertEqual(notice.kind, 'fault')
        self.poll(46); self.executor.run_next()
        self.assertEqual(self.calls, [notice])

    def test_vision_is_not_suppressed(self):
        self.samples['audio']['recovery_pending'] = True
        self.samples['vision'] = dict(expected=True, healthy=False, generation=1,
            reason='vision_worker_failed', recovery_pending=True)
        self.poll(0); self.poll(30); self.executor.run_next()
        self.assertEqual([n.channel for n in self.calls], ['vision'])

    def test_only_actual_true_suppresses_legacy_is_unchanged(self):
        for value in (None, False, 1, 'True', 'true', [], {}):
            with self.subTest(value=value):
                self.samples['audio']['generation'] += 1
                if value is None: self.samples['audio'].pop('recovery_pending', None)
                else: self.samples['audio']['recovery_pending'] = value
                start, count = self.now + 100, len(self.calls)
                self.poll(start); self.poll(start + 30); self.executor.run_next()
                self.assertEqual(len(self.calls), count + 1)

    def test_queued_claim_rechecks_gate_without_losing_notice(self):
        self.poll(0); self.poll(30)
        self.assertEqual(len(self.executor.jobs), 1)
        notice = self.s._notices['audio']
        self.samples['audio']['recovery_pending'] = True
        self.poll(31); self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.s._monitor.is_current(notice))
        self.samples['audio']['recovery_pending'] = False
        self.poll(32); self.executor.run_next()
        self.assertEqual(self.calls, [notice])

    def test_healed_suppressed_fault_has_no_fake_recovery(self):
        self.samples['audio']['recovery_pending'] = True
        self.poll(0); self.poll(30); self.executor.run_next()
        self.samples['audio'].update(healthy=True, reason='ok')
        for now in range(31, 65): self.poll(now); self.executor.run_next()
        self.samples['audio']['recovery_pending'] = False
        self.poll(65); self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.s.is_running)

    def test_claimed_transport_finishes_without_stranding_sending(self):
        self.poll(0); self.poll(30)
        def begin(cancel):
            self.samples['audio']['recovery_pending'] = True
            self.poll(31)
            self.assertFalse(cancel.is_set())
        self.hook = begin
        self.executor.run_next()
        self.hook = None
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.s._monitor._states['audio'].sending)
        self.samples['audio']['recovery_pending'] = False
        self.poll(931); self.executor.run_next()
        self.assertEqual([n.kind for n in self.calls], ['fault', 'reminder'])
