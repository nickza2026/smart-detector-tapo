import json
import logging
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_tapo.capture import capture
from smart_tapo.capture_diagnostics import describe_capture
from smart_tapo.desktop_controller import _SafeLog, _EMERGENCY_HINTS, DesktopController
from tests import test_capture as fixtures

png, ui = fixtures.png, fixtures.ui


class CaptureDiagnosticsTests(unittest.TestCase):
    def test_validation_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fixtures.CaptureTests().config(directory)
            path = Path(directory) / 'capture_status.json'
            data = dict(target_interval_seconds=1, last_interval_seconds=1.284,
                        cycle_seconds=.2, frames=20, fresh_frames=12,
                        skipped_slots=3, captured_at=100)
            path.write_text(json.dumps(data))
            text = describe_capture(config, now=101)
            self.assertIn('1.284s', text)
            self.assertIn('12/20', text)
            self.assertNotIn('ข้อมูลเก่า', text)
            self.assertIn('ข้อมูลเก่า', describe_capture(config, now=110))
            for change in ({'frames': True}, {'cycle_seconds': float('nan')},
                           {'captured_at': 'secret'}, {'secret': 123},
                           {'last_interval_seconds': -1}, {'frames': 1.5}):
                path.write_text(json.dumps(dict(data, **change)))
                self.assertEqual(describe_capture(config, now=101), 'ยังไม่มีข้อมูลจังหวะรับภาพ')
            path.write_bytes(b'x' * 4097)
            self.assertEqual(describe_capture(config), 'ยังไม่มีข้อมูลจังหวะรับภาพ')
            path.unlink()
            other = Path(directory) / 'secret'
            other.write_text(json.dumps(data))
            path.symlink_to(other)
            self.assertEqual(describe_capture(config), 'ยังไม่มีข้อมูลจังหวะรับภาพ')

    def test_slow_capture_monotonic_snapshot_without_extra_clock_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fixtures.CaptureTests().config(directory)
            stop = threading.Event()
            ticks = iter([0, 0, 0, 0, 0, 2.4, 2.4, 2.4, 2.4, 3.1])
            class Fake:
                calls = 0
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args):
                    self.calls += 1
                    if self.calls == 2:
                        stop.set()
                    return png('white' if self.calls == 2 else 'black')
            with patch('smart_tapo.capture.time.time', side_effect=[100, 50]):
                self.assertEqual(capture(config, stop, adb=Fake(),
                                         monotonic=lambda: next(ticks)), 0)
            data = json.loads((Path(directory) / 'capture_status.json').read_text())
            self.assertEqual(data['frames'], 2)
            self.assertEqual(data['fresh_frames'], 1)
            self.assertEqual(data['skipped_slots'], 1)
            self.assertAlmostEqual(data['last_interval_seconds'], .7)
            self.assertAlmostEqual(data['cycle_seconds'], .7)
            self.assertEqual(data['captured_at'], 50)
            self.assertEqual(sorted(json.loads(p.read_text())['sequence'] for p in
                                    Path(directory).rglob('*.ready.json')), [1, 3])
            with self.assertRaises(StopIteration):
                next(ticks)

    def test_diagnostic_failure_does_not_fail_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fixtures.CaptureTests().config(directory)
            class Fake:
                def connect(self, stop): pass
                def ensure_live(self): pass
                def run(self, *args): return png()
            with patch('smart_tapo.capture.atomic_bytes', side_effect=OSError('secret')):
                with self.assertLogs('smart_tapo.capture', level='WARNING') as logs:
                    self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake()), 0)
            self.assertNotIn('secret', str(logs.output))
            self.assertEqual(len(list(Path(directory).rglob('*.ready.json'))), 1)

    def test_safe_emergency_protocol_and_hints(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        for kind in _EMERGENCY_HINTS:
            state = kind.removeprefix('emergency_')
            handler.emit(logging.LogRecord('test', logging.INFO, '', 0,
                                          'Emergency state: %s', (state,), None))
            self.assertEqual(messages.get_nowait(), ('worker', kind))
            messages.put_nowait(('worker', kind))
            text = ' '.join(controller.drain_messages())
            self.assertIn(_EMERGENCY_HINTS[kind], text)
        for args in [('secret-token',), ('active', 'secret'), ({'secret': 'active'},), ()]:
            handler.emit(logging.LogRecord('test', logging.WARNING, '', 0,
                                          'Emergency state: %s', args, None))
        self.assertTrue(messages.empty())
        handler.emit(logging.LogRecord('test', logging.WARNING, '', 0,
                                      'secret-token', (), None))
        self.assertEqual(messages.get_nowait(), ('worker', 'warning'))


if __name__ == '__main__':
    unittest.main()
