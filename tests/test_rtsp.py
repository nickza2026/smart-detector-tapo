import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from smart_tapo.config import Config, Rect
from smart_tapo.capture.rtsp import (EncodeCadence, PtsContinuity, RtspReader,
    StatusHeartbeat, _decoder_main, _latest_put, capture_rtsp,
    read_rtsp_reference, rtsp_source_fingerprint, sign_rtsp_proof,
    valid_rtsp_proof, validate_rtsp_url)
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.worker import Worker


def config_for(directory):
    config = replace(Config(), capture_backend='rtsp', data_dir=Path(directory),
                     expected_size=(8, 8), video=Rect(0, 0, 8, 8), roi=Rect(0, 0, 4, 4),
                     clock_roi=Rect(999, 999, 2, 2), stuck_consecutive_frames=2,
                     rtsp_url='rtsp://127.0.0.1:12345/synthetic')
    return replace(config, rtsp_source_binding=rtsp_source_fingerprint(config)).validate()


def png(size=(8, 8)):
    stream = io.BytesIO()
    Image.new('RGB', size).save(stream, 'PNG')
    return stream.getvalue()


def frame_for(config, session='synthetic', sequence=1, age=0):
    received, wall = time.monotonic()-age, time.time()-age
    content = png()
    proof = sign_rtsp_proof(config, content, session, sequence, received, wall, 10.+sequence)
    marker = publish_frame(config, content, session, sequence, True,
                           captured_at=wall, captured_monotonic=received, source_proof=proof)
    frame, image = read_frame(marker, config)
    return frame, image, marker


