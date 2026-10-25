from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from smart_tapo.config import Config, Rect
from smart_tapo.capture import Adb, CaptureError, camera_center, capture, decode_clock, live_ui


def ui(*texts):
    return ('<hierarchy>' + ''.join('<node text="%s" bounds="[0,0][2,2]" />' %
            t.replace('&', '&amp;') for t in texts) + '</hierarchy>').encode()


def png(color='black'):
    out = io.BytesIO()
    Image.new('RGB', (4, 4), color).save(out, format='PNG')
    return out.getvalue()


class CaptureTests(unittest.TestCase):
    def config(self, directory):
        return replace(Config(), data_dir=Path(directory), expected_size=(4, 4),
                       video=Rect(0, 0, 4, 4), roi=Rect(0, 0, 2, 2), clock_roi=Rect(0, 0, 2, 1),
                       camera_name='Camera', adb_serial='127.0.0.1:12345')

    def test_ui_safety(self):
        self.assertTrue(live_ui(ui('Camera', 'Pan & Tilt'), 'Camera'))
        self.assertFalse(live_ui(ui('Other', 'Pan & Tilt'), 'Camera'))
        self.assertIsNone(camera_center(ui('Camera'), 'Camera', (4, 4)))
        self.assertEqual(camera_center(ui('Home', 'Camera'), 'Camera', (4, 4)), (1, 1))
        self.assertIsNone(camera_center(ui('Home', 'Camera', 'Camera'), 'Camera', (4, 4)))
        password = ui('Home', 'Camera').replace(b'<hierarchy>', b'<hierarchy><node password="true"/>')
        self.assertIsNone(camera_center(password, 'Camera', (4, 4)))

    def test_runner_timeout_and_sanitized_errors(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            def runner(argv, **kw):
                calls.append((argv, kw))
                return subprocess.CompletedProcess(argv, 1, b'', b'secret password')
            adb = Adb(self.config(d), runner=runner)
            with self.assertRaises(CaptureError) as context:
                adb.run('get-state')
            self.assertNotIn('secret', str(context.exception))
            self.assertEqual(calls[0][0][1:3], ['-s', '127.0.0.1:12345'])
            self.assertEqual(calls[0][1]['timeout'], 15)

    def test_decode(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            with self.assertRaises(CaptureError):
                decode_clock(b'not png', config)
            self.assertNotEqual(decode_clock(png(), config), decode_clock(png('white'), config))

    def test_first_frame_atomic_publish_and_decode_failure(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            class Fake:
                content = png()
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return self.content
            fake = Fake()
            self.assertEqual(capture(config, threading.Event(), once=True, adb=fake), 0)
            markers = list(Path(d).rglob('*.ready.json'))
            self.assertEqual(len(markers), 1)
            frame = json.loads(markers[0].read_text())
            self.assertFalse(frame['fresh'])
            self.assertEqual((markers[0].parent / frame['image_name']).read_bytes(), fake.content)
            self.assertEqual(list(Path(d).rglob('*.tmp')), [])
            fake.content = b'bad'
            self.assertEqual(capture(config, threading.Event(), once=True, adb=fake), 1)
            self.assertEqual(len(list(Path(d).rglob('*.ready.json'))), 1)

    def test_quota_retains_pending(self):
        with tempfile.TemporaryDirectory() as d:
            config = replace(self.config(d), max_image_storage_mb=0.000001)
            class Fake:
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return png()
            self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake()), 0)
            self.assertEqual(list(Path(d).rglob('*.ready.json')), [])

    def test_missed_slots_increment_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            ticks = iter([0.0, 0.0, 3.0, 3.0, 3.0, 3.0])
            class Fake:
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return png()
            self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake(),
                                     monotonic=lambda: next(ticks)), 0)
            frame = json.loads(next(Path(d).rglob('*.ready.json')).read_text())
            self.assertEqual(frame['sequence'], 4)


if __name__ == '__main__':
    unittest.main()
