"""Navigation fences must invalidate visual proof without implying EMPTY."""
import io
import tempfile
import unittest
from dataclasses import replace
from contextlib import contextmanager
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from smart_tapo.config import Config, Rect
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.capture.coordination import NavigationSnapshot
from smart_tapo.worker import Worker


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(Config(), data_dir=Path(self.temp.name), expected_size=(4, 4),
                              roi=Rect(0, 0, 2, 2), video=Rect(0, 0, 4, 4),
                              clock_roi=Rect(0, 0, 1, 1), notify_enabled=True,
                              stuck_consecutive_frames=2)
        self.now = 1000.
        self.state = NavigationSnapshot('initial', False, 0.)
        self.sent = []
        self.detector = SimpleNamespace(observe=lambda _: SimpleNamespace(label='PRESENT'))
        self.worker = Worker(self.config, self.detector,
                             lambda message, notification: self.sent.append(message) or 'success',
                             clock=lambda: self.now)
        png = io.BytesIO()
        Image.new('RGB', (4, 4)).save(png, format='PNG')
        self.png = png.getvalue()
        self.mock = patch('smart_tapo.capture.coordination.navigation_snapshot',
                          side_effect=lambda _: self.state)
        self.mock.start()

    def tearDown(self):
        self.worker.close()
        self.mock.stop()
        self.temp.cleanup()

    def frame(self, sequence, captured=None, session='s',epoch='current'):
        self.now += 1
        stamp = self.now if captured is None else captured
        marker = publish_frame(self.config, self.png, session, sequence, True,
                               captured_at=self.now, captured_monotonic=stamp)
        frame, image = read_frame(marker, self.config)
        if epoch!='current':object.__setattr__(frame,'clock_epoch',epoch)
        self.worker.process(frame, image, marker)

    def assert_no_proof(self):
        self.assertEqual((self.worker.count, self.worker.empty, self.worker.live_count), (0, 0, 0))
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)
        self.assertEqual(self.sent, [])

    def test_prior_boot_or_legacy_spool_cannot_become_live_after_wall_rollback(self):
        self.state=NavigationSnapshot('new',False,10.)
        self.frame(1,captured=90000.,epoch='0'*64)
        self.frame(2,captured=90001.,epoch='0'*64)
        self.frame(3,captured=90002.,epoch=None)
        self.assert_no_proof()

    def test_recovering_resets_and_fresh_new_session_resumes(self):
        self.frame(1)
        self.state = NavigationSnapshot('new', True, 1001.5)
        self.frame(2)
        self.assert_no_proof()
        self.frame(3)
        self.assert_no_proof()
        self.state = NavigationSnapshot('new', False, 1001.5)
        self.frame(1, session='fresh')
        self.assertEqual(self.worker.count, 1)
        self.frame(2, session='fresh')
        self.worker.drain(wait=True)
        self.assertEqual(len(self.sent), 1)

    def test_old_spool_is_rejected_after_recovery_finished(self):
        self.state = NavigationSnapshot('new', False, 1100.)
        self.frame(1)
        self.frame(2)
        self.assert_no_proof()
        self.assertIsNone(self.worker.active())

    def test_navigation_during_detector_decision_cannot_notify(self):
        self.frame(1)
        def observe(_):
            self.state = NavigationSnapshot('new', False, 1001.5)
            return SimpleNamespace(label='PRESENT')
        self.detector.observe = observe
        self.frame(2)
        self.assert_no_proof()
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'incomplete')
        self.assertIsNone(event['first_clear'])

    def test_generation_change_between_frames_restarts_event_proof(self):
        self.frame(1)
        original = self.worker.active()['id']
        self.state = NavigationSnapshot('new', False, 1001.5)
        self.frame(2)
        self.assertEqual(self.worker.count, 1)
        self.assertEqual(len(self.sent), 0)
        self.assertNotEqual(self.worker.active()['id'], original)
        self.frame(3, session='another')
        self.assertEqual(self.worker.count, 1)
        self.frame(4, session='another')
        self.worker.drain(wait=True)
        self.assertEqual(len(self.sent), 1)

    def test_notification_commit_and_enqueue_share_ui_lock(self):
        self.frame(1)
        held = []
        @contextmanager
        def tracked_lock(serial, timeout):
            self.assertEqual(serial, self.config.adb_serial)
            self.assertEqual(timeout, 5)
            held.append(True)
            try:
                yield
            finally:
                held.pop()
        def submit(sender, message, notification):
            self.assertTrue(held)
            self.assertFalse(self.worker.db.in_transaction)
            row = self.worker.db.execute('SELECT status FROM notifications').fetchone()
            self.assertEqual(row['status'], 'pending')
            future = Future()
            future.set_result('success')
            return future
        with patch('smart_tapo.worker.ui_lock', tracked_lock), patch.object(self.worker.executor, 'submit', side_effect=submit) as queued:
            self.frame(2)
        self.assertEqual(queued.call_count, 1)
        self.assertFalse(held)

    def test_lock_busy_preserves_pending_proof_without_dispatch(self):
        self.frame(1)
        with patch('smart_tapo.worker.ui_lock', side_effect=ValueError('ui_dump_busy')):
            self.frame(2)
        self.assertEqual((self.worker.count, self.worker.live_count), (1, 1))
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM frames').fetchone()[0], 1)
        self.assertEqual(self.sent, [])
        self.worker.scan()
        self.worker.drain(wait=True)
        self.assertEqual(len(self.sent), 1)

    def test_lock_unavailable_resets_without_dispatch(self):
        self.frame(1)
        with patch('smart_tapo.worker.ui_lock', side_effect=ValueError('ui_dump_lock_unavailable')):
            self.frame(2)
        self.assert_no_proof()

    def test_existing_success_is_not_erased_by_recovery(self):
        self.frame(1)
        self.frame(2)
        self.worker.drain(wait=True)
        self.state = NavigationSnapshot('new', True, 1002.5)
        self.frame(3)
        self.assertEqual((self.worker.count, self.worker.empty, self.worker.live_count), (0, 0, 0))
        row = self.worker.db.execute('SELECT status FROM notifications').fetchone()
        self.assertEqual(row['status'], 'success')
        self.assertEqual(self.worker.active()['kind'], 'stuck')
        self.assertIsNone(self.worker.active()['first_clear'])
        self.assertEqual(len(self.sent), 1)

    def test_unavailable_snapshot_resets_without_empty(self):
        self.frame(1)
        with patch('smart_tapo.capture.coordination.navigation_snapshot', side_effect=ValueError('invalid')):
            self.frame(2)
        self.assert_no_proof()
