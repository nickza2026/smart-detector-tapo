import threading
import unittest

from smart_tapo.capture.live_verifier import LiveVerifier


class LiveVerifierTests(unittest.TestCase):
    def test_pending_progress_expiry_and_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def verify():
            calls.append(1)
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test barrier timed out')
            return True
        verifier = LiveVerifier(verify, 10)
        verifier.establish(0)
        try:
            self.assertTrue(verifier.check(5))
            self.assertTrue(entered.wait(3))
            for now in (6, 7, 9):
                self.assertTrue(verifier.check(now))
            self.assertFalse(verifier.check(10))
            self.assertFalse(verifier.check(11))
            self.assertEqual(calls, [1])
            release.set()
            verifier.pending.result(timeout=3)
            self.assertTrue(verifier.check(12))
        finally:
            release.set()
            verifier.close()
        self.assertIsNone(verifier.executor)

    def test_negative_and_exception_invalidate_and_reset(self):
        for result in (False, RuntimeError('private detail')):
            def verify():
                if isinstance(result, Exception):
                    raise result
                return result
            verifier = LiveVerifier(verify, 10)
            verifier.establish(0)
            try:
                verifier.check(5)
                try:
                    verifier.pending.result(timeout=3)
                except RuntimeError:
                    pass
                with self.assertRaises(ValueError) as error:
                    verifier.check(6)
                self.assertNotIn('private detail', str(error.exception))
                self.assertEqual(verifier.verified_at, float('-inf'))
                verifier.reset()
                verifier.establish(20)
                self.assertTrue(verifier.check(20))
            finally:
                verifier.close()

    def test_close_joins_owned_check(self):
        entered, release, finished = (threading.Event() for _ in range(3))
        def verify():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test barrier timed out')
            finished.set()
            return True
        verifier = LiveVerifier(verify, 10)
        verifier.establish(0)
        verifier.check(5)
        self.assertTrue(entered.wait(3))
        release.set()
        verifier.close()
        self.assertTrue(finished.is_set())
        self.assertIsNone(verifier.pending)
