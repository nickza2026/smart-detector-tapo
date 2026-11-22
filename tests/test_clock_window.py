"""Synthetic actual-PNG capture regression; no ADB/provider access."""
import io
from dataclasses import replace
from unittest.mock import patch
from PIL import Image
from tests.test_capture_interval_duration import DurationTests
from smart_tapo.capture import capture
from smart_tapo.frames import publish_frame, read_frame


class ClockWindowTests(DurationTests):
    def test_short_pass_between_clock_ticks_reaches_worker(self):
        # Baseline accepts this config without needing any newly introduced API.
        self.config = replace(self.config, capture_interval_seconds=.5,
                              stuck_minimum_seconds=9, clear_minimum_seconds=.5)
        # SimpleNamespace permits running the same behavioural RED before fields exist.
        from types import SimpleNamespace
        values = vars(self.config).copy()
        values.update(clock_window_enabled=True, clock_check_interval_seconds=2.)
        self.config = SimpleNamespace(**values)
        self.config.validate = lambda: None
        self.worker.config = self.config
        now = [1000.]
        markers, samples = [], []
        class Stop:
            def is_set(inner): return len(samples) >= 9
            def wait(inner, delay): now[0] += delay; return False
        class Verifier:
            def __init__(inner, *args): pass
            def establish(inner, now): pass
            def check(inner, now): return True
            def reset(inner): pass
            def close(inner): pass
        class Fake:
            def verify_live(inner): return True
            def connect(inner, stop): pass
            def ensure_live(inner): pass
            def run(inner, *args):
                index = len(samples)
                image = Image.new('RGB', (4,4), 'black')
                image.putpixel((0,0), (index//4*50, 0, 0))
                # The only PRESENT image has an unchanged clock.
                image.putpixel((3,3), (255,0,0) if index == 1 else (0,0,0))
                stream = io.BytesIO(); image.save(stream, format='PNG')
                samples.append(index)
                if index < 4: inner.assert_no_early(markers)
                return stream.getvalue()
            def assert_no_early(inner, markers):
                self.assertEqual(markers, [])
        def publish(*args, **kwargs):
            marker = publish_frame(*args, **kwargs); markers.append(marker); return marker
        with patch('smart_tapo.capture.LiveVerifier', Verifier), \
             patch('smart_tapo.capture.publish_frame', publish), \
             patch('smart_tapo.capture.time.time', lambda: now[0]):
            capture(self.config, Stop(), adb=Fake(), monotonic=lambda: now[0])
        seen = []
        original = self.worker.detector.observe
        def observe(path):
            with Image.open(path) as image:
                self.label = 'PRESENT' if image.getpixel((3,3))[0] else 'EMPTY'
            seen.append(self.label)
            return original(path)
        self.worker.detector.observe = observe
        for marker in markers:
            frame, image = read_frame(marker, self.config)
            self.now = now[0]
            with patch('smart_tapo.worker.time.monotonic', lambda: now[0]):
                self.worker.process(frame, image, marker)
        self.assertIn('PRESENT', seen)
        events = self.worker.db.execute('SELECT * FROM events').fetchall()
        self.assertEqual([event['kind'] for event in events], ['normal_visual'])
        self.assertEqual(events[0]['started'], 1000.5)
        self.assertEqual(len(self.notifications()), 0)
        self.assertEqual([read_frame(m,self.config)[0].captured_at for m in markers[:4]],
                         [1000.5,1001.,1001.5,1002.])

    def capture_case(self, *, freeze=False, failure=None, shots=25, quota=False, once=False):
        from smart_tapo.capture.coordination import NavigationSnapshot
        self.config = replace(self.config, capture_interval_seconds=.5,
                              clock_window_enabled=True, max_image_storage_mb=.0001 if quota else 512)
        self.worker.config = self.config
        now, count = [1000.], [0]
        state = [NavigationSnapshot('window-test', False, 0)]
        markers = []
        class Stop:
            stopped = False
            def is_set(inner): return inner.stopped or count[0] >= shots
            def wait(inner, delay): now[0] += delay; return False
        stop = Stop()
        class Verifier:
            def __init__(inner,*args): pass
            def establish(inner, now): pass
            def check(inner, when): return not (failure == 'ui' and count[0] >= 4)
            def reset(inner): pass
            def close(inner): pass
        class Fake:
            def verify_live(inner): return True
            def connect(inner, stop): pass
            def ensure_live(inner): pass
            def run(inner,*args):
                index = count[0]; count[0] += 1
                if index == 3:
                    if failure == 'gap': now[0] += 3
                    if failure == 'rollback': now[0] -= 3
                    if failure == 'overrun': now[0] += 1.2
                    if failure == 'nav': state[0] = NavigationSnapshot('changed', False, now[0])
                    if failure == 'stop': stop.stopped = True
                    if failure == 'png': return b'not a png'
                    if failure == 'missing':
                        for path in (self.config.data_dir/'pending').rglob('*.png'): path.unlink()
                if failure == 'jitter' and index == 4: now[0] += .1
                clock_value=0 if freeze else index//4*50
                if failure == 'recoverfreeze': clock_value=0 if index<8 else ((index-8)//4+1)*50
                image = Image.new('RGB',(4,4), (clock_value%255,0,0))
                stream=io.BytesIO(); image.save(stream,format='PNG'); return stream.getvalue()
        def publish(*args,**kwargs):
            marker=publish_frame(*args,**kwargs); markers.append(marker); return marker
        with patch('smart_tapo.capture.LiveVerifier', Verifier), \
             patch('smart_tapo.capture.publish_frame',publish), \
             patch('smart_tapo.capture.navigation_snapshot',lambda _:state[0]), \
             patch('smart_tapo.capture.time.time',lambda:now[0]):
            self.capture_status=capture(self.config,stop,once=once,adb=Fake(),monotonic=lambda:now[0])
        self.assertEqual(list((self.config.data_dir/'pending').rglob('*.png')), [])
        return markers, now[0]

    def test_known_frozen_window_requires_resync_before_publication(self):
        from smart_tapo.capture.clock_window import PendingWindow
        self.config=replace(self.config,capture_interval_seconds=.5,clock_window_enabled=True)
        window=PendingWindow(self.config)
        try:
            for index in range(9):
                rows,proof=window.add(self.png,b'A' if index<8 else b'B',
                    1000+index*.5,1000+index*.5,index+1,'s','generation')
                self.assertEqual(rows,[], 'First progress after a known freeze replayed provisional PNGs')
                self.assertIsNone(proof)
            self.assertEqual(window.paths,[])
            for index in range(9,13):
                rows,proof=window.add(self.png,b'B' if index<12 else b'C',
                    1000+index*.5,1000+index*.5,index+1,'s','generation')
                if index<12:self.assertEqual(rows,[])
            self.assertEqual([row[2] for row in rows],[1004.5,1005.,1005.5,1006.])
            self.assertTrue(all(row[0].exists() for row in rows))
            self.assertIsNotNone(proof)
        finally:window.reset()
        self.assertEqual(list((self.config.data_dir/'pending').rglob('*.png')),[])
        self.assertEqual(len(self.notifications()),0)

    def test_actual_capture_recovery_discards_first_progress_then_releases_new_window(self):
        from smart_tapo.capture.coordination import NavigationSnapshot
        markers,_=self.capture_case(failure='recoverfreeze',shots=14)
        frames=[read_frame(marker,self.config)[0] for marker in markers]
        self.assertEqual([frame.captured_at for frame in frames],[1004.5,1005.,1005.5,1006.])
        for frame,marker in zip(frames,markers):
            self.now=frame.window_proof['end_at']
            with patch('smart_tapo.worker.time.monotonic',lambda:frame.window_proof['end_mono']), \
                 patch('smart_tapo.worker.coordination.navigation_snapshot',
                       lambda _:NavigationSnapshot('window-test',False,0)):
                self.worker.process(frame,marker.parent/frame.image_name,marker)
        self.assertEqual(self.worker.count,4)
        self.assertEqual(len(self.notifications()),0)
        self.assertEqual(self.worker.active()['started'],1004.5)

    def test_explicit_uncertainty_cleanup_does_not_restore_trust(self):
        from smart_tapo.capture.clock_window import PendingWindow
        self.config=replace(self.config,capture_interval_seconds=.5,clock_window_enabled=True)
        for reason in ('ui_expired','quota','clock_rollback','capture_error','navigation','gap'):
            with self.subTest(reason=reason):
                window=PendingWindow(self.config)
                try:
                    window.add(self.png,b'old',999,999,1,'old','old')
                    window.add(self.png,b'old',999.5,999.5,2,'old','old')
                    window.reset(reason)
                    for index in range(5):
                        rows,proof=window.add(self.png,b'A' if index<4 else b'B',
                            1000+index*.5,1000+index*.5,index+1,'s','g')
                        self.assertEqual(rows,[])
                        self.assertIsNone(proof)
                    self.assertEqual(window.paths,[])
                    for index in range(5,9):
                        rows,proof=window.add(self.png,b'B' if index<8 else b'C',
                            1000+index*.5,1000+index*.5,index+1,'s','g')
                    self.assertEqual([row[2] for row in rows],[1002.5,1003.,1003.5,1004.])
                finally:window.reset()

    def test_once_window_mode_does_not_claim_success_without_ready_artifact(self):
        markers,_=self.capture_case(shots=5,once=True)
        self.assertEqual(markers,[])
        self.assertEqual(self.capture_status,1)

    def test_frozen_clock_beyond_stuck_duration_never_publishes_or_alerts(self):
        markers,_ = self.capture_case(freeze=True)
        self.assertEqual(markers,[])
        self.worker.scan()
        self.assertEqual(len(self.notifications()),0)

    def test_pending_discard_boundaries(self):
        for failure in ('gap','rollback','overrun','nav','stop','png','ui','missing'):
            with self.subTest(failure=failure):
                markers,_ = self.capture_case(failure=failure,shots=5)
                self.assertEqual(markers,[])
        markers,_ = self.capture_case(quota=True,shots=5)
        self.assertEqual(markers,[])

    def test_missing_last_png_is_checked_before_any_ready_marker(self):
        from smart_tapo.capture.clock_window import PendingWindow
        original=PendingWindow.add
        def remove_last(window,*args,**kwargs):
            rows,proof=original(window,*args,**kwargs)
            if rows: rows[-1][0].unlink()
            return rows,proof
        with patch.object(PendingWindow,'add',remove_last):
            markers,_=self.capture_case(shots=6)
        self.assertEqual(markers,[])
        self.assertEqual(len(self.notifications()),0)

    def test_clock_check_jitter_keeps_only_bounded_pending_images(self):
        markers,_=self.capture_case(failure='jitter',shots=6)
        self.assertEqual(len(markers),4)
        frames=[read_frame(marker,self.config)[0] for marker in markers]
        self.assertAlmostEqual(frames[0].window_proof['end_at']-frames[0].window_proof['health_start_at'],2.1)
        self.assertLessEqual(frames[-1].captured_at-frames[0].captured_at,2)
        self.assertEqual(frames[0].captured_at,1000.5)

    def test_zero_legacy_minimum_cannot_halve_window_alert_duration(self):
        markers,_=self.capture_case(shots=25)
        self.assertEqual(self.config.stuck_minimum_seconds,0)
        for marker in markers:
            frame,image=read_frame(marker,self.config)
            self.now=frame.window_proof['end_at']
            from smart_tapo.capture.coordination import NavigationSnapshot
            with patch('smart_tapo.worker.time.monotonic',lambda:frame.window_proof['end_mono']), \
                 patch('smart_tapo.worker.coordination.navigation_snapshot',
                       lambda _:NavigationSnapshot('window-test',False,0)):
                self.worker.process(frame,image,marker)
            if frame.captured_at < 1009.5:
                self.assertEqual(len(self.notifications()),0)
        self.assertEqual(len(self.notifications()),1)
        event=self.worker.active()
        self.assertEqual(event['started'],1000.5)
        self.assertGreaterEqual(event['last_present']-event['started'],9)

    def test_old_valid_artifact_cannot_become_window_proof(self):
        self.config = replace(self.config,clock_window_enabled=True,capture_interval_seconds=.5)
        self.worker.config=self.config
        marker=publish_frame(self.config,self.png,'old',1,True,captured_at=self.now,captured_monotonic=self.now)
        frame,image=read_frame(marker,self.config)
        self.worker.process(frame,image,marker)
        self.assertEqual(self.worker.count,0)
        self.assertEqual(len(self.notifications()),0)

    def test_cleanup_preserves_previous_run_and_published_artifacts(self):
        from smart_tapo.capture.clock_window import PendingWindow
        self.config=replace(self.config,capture_interval_seconds=.5,clock_window_enabled=True)
        old=self.config.data_dir/'pending'/'cabinet-1'/'previous-run'/'keep.png'
        old.parent.mkdir(parents=True);old.write_bytes(self.png)
        marker=publish_frame(self.config,self.png,'valid',1,True)
        window=PendingWindow(self.config)
        window.add(self.png,b'first',1000,1000,1,'s','generation')
        window.add(self.png,b'first',1000.5,1000.5,2,'s','generation')
        owned=[row[0] for row in window.paths]
        self.assertTrue(all(path.exists() for path in owned))
        window.reset()
        self.assertFalse(any(path.exists() for path in owned))
        self.assertTrue(old.exists())
        self.assertTrue(marker.exists())
        self.assertTrue(read_frame(marker,self.config)[1].exists())

    def test_window_validator_rejects_forged_coverage(self):
        from smart_tapo.capture.clock_window import valid_window, config_token
        from smart_tapo.capture.coordination import NavigationSnapshot
        from smart_tapo.frames import Frame
        from smart_tapo.clock_epoch import current_clock_epoch
        self.config=replace(self.config,clock_window_enabled=True,capture_interval_seconds=.5)
        state=NavigationSnapshot('window-test',False,0)
        proof=dict(session='s',epoch=current_clock_epoch(),config=config_token(self.config),
                   generation=state.generation,start_at=1000.5,end_at=1002.,start_mono=1000.5,
                   end_mono=1002.,health_start_at=1000.,health_start_mono=1000.,
                   ui_healthy=True,clock_progress=True,ui_at=1000.,ui_until=1010.)
        frame=Frame('sample',self.config.camera_id,'s',2,1000.5,1000.5,4,4,'sample.png',True,window_proof=proof)
        self.assertTrue(valid_window(frame,self.config,state,1002.,1002.))
        for change in ({'session':'other'},{'epoch':'wrong'},{'generation':'other'},
                       {'config':'old'},{'end_at':999.},{'end_at':1002.1},
                       {'end_mono':1002.1},{'start_at':1001.},{'ui_healthy':False},
                       {'ui_until':1001.},{'clock_progress':False},
                       {'health_start_at':997.},{'health_start_mono':997.}):
            with self.subTest(change=change):
                self.assertFalse(valid_window(replace(frame,window_proof={**proof,**change}),
                                              self.config,state,1002.,1002.))
