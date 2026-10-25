import logging
import os
import queue
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from smart_tapo.config import Config
from smart_tapo.desktop_controller import DesktopController, _child
from smart_tapo.runtime import ProcessLock


class DesktopControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(data_dir=root / 'data', baseline_path=root / 'missing.png')
        self.controller = DesktopController()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.controller.stop_all()
        deadline = time.monotonic() + 15
        while any(self.controller.is_running(r) for r in ('capture', 'worker')) and time.monotonic() < deadline:
            self.controller.snapshot()
            time.sleep(.02)
        for record in self.controller._roles.values():
            process = record['process']
            if process is not None:
                process.join(timeout=5)
        self.controller.snapshot()

    def wait_state(self, role, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.controller.snapshot()[role]['state']
            if state in expected:
                return state
            time.sleep(.02)
        self.fail('Child did not reach expected state')

    def test_spawn_failure_without_camera_or_restart(self):
        self.assertTrue(self.controller.start_worker(self.config))
        self.assertFalse(self.controller.start_worker(self.config))
        self.assertEqual(self.wait_state('worker', {'failed'}), 'failed')
        self.assertIsNone(self.controller.snapshot()['worker']['pid'])
        self.assertEqual(self.controller.snapshot()['capture']['state'], 'stopped')
        self.assertFalse(self.controller.is_running('worker'))

    def test_real_spawn_worker_stop_and_duplicate(self):
        from PIL import Image
        baseline = Path(self.temp.name) / 'baseline.png'
        Image.new('RGB', self.config.expected_size).save(baseline)
        config = replace(self.config, baseline_path=baseline, alignment_enabled=False)
        self.assertTrue(self.controller.start_worker(config))
        self.assertEqual(self.wait_state('worker', {'running', 'failed'}), 'running')
        self.assertFalse(self.controller.start_worker(config))
        self.controller.stop('worker')
        self.assertTrue(self.controller.is_running('worker'))
        self.assertEqual(self.wait_state('worker', {'stopped', 'failed'}), 'stopped')
        self.assertTrue((config.data_dir / 'worker.sqlite3').exists())

    def test_existing_worker_lock_not_stolen(self):
        with ProcessLock(self.config.data_dir / 'worker.lock'):
            self.controller.start_worker(self.config)
            self.assertEqual(self.wait_state('worker', {'failed'}), 'failed')
        with ProcessLock(self.config.data_dir / 'worker.lock'):
            pass

    def test_credential_requirement_and_safe_start_failure(self):
        with self.assertRaises(ValueError):
            self.controller.start_worker(replace(self.config, notify_enabled=True), 'secret', '')
        with patch.object(self.controller._context, 'Process', side_effect=RuntimeError('secret')):
            self.assertFalse(self.controller.start_worker(self.config))
        self.assertNotIn('secret', repr(self.controller.drain_messages()))
        self.assertEqual(self.controller.snapshot()['worker']['state'], 'failed')

    def test_child_environment_isolation_and_redaction(self):
        root = logging.getLogger()
        old_handlers, old_level = list(root.handlers), root.level
        self.addCleanup(setattr, root, 'handlers', old_handlers)
        self.addCleanup(root.setLevel, old_level)
        messages = queue.Queue(64)
        stop = self.controller._context.Event()
        stop.set()
        class FakeWorker:
            def __init__(self, config):
                self.assert_environment = None
                if 'PUSHOVER_TOKEN' in os.environ or 'PUSHOVER_USER' in os.environ:
                    raise AssertionError('ambient credentials retained')
            def close(self):
                pass
        with patch.dict(os.environ, {'PUSHOVER_TOKEN': 'ambient-secret', 'PUSHOVER_USER': 'ambient-user'}):
            with patch('smart_tapo.worker.Worker', FakeWorker):
                _child('worker', self.config, stop, messages, 'ignored-secret', 'ignored-user')
        values = []
        while not messages.empty():
            values.append(messages.get_nowait())
        self.assertIn(('worker', 'ready'), values)
        self.assertNotIn('secret', repr(values))

    def test_stop_idle_and_role_validation(self):
        self.controller.stop_all()
        self.assertEqual(self.controller.drain_messages(), [])
        with self.assertRaises(ValueError):
            self.controller.stop('invalid')
