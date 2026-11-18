import tempfile
import unittest
import multiprocessing
from dataclasses import replace
from pathlib import Path

from smart_tapo.ram_frames import FrameChannel, MemoryFrame
from smart_tapo.desktop_controller import DesktopController
from smart_tapo.worker import Worker
from smart_tapo.vision import Observation
from tests.test_rtsp import config_for, png
from smart_tapo.capture.rtsp import sign_rtsp_proof
import time
import queue
import io
from unittest.mock import patch
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.history import get_images, list_events
from smart_tapo.evidence_budget import EvidenceStorageError


def _publish_then_exit(channel, envelope):
    channel.publish(envelope)


def _wait_for_stop(stop):
    stop.wait(10)


class RamTests(unittest.TestCase):
    def test_desktop_default_and_empty_stream(self):
        self.assertEqual(DesktopController.video_storage_mode, 'ram')
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            class Detector:
                def observe_image(self, image):
                    return Observation('EMPTY')
            now = [time.time(), time.monotonic()]
            worker = Worker(config, detector=Detector(), clock=lambda: now[0])
            statements = []
            worker.db.set_trace_callback(statements.append)
            try:
                with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: now[1]):
                    for sequence in range(1, 1506):
                        now[0] += .2
                        now[1] += .2
                        wall, received = now
                        content = png()
                        proof = sign_rtsp_proof(config, content, 'test', sequence, received, wall, sequence*.2)
                        envelope = MemoryFrame.create(1, config, content, 'test', sequence, wall, received, proof)
                        worker.process_memory(envelope, generation=1)
                        if sequence % 60 == 0:
                            worker.cleanup()
                self.assertFalse(any('frames' in sql.lower() for sql in statements))
                self.assertFalse(any(sql.startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements))
                self.assertEqual(worker._memory_sequence, 1505)
                self.assertFalse(hasattr(worker, '_memory_retired'))
                self.assertEqual(worker.db.execute('SELECT count(*) FROM frames').fetchone()[0], 0)
                self.assertFalse(list(Path(directory).rglob('*.png')))
                self.assertFalse(list(Path(directory).rglob('*.ready.json')))
                self.assertFalse(list(Path(directory).rglob('*.tmp')))
            finally:
                worker.close()

    def test_receive_before_worker_start_cannot_acquire_new_scope(self):
        import threading
        from smart_tapo.capture.rtsp import capture_rtsp
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            channel = FrameChannel(multiprocessing.get_context('spawn'), queue.Queue(maxsize=2))
            stop = threading.Event()
            class Reader:
                calls = 0
                def __init__(self, config):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def get(self):
                    self.calls += 1
                    if self.calls > 1:
                        stop.set()
                        return None
                    received = time.monotonic() - .1
                    channel.fence()  # Worker starts after decoder received pixels
                    return ('test', 1, received, time.time()-.1, 1., True, 8, 8, png(), 0)
            capture_rtsp(config, stop, once=True, reader_factory=Reader, channel=channel)
            self.assertIsNone(channel.receive(timeout=.01))

    def test_current_generation_cannot_relabel_pre_scope_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            channel = FrameChannel(multiprocessing.get_context('spawn'), queue.Queue(maxsize=2))
            channel.fence()
            generation, cutoff = channel.snapshot()
            class Detector:
                calls = 0
                restart = False
                def observe_image(self, image):
                    self.calls += 1
                    if self.restart:
                        channel.fence()
                    return Observation('PRESENT')
            detector = Detector()
            worker = Worker(config, detector=detector)
            try:
                for received in (cutoff-.1, cutoff):
                    wall = time.time()
                    proof = sign_rtsp_proof(config, png(), 'test', 1, received, wall, 1.)
                    envelope = MemoryFrame.create(generation, config, png(), 'test', 1, wall, received, proof)
                    self.assertFalse(channel.publish(envelope))
                    self.assertFalse(worker.process_memory(envelope, generation=generation,
                                     fresh_after=cutoff, scope_check=channel.snapshot,
                                     generation_guard=channel.epoch.get_lock()))
                self.assertEqual(detector.calls, 0)
                self.assertEqual(worker.count, 0)
                self.assertIsNone(worker.active())
                time.sleep(.02)
                received, wall = time.monotonic(), time.time()
                proof = sign_rtsp_proof(config, png(), 'test', 2, received, wall, 2.)
                envelope = MemoryFrame.create(generation, config, png(), 'test', 2, wall, received, proof)
                detector.restart = True
                self.assertFalse(worker.process_memory(envelope, generation=generation,
                                 fresh_after=cutoff, scope_check=channel.snapshot,
                                 generation_guard=channel.epoch.get_lock()))
                self.assertEqual(detector.calls, 1)
                self.assertEqual(worker.count, 0)
                self.assertIsNone(worker.active())
            finally:
                worker.close()

    def test_controller_transport_close_is_explicit_and_idempotent(self):
        controller = DesktopController()
        manager_process = controller._frame_manager._process
        self.assertTrue(manager_process.is_alive())
        try:
            for role in ('capture', 'worker'):
                stop = controller._context.Event()
                process = controller._context.Process(target=_wait_for_stop, args=(stop,))
                messages = controller._context.Queue(maxsize=2)
                controller._roles[role].update(state='running', process=process, stop=stop, messages=messages)
                process.start()
                with self.assertRaises(RuntimeError):
                    controller.close()
                self.assertTrue(manager_process.is_alive())
                controller.stop_all()
                deadline = time.monotonic() + 5
                while controller.is_running(role) and time.monotonic() < deadline:
                    controller.snapshot()
                    time.sleep(.01)
                self.assertFalse(controller.is_running(role))
                self.assertTrue(manager_process.is_alive())
            controller.close()
            self.assertFalse(manager_process.is_alive())
            controller.close()
            controller.shutdown_transport()
            with self.assertRaises(RuntimeError):
                controller.start_capture(None)
        finally:
            controller.stop_all()
            for record in controller._roles.values():
                process = record['process']
                if process is not None:
                    process.join(timeout=5)
            controller.close()

    def test_capture_ram_never_spools_or_falls_back(self):
        import threading
        from smart_tapo.capture.rtsp import capture_rtsp
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            now = time.monotonic()
            class Reader:
                def __init__(self, config):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def get(self):
                    time.sleep(.02)  # strictly later than a coarse-clock fence
                    return ('test', 1, time.monotonic(), time.time(), 1., True, 8, 8, png(), 0)
            context = multiprocessing.get_context('spawn')
            channel = FrameChannel(context, queue.Queue(maxsize=2))
            channel.fence()
            self.assertEqual(capture_rtsp(config, threading.Event(), once=True,
                                         reader_factory=Reader, channel=channel), 0)
            self.assertIsInstance(channel.receive(), MemoryFrame)
            self.assertFalse((Path(directory) / 'frames').exists())
            with patch.object(channel, 'publish', side_effect=RuntimeError('channel unavailable')):
                with self.assertRaises(RuntimeError):
                    capture_rtsp(config, threading.Event(), once=True,
                                 reader_factory=Reader, channel=channel)
            self.assertFalse((Path(directory) / 'frames').exists())

    def test_queue_bounds_and_restart_fence(self):
        context = multiprocessing.get_context('spawn')
        with context.Manager() as manager:
            channel = FrameChannel(context, manager.Queue(maxsize=2))
            with tempfile.TemporaryDirectory() as directory:
                config = config_for(directory)
                generation = channel.fence()  # Capture first
                time.sleep(.02)
                envelope = MemoryFrame.create(generation, config, png() + bytes(1024 * 1024), 'test', 1,
                                              time.time(), time.monotonic(), {})
                # Producer exit without any consumer must not strand/truncate
                # the shared transport; its independently surviving peer can read.
                producer = context.Process(target=_publish_then_exit, args=(channel, envelope))
                producer.start()
                producer.join(timeout=5)
                self.assertEqual(producer.exitcode, 0)
                producer.close()
                self.assertTrue(channel.publish(envelope))
                self.assertFalse(channel.publish(envelope))
                worker_generation = channel.fence()  # Worker starts/restarts
                self.assertNotEqual(channel.receive().generation, worker_generation)
                channel.fence()  # Worker stops; Capture survives
                channel.fence()  # Worker starts before next Capture
                channel.fence()  # Capture stops/restarts; Worker survives
                self.assertNotEqual(channel.receive().generation, channel.generation)
                self.assertIsNone(channel.receive(timeout=.01))
                with patch('smart_tapo.ram_frames.MAX_PAYLOAD', 1):
                    with self.assertRaises(ValueError):
                        MemoryFrame.create(channel.generation, config, png(), 'test', 2,
                                           time.time(), time.monotonic(), {})

    def test_expiry_during_inference_and_generation_commit_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            now = [time.time(), time.monotonic()]
            class Detector:
                def observe_image(self, image):
                    now[0] += 10
                    now[1] += 10
                    return Observation('PRESENT')
            worker = Worker(config, detector=Detector(), clock=lambda: now[0])
            try:
                proof = sign_rtsp_proof(config, png(), 'test', 1, now[1], now[0], 1.)
                envelope = MemoryFrame.create(1, config, png(), 'test', 1, now[0], now[1], proof)
                with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: now[1]):
                    self.assertFalse(worker.process_memory(envelope, generation=1))
                self.assertIsNone(worker.active())
                self.assertEqual(worker.count, 0)
            finally:
                worker.close()

    def test_legacy_and_ram_temporal_history_equivalence(self):
        with tempfile.TemporaryDirectory() as ram_dir, tempfile.TemporaryDirectory() as spool_dir:
            configs = [replace(config_for(directory), stuck_consecutive_frames=10,
                               clear_consecutive_frames=2, notify_enabled=False)
                       for directory in (ram_dir, spool_dir)]
            now = [time.time(), time.monotonic()]
            label = ['PRESENT']
            class Detector:
                def observe_image(self, image):
                    return Observation(label[0], roi_png=png((4, 4)))
                def observe(self, image):
                    return self.observe_image(image)
            workers = [Worker(config, detector=Detector(), clock=lambda: now[0]) for config in configs]
            try:
                with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: now[1]):
                    for sequence in range(1, 13):
                        now[0] += 1
                        now[1] += 1
                        label[0] = 'PRESENT' if sequence <= 10 else 'EMPTY'
                        for index, (config, worker) in enumerate(zip(configs, workers)):
                            proof = sign_rtsp_proof(config, png(), 'test', sequence, now[1], now[0], sequence)
                            if index == 0:
                                envelope = MemoryFrame.create(1, config, png(), 'test', sequence, now[0], now[1], proof)
                                worker.process_memory(envelope, generation=1)
                            else:
                                marker = publish_frame(config, png(), 'test', sequence, True,
                                                       captured_at=now[0], captured_monotonic=now[1], source_proof=proof)
                                frame, image = read_frame(marker, config)
                                worker.process(frame, image, marker)
                summaries = []
                for config in configs:
                    event = list_events(config, now=now[0]).events[0]
                    summaries.append((event.kind, event.started, event.last_present, event.first_clear,
                                      event.closed, tuple(image.role for image in get_images(config, event.id, now=now[0]))))
                self.assertEqual(summaries[0], summaries[1])
                self.assertEqual(summaries[0][0], 'stuck')
            finally:
                for worker in workers:
                    worker.close()

    def test_events_history_gap_and_evidence_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(config_for(directory), stuck_consecutive_frames=10,
                             clear_consecutive_frames=2, notify_enabled=False)
            now = [time.time(), time.monotonic()]
            label = ['PRESENT']
            class Detector:
                def observe_image(self, image):
                    return Observation(label[0], roi_png=png((4, 4)))
            worker = Worker(config, detector=Detector(), clock=lambda: now[0])
            def feed(sequence, generation=1):
                now[0] += 1
                now[1] += 1
                proof = sign_rtsp_proof(config, png(), 'test', sequence, now[1], now[0], sequence)
                envelope = MemoryFrame.create(generation, config, png(), 'test', sequence, now[0], now[1], proof)
                return worker.process_memory(envelope, generation=generation)
            try:
                with patch('smart_tapo.capture.rtsp.time.monotonic', side_effect=lambda: now[1]):
                    for sequence in range(1, 6):
                        feed(sequence)
                    feed(7)  # dropped sample invalidates consecutive proof
                    self.assertEqual(worker.count, 1)
                    self.assertNotEqual(worker.active()['kind'], 'stuck')
                    for sequence in range(8, 17):
                        feed(sequence)
                    self.assertEqual(worker.active()['kind'], 'stuck')
                    label[0] = 'EMPTY'
                    feed(17)
                    feed(18)
                events = list_events(config, now=now[0]).events
                event = next(event for event in events if event.kind == 'stuck')
                self.assertIsNotNone(event.closed)
                self.assertEqual({image.role for image in get_images(config, event.id, now=now[0])},
                                 {'first', 'first_full', 'threshold', 'clear'})
                self.assertEqual(worker.db.execute('SELECT count(*) FROM frames').fetchone()[0], 0)
                worker.config = replace(config, max_image_storage_mb=.000001)
                with self.assertRaises(EvidenceStorageError):
                    worker.snapshot('new', 'first_full', png(), full_frame=True)
                self.assertEqual(worker.db.execute("SELECT count(*) FROM evidence WHERE event='new'").fetchone()[0], 0)
            finally:
                worker.close()

    def test_old_generation_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            class Detector:
                calls = 0
                def observe_image(self, image):
                    self.calls += 1
                    return Observation('EMPTY')
            detector = Detector()
            worker = Worker(config, detector=detector)
            try:
                wall, received = time.time(), time.monotonic()
                content = png()
                proof = sign_rtsp_proof(config, content, 'test', 1, received, wall, 1.)
                envelope = MemoryFrame.create(1, config, content, 'test', 1, wall, received, proof)
                worker.process_memory(envelope, generation=2)
                worker.process_memory(replace(envelope, png=content+b'bad'), generation=1)
                proof['signature'] = '0' * 64
                self.assertNotEqual(envelope.unpack(config).source_proof['signature'], proof['signature'])
                forged = MemoryFrame.create(1, config, content, 'test', 1, wall, received, proof)
                worker.process_memory(forged, generation=1)
                for invalid in (png((7, 8)), b'not PNG'):
                    signed = sign_rtsp_proof(config, invalid, 'test', 1, received, wall, 1.)
                    bad_image = MemoryFrame.create(1, config, invalid, 'test', 1, wall, received, signed)
                    worker.process_memory(bad_image, generation=1)
                worker.process_memory(envelope, generation=1, generation_check=lambda: 2)
                self.assertEqual(detector.calls, 1)
                self.assertEqual(worker.count, 0)
            finally:
                worker.close()
