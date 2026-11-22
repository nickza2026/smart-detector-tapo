"""Offline audio tests: every endpoint and PCM source is synthetic."""
import math
import multiprocessing
from pathlib import Path
import queue
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import wave

from smart_tapo.audio import (AudioProbe, ProbeError, capture_job, checked_directory,
                              loopback_devices, pcm_levels, synthetic_fixture_job)


DEVICE = dict(index=1, name='Output loopback', rate=8000, channels=2)


class Channel:
    def __init__(self):
        self.events = []
    def send(self, event):
        self.events.append(event)
    def put_nowait(self, event):
        self.events.append(event)
    def close(self):
        pass
    def cancel_join_thread(self):
        pass


class Stop:
    def is_set(self):
        return False


class FakeAudio:
    def __init__(self, *, silent=False, fail=False):
        self.opened = []
        self.silent = silent
        self.fail = fail
    def get_host_api_info_by_type(self, kind):
        return {'index': 7}
    def get_device_count(self):
        return 3
    def get_device_info_by_index(self, index):
        return dict(name='Output loopback' if index == 1 else 'Microphone',
                    isLoopbackDevice=index != 0, hostApi=7 if index != 2 else 8,
                    defaultSampleRate=8000, maxInputChannels=2)
    def open(self, **kwargs):
        self.opened.append(kwargs)
        if self.fail:
            raise RuntimeError('PRIVATE driver / credentials')
        return self
    def read(self, count, **kwargs):
        return struct.pack('<h', 0 if self.silent else 32767) * count * 2
    def close(self):
        pass
    def terminate(self):
        pass


class FakeAPI:
    paWASAPI = 13
    paInt16 = 8
    def __init__(self, audio):
        self.audio = audio
    def PyAudio(self):
        return self.audio


def synthetic_capture_job(final,progress,stop,device,staging,duration):
    """Frozen/spawn smoke target: pure generated PCM, never imports PortAudio."""
    capture_job(final,progress,stop,device,staging,duration,FakeAPI(FakeAudio()))


