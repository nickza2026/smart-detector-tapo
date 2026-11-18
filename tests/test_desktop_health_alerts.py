"""All sender/HTTP/executor operations are fakes; never contact a provider."""
from concurrent.futures import Future
import json
import threading
import unittest
from unittest.mock import patch
from urllib import error

from smart_tapo.desktop_health_alerts import HealthAlertSupervisor, SNAPSHOT_MAX_AGE, send_health
from smart_tapo.health_alerts import HealthMonitor, Notice
from smart_tapo.notify_api import NotifyResult, _NoRedirect


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class HeldExecutor:
    def __init__(self, **kwargs):
        self.jobs = []
        self.submissions = 0
        self.shutdown_args = None

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        self.submissions += 1
        return future

    def run_next(self):
        while self.jobs:
            future, fn, args = self.jobs.pop(0)
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args))
                except BaseException as exc:
                    future.set_exception(exc)
                    raise
                return future
        return None

    def shutdown(self, **kwargs):
        self.shutdown_args = kwargs
        if kwargs.get('cancel_futures'):
            for future, _, _ in self.jobs:
                future.cancel()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.executor = HeldExecutor()
        self.calls = []
        self.importances = []
        self.result = 'success'
        self.hook = None
        self.patch = patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor',
                           return_value=self.executor)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.s = HealthAlertSupervisor(self.sender, self.clock)
        self.addCleanup(self.s.close)
        self.config = dict(notify_enabled=True, health_notify_enabled=True,
                           health_failure_seconds=30, health_repeat_seconds=900,
                           camera_id='camera', data_dir='data', rtsp_url='private-url')
        self.keys = ('fake-token', 'fake-user')
        self.samples = {}
        self.sample()

    def sender(self, token, user, notice, cancel, importance):
        self.calls.append((token, user, notice))
        self.importances.append(importance)
        if self.hook:
            self.hook(cancel)
        return self.result

    def sample(self, channel='vision', healthy=False, expected=True, generation=1,
               reason=None, observed=None):
        self.samples[channel] = dict(
            expected=expected, healthy=healthy,
            reason=reason or ('vision_worker_failed' if channel == 'vision' else 'audio_listener_failed'),
            observed_at=self.clock.now if observed is None else observed,
            generation=generation)

    def poll(self, now=None, fresh=True):
        if now is not None:
            self.clock.now = now
        if fresh:
            for sample in self.samples.values():
                sample['observed_at'] = self.clock.now
        self.s.poll(self.config, self.keys, self.samples)

    def queue_fault(self):
        self.poll(0)
        self.poll(30)
        self.assertEqual(self.executor.submissions, 1)
        self.assertTrue(self.s.is_running)

    def test_delay_and_repeat_boundaries(self):
        self.poll(0)
        self.poll(29.99)
        self.assertEqual(self.executor.submissions, 0)
        self.poll(30)
        self.executor.run_next()
        self.assertEqual(self.calls[0][2].kind, 'fault')
        self.poll(929.99)
        self.assertEqual(self.executor.submissions, 1)
        self.poll(930)
        self.executor.run_next()
        self.assertEqual(self.calls[-1][2].kind, 'reminder')

    def test_long_gap_no_catchup(self):
        self.queue_fault()
        self.executor.run_next()
        self.poll(100000)
        self.executor.run_next()
        for _ in range(10):
            self.poll()
        self.assertEqual(len(self.calls), 2)

    def test_two_channels_bounded_one_future(self):
        self.sample('audio')
        self.queue_fault()
        for _ in range(100):
            self.poll(30)
        self.assertEqual(self.executor.submissions, 1)
        self.assertLessEqual(len(self.s._notices), 2)
        self.executor.run_next()
        self.poll(30)
        self.executor.run_next()
        self.assertEqual({call[2].channel for call in self.calls}, {'vision', 'audio'})
        self.assertEqual(self.executor.submissions, 2)

    def test_disabled_by_default(self):
        self.config = {}
        self.poll(0)
        self.poll(1000)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.executor.submissions, 0)
        self.assertEqual(self.s.status, 'disabled')

    def test_both_enable_flags_required(self):
        for flag in ('notify_enabled', 'health_notify_enabled'):
            self.config[flag] = False
            self.poll(0)
            self.poll(1000)
            self.assertEqual(self.executor.submissions, 0)
            self.config[flag] = True

    def test_bool_flags_not_truthy_integers(self):
        self.config['notify_enabled'] = 1
        self.poll(0)
        self.poll(30)
        self.assertEqual(self.executor.submissions, 0)

    def test_credentials_missing_invalid_redacted(self):
        for credentials in (None, ('', 'x'), ('x', ' '), (True, 'x'), ('x' * 4097, 'x'), ['x', 'y']):
            self.keys = credentials
            self.poll(0)
            self.poll(30)
            self.assertEqual(self.s.status, 'missing_credentials')
            self.assertEqual(self.executor.submissions, 0)

    def test_credentials_trimmed_explicitly(self):
        self.keys = (' fake-token ', ' fake-user ')
        self.queue_fault()
        self.executor.run_next()
        self.assertEqual(self.calls[0][:2], ('fake-token', 'fake-user'))

    def test_only_expected_arms(self):
        self.sample(expected=False)
        self.poll(0)
        self.poll(1000)
        self.assertEqual(self.executor.submissions, 0)
        self.assertEqual(self.s.status, 'idle')

    def test_healthy_before_delay(self):
        self.poll(0)
        self.clock.now = 29
        self.sample(healthy=True)
        self.poll()
        self.poll(100)
        self.assertEqual(self.executor.submissions, 0)

    def test_held_queued_fault_cancelled_on_health(self):
        self.queue_fault()
        self.clock.now = 31
        self.sample(healthy=True)
        self.poll()
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.s.is_running)

    def test_held_queued_fault_cancelled_on_manual_stop(self):
        self.queue_fault()
        self.sample(expected=False)
        self.poll(31)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.s.is_running)

    def test_held_queued_fault_cancelled_on_key_change(self):
        self.queue_fault()
        self.keys = ('new-token', 'new-user')
        self.poll(31)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.poll(60)
        self.assertEqual(self.executor.submissions, 1)
        self.poll(61)
        self.executor.run_next()
        self.assertEqual(self.calls[0][:2], self.keys)

    def test_rapid_scope_changes_do_not_accumulate_executor_queue(self):
        self.queue_fault()
        for index in range(100):
            self.keys = ('rotated-token-' + str(index), 'fake-user')
            self.poll(31 + index * 31)
        self.assertEqual(self.executor.submissions, 1)
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.poll(self.clock.now + 30)
        self.executor.run_next()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:2], self.keys)

    def test_settings_scope_change_restarts_grace(self):
        self.queue_fault()
        self.config['camera_id'] = 'other'
        self.poll(31)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.poll(60)
        self.assertEqual(self.executor.submissions, 1)
        self.poll(61)
        self.executor.run_next()
        self.assertEqual(len(self.calls), 1)

    def test_all_setting_scope_fields_invalidate(self):
        self.poll(0)
        epoch = self.s._epoch
        for name, value in [('data_dir', 'other'), ('rtsp_url', 'other'),
                            ('health_failure_seconds', 40), ('health_repeat_seconds', 1000)]:
            self.config[name] = value
            self.poll()
            self.assertGreater(self.s._epoch, epoch)
            epoch = self.s._epoch

    def test_generation_change_restarts_grace(self):
        self.queue_fault()
        self.sample(generation=2)
        self.poll(31)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.poll(60)
        self.assertEqual(self.executor.submissions, 1)
        self.poll(61)
        self.executor.run_next()
        self.assertEqual(len(self.calls), 1)

    def test_disable_cancels_queued(self):
        self.queue_fault()
        self.config['notify_enabled'] = False
        self.poll(31)
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.s.status, 'disabled')

    def test_scope_change_ignores_inflight_success(self):
        self.queue_fault()
        def change(cancel):
            self.keys = ('new-token', 'new-user')
            self.sample(healthy=True)
            self.poll(31)
            self.assertTrue(cancel.is_set())
        self.hook = change
        self.executor.run_next()
        self.hook = None
        self.poll(32)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.s.status, 'monitoring')
        self.assertFalse(self.s.is_running)

    def test_inflight_acceptance_after_health_ordered_recovery(self):
        self.queue_fault()
        def recover(cancel):
            self.sample(healthy=True)
            self.poll(31)
            self.assertFalse(cancel.is_set())
            self.assertEqual(self.executor.submissions, 1)
        self.hook = recover
        self.executor.run_next()
        self.hook = None
        for now in range(32,62):
            self.poll(now)
        self.executor.run_next()
        self.poll(62)
        self.assertEqual([c[2].kind for c in self.calls], ['fault', 'recovered'])
        self.assertFalse(self.s.is_running)

    def test_manual_stop_after_accepted_has_no_recovery(self):
        self.queue_fault()
        self.executor.run_next()
        self.sample(expected=False)
        self.poll(31)
        self.poll(10000)
        self.assertEqual([c[2].kind for c in self.calls], ['fault'])

    def test_one_returning_sample_does_not_split_accepted_incident(self):
        self.queue_fault()
        self.executor.run_next()
        self.sample(healthy=True,observed=31)
        self.poll(31,fresh=False)
        self.executor.run_next()
        self.sample(healthy=False,observed=31)
        self.poll(40,fresh=False)
        self.poll(70,fresh=False)
        self.executor.run_next()
        self.assertEqual([c[2].kind for c in self.calls],['fault'])

    def test_audio_snapshot_uses_same_eight_second_freshness_as_panel(self):
        self.config['health_failure_seconds']=1
        self.samples={}
        for now in range(41):
            if now%8==0:
                self.sample(channel='audio',healthy=True,reason='ok',observed=now)
            self.poll(now,fresh=False)
            self.executor.run_next()
        self.assertEqual(self.calls,[])

    def test_manual_stop_during_send_ignores_completion(self):
        self.queue_fault()
        def stop(cancel):
            self.sample(expected=False)
            self.poll(31)
            self.assertTrue(cancel.is_set())
        self.hook = stop
        self.executor.run_next()
        self.hook = None
        self.poll(32)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.s.is_running)

    def test_failed_ambiguous_delivery_paced(self):
        self.result = 'failed'
        self.queue_fault()
        self.executor.run_next()
        self.assertTrue(self.s.is_running)  # Retry waiting is pending work.
        for _ in range(20):
            self.poll(31)
        self.assertEqual(self.executor.submissions, 1)
        self.poll(60)
        self.result = 'ambiguous'
        self.executor.run_next()
        self.poll(119)
        self.assertEqual(self.executor.submissions, 2)
        self.poll(120)
        self.executor.run_next()
        self.poll(1019)
        self.assertEqual(self.executor.submissions, 3)
        self.poll(1020)
        self.assertEqual(self.executor.submissions, 4)

    def test_failed_recovery_stays_bounded(self):
        self.queue_fault()
        self.executor.run_next()
        self.sample(healthy=True)
        self.poll(31)
        for now in range(32,62):
            self.poll(now)
        self.result = 'failed'
        self.executor.run_next()
        self.poll(62)
        self.assertEqual(self.executor.submissions, 2)
        self.assertTrue(self.s.is_running)
        self.poll(91)
        self.result = 'success'
        self.executor.run_next()
        self.poll(92)
        self.assertEqual([c[2].kind for c in self.calls], ['fault', 'recovered', 'recovered'])
        self.assertFalse(self.s.is_running)

    def test_sender_exception_redacted_and_ambiguous(self):
        self.queue_fault()
        def fail(cancel):
            raise RuntimeError('fake-token private-url response-secret')
        self.hook = fail
        self.executor.run_next()
        self.assertEqual(self.s.status, 'ambiguous')
        self.assertNotIn('fake-token', repr(self.s))

    def test_close_cancels_queued_idempotently(self):
        self.queue_fault()
        self.s.close()
        self.s.close()
        self.executor.run_next()
        self.poll(100)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.s.status, 'closed')
        self.assertFalse(self.s.is_running)
        self.assertEqual(self.executor.shutdown_args, dict(wait=False, cancel_futures=True))

    def test_close_during_http_reaps_without_new_requests(self):
        self.queue_fault()
        def closing(cancel):
            self.s.close()
            self.assertTrue(cancel.is_set())
            self.poll(31)
        self.hook = closing
        self.executor.run_next()
        self.hook = None
        self.poll(10000)
        self.assertIsNone(self.s._future)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.s.status, 'closed')

    def test_stale_healthy_sample_becomes_unknown(self):
        self.sample(healthy=True)
        self.poll(0)
        self.poll(SNAPSHOT_MAX_AGE + 1, fresh=False)
        self.poll(SNAPSHOT_MAX_AGE + 30.99, fresh=False)
        self.assertEqual(self.executor.submissions, 0)
        self.poll(SNAPSHOT_MAX_AGE + 31, fresh=False)
        self.executor.run_next()
        self.assertEqual(self.calls[0][2].reason, 'unknown')

    def test_out_of_order_sample_cannot_recover(self):
        self.poll(0)
        self.poll(30)
        self.executor.run_next()
        self.sample(healthy=True, observed=29)
        self.poll(31, fresh=False)
        self.assertEqual(self.executor.submissions, 1)
        self.assertFalse(self.s.is_running)

    def test_unknown_startup_arms_without_input(self):
        self.sample(observed=float('nan'))
        self.poll(0, fresh=False)
        self.poll(30, fresh=False)
        self.executor.run_next()
        self.assertEqual(self.calls[0][2].kind, 'fault')
        self.assertEqual(self.calls[0][2].reason, 'unknown')

    def test_invalid_settings_fail_closed(self):
        for key, value in [('health_failure_seconds', True), ('health_repeat_seconds', 59),
                           ('health_failure_seconds', float('nan'))]:
            self.config[key] = value
            self.poll(0)
            self.assertEqual(self.s.status, 'invalid_settings')
            self.assertEqual(self.executor.submissions, 0)

    def test_invalid_clock_cancels(self):
        self.queue_fault()
        self.poll(float('nan'))
        self.executor.run_next()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.s.status, 'invalid_clock')

    def test_backwards_poll_cannot_recover(self):
        self.queue_fault()
        self.sample(healthy=True)
        self.poll(29)
        self.executor.run_next()
        self.assertEqual(self.calls[0][2].kind, 'fault')

    def test_invalid_generation_cannot_arm(self):
        for generation in (None, True, -1, '1'):
            self.sample(generation=generation)
            self.poll(0)
            self.poll(30)
            self.assertEqual(self.executor.submissions, 0)

    def test_configured_importance_reaches_sender_and_is_a_scope(self):
        self.config['notify_importance'] = 'critical'
        self.poll(0)
        self.poll(30)
        self.executor.run_next()
        self.assertEqual(self.importances, ['critical'])
        self.config['notify_importance'] = 'urgent'
        self.poll(31)
        self.poll(61)
        self.executor.run_next()
        self.assertEqual(self.importances, ['critical', 'normal'])