class RtspTests(unittest.TestCase):
    def test_png_cadence_never_throttles_decode_or_pts_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(config_for(directory), capture_interval_seconds=.5)
            state = {'count': 0}
            halt = threading.Event()
            decoder = MagicMock()
            def read():
                state['count'] += 1
                if state['count'] == 100:
                    halt.set()
                return True, SimpleNamespace(shape=(8, 8, 3))
            decoder.read.side_effect = read
            cv = SimpleNamespace(CAP_FFMPEG=1, CAP_PROP_OPEN_TIMEOUT_MSEC=2,
                                 CAP_PROP_READ_TIMEOUT_MSEC=3, CAP_PROP_FPS=4,
                                 CAP_PROP_PTS=5, VideoCapture=MagicMock(return_value=decoder),
                                 IMWRITE_PNG_COMPRESSION=16, IMWRITE_PNG_FILTER=18, IMWRITE_PNG_FILTER_NONE=8,
                                 imencode=MagicMock(return_value=(True, SimpleNamespace(tobytes=lambda: b'png'))))
            decoder.get.side_effect = lambda prop: 25. if prop == cv.CAP_PROP_FPS else state['count']
            output = queue.Queue(maxsize=1)
            output.cancel_join_thread = lambda: None
            tracker = SimpleNamespace(observe=MagicMock(return_value=True))
            with patch.dict('sys.modules', {'cv2': cv}), \
                 patch('smart_tapo.capture.rtsp.os.dup2'), \
                 patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: 100+state['count']*.04), \
                 patch('smart_tapo.capture.rtsp.PtsContinuity', return_value=tracker):
                _decoder_main(config, output, halt)
            self.assertEqual(decoder.read.call_count, 100)
            self.assertEqual(tracker.observe.call_count, 100)
            self.assertLessEqual(cv.imencode.call_count, 16)
            self.assertGreaterEqual(cv.imencode.call_count, 14)
            self.assertEqual(cv.imencode.call_args.args[2], [16, 0, 18, 8])
            row = output.get_nowait()
            self.assertGreaterEqual(row[1], 90)  # decoded includes unencoded frames
            self.assertEqual(row[9], cv.imencode.call_count-2)  # queue overwrites only
            decoder.release.assert_called_once()
            cadence = EncodeCadence(1.)
            self.assertTrue(cadence.due(0.))
            self.assertFalse(cadence.due(.2))
            self.assertTrue(cadence.due(.25))  # not a second one-second throttle

    def test_watchdog_reaps_stalled_and_dead_children_with_fresh_owned_resources(self):
        class FakeQueue(queue.Queue):
            def close(self): self.closed = True
            def cancel_join_thread(self): pass
        class FakeProcess:
            def __init__(self, **kwargs):
                self.alive = False
                self.closed = False
                self.terminated = False
            def start(self): self.alive = True
            def is_alive(self): return self.alive
            def join(self, timeout): pass
            def terminate(self): self.alive = False; self.terminated = True
            def kill(self): self.alive = False
            def close(self): self.closed = True
        class Context:
            def __init__(self): self.processes = []
            def Queue(self, maxsize): return FakeQueue(maxsize=maxsize)
            def Event(self): return threading.Event()
            def Process(self, **kwargs):
                process = FakeProcess(**kwargs)
                self.processes.append(process)
                return process
        for dead in (False, True):
            with self.subTest(dead=dead), tempfile.TemporaryDirectory() as directory:
                config = config_for(directory)
                context, now = Context(), [0.]
                reader = RtspReader(config, context=context, clock=lambda: now[0])
                with reader:
                    old_process, old_queue, old_halt = reader.process, reader.output, reader.halt
                    if dead:
                        old_process.alive = False
                        now[0] = .1
                    else:
                        now[0] = reader.watchdog_seconds+.1
                    self.assertEqual(reader.get(timeout=0), ('error',))
                    self.assertTrue(old_process.closed)
                    self.assertTrue(old_queue.closed)
                    self.assertTrue(old_halt.is_set())
                    self.assertEqual(old_process.terminated, not dead)
                    self.assertIsNone(reader.get(timeout=0))
                    self.assertEqual(len(context.processes), 1)  # backoff, no busy respawn
                    now[0] += config.rtsp_reconnect_seconds+.1
                    self.assertIsNone(reader.get(timeout=0))
                    self.assertEqual(len(context.processes), 2)
                    self.assertIsNot(reader.output, old_queue)
                    self.assertIsNot(reader.halt, old_halt)
                    reader.output.put_nowait(('progress',))
                    self.assertEqual(reader.get(timeout=0), ('progress',))
                    self.assertEqual(reader.last_progress, now[0])
                reader.__exit__()  # idempotent, no restart after stop
                now[0] += 100
                self.assertIsNone(reader.get(timeout=0))
                self.assertEqual(len(context.processes), 2)
                self.assertTrue(all(p.closed for p in context.processes))

    def test_status_heartbeat_healthy_reconnecting_and_exact_safe_context(self):
        statuses = []
        heartbeat = StatusHeartbeat(statuses.append)
        with self.assertLogs('smart_tapo.capture.rtsp', level='INFO') as logs:
            for now in (0., 1., 4.99, 5., 6., 10.):
                heartbeat.report('healthy', now, 'synthetic', 42, 3)
            heartbeat.report('reconnecting', 10.1, 'synthetic', 42, 3)
            heartbeat.report('reconnecting', 14., 'synthetic', 42, 3)
            heartbeat.report('reconnecting', 15.1, 'synthetic', 42, 3)
        self.assertEqual([s.reason for s in statuses], ['healthy']*3+['reconnecting']*2)
        for record in logs.records:
            self.assertEqual(record.msg, 'RTSP image status: %s')
            self.assertEqual(set(record.rtsp_context), {'reason', 'session_id', 'decoded', 'dropped'})
            self.assertNotIn('rtsp://', record.getMessage())

    def test_silent_reader_reports_waiting_not_healthy_every_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            now, statuses, stop = [0.], [], threading.Event()
            class Reader:
                def __init__(self, _): pass
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def get(self):
                    now[0] += 1
                    if now[0] >= 12:
                        stop.set()
                    return None
            with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: now[0]):
                self.assertEqual(capture_rtsp(config, stop, reader_factory=Reader,
                                             status_callback=statuses.append), 0)
            self.assertEqual([s.reason for s in statuses], ['starting', 'waiting', 'waiting', 'stopped'])
            self.assertTrue(all(s.decoded == 0 and s.dropped == 0 for s in statuses))

    def test_url_validation_never_echoes_input(self):
        for value in ['rtsp://u:secret@localhost/stream', 'http://localhost/live',
                      'rtsp://localhost/path?token=secret', 'rtsp://localhost/#secret',
                      'rtsp://localhost:99999/a', 'rtsp://localhost/\nsecret',
                      'rtsp://localhost/%73ecret', 'rtsp://localhost/'+'x'*4096]:
            with self.subTest(value=value[:20]), self.assertRaises(ValueError) as error:
                validate_rtsp_url(value)
            self.assertNotIn('secret', str(error.exception))
        self.assertEqual(validate_rtsp_url('rtsp://127.0.0.1:8554/tapo_c500'),
                         'rtsp://127.0.0.1:8554/tapo_c500')

    def test_pts_positive_and_fail_closed(self):
        tracker = PtsContinuity()
        values = [tracker.observe(i*.1, 100+i*.1) for i in range(15)]
        self.assertTrue(values[-1])
        for pts, received in [(1.4, 101.5), (1.3, 101.6), (None, 102), (7., 107.)]:
            self.assertFalse(tracker.observe(pts, received))
        burst = PtsContinuity()
        self.assertFalse(any(burst.observe(i*.1, 100+i*.001) for i in range(200)))

    def test_advancing_pts_in_same_receive_tick_does_not_break_continuity(self):
        tracker=PtsContinuity()
        # TCP batching plus a coarse Windows receive clock: distinct source
        # timestamps can share one receive tick. This is not duplicate PTS.
        values=[tracker.observe(i*.06,100+(i//2)*.12) for i in range(80)]
        self.assertTrue(all(values[-40:]),'Valid paced pairs must remain primed across equal receive ticks')

    def test_same_tick_support_does_not_accept_frozen_clock_burst_or_bad_pts(self):
        frozen=PtsContinuity()
        self.assertFalse(any(frozen.observe(i*.06,100.) for i in range(100)))
        burst=PtsContinuity()
        self.assertFalse(any(burst.observe(i*.06,100+(i//2)*.001) for i in range(200)))
        for bad_pts,bad_received in ((2.34,102.4),(2.28,102.4),(None,102.4),(2.4,102.27),(8.,108.)):
            with self.subTest(pts=bad_pts,received=bad_received):
                tracker=PtsContinuity()
                for i in range(40):tracker.observe(i*.06,100+(i//2)*.12)
                self.assertFalse(tracker.observe(bad_pts,bad_received))

    def test_network_stall_then_backlog_burst_keeps_priming(self):
        # Live relay sample: one frame delayed 0.45 s, then the backlog arrives at once.
        tracker=PtsContinuity()
        arrivals=[100+i*.06 for i in range(60)]
        arrivals+= [arrivals[-1]+.45]+[arrivals[-1]+.45+.002*k for k in range(1,8)]
        arrivals+= [100+i*.06 for i in range(68,140)]
        values=[tracker.observe(i*.06,received) for i,received in enumerate(arrivals)]
        self.assertTrue(all(values[20:]),'Bounded network jitter must not restart the proof session')

    def test_long_stall_and_faster_than_real_time_still_reset(self):
        stalled=PtsContinuity()
        for i in range(40):stalled.observe(i*.06,100+i*.06)
        self.assertFalse(stalled.observe(40*.06,100+39*.06+1.))
        replay=PtsContinuity()
        values=[replay.observe(i*.06,100+i*.04) for i in range(300)]
        self.assertFalse(any(values[60:]),'A 1.5x replay must never stay primed')

    def test_latest_only_backpressure(self):
        output = queue.Queue(maxsize=1)
        for i in range(1000):
            _latest_put(output, i)
        self.assertEqual(output.qsize(), 1)
        self.assertEqual(output.get_nowait(), 999)

    def test_source_proof_tampering_expiry_geometry_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            frame, image, _ = frame_for(config)
            self.assertTrue(valid_rtsp_proof(frame, config, image))
            self.assertFalse(valid_rtsp_proof(frame, config, image, monotonic=frame.captured_monotonic+3))
            self.assertFalse(valid_rtsp_proof(frame, replace(config, rtsp_url='rtsp://localhost/other'), image))
            self.assertFalse(valid_rtsp_proof(replace(frame, sequence=2), config, image))
            forged = dict(frame.source_proof, received=frame.captured_monotonic+.1)
            self.assertFalse(valid_rtsp_proof(replace(frame, source_proof=forged), config, image))
            self.assertFalse(valid_rtsp_proof(replace(frame, source_proof=None), config, image))
            image.write_bytes(png((9, 9)))
            self.assertFalse(valid_rtsp_proof(frame, config, image))

    def test_worker_audio_navigation_independent_and_untrusted_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            detector = SimpleNamespace(observe=lambda _: SimpleNamespace(label='PRESENT'))
            mono, wall = time.monotonic(), time.time()
            worker = Worker(config, detector=detector, clock=lambda: wall)
            try:
                with patch('smart_tapo.capture.rtsp.time.monotonic', return_value=mono), \
                     patch('smart_tapo.capture.rtsp.time.time', return_value=wall), \
                     patch('smart_tapo.worker.coordination.navigation_snapshot', side_effect=AssertionError('ADB used')), \
                     patch('smart_tapo.worker.ui_lock', side_effect=AssertionError('ADB lock used')):
                    first = frame_for(config, age=1.1)
                    worker.process(*first)
                    worker.process(*frame_for(config, sequence=2))
                    self.assertEqual(worker.count, 2)
                    self.assertEqual(worker.db.execute('SELECT status FROM notifications').fetchone()[0], 'shadow')
                    worker.process(*frame_for(config, session='reconnected'))
                    self.assertEqual(worker.count, 1)
                    bad_frame, image, marker = frame_for(config, session='untrusted')
                    worker.process(replace(bad_frame, source_proof=None), image, marker)
                    self.assertEqual(worker.count, 0)
            finally:
                worker.close()

    def test_slow_inference_rechecks_proof_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            frame, image, marker = frame_for(config)
            detector = SimpleNamespace(observe=lambda _: SimpleNamespace(label='PRESENT'))
            worker = Worker(config, detector=detector)
            try:
                mono = time.monotonic()
                with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=[mono, mono+3]):
                    worker.process(frame, image, marker)
                self.assertEqual(worker.count, 0)
                self.assertIsNone(worker.active())
            finally:
                worker.close()

    def test_error_and_geometry_mismatch_never_publish_and_logs_are_safe(self):
        for row, expected in [(('error',), 'reconnecting'),
                              (('session', 1, 0., 0., 1., True, 999, 999, b'', 0), 'geometry_changed')]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                config = config_for(directory)
                stop = threading.Event()
                class Reader:
                    def __init__(self, _): pass
                    def __enter__(self): return self
                    def __exit__(self, *_): pass
                    def get(self):
                        stop.set()
                        return row
                with self.assertLogs('smart_tapo.capture.rtsp', level='INFO') as logs:
                    self.assertEqual(capture_rtsp(config, stop, reader_factory=Reader), 0)
                self.assertIn(expected, '\n'.join(logs.output))
                self.assertNotIn(config.rtsp_url, '\n'.join(logs.output))
                self.assertFalse((config.data_dir/'frames').exists())

    def test_legacy_marker_history_readable_but_not_rtsp_alert_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            frame, image, marker = frame_for(config)
            payload = json.loads(marker.read_text())
            payload.pop('source_proof')
            marker.write_text(json.dumps(payload))
            old, _ = read_frame(marker, config)
            self.assertIsNone(old.source_proof)
            self.assertFalse(valid_rtsp_proof(old, config, image))

    def test_reference_adopts_geometry_and_capture_requires_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            class Reader:
                def __init__(self, _): pass
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def get(self):
                    return ('session', 1, time.monotonic(), time.time(), 1., True, 12, 10, png((12, 10)), 0)
            self.assertEqual(read_rtsp_reference(config, threading.Event(), reader_factory=Reader).size, (12, 10))
            self.assertEqual(capture_rtsp(replace(config, rtsp_source_binding=''), threading.Event(), once=True), 1)
            stop = threading.Event()
            stop.set()
            self.assertEqual(capture_rtsp(config, stop, reader_factory=Reader), 0)

    def test_capture_publication_and_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            class Reader:
                def __init__(self, _): pass
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def get(self):
                    return ('session', 10, time.monotonic(), time.time(), 3., True, 8, 8, png(), 7)
            self.assertEqual(capture_rtsp(config, threading.Event(), once=True, reader_factory=Reader), 0)
            marker = next((config.data_dir/'frames'/config.camera_id).glob('*.ready.json'))
            frame, image = read_frame(marker, config)
            self.assertTrue(valid_rtsp_proof(frame, config, image))


if __name__ == '__main__':
    unittest.main()