class AudioTests(unittest.TestCase):
    def run_job(self, audio, *, device=DEVICE, staging=None):
        final, progress = Channel(), Channel()
        capture_job(final, progress, Stop(), device, staging, .02, FakeAPI(audio))
        return final.events[0], progress.events

    def test_construction_does_nothing(self):
        with patch('multiprocessing.get_context', side_effect=AssertionError):
            probe = AudioProbe()
            self.assertFalse(probe.is_running)
            self.assertEqual(probe.poll(), [])
            probe.close()
            with self.assertRaises(ValueError):
                probe.start_list()

    def test_pcm_levels(self):
        rms, peak, clipped = pcm_levels(struct.pack('<hh', 16384, -16384))
        self.assertAlmostEqual(rms, -6.020599913)
        self.assertAlmostEqual(peak, rms)
        self.assertFalse(clipped)
        self.assertEqual(pcm_levels(b'\0\0' * 8), (None, None, False))
        self.assertTrue(pcm_levels(struct.pack('<h', -32768))[2])
        with self.assertRaises(ProbeError):
            pcm_levels(b'x')

    def test_only_wasapi_loopback(self):
        self.assertEqual(loopback_devices(FakeAudio(), FakeAPI(None)), [DEVICE])

    def test_changed_endpoint_never_falls_back(self):
        audio = FakeAudio()
        result, _ = self.run_job(audio, device=dict(DEVICE, name='old output'))
        self.assertEqual(result, {'kind': 'error', 'code': 'device_changed'})
        self.assertFalse(audio.opened)

    def test_driver_exception_sanitized(self):
        result, _ = self.run_job(FakeAudio(fail=True))
        self.assertEqual(result, {'kind': 'error', 'code': 'audio_unavailable'})

    def test_monitor_silence_and_clipping(self):
        result, levels = self.run_job(FakeAudio(silent=True))
        self.assertTrue(result['silent'])
        self.assertIsNone(result['path'])
        self.assertEqual(result['frames'], 160)
        self.assertIsNone(levels[0]['rms_dbfs'])
        result, levels = self.run_job(FakeAudio())
        self.assertFalse(result['silent'])
        self.assertTrue(levels[0]['clipped'])

    def test_pcm_wav_native_header_and_exclusive_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            staging = str(Path(temp) / 'clip.partial')
            result, _ = self.run_job(FakeAudio(), staging=staging)
            self.assertEqual(result['kind'], 'finished')
            with wave.open(staging, 'rb') as reader:
                self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (8000, 2, 2))
                self.assertEqual(reader.getnframes(), 160)
                self.assertEqual(len(reader.readframes(160)), 640)
            original = Path(staging).read_bytes()
            result, _ = self.run_job(FakeAudio(), staging=staging)
            self.assertEqual(result['kind'], 'error')
            self.assertEqual(Path(staging).read_bytes(), original)

    def test_clean_explicit_stop_wav_header(self):
        class StopAfterChunk:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 1
        with tempfile.TemporaryDirectory() as temp:
            staging = str(Path(temp) / 'stopped.partial')
            final = Channel()
            capture_job(final, Channel(), StopAfterChunk(), DEVICE, staging, 1, FakeAPI(FakeAudio()))
            self.assertEqual(final.events[0]['frames'], 1024)
            with wave.open(staging, 'rb') as reader:
                self.assertEqual(reader.getnframes(), 1024)

    def test_storage_count_and_total_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i in range(20):
                (root / str(i)).touch()
            with self.assertRaises(ProbeError):
                checked_directory(root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (root / 'large').open('wb') as file:
                file.truncate(33 * 1024 * 1024)
            with self.assertRaises(ProbeError):
                checked_directory(root)

    def test_reparse_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            real = Path.lstat
            class Info:
                st_mode = 0o040755
                st_file_attributes = 0x400
            with patch.object(Path, 'lstat', lambda path: Info() if path == Path(temp) else real(path)):
                with self.assertRaises(ProbeError):
                    checked_directory(Path(temp))

    def test_record_finalized_only_after_reap_and_unique(self):
        real = multiprocessing.get_context('spawn')
        class Process:
            exitcode = 0
            alive = True
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args, api=FakeAPI(FakeAudio()))
            def is_alive(self): return self.alive
            def join(self, timeout): pass
            def close(self): pass
        class Context:
            Pipe = staticmethod(real.Pipe)
            Queue = staticmethod(real.Queue)
            Event = staticmethod(real.Event)
        Context.Process = Process
        with tempfile.TemporaryDirectory() as temp:
            probe = AudioProbe(context=Context())
            paths = []
            for _ in range(2):
                probe.start_capture(DEVICE, Path(temp), duration=.02)
                staging = probe._staging
                self.assertTrue(staging.exists())
                self.assertFalse(list(Path(temp).glob('*.wav')) if not paths else False)
                self.assertFalse(any(e['kind'] == 'finished' for e in probe.poll()))
                self.assertTrue(probe.is_running)
                probe._process.alive = False
                result = probe.poll()[-1]
                self.assertEqual(result['kind'], 'finished')
                paths.append(result['path'])
                self.assertFalse(staging.exists())
                with wave.open(result['path'], 'rb') as reader:
                    self.assertEqual(reader.getnframes(), 160)
            self.assertNotEqual(*paths)
            self.assertEqual(len(list(Path(temp).glob('*.wav'))), 2)
            probe.start_capture(DEVICE, Path(temp), duration=.02)
            staging = probe._staging
            probe._process.alive = False
            probe._process.exitcode = 1
            self.assertEqual(probe.poll()[-1], {'kind': 'error', 'code': 'worker_failed'})
            self.assertFalse(staging.exists())
            self.assertEqual(len(list(Path(temp).glob('*.wav'))), 2)

    def test_validation(self):
        probe = AudioProbe()
        for device in (None, {}, {'index': 0, 'name': ''}):
            with self.assertRaises(ValueError):
                probe.start_capture(device)
        for duration in (0, 16, math.nan):
            with self.assertRaises(ValueError):
                probe.start_capture(DEVICE, duration=duration)

    def test_result_arriving_between_initial_poll_and_reap_is_not_lost(self):
        probe=AudioProbe(clock=lambda:0)
        probe._process=Mock(exitcode=0)
        probe._process.is_alive.return_value=False
        probe._progress=Mock()
        probe._progress.get_nowait.side_effect=queue.Empty
        probe._recv=Mock()
        probe._recv.poll.side_effect=[False,True]
        probe._recv.recv.return_value={'kind':'devices','devices':[]}
        probe._result=None;probe._forced=None;probe._stop_at=None;probe._kill_at=None;probe._deadline=10
        self.assertEqual(probe.poll(),[{'kind':'devices','devices':[]}])
        self.assertFalse(probe.is_running)

    def test_offline_spawn_reaps_once(self):
        probe = AudioProbe()
        probe._start(synthetic_fixture_job, (), 5)
        with self.assertRaises(ValueError):
            probe.start_list()
        events = []
        limit = time.monotonic() + 8
        while probe.is_running and time.monotonic() < limit:
            events.extend(probe.poll())
            time.sleep(.01)
        try:
            self.assertFalse(probe.is_running)
            self.assertEqual(events, [{'kind': 'devices', 'devices': []}])
            self.assertEqual(probe.poll(), [])
        finally:
            if probe.is_running:
                probe._process.kill()
                probe._process.join(1)

    def test_timeout_and_stop_owned_process_only(self):
        class Process:
            exitcode = None
            alive = True
            terminated = False
            killed = False
            def is_alive(self): return self.alive
            def terminate(self): self.terminated = True
            def kill(self):
                self.killed = True
                self.alive = False
                self.exitcode = -1
            def join(self, timeout): self.join_timeout = timeout
            def close(self): pass
        class Receiver:
            def poll(self): return False
            def close(self): pass
        class Event:
            def set(self): pass
        now = [0]
        probe = AudioProbe(clock=lambda: now[0])
        process = Process()
        probe._process = process
        probe._recv = Receiver()
        probe._progress = queue.Queue()
        probe._progress.close = lambda: None
        probe._stop = Event()
        probe._result = probe._forced = probe._stop_at = probe._kill_at = None
        probe._deadline = 10
        now[0] = 10
        self.assertEqual(probe.poll(), [])
        self.assertTrue(probe.is_running)
        now[0] = 12
        probe.poll()
        self.assertTrue(process.terminated)
        now[0] = 13
        self.assertEqual(probe.poll(), [{'kind': 'error', 'code': 'timeout'}])
        self.assertTrue(process.killed)
        self.assertEqual(process.join_timeout, 0)
        self.assertFalse(probe.is_running)


if __name__ == '__main__':
    unittest.main()
