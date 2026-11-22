"""Telemetry saturation must not manufacture a producer timeout."""
import math
import queue
import unittest
from unittest.mock import Mock

from smart_tapo.rtsp_audio import RtspContinuousListener, _progress


class ProgressFreshnessTests(unittest.TestCase):
    def runtime(self, now=120.):
        runtime = RtspContinuousListener(clock=lambda: now)
        runtime._process = Mock()
        runtime._process.is_alive.return_value = True
        runtime._progress = queue.Queue(maxsize=8)
        runtime._progress.close = Mock()
        runtime._recv = Mock()
        runtime._recv.poll.return_value = False
        runtime._stop = Mock()
        runtime._result = runtime._forced = runtime._stop_at = runtime._kill_at = None
        runtime._heartbeat = 100.
        return runtime

    def produce(self, runtime):
        for i in range(12):
            _progress(runtime._progress, dict(kind='listener_progress',
                heartbeat=119.8 if i == 11 else 100. + i * .5,
                frames=(i + 1) * 8000,
                pcm_session='producer-session', pcm_started_at=100.,
                stage='persisted', observations_total=i))

    def test_saturation_retains_latest_ordered_producer_telemetry(self):
        runtime = self.runtime()
        self.produce(runtime)
        events = runtime.poll()
        stamps = [event['heartbeat'] for event in events]
        self.assertEqual(len(events), 8)
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(type(stamp) is float and math.isfinite(stamp) for stamp in stamps))
        self.assertEqual(events[-1]['observations_total'], 11)
        self.assertEqual(events[-1]['frames'], 96000)
        self.assertEqual(events[-1]['pcm_session'], 'producer-session')

    def test_gui_pause_does_not_timeout_continuing_producer(self):
        runtime = self.runtime()
        self.produce(runtime)
        runtime.poll()
        runtime._stop.set.assert_not_called()
        self.assertIsNone(runtime._forced)
        self.assertAlmostEqual(runtime._heartbeat, 119.8)

    def test_stopped_producer_still_times_out(self):
        runtime = self.runtime()
        _progress(runtime._progress, dict(kind='listener_progress', heartbeat=101.))
        runtime.poll()
        runtime._stop.set.assert_called_once_with()
        self.assertEqual(runtime._forced, 'audio_timeout')

    def test_status_uses_same_freshness_policy(self):
        runtime = self.runtime()
        self.produce(runtime)
        _progress(runtime._progress, dict(kind='listener_status', heartbeat=120.,
            code='rtsp_disconnected', waiting_audio=True, observations_total=11))
        events = runtime.poll()
        self.assertEqual(events[-1]['code'], 'rtsp_disconnected')
        self.assertTrue(events[-1]['waiting_audio'])

    def test_feeder_full_empty_race_is_nonblocking(self):
        channel = Mock()
        channel.put_nowait.side_effect = queue.Full
        channel.get_nowait.side_effect = queue.Empty
        _progress(channel, {'kind': 'listener_progress', 'heartbeat': 120.})
        self.assertLessEqual(channel.put_nowait.call_count, 2)
        self.assertLessEqual(channel.get_nowait.call_count, 1)

    def test_consumer_refill_race_is_nonblocking(self):
        channel = Mock()
        channel.put_nowait.side_effect = queue.Full
        _progress(channel, {'kind': 'listener_progress', 'heartbeat': 120.})
        self.assertLessEqual(channel.put_nowait.call_count, 2)
        self.assertLessEqual(channel.get_nowait.call_count, 1)

    def test_final_error_pipe_preserves_code_and_cumulative_totals(self):
        runtime = self.runtime(now=101.)
        runtime._process.is_alive.return_value = False
        runtime._recv.poll.return_value = True
        runtime._recv.recv.return_value = dict(kind='error', code='storage_error',
            observations_total=42, ready_total=20, end_total=22, ambiguous_total=3)
        events = runtime.poll()
        self.assertEqual(events[-1]['code'], 'storage_error')
        self.assertEqual(events[-1]['observations_total'], 42)
        self.assertIsNone(runtime._process)


if __name__ == '__main__':
    unittest.main()
