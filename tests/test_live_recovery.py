import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

from smart_tapo.config import Config, Rect
from smart_tapo.capture import Adb, CaptureError
from smart_tapo.capture.page_mode import LIVE_ACTIVITY, LIVE_REQUIRED, PLAYBACK_ACTIVITY, classify_page
from smart_tapo.capture.coordination import navigation_scope, navigation_snapshot, navigation_lock
from smart_tapo.capture.ui_lock import ui_lock
from unittest.mock import patch
import time
from contextlib import contextmanager


def live(refresh=True, camera='Camera'):
    root = ET.Element('hierarchy')
    for name in sorted(LIVE_REQUIRED | {'title'}):
        attrs = {'resource-id': 'com.tplink.iot:id/' + name, 'bounds': '[0,0][4,4]'}
        if name == 'title': attrs['text'] = camera
        ET.SubElement(root, 'node', attrs)
    if refresh:
        ET.SubElement(root, 'node', {'text': 'Tap to refresh', 'bounds': '[1,1][3,3]'})
    return ET.tostring(root)


class RecoveryTests(unittest.TestCase):
    def test_stop_during_navigation_lock_wait_cannot_dispatch_new_tap(self):
        stop=threading.Event()
        with tempfile.TemporaryDirectory() as d:
            adb=self.adapter(self.config(d),live())
            @contextmanager
            def acquired_after_stop(config):
                with navigation_scope(config) as state:
                    stop.set()
                    yield state
            with patch('smart_tapo.capture.navigation_scope',acquired_after_stop):
                with self.assertRaises(CaptureError):adb.recover_live(stop)
            self.assertEqual(adb.taps,0)

    def config(self, directory):
        return replace(Config(), data_dir=Path(directory), adb_serial='synthetic', camera_name='Camera',
                       expected_size=(4,4), video=Rect(0,0,4,4))

    def adapter(self, config, xml, activity=LIVE_ACTIVITY):
        class Fake(Adb):
            taps = 0
            def run(self, *args, **kwargs):
                if 'dumpsys' in args: return ('topResumedActivity: '+activity).encode()
                if 'tap' in args: self.taps += 1
                return b''
            def ui(self): return xml
        return Fake(config)

    def test_negative_pages_and_ambiguous_bounds_never_tap(self):
        variants = [(live(camera='Wrong'), LIVE_ACTIVITY), (live(), PLAYBACK_ACTIVITY),
                    (live().replace(b'<hierarchy>',b'<hierarchy><node password="true"/>'), LIVE_ACTIVITY),
                    (live().replace(b'[1,1][3,3]',b'[1,1][8,8]'), LIVE_ACTIVITY),
                    (live().replace(b'</hierarchy>',b'<node text="Tap to refresh" bounds="[1,1][3,3]"/></hierarchy>'), LIVE_ACTIVITY)]
        with tempfile.TemporaryDirectory() as d:
            for xml, activity in variants:
                adb = self.adapter(self.config(d), xml, activity)
                with self.assertRaises(CaptureError): adb.recover_live()
                self.assertEqual(adb.taps, 0)
            self.assertEqual(navigation_snapshot(self.config(d)).generation, 'initial')

    def test_retry_budget_and_cancel_wait(self):
        class FastStop:
            waits = []
            def is_set(self): return False
            def wait(self, seconds): self.waits.append(seconds); return False
        with tempfile.TemporaryDirectory() as d:
            adb = self.adapter(self.config(d), live())
            stop = FastStop()
            with self.assertRaises(CaptureError): adb.recover_live(stop)
            self.assertEqual(adb.taps, 3)
            self.assertEqual(stop.waits, [0.5, 1.0, 2.0])
            stop.waits = []
            with patch.object(stop, 'wait', return_value=True):
                with self.assertRaises(CaptureError): adb.recover_live(stop)
            self.assertEqual(adb.taps, 4)

    def test_durable_fence_failure_and_malformed_state(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            old = navigation_snapshot(config)
            with self.assertRaises(RuntimeError):
                with navigation_scope(config) as entered:
                    self.assertTrue(navigation_snapshot(config).recovering)
                    self.assertNotEqual(entered.generation, old.generation)
                    raise RuntimeError('simulated action failure')
            state = navigation_snapshot(config)
            self.assertFalse(state.recovering)
            self.assertEqual(state.generation, entered.generation)
            self.assertEqual(state.changed_monotonic, entered.changed_monotonic)
            next(Path(d).glob('navigation-*.json')).write_text('{broken')
            with self.assertRaises(ValueError): navigation_snapshot(config)
            with self.assertRaises(ValueError):
                with navigation_scope(config): pass

    def test_two_navigation_actors_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            active = []; errors = []; overlap = []
            def actor():
                try:
                    with navigation_scope(config):
                        active.append(1)
                        overlap.append(len(active))
                        time.sleep(0.02)
                        active.pop()
                except Exception as error: errors.append(error)
            threads = [threading.Thread(target=actor) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(overlap, [1,1])

    def test_healthy_ensure_has_no_navigation_write(self):
        with tempfile.TemporaryDirectory() as d:
            config = self.config(d)
            adb = self.adapter(config, live(False))
            adb.ensure_live()
            self.assertEqual(adb.taps, 0)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_abandoned_recovery_is_cleared_only_after_fresh_live_proof(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            config=self.config(d)
            with navigation_scope(config):pass
            path=next(Path(d).glob('navigation-*.json'))
            record=json.loads(path.read_text());record['recovering']=True
            path.write_text(json.dumps(record))
            old=navigation_snapshot(config).generation
            adb=self.adapter(config,live(False))
            self.assertEqual(adb.recover_live().mode,'live')
            self.assertFalse(navigation_snapshot(config).recovering)
            self.assertNotEqual(navigation_snapshot(config).generation,old)
            self.assertEqual(adb.taps,0)

    def test_prior_boot_cutoff_can_rebase_after_fresh_live_without_tapping(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            config=self.config(d)
            with navigation_scope(config):pass
            path=next(Path(d).glob('navigation-*.json'))
            record=json.loads(path.read_text());record['changed_monotonic']=time.monotonic()+86400
            path.write_text(json.dumps(record))
            self.adapter(config,live(False)).ensure_live()
            self.assertLessEqual(navigation_snapshot(config).changed_monotonic,time.monotonic())

    def test_readonly_ui_dump_does_not_block_audio_decision_lock(self):
        with tempfile.TemporaryDirectory() as d:
            config=self.config(d);entered=threading.Event();release=threading.Event();errors=[]
            def reader():
                try:
                    with ui_lock(config.adb_serial,1):
                        entered.set()
                        if not release.wait(3):raise RuntimeError('Barrier deadline')
                except Exception as error:errors.append(error)
            thread=threading.Thread(target=reader);thread.start()
            try:
                self.assertTrue(entered.wait(1))
                with navigation_lock(config.adb_serial,.1):pass
            finally:release.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])

    def test_capture_reconnect_resets_session_and_freshness(self):
        from smart_tapo.capture import capture
        from PIL import Image
        import io
        class Verifier:
            checks = 0
            def __init__(self, *args): pass
            def establish(self, now): pass
            def check(self, now):
                self.checks += 1
                # First frame has pre-capture, post-capture and fenced checks.
                # Lose proof before the second frame, after one publication.
                if self.checks == 4: raise ValueError('detected overlay')
                return True
            def reset(self): pass
            def close(self): pass
        with tempfile.TemporaryDirectory() as d:
            config = replace(self.config(d), roi=Rect(0,0,2,2), clock_roi=Rect(0,0,2,1),
                             capture_interval_seconds=0.025)
            stop = threading.Event(); published = []
            class Fake:
                connections = 0
                def verify_live(self): return True
                def connect(self, event): self.connections += 1
                def ensure_live(self): pass
                def run(self, *args):
                    stream = io.BytesIO()
                    Image.new('RGB',(4,4),'white' if self.connections > 1 else 'black').save(stream,format='PNG')
                    return stream.getvalue()
            fake = Fake()
            def publish(config, png, session, sequence, fresh, **kwargs):
                published.append((session, fresh))
                if len(published) == 2: stop.set()
                return Path('synthetic.ready.json')
            with patch('smart_tapo.capture.LiveVerifier', Verifier), patch('smart_tapo.capture.publish_frame', publish):
                self.assertEqual(capture(config, stop, adb=fake), 0)
            self.assertEqual(fake.connections, 2)
            self.assertEqual([item[1] for item in published], [False,False])
            self.assertNotEqual(published[0][0],published[1][0])

    def test_refresh_overlay_vetoes_live_controls(self):
        mode = classify_page(live(), 'Camera', LIVE_ACTIVITY)
        self.assertEqual((mode.mode, mode.reason), ('unknown', 'live_refresh_required'))

    def test_recovery_taps_only_fresh_verified_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            config = replace(Config(), data_dir=Path(d), adb_serial='synthetic', camera_name='Camera',
                             expected_size=(4,4), video=Rect(0,0,4,4))
            class Fake(Adb):
                refreshed = False
                taps = 0
                def run(self, *args, **kwargs):
                    if 'dumpsys' in args: return ('topResumedActivity: '+LIVE_ACTIVITY).encode()
                    if 'tap' in args: self.taps += 1; self.refreshed = True
                    return b''
                def ui(self): return live(not self.refreshed)
            adb = Fake(config)
            self.assertEqual(adb.recover_live().mode, 'live')
            self.assertEqual(adb.taps, 1)
            self.assertEqual(adb.recover_live().mode, 'live')
            self.assertEqual(adb.taps, 1)

    def test_cancel_never_taps(self):
        stop = threading.Event(); stop.set()
        with tempfile.TemporaryDirectory() as d:
            adb = Adb(replace(Config(), data_dir=Path(d), adb_serial='synthetic'))
            with self.assertRaises(CaptureError): adb.recover_live(stop)
