"""Live proof must still be valid after the publication fence is acquired."""
import io
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from smart_tapo.capture import _capture
from smart_tapo.capture.coordination import navigation_lock
from smart_tapo.capture.live_verifier import LiveVerifier
from smart_tapo.capture.page_mode import PageMode
from smart_tapo.config import Config, Rect


class PublicationProofTests(unittest.TestCase):
    def run_wait_case(self, negative):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), data_dir=Path(directory), adb_serial='proof-test',
                             camera_name='Camera', expected_size=(4,4), video=Rect(0,0,4,4),
                             roi=Rect(0,0,2,2), clock_roi=Rect(0,0,2,1))
            now = [0.0]
            attempting = threading.Event()
            stream = io.BytesIO()
            Image.new('RGB', (4,4)).save(stream, format='PNG')
            class Fake:
                def connect(self, stop): pass
                def ensure_live(self): pass
                def run(self, *args): return stream.getvalue()
            verifier = LiveVerifier(lambda: True, 10)
            pending = Future()
            if negative:
                verifier.pending = pending
                verifier.started_at = 0
            @contextmanager
            def publication_lock(serial, timeout):
                attempting.set()
                with navigation_lock(serial, timeout): yield
            results, errors = [], []
            def capture():
                try:
                    results.append(_capture(config, threading.Event(), True, Fake(), lambda: now[0], verifier))
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=capture)
            try:
                with patch('smart_tapo.capture.navigation_lock', publication_lock), patch('smart_tapo.capture.publish_frame') as publish:
                    with self.assertLogs('smart_tapo.capture', level='WARNING') as logs:
                        with navigation_lock(config.adb_serial, 1):
                            thread.start()
                            self.assertTrue(attempting.wait(1), 'capture never reached publication lock')
                            if negative:
                                pending.set_result(PageMode('unknown', 'live_refresh_required'))
                            else:
                                now[0] = 10.0
                        thread.join(2)
                        publish.assert_not_called()
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    publish.assert_not_called()
                    self.assertEqual(results, [1])
                    self.assertTrue(any(('live_refresh_required' if negative else 'publication paused') in line for line in logs.output))
            finally:
                thread.join(3)
                verifier.close()

    def test_expiry_while_waiting_for_navigation_lock(self):
        self.run_wait_case(False)

    def test_negative_completion_while_waiting_for_navigation_lock(self):
        self.run_wait_case(True)