class FakeResponse:
    def __init__(self, body, code=202):
        self.body = body
        self.code = code
        self.headers = {}
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]

    def close(self):
        pass


class FakeOpener:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.calls = []

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        if self.failure:
            raise self.failure
        return self.response


class TransportTests(unittest.TestCase):
    base = 'https://notify.invalid'

    def setUp(self):
        self.notice = Notice('vision', 'fault', 'vision_worker_failed', 1, 1, 7, 'a' * 32)
        self.cancel = threading.Event()
        self.alerts = []

    def fake_send(self, target, alert, cancel):
        self.alerts.append((target, alert))
        return NotifyResult('success', 'provider_accepted')

    def send(self, importance='critical', key='fake-key', base=None):
        with patch('smart_tapo.desktop_health_alerts.send_alert', side_effect=self.fake_send):
            return send_health(key, base or self.base, self.notice, self.cancel, importance)

    def test_fault_alert_contract(self):
        self.assertEqual(self.send(), 'success')
        target, alert = self.alerts[0]
        self.assertEqual((target.key, target.base_url), ('fake-key', self.base))
        self.assertEqual(alert.alert_id, 'health-' + 'a' * 32 + '-7')
        self.assertEqual(alert.idempotency_key, 'smarttapo:' + alert.alert_id)
        self.assertEqual(alert.importance, 'critical')
        self.assertEqual(alert.title, 'SmartTapo ระบบภาพ/เสียง')
        self.assertIn('vision_worker_failed', alert.message)

    def test_retry_of_same_notice_reuses_alert_identity(self):
        self.send()
        self.send()
        self.assertEqual(self.alerts[0][1].alert_id, self.alerts[1][1].alert_id)

    def test_monitor_notices_get_distinct_scopes(self):
        first, second = HealthMonitor(), HealthMonitor()
        self.assertNotEqual(first._scope, second._scope)
        self.assertRegex(first._scope, '^[0-9a-f]{32}$')

    def test_recovery_is_always_normal(self):
        self.notice = Notice('audio', 'recovered', 'ok', 1, 1, 1, 'b' * 32)
        self.assertEqual(self.send('critical'), 'success')
        self.assertEqual(self.alerts[0][1].importance, 'normal')
        self.assertIn('กลับมาทำงานปกติแล้ว', self.alerts[0][1].message)

    def test_invalid_credentials_target_or_importance_no_transport(self):
        for key, base, importance in ((True, self.base, 'normal'), ('', self.base, 'normal'),
                                      ('fake-key', 'http://notify.invalid', 'normal'),
                                      ('fake-key', 'https://user:pass@notify.invalid', 'normal'),
                                      ('fake-key', self.base, 'urgent')):
            with self.subTest(base=base, importance=importance):
                self.assertEqual(self.send(importance, key, base), 'failed')
        self.assertEqual(self.alerts, [])

    def test_cancel_before_send_no_transport(self):
        self.cancel.set()
        self.assertEqual(self.send(), 'failed')
        self.assertEqual(self.alerts, [])

    def test_single_post_through_notify_client_without_redirects(self):
        body = json.dumps({'alert_id': 'health-' + 'a' * 32 + '-7', 'status': 'provider_accepted',
                           'expires_at': '2026-11-18T02:05:00Z', 'acknowledged': False,
                           'delivery_verified': False, 'importance': 'critical'}).encode()
        opener = FakeOpener(FakeResponse(body))
        with patch('smart_tapo.notify_api.request.build_opener', return_value=opener) as build:
            result = send_health('fake-key', self.base, self.notice, self.cancel, 'critical')
        self.assertEqual(result, 'success')
        self.assertEqual(len(opener.calls), 1)
        req, timeout = opener.calls[0]
        self.assertEqual(req.full_url, self.base + '/v1/alerts')
        self.assertEqual(timeout, 10)
        self.assertEqual(req.get_header('Authorization'), 'Bearer fake-key')
        self.assertEqual(req.get_header('Idempotency-key'), 'smarttapo:health-' + 'a' * 32 + '-7')
        self.assertIsInstance(build.call_args.args[0], _NoRedirect)

    def test_transport_failures_are_redacted_states(self):
        for failure, expected in ((TimeoutError('fake-key secret'), 'ambiguous'),
                                  (RuntimeError('fake-key secret'), 'ambiguous')):
            opener = FakeOpener(failure=failure)
            with patch('smart_tapo.notify_api.request.build_opener', return_value=opener):
                self.assertEqual(send_health('fake-key', self.base, self.notice, self.cancel), expected)
        failed = FakeOpener(error.HTTPError(self.base, 401, 'secret', {}, None))
        with patch('smart_tapo.notify_api.request.build_opener', return_value=failed):
            self.assertEqual(send_health('fake-key', self.base, self.notice, self.cancel), 'failed')


if __name__ == '__main__':
    unittest.main()


