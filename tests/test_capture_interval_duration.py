"""Synthetic elapsed proof regressions; no devices or providers."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from tests import test_worker as fixtures


class DurationTests(fixtures.WorkerTests):
    def configure_duration(self):
        values = vars(self.config).copy()
        values.update(capture_interval_seconds=.5, stuck_minimum_seconds=9., clear_minimum_seconds=1.)
        self.worker.config = SimpleNamespace(**values)

    def tick(self, sequence, label='PRESENT', session='s', age=0):
        self.now -= .5
        return self.frame(sequence, label, session, age)

    def test_actual_capture_repeated_clock_subframes_are_nonvoting(self):
        from unittest.mock import patch
        from smart_tapo.capture import capture
        from smart_tapo.frames import publish_frame, read_frame
        import io
        from PIL import Image
        self.config = replace(self.config, capture_interval_seconds=.5,
                              stuck_minimum_seconds=9, clear_minimum_seconds=1)
        self.worker.config = self.config
        now = [1000.]
        markers = []
        class Stop:
            def is_set(inner): return len(markers) >= 26
            def wait(inner, delay): now[0] += delay; return False
        class Verifier:
            def __init__(inner, *args): pass
            def establish(inner, now): pass
            def check(inner, now): return True
            def reset(inner): pass
            def close(inner): pass
        class Fake:
            shots = 0
            def verify_live(inner): return True
            def connect(inner, stop): pass
            def ensure_live(inner): pass
            def run(inner, *args):
                stream = io.BytesIO()
                Image.new('RGB', (4,4), 'white' if (inner.shots // 2) % 2 else 'black').save(stream, format='PNG')
                inner.shots += 1
                return stream.getvalue()
        def publish(*args, **kwargs):
            marker = publish_frame(*args, **kwargs)
            markers.append(marker)
            return marker
        with patch('smart_tapo.capture.LiveVerifier', Verifier), \
             patch('smart_tapo.capture.publish_frame', publish), \
             patch('smart_tapo.capture.time.time', lambda: now[0]):
            self.assertEqual(capture(self.config, Stop(), adb=Fake(), monotonic=lambda:now[0]), 0)
        frames = [read_frame(marker, self.config) for marker in markers]
        self.assertEqual([frame.fresh for frame, _ in frames[:6]], [False,False,True,False,True,False])
        for index, ((frame, image), marker) in enumerate(zip(frames, markers)):
            self.now = frame.captured_at
            self.label = 'PRESENT' if index < 21 else 'EMPTY'
            previous = (self.worker.count, self.worker.empty, self.worker.live_count)
            self.worker.process(frame, image, marker)
            if index >= 3 and not frame.fresh:
                self.assertEqual((self.worker.count, self.worker.empty, self.worker.live_count), previous)
            if index < 20: self.assertEqual(len(self.notifications()), 0)
            if index == 20:
                self.assertEqual(len(self.notifications()), 1)
                self.assertEqual(self.worker.count, 10)
            if index == 23: self.assertIsNotNone(self.worker.active())
            if index == 24:
                self.assertIsNone(self.worker.active())
                self.assertEqual(self.worker.empty, 2)

    def subframe(self, sequence, fresh, label='PRESENT', session='s', health='healthy', age=0):
        from smart_tapo.frames import publish_frame, read_frame
        self.now += .5
        self.label = label
        marker = publish_frame(self.config, self.png, session, sequence, fresh,
                               captured_at=self.now-age, captured_monotonic=self.now-age,
                               health=health)
        frame, image = read_frame(marker, self.config)
        self.worker.process(frame, image, marker)

    def test_frozen_clock_subframes_expire_without_voting(self):
        self.config = replace(self.config, capture_interval_seconds=.5, stuck_minimum_seconds=9)
        self.worker.config = self.config
        self.subframe(1, True)
        for sequence in range(2, 5):
            self.subframe(sequence, False)
            self.assertEqual(self.worker.count, 1)
        self.subframe(5, False)
        self.assertEqual((self.worker.count, self.worker.live_count), (0, 0))
        self.subframe(6, True)
        self.assertEqual(self.worker.count, 1)
        self.assertEqual(len(self.notifications()), 0)

    def test_nonfresh_navigation_generation_and_recovery_reset(self):
        from unittest.mock import patch
        from smart_tapo.capture.coordination import NavigationSnapshot
        self.config = replace(self.config, capture_interval_seconds=.5)
        self.worker.config = self.config
        state = [NavigationSnapshot('first', False, 0)]
        with patch('smart_tapo.capture.coordination.navigation_snapshot', side_effect=lambda _:state[0]):
            self.subframe(1, True)
            self.subframe(2, False)
            self.assertEqual(self.worker.count, 1)
            state[0] = NavigationSnapshot('changed', False, 0)
            self.subframe(3, False)
            self.assertEqual(self.worker.count, 0)
            self.subframe(4, True)
            state[0] = NavigationSnapshot('changed', True, 0)
            self.subframe(5, False)
            self.assertEqual((self.worker.count, self.worker.live_count), (0, 0))

    def test_nonfresh_unhealthy_future_and_stale_reset(self):
        self.config = replace(self.config, capture_interval_seconds=.5)
        self.worker.config = self.config
        sequence = 0
        for kwargs in ({'health':'unhealthy'}, {'age':-1}, {'age':10}):
            sequence += 1
            self.subframe(sequence, True)
            self.assertEqual(self.worker.count, 1)
            sequence += 1
            self.subframe(sequence, False, **kwargs)
            self.assertEqual((self.worker.count, self.worker.live_count), (0, 0))

    def test_repeated_clock_cannot_bridge_sequence_session_or_fresh_unknown(self):
        self.config = replace(self.config, capture_interval_seconds=.5)
        self.worker.config = self.config
        self.subframe(1, True)
        self.subframe(3, False)
        self.assertEqual(self.worker.count, 0)
        self.subframe(4, True)
        self.subframe(5, False, session='other')
        self.assertEqual(self.worker.count, 0)
        self.subframe(6, True, session='other')
        self.subframe(7, False, session='other')
        self.assertEqual(self.worker.count, 1)
        self.subframe(8, True, label='UNKNOWN', session='other')
        self.assertEqual(self.worker.count, 0)
        self.subframe(9, False, session='other')
        self.assertEqual(self.worker.count, 0)

    def test_half_second_does_not_halve_stuck_or_clear_proof(self):
        self.configure_duration()
        for i in range(1, 19):
            self.tick(i)
        self.assertEqual(len(self.notifications()), 0)
        self.tick(19)
        self.assertEqual(len(self.notifications()), 1)
        self.tick(20, 'EMPTY')
        self.tick(21, 'EMPTY')
        self.assertIsNotNone(self.worker.active())
        self.tick(22, 'EMPTY')
        self.assertIsNone(self.worker.active())

    def test_unknown_and_session_restart_elapsed_proof(self):
        self.configure_duration()
        for i in range(1, 18): self.tick(i)
        self.tick(18, 'UNKNOWN')
        for i in range(19, 36): self.tick(i)
        self.assertEqual(len(self.notifications()), 0)
        self.tick(36, session='new')
        for i in range(37, 54): self.tick(i, session='new')
        self.assertEqual(len(self.notifications()), 0)
        self.tick(54, session='new')
        self.assertEqual(len(self.notifications()), 1)

    def test_gap_resets_duration_even_when_event_was_already_stuck(self):
        self.configure_duration()
        for i in range(1, 20): self.tick(i)
        self.tick(20, 'EMPTY')
        self.now += 5
        self.tick(22, 'EMPTY')
        self.tick(23, 'EMPTY')
        self.assertIsNotNone(self.worker.active())
        self.tick(24, 'EMPTY')
        self.assertIsNone(self.worker.active())

    def test_one_second_and_half_second_have_same_first_alert_time(self):
        self.configure_duration()
        self.worker.config.capture_interval_seconds = 1
        start = self.now
        for i in range(1, 10): self.frame(i)
        self.assertEqual(len(self.notifications()), 0)
        self.frame(10)
        self.assertEqual(len(self.notifications()), 1)
        self.assertEqual(self.worker.active()['last_present'] - self.worker.active()['started'], 9)
        self.assertEqual(self.now - start, 10)

    def test_live_duration_cannot_borrow_stale_duration(self):
        self.configure_duration()
        for i in range(1, 20): self.tick(i, age=10)
        for i in range(20, 38): self.tick(i)
        self.assertEqual(len(self.notifications()), 0)
        self.tick(38)
        self.assertEqual(len(self.notifications()), 1)


class SettingsTests(unittest.TestCase):
    def app(self):
        from smart_tapo.desktop import DesktopApp
        from smart_tapo.config import Config
        app = DesktopApp.__new__(DesktopApp)
        app.config = Config()
        var = lambda value: SimpleNamespace(get=lambda: value)
        app.form = {key: var(value) for key, value in
                    [('capture_interval_seconds', '0.5'), ('stuck_consecutive_frames', '10'),
                     ('deposit_history_hours', '24')]}
        app.notify = var(False)
        return app

    def test_preset_preserves_nominal_elapsed_and_existing_minima(self):
        app = self.app()
        config = app._candidate()
        self.assertEqual((config.capture_interval_seconds, config.stuck_minimum_seconds,
                          config.clear_minimum_seconds), (.5, 9, 1))
        app.config = replace(config, stuck_minimum_seconds=15)
        self.assertEqual(app._candidate().stuck_minimum_seconds, 15)

    def test_bad_interval_rejected(self):
        from smart_tapo.config import Config
        for value in (0, -1, float('nan'), float('inf'), .000001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(Config(), capture_interval_seconds=value).validate()

    def test_backward_load_and_round_trip(self):
        import json
        import tempfile
        from pathlib import Path
        from dataclasses import asdict
        from smart_tapo.config import load_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{}', encoding='utf-8')
            old = load_config(path)
            self.assertEqual((old.capture_interval_seconds, old.stuck_minimum_seconds), (1, 0))
            path.write_text(json.dumps(asdict(self.app()._candidate()), default=str), encoding='utf-8')
            saved = load_config(path)
            self.assertEqual((saved.capture_interval_seconds, saved.stuck_minimum_seconds), (.5, 9))

    def test_save_rejected_while_any_role_or_audio_active(self):
        for role in ('capture', 'worker', 'audio'):
            app = self.app()
            app.busy = False
            app._active = lambda: role != 'audio'
            app._audio_active = lambda: role == 'audio'
            errors = []
            app._error = errors.append
            app.store = SimpleNamespace(save=lambda _: self.fail('active save'))
            self.assertFalse(app.save_settings())
            self.assertTrue(errors)


if __name__ == '__main__': unittest.main()
