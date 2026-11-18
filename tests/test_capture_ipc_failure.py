import errno
import json
import pickle
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_rtsp import config_for, png
from smart_tapo.capture.rtsp import RtspReader, capture_rtsp
from smart_tapo.desktop_controller import _child, _SafeLog
from smart_tapo.audit_log import valid_detail, safe_runtime_failure
import logging
import multiprocessing
from collections import deque
from smart_tapo.ram_frames import FrameChannel
from smart_tapo.desktop_controller import DesktopController


class CaptureIpcFailureTests(unittest.TestCase):
    def test_decoder_ipc_errors_reap_then_backoff_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            for error in (EOFError('secret'), OSError(errno.EPIPE, 'secret'), pickle.UnpicklingError('secret')):
                clock = [10.]
                reader = RtspReader(config, clock=lambda: clock[0])
                reader.closed = False
                reader.last_progress = 10.
                reader.process = MagicMock()
                reader.output = MagicMock()
                reader.output.get.side_effect = error
                with patch.object(reader, '_reap') as reap, patch.object(reader, '_spawn') as spawn:
                    self.assertEqual(reader.get(), ('error',))
                    reap.assert_called_once()
                    spawn.assert_not_called()
                    self.assertEqual(reader.restart_at, 10.+config.rtsp_reconnect_seconds)
                    reader.process = None  # real _reap's postcondition
                    with patch('smart_tapo.capture.rtsp.time.sleep') as idle_wait:
                        self.assertIsNone(reader.get())
                        idle_wait.assert_called_once()
                    spawn.assert_not_called()
                    clock[0] = reader.restart_at
                    self.assertIsNone(reader.get())
                    spawn.assert_called_once()

    def test_permission_is_not_treated_as_decoder_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = RtspReader(config_for(directory), clock=lambda: 10.)
            reader.closed = False
            reader.last_progress = 10.
            reader.process = MagicMock()
            reader.output = MagicMock()
            reader.output.get.side_effect = PermissionError(errno.EACCES, 'secret')
            with patch.object(reader, '_reap') as reap:
                with self.assertRaises(PermissionError):
                    reader.get()
                reap.assert_not_called()

    def test_unrelated_queue_faults_are_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            for error in (OSError(errno.ENOSPC, 'secret'), OSError(errno.ENOMEM, 'secret'), ValueError('secret')):
                reader = RtspReader(config_for(directory), clock=lambda: 10.)
                reader.closed = False
                reader.last_progress = 10.
                reader.process = MagicMock()
                reader.output = MagicMock()
                reader.output.get.side_effect = error
                with patch.object(reader, '_reap') as reap, patch.object(reader, '_spawn') as spawn:
                    with self.assertRaises(type(error)):
                        reader.get()
                    reap.assert_not_called()
                    spawn.assert_not_called()

    def test_failed_decoder_row_fences_ram(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            channel = MagicMock()
            class Reader:
                def __init__(self, config):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def get(self):
                    stop.set()
                    return ('error',)
            capture_rtsp(config_for(directory), stop, channel=channel, reader_factory=Reader)
            channel.fence.assert_called_once()

    def test_unexpected_capture_failure_fences_and_records_only_safe_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            messages = queue.Queue()
            channel = MagicMock()
            with patch('smart_tapo.capture.rtsp.capture_rtsp', side_effect=PermissionError(errno.EACCES, 'rtsp://secret/token')):
                with self.assertRaises(SystemExit):
                    _child('capture', config, threading.Event(), messages, frame_channel=channel)
            channel.invalidate.assert_called_once()
            records = [json.loads(line) for line in (Path(directory)/'logs'/('capture-'+config.camera_id+'.jsonl')).read_text().splitlines()]
            details = [record['detail'] for record in records if record['kind'] == 'runtime_failure']
            self.assertEqual(details, [{'stage': 'capture', 'kind': 'permission', 'recoverable': False}])
            self.assertNotIn('secret', json.dumps(records))
            self.assertLess([r['kind'] for r in records].index('runtime_failure'), [r['kind'] for r in records].index('failed'))

    def test_publish_disk_full_is_fatal_not_reconnect_and_has_safe_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            channel = MagicMock()
            channel.snapshot.return_value = (1, 0.)
            class Reader:
                def __init__(self, config):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def get(self):
                    return ('test', 1, time.monotonic(), time.time(), 1., True, 8, 8, png(), 0)
            def controlled(config, stop, **kwargs):
                return capture_rtsp(config, stop, reader_factory=Reader, **kwargs)
            with patch('smart_tapo.capture.rtsp.capture_rtsp', side_effect=controlled), patch('smart_tapo.capture.rtsp.sign_rtsp_proof', side_effect=OSError(errno.ENOSPC, 'secret path')):
                with self.assertRaises(SystemExit):
                    _child('capture', config, threading.Event(), queue.Queue(), frame_channel=channel)
            records = [json.loads(line) for line in (Path(directory)/'logs'/('capture-'+config.camera_id+'.jsonl')).read_text().splitlines()]
            details = [record['detail'] for record in records if record['kind'] == 'runtime_failure']
            self.assertEqual(details, [{'stage': 'frame_publish', 'kind': 'disk_full', 'recoverable': False}])
            channel.invalidate.assert_called_once()
            channel.publish.assert_not_called()
            self.assertNotIn('secret', json.dumps(records))

    def test_fatal_invalidation_does_not_wait_for_worker_commit_lock(self):
        channel = FrameChannel(multiprocessing.get_context('spawn'), queue.Queue(maxsize=2))
        ready, release = threading.Event(), threading.Event()
        def hold_commit():
            with channel.epoch.get_lock():
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold_commit)
        thread.start()
        self.assertTrue(ready.wait(2))
        try:
            channel.invalidate()
            self.assertEqual(channel.snapshot(), (-1, float('inf')))
        finally:
            release.set()
            thread.join(2)
        channel.fence()
        self.assertGreaterEqual(channel.snapshot()[0], 1)
        self.assertLess(channel.snapshot()[1], float('inf'))

    def test_safe_log_and_gui_use_only_allowlisted_detail(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'capture')
        record = logging.LogRecord('test', logging.ERROR, 'secret path', 1,
                                   'Safe runtime failure', ('secret URL',), None)
        record.runtime_failure = safe_runtime_failure(RuntimeError('secret'), 'capture')
        handler.emit(record)
        record.runtime_failure = {'stage': 'secret', 'kind': 'RuntimeError', 'recoverable': False}
        handler.emit(record)
        controller = DesktopController.__new__(DesktopController)
        controller._roles = {'capture': dict(state='stopped', process=None, stop=None, messages=messages)}
        controller._messages = deque(maxlen=128)
        text = '\n'.join(controller.drain_messages())
        self.assertIn('[capture/unknown]', text)
        self.assertIn('ยังไม่ทราบสาเหตุ', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('RuntimeError', text)

    def test_audit_safe_failure_shape_rejects_untrusted_values(self):
        self.assertTrue(valid_detail('runtime_failure', {'stage': 'decoder_ipc', 'kind': 'decoder_ipc', 'recoverable': True}))
        self.assertFalse(valid_detail('runtime_failure', {'stage': 'rtsp://secret', 'kind': 'OSError', 'recoverable': True}))
