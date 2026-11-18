"""Finite recovery state and advancing-silent-PCM success, no physical input."""
import unittest
from smart_tapo.audio_recovery import AudioRecovery


class AudioRecoveryTests(unittest.TestCase):
    def test_three_attempts_only_and_repeated_failures_do_not_slide_deadline(self):
        state = AudioRecovery()
        now = 0.
        for count in range(3):
            state.fail(now)
            state.fail(now + 1)
            self.assertEqual(state.due, now + 15)
            self.assertFalse(state.take_due(now + 14.99))
            self.assertTrue(state.take_due(now + 15))
            self.assertFalse(state.take_due(now + 15))
            self.assertEqual(state.attempts, count + 1)
            now += 15
        state.fail(now)
        self.assertFalse(state.pending)
        self.assertFalse(state.take_due(now + 1000))

    def test_one_good_or_replayed_proof_does_not_reset_budget(self):
        state = AudioRecovery()
        state.fail(0); state.take_due(15)
        for _ in range(100): state.observe_good(16)
        self.assertEqual(state.attempts, 1)
        state.observe_good(25)  # >8 second gap restarts the stable window.
        state.observe_good(30)
        self.assertTrue(state.pending)
        self.assertEqual(state.good_since, 25)

    def test_thirty_seconds_advancing_proof_rearms_future_incident(self):
        state = AudioRecovery()
        state.fail(0); state.take_due(15)
        for stamp in range(16, 47, 5): state.observe_good(stamp)
        self.assertFalse(state.pending)
        self.assertEqual(state.attempts, 0)
        state.fail(100)
        self.assertEqual(state.due, 115)

    def test_manual_reset_cancels_due_attempt(self):
        state = AudioRecovery()
        state.fail(0); state.reset()
        self.assertFalse(state.take_due(100))
