"""Device-free regressions for short publication fences and safe diagnostics."""
import io
import tempfile
import threading
import time
from smart_tapo.config import Rect
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from smart_tapo.capture import _capture, Adb
from smart_tapo.capture.live_verifier import LiveVerifier
from smart_tapo.capture.page_mode import PageMode, classify_page, LIVE_ACTIVITY
from smart_tapo.capture.ui_lock import ui_lock
from tests.test_live_recovery import live, RecoveryTests


class CaptureContentionTests(unittest.TestCase):
    def test_publication_does_not_wait_for_readonly_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(RecoveryTests().config(directory), roi=Rect(0,0,2,2),
                             clock_roi=Rect(0,0,2,1))
            entered, release, published = (threading.Event() for _ in range(3))
            errors = []
            def reader():
                try:
                    with ui_lock(config.adb_serial, 1):
                        entered.set()
                        release.wait(2)
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=reader)
            stream = io.BytesIO(); Image.new('RGB', (4,4)).save(stream, format='PNG')
            class Fake:
                def run(self, *args):
                    thread.start()
                    if not entered.wait(1): raise RuntimeError('reader barrier')
                    return stream.getvalue()
                def connect(self, stop): pass
                def ensure_live(self): pass
            class Verifier:
                def establish(self, now): pass
                def check(self, now): return True
                def reset(self): pass
            def publish(*args, **kwargs):
                published.set(); return Path('synthetic.ready.json')
            result = []
            worker = threading.Thread(target=lambda: result.append(_capture(config, threading.Event(), True, Fake(), time.monotonic, Verifier())))
            try:
                with patch('smart_tapo.capture.publish_frame', publish):
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(published.wait(.4), 'publication blocked by read-only UI collection')
            finally:
                release.set(); worker.join(3); thread.join(3)
            self.assertEqual(errors, [])
            self.assertEqual(result, [0])

    def test_refresh_without_obscured_controls(self):
        xml = b'<hierarchy><node resource-id="com.tplink.iot:id/title" text="Camera" bounds="[0,0][4,4]"/><node text="Tap to refresh" bounds="[1,1][3,3]"/></hierarchy>'
        self.assertEqual(classify_page(xml, 'Camera', LIVE_ACTIVITY).reason, 'live_refresh_required')
        self.assertEqual(classify_page(xml.replace(b'Tap to refresh', b''), 'Camera', LIVE_ACTIVITY).reason, 'incomplete_live_layout')

    def test_obscured_overlay_still_rejects_unsafe_targets(self):
        xml = b'<hierarchy><node resource-id="com.tplink.iot:id/title" text="Camera" bounds="[0,0][4,4]"/><node text="Tap to refresh" bounds="[1,1][3,3]"/></hierarchy>'
        variants = [xml.replace(b'Camera', b'Wrong'),
                    xml.replace(b'</hierarchy>', b'<node password="true"/></hierarchy>'),
                    xml.replace(b'</hierarchy>', b'<node selected="true" text="Playback"/></hierarchy>'),
                    xml.replace(b'</hierarchy>', b'<node text="Tap to refresh" bounds="[1,1][3,3]"/></hierarchy>'),
                    xml.replace(b'[1,1][3,3]', b'[1,1][8,8]')]
        from smart_tapo.capture import CaptureError
        with tempfile.TemporaryDirectory() as directory:
            for candidate in variants:
                adb = RecoveryTests().adapter(RecoveryTests().config(directory), candidate)
                with self.assertRaises(CaptureError): adb.recover_live()
                self.assertEqual(adb.taps, 0)

    def test_async_mode_reason_is_safe_and_negative(self):
        for reason, expected in [('live_refresh_required', 'live_refresh_required'), ('private camera title', 'unrecognized_reason')]:
            verifier = LiveVerifier(lambda: True, 10)
            verifier.establish(0)
            future = Future(); future.set_result(PageMode('unknown', reason))
            verifier.pending = future; verifier.started_at = 5
            try:
                with self.assertRaises(ValueError) as caught: verifier.check(6)
                self.assertEqual(caught.exception.reason, expected)
                self.assertNotIn('private camera title', str(caught.exception))
                self.assertEqual(verifier.verified_at, float('-inf'))
            finally: verifier.close()

    def test_real_adapter_verification_retains_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = RecoveryTests().adapter(RecoveryTests().config(directory), live())
            self.assertEqual(adb.verify_live().reason, 'live_refresh_required')
