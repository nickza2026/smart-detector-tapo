"""Decision-lock contention retries the pending frame, not a synthetic UNKNOWN."""
import io
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from smart_tapo.config import Config, Rect
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.capture.coordination import NavigationSnapshot
from smart_tapo.worker import Worker


class WorkerContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(Config(), data_dir=Path(self.temp.name), expected_size=(4, 4),
                              roi=Rect(0, 0, 2, 2), video=Rect(0, 0, 4, 4),
                              clock_roi=Rect(0, 0, 1, 1), notify_enabled=True,
                              stuck_consecutive_frames=10)
        self.now = 1000.
        self.state = NavigationSnapshot('initial', False, 0.)
        self.seen = []
        self.sent = []
        self.busy = False
        self.error = ValueError('ui_dump_busy')
        self.detector = SimpleNamespace(observe=self.observe)
        self.worker = Worker(self.config, self.detector,
                             lambda message, notification: self.sent.append(message) or 'success',
                             clock=lambda: self.now)
        self.snapshots = patch('smart_tapo.capture.coordination.navigation_snapshot',
                               side_effect=lambda _: self.state)
        self.locks = patch('smart_tapo.worker.ui_lock', side_effect=self.lock)
        self.snapshots.start()
        self.locks.start()
        png = io.BytesIO()
        Image.new('RGB', (4, 4)).save(png, format='PNG')
        self.png = png.getvalue()

    def tearDown(self):
        self.worker.close()
        self.locks.stop()
        self.snapshots.stop()
        self.temp.cleanup()

    def observe(self, image):
        self.seen.append(Path(image).name)
        return SimpleNamespace(label='PRESENT')

    def lock(self, serial, timeout):
        if self.busy:
            raise self.error
        return nullcontext()

    def publish(self, sequence):
        self.now += 1
        marker = publish_frame(self.config, self.png, 's', sequence, True,
                               captured_at=self.now, captured_monotonic=self.now)
        return read_frame(marker, self.config), marker

    def prime(self):
        for sequence in range(1, 6):
            self.publish(sequence)
            self.worker.scan()
        self.assertEqual(self.worker.count, 5)

    def processed(self):
        return self.worker.db.execute('SELECT count(*) FROM frames').fetchone()[0]

    def test_busy_retries_same_frame_before_later_frames_and_reaches_ten(self):
        self.prime()
        original = self.worker.active()['id']
        (_, image), _ = self.publish(6)
        self.publish(7)
        self.busy = True
        self.worker.scan()
        self.assertEqual(self.processed(), 5)
        self.assertEqual(self.worker.count, 5)
        self.assertEqual(self.worker.active()['id'], original)
        self.assertEqual(self.seen[-1], image.name)
        self.assertEqual(len(self.seen), 6)  # No later frame overtakes frame six.
        self.assertFalse(self.sent)
        self.busy = False
        self.worker.scan()
        self.assertEqual(self.processed(), 7)
        self.assertEqual(self.worker.count, 7)
        self.assertEqual(self.seen[5:7], [image.name, image.name])
        for sequence in (8, 9):
            self.publish(sequence)
            self.worker.scan()
            self.assertFalse(self.sent)
        self.publish(10)
        self.worker.scan()
        self.worker.drain(wait=True)
        self.assertEqual((self.worker.count, self.worker.live_count), (10, 10))
        self.assertEqual(self.processed(), 10)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(set(self.seen)), 10)

    def test_timeout_is_deferred_once_per_poll(self):
        self.prime()
        self.publish(6)
        self.error = TimeoutError()
        self.busy = True
        for _ in range(2):
            self.worker.scan()
            self.assertEqual(self.processed(), 5)
            self.assertEqual(self.worker.count, 5)
        self.busy = False
        self.worker.scan()
        self.assertEqual(self.worker.count, 6)

    def test_retry_rechecks_navigation_boot_and_age_without_dispatch(self):
        for change in ('navigation', 'boot', 'stale', 'unknown', 'not_fresh'):
            with self.subTest(change=change):
                # Reset the fixture between scenarios without retaining observations.
                if change != 'navigation':
                    self.tearDown()
                    self.setUp()
                self.prime()
                (frame, image), marker = self.publish(6)
                self.busy = True
                self.worker.scan()
                self.assertEqual(self.processed(), 5)
                self.busy = False
                if change == 'navigation':
                    self.state = NavigationSnapshot('new', False, self.now + .5)
                elif change == 'stale':
                    self.now += self.config.max_live_age_seconds + 1
                elif change == 'unknown':
                    self.detector.observe = lambda _: SimpleNamespace(label='UNKNOWN')
                elif change == 'not_fresh':
                    object.__setattr__(frame, 'fresh', False)
                boot = patch('smart_tapo.worker.current_clock_epoch', return_value='other') if change == 'boot' else nullcontext()
                with boot:
                    self.worker.process(frame, image, marker)
                self.assertEqual(self.processed(), 6)
                self.assertEqual(self.worker.live_count, 0)
                self.assertFalse(self.sent)
                self.assertEqual(self.worker.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_unavailable_and_invalid_locks_fail_closed_instead_of_deferring(self):
        for error in (ValueError('ui_dump_lock_unavailable'), ValueError('invalid'), PermissionError('denied')):
            with self.subTest(error=error):
                self.worker.count = self.worker.live_count = 5
                (frame, image), marker = self.publish(self.processed() + 1)
                self.error = error
                self.busy = True
                self.worker.process(frame, image, marker)
                self.assertEqual(self.worker.count, 0)
                self.assertEqual(self.worker.live_count, 0)
                self.assertFalse(self.sent)
