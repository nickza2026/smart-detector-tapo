import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import contextmanager

from smart_tapo.capture import Adb, DiagnosticCaptureError
from smart_tapo.capture.live_verifier import LiveVerifier, LiveVerificationError
from smart_tapo.capture.page_mode import PageMode
from smart_tapo.capture.ui_diagnostics import observation, valid_ui_check


class DiagnosticTests(unittest.TestCase):
    def test_schema_is_closed_and_numeric(self):
        detail = dict(observation(), state='failed', reason='adb_timeout')
        self.assertTrue(valid_ui_check(detail))
        for value in (True, -1.0, float('nan'), float('inf'), 86401.0, 1, 'secret'):
            self.assertFalse(valid_ui_check(dict(detail, dump_seconds=value)))
        self.assertFalse(valid_ui_check(dict(detail, secret='private')))
        self.assertFalse(valid_ui_check(dict(detail, reason='private')))

    def test_known_verification_error_survives_worker(self):
        def verify():
            raise LiveVerificationError('live_inactivity_confirmation')
        verifier = LiveVerifier(verify, 10)
        try:
            verifier.establish(0)
            verifier.check(5)
            verifier.pending.result(timeout=3)
            with self.assertLogs('smart_tapo.capture.live_verifier') as logs:
                with self.assertRaises(LiveVerificationError) as error:
                    verifier.check(6)
            self.assertEqual(error.exception.reason, 'live_inactivity_confirmation')
            self.assertEqual(logs.records[0].ui_check_context['reason'], error.exception.reason)
        finally:
            verifier.close()

    def test_pending_expiry_is_rate_limited_and_old_completion_not_valid(self):
        entered, release = threading.Event(), threading.Event()
        def verify():
            entered.set()
            release.wait(3)
            return True
        verifier = LiveVerifier(verify, 10)
        try:
            verifier.establish(0)
            verifier.check(5)
            self.assertTrue(entered.wait(3))
            with self.assertLogs('smart_tapo.capture.live_verifier') as logs:
                for now in (10, 10.5, 11, 14, 15):
                    self.assertFalse(verifier.check(now))
            self.assertEqual(len(logs.records), 2)
            release.set()
            verifier.pending.result(timeout=3)
            with self.assertLogs('smart_tapo.capture.live_verifier') as logs:
                self.assertFalse(verifier.check(20))
            self.assertNotIn('valid', [r.ui_check_context['state'] for r in logs.records])
        finally:
            release.set()
            verifier.close()

    def adapter(self, runner):
        return Adb(SimpleNamespace(adb_serial='test', adb_path='adb',
                   tapo_package='com.example.app', adb_timeout_seconds=15), runner=runner)

    def test_adb_errors_are_typed_and_private(self):
        failures = [(subprocess.TimeoutExpired('secret', 15, output=b'secret'), 'adb_timeout'),
                    (OSError('secret'), 'adb_unavailable'),
                    (SimpleNamespace(returncode=1, stdout=b'secret'), 'adb_nonzero')]
        for failure, reason in failures:
            def runner(*args, **kwargs):
                self.assertEqual(kwargs['timeout'], 15)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            with self.assertRaises(DiagnosticCaptureError) as error:
                self.adapter(runner).run('shell', 'anything')
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn('secret', str(error.exception))

    def test_phase_timeout_cleanup_and_snapshot(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv[3:])
            if 'cat' in argv:
                raise subprocess.TimeoutExpired('private', 15)
            return SimpleNamespace(returncode=0, stdout=b'UI hierarchy dumped')
        adb = self.adapter(runner)
        adb._ui_diagnostic.detail = observation()
        with self.assertRaises(DiagnosticCaptureError):
            adb.ui()
        detail = adb.ui_check_diagnostics()
        self.assertEqual(detail['stage'], 'ui_read')
        for field in ('lock_seconds', 'dump_seconds', 'read_seconds', 'cleanup_seconds'):
            self.assertIsInstance(detail[field], float)
        self.assertIsNone(detail['foreground_seconds'])
        self.assertEqual(len(calls), 3)
        detail['stage'] = 'private'
        self.assertEqual(adb.ui_check_diagnostics()['stage'], 'ui_read')

    def test_parser_and_cleanup_failures_remain_distinct(self):
        for cleanup_failure in (False, True):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if 'rm' in argv and cleanup_failure:
                    return SimpleNamespace(returncode=1, stdout=b'private')
                return SimpleNamespace(returncode=0, stdout=b'<broken' if 'cat' in argv else b'dumped')
            adb = self.adapter(runner)
            adb._ui_diagnostic.detail = observation()
            with self.assertRaises(DiagnosticCaptureError) as error:
                adb.ui()
            self.assertEqual(error.exception.reason, 'adb_nonzero' if cleanup_failure else 'ui_hierarchy_invalid')
            self.assertEqual(adb.ui_check_diagnostics()['stage'], 'ui_cleanup' if cleanup_failure else 'classify')
            self.assertEqual(len(calls), 3)

    def test_lock_failure_measured_without_adb(self):
        @contextmanager
        def busy(*args):
            raise ValueError('ui_dump_busy')
            yield
        adb = self.adapter(lambda *a, **k: self.fail('ADB must not run'))
        with patch('smart_tapo.capture.ui_lock', busy):
            with self.assertRaises(DiagnosticCaptureError) as error:
                adb.read_page_mode()
        self.assertEqual(error.exception.reason, 'ui_lock_busy')
        detail = adb.ui_check_diagnostics()
        self.assertEqual(detail['stage'], 'lock')
        self.assertIsInstance(detail['lock_seconds'], float)

    def test_page_rejection_and_unknown_exception_do_not_leak(self):
        for outcome, reason in ((PageMode('playback', 'observed_playback_layout'), 'observed_playback_layout'),
                                (RuntimeError('PRIVATE CAMERA XML'), 'verification_exception')):
            def verify():
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            verifier = LiveVerifier(verify, 10)
            try:
                verifier.establish(0)
                verifier.check(5)
                verifier.pending.result(timeout=3)
                with self.assertLogs('smart_tapo.capture.live_verifier') as logs:
                    with self.assertRaises(ValueError):
                        verifier.check(6)
                detail = logs.records[0].ui_check_context
                self.assertEqual(detail['reason'], reason)
                self.assertTrue(valid_ui_check(detail))
                self.assertNotIn('PRIVATE', repr(detail))
                self.assertEqual(verifier.verified_at, float('-inf'))
            finally:
                verifier.close()
