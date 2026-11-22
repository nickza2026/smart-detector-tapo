from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from PIL import Image

from smart_tapo.config import Config, Rect
from smart_tapo.capture import Adb, CaptureError, camera_center, capture, decode_clock, live_ui
from smart_tapo.capture.page_mode import LIVE_ACTIVITY,LIVE_REQUIRED


def ui(*texts):
    body=''.join('<node text="%s" bounds="[0,0][2,2]" resource-id="%s"/>' %
            (t.replace('&', '&amp;'),'com.tplink.iot:id/title' if i==0 else '') for i,t in enumerate(texts))
    if 'Pan & Tilt' in texts:
        body+=''.join('<node resource-id="com.tplink.iot:id/%s" bounds="[0,0][2,2]"/>'%name for name in sorted(LIVE_REQUIRED))
    return ('<hierarchy>'+body+'</hierarchy>').encode()


class SyntheticLiveAdapter:
    def verify_live(self):return live_ui(self.ui(),'Camera',activity=LIVE_ACTIVITY)


def png(color='black'):
    out = io.BytesIO()
    Image.new('RGB', (4, 4), color).save(out, format='PNG')
    return out.getvalue()


class CaptureTests(unittest.TestCase):
    def test_playback_timeline_cannot_be_live_even_when_old_control_labels_remain(self):
        xml=ui('Camera','Pan & Tilt').replace(b'</hierarchy>',b'<node resource-id="com.tplink.iot:id/play_back_ruler" bounds="[0,0][2,2]"/></hierarchy>')
        self.assertFalse(live_ui(xml,'Camera'))

    def test_camera_name_and_pan_tilt_labels_alone_do_not_authorize_live(self):
        xml=b'<hierarchy><node text="Camera"/><node text="Pan &amp; Tilt"/></hierarchy>'
        self.assertFalse(live_ui(xml,'Camera'))
        self.assertFalse(live_ui(xml,'Camera',activity=LIVE_ACTIVITY))

    def test_failed_zero_exit_dump_never_returns_previous_live_xml(self):
        calls=[]
        def runner(argv,**kwargs):
            calls.append(argv)
            output=b'ERROR: could not get idle state.' if 'uiautomator' in argv else ui('Camera','Pan & Tilt')
            return subprocess.CompletedProcess(argv,0,stdout=output,stderr=b'')
        config=replace(Config(),adb_serial='synthetic-device')
        with self.assertRaises(CaptureError):Adb(config,runner=runner).ui()
        self.assertFalse(any('cat' in call for call in calls))

    def test_ui_dump_uses_unique_files_and_deletes_only_its_own_file(self):
        calls=[];files={};dump_paths=[]
        def runner(argv,**kwargs):
            calls.append(argv)
            if 'uiautomator' in argv:
                dump_paths.append(argv[-1]);files[argv[-1]]=ui('Camera','Pan & Tilt')
                output=b'UI hierchary dumped to: '+argv[-1].encode()
            elif 'cat' in argv:output=files.get(argv[-1],b'')
            else:
                files.pop(argv[-1],None);output=b''
            return subprocess.CompletedProcess(argv,0,stdout=output,stderr=b'')
        adapter=Adb(replace(Config(),adb_serial='synthetic-device'),runner=runner)
        self.assertEqual(adapter.ui(),ui('Camera','Pan & Tilt'))
        self.assertEqual(adapter.ui(),ui('Camera','Pan & Tilt'))
        self.assertNotEqual(dump_paths[0],dump_paths[1])
        self.assertEqual(files,{})
        self.assertTrue(all(path!='/sdcard/smart-tapo-window.xml' for path in dump_paths))

    def test_missing_or_invalid_new_dump_cannot_leave_a_fresh_request_receipt(self):
        for payload in (b'',b'<not-a-hierarchy/>',b'<broken',b'x'*(1024*1024+1)):
            calls=[]
            def runner(argv,**kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv,0,stdout=payload if 'cat' in argv else b'',stderr=b'')
            adapter=Adb(replace(Config(),adb_serial='synthetic-device'),runner=runner)
            with self.assertRaises(CaptureError):adapter.ui()
            self.assertIsNone(adapter.last_ui_request_id)
            self.assertIn('rm',calls[-1])

    def config(self, directory):
        return replace(Config(), data_dir=Path(directory), expected_size=(4, 4),
                       video=Rect(0, 0, 4, 4), roi=Rect(0, 0, 2, 2), clock_roi=Rect(0, 0, 2, 1),
                       camera_name='Camera', adb_serial='127.0.0.1:12345')

    def test_ui_safety(self):
        self.assertTrue(live_ui(ui('Camera', 'Pan & Tilt'), 'Camera',activity=LIVE_ACTIVITY))
        self.assertFalse(live_ui(ui('Other', 'Pan & Tilt'), 'Camera',activity=LIVE_ACTIVITY))
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
            class Fake(SyntheticLiveAdapter):
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
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return png()
            self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake()), 0)
            self.assertEqual(list(Path(d).rglob('*.ready.json')), [])

    def test_missed_slots_increment_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            # Include the final proof-age sample inside the publication fence.
            ticks = iter([0.0, 0.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0])
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return png()
            self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake(),
                                     monotonic=lambda: next(ticks)), 0)
            frame = json.loads(next(Path(d).rglob('*.ready.json')).read_text())
            self.assertEqual(frame['sequence'], 4)

    def test_periodic_ui_does_not_block_frames_and_expiry_suppresses_publish(self):
        with tempfile.TemporaryDirectory() as d:
            config = replace(self.config(d), ui_check_interval_seconds=10)
            stop, entered, release = (threading.Event() for _ in range(3))
            now = [0.0]
            screenshots, published = [], []
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self):
                    entered.set()
                    if not release.wait(3):
                        raise CaptureError('test barrier timed out')
                    return ui('Camera', 'Pan & Tilt')
                def run(self, *args):
                    screenshots.append(now[0])
                    if len(screenshots) == 1:
                        now[0] = 5.0
                    else:
                        # The verifier must have started without blocking capture.
                        if not entered.wait(3):
                            raise AssertionError('verifier did not start')
                        now[0] += 1
                    if now[0] == 11:
                        stop.set()
                    return png('white' if len(screenshots) % 2 else 'black')
            def publish(*args, **kwargs):
                published.append(kwargs['captured_monotonic'])
                return Path('test.ready.json')
            from smart_tapo.capture.live_verifier import LiveVerifier
            original_close = LiveVerifier.close
            def close(verifier):
                release.set()
                original_close(verifier)
            try:
                with patch('smart_tapo.capture.publish_frame', side_effect=publish), \
                     patch.object(LiveVerifier, 'close', close), \
                     patch('smart_tapo.capture.LOG.warning') as warning:
                    self.assertEqual(capture(config, stop, adb=Fake(),
                                             monotonic=lambda: now[0]), 0)
            finally:
                release.set()
            self.assertIn(9.0, published)
            self.assertNotIn(10.0, published)
            self.assertNotIn(11.0, published)
            self.assertGreater(len(screenshots), 3)
            self.assertEqual([call.args[0] for call in warning.call_args_list],
                             ['Live UI verification pending: capture publication paused'])

    def test_initial_live_failure_never_captures(self):
        with tempfile.TemporaryDirectory() as d:
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): raise CaptureError('not live')
                def run(self, *args): raise AssertionError('unsafe screenshot')
            self.assertEqual(capture(self.config(d), threading.Event(), once=True,
                                     adb=Fake()), 1)
            self.assertEqual(list(Path(d).rglob('*.ready.json')), [])

    def test_screenshot_progress_releases_a_slow_periodic_ui_check(self):
        with tempfile.TemporaryDirectory() as d:
            now = [0.0]
            stop, entered, release = threading.Event(), threading.Event(), threading.Event()
            shots = []
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self):
                    if now[0] >= 10:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('Periodic UI verification blocked screenshot progress')
                    return ui('Camera','Pan & Tilt')
                def run(self,*args):
                    shots.append(now[0])
                    if len(shots) == 1:
                        now[0] = 5.0
                    elif len(shots) == 2:
                        now[0] = 10.0
                    else:
                        if not entered.wait(3):
                            raise AssertionError('UI verifier was not scheduled')
                        self.pending_during_screenshot = not release.is_set()
                        release.set()
                        stop.set()
                    return png()
            fake = Fake()
            try:
                self.assertEqual(capture(self.config(d), stop, adb=fake, monotonic=lambda:now[0]), 0)
                self.assertTrue(fake.pending_during_screenshot)
            finally:
                release.set()

    def test_diagnostic_timing_includes_decode_and_file_publication(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            class Fake(SyntheticLiveAdapter):
                def connect(self, stop): pass
                def ensure_live(self): pass
                def ui(self): return ui('Camera', 'Pan & Tilt')
                def run(self, *args): return png()
            with patch('smart_tapo.capture.time.perf_counter', side_effect=[10.0, 11.25]), \
                 patch('smart_tapo.capture.LOG.info') as log:
                self.assertEqual(capture(config, threading.Event(), once=True, adb=Fake()), 0)
            sample = next(call for call in log.call_args_list if call.args[0] == 'Capture sample')
            self.assertEqual(sample.kwargs['extra']['frame_context']['elapsed_seconds'], 1.25)


if __name__ == '__main__':
    unittest.main()
