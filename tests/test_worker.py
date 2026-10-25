from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from smart_tapo.config import Config, Rect
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.worker import Worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.
        self.label = 'PRESENT'
        self.config = replace(Config(), data_dir=Path(self.temp.name), expected_size=(4, 4),
                              roi=Rect(0, 0, 2, 2), video=Rect(0, 0, 4, 4), clock_roi=Rect(0, 0, 1, 1))
        image = io.BytesIO()
        Image.new('RGB', (4, 4)).save(image, format='PNG')
        self.png = image.getvalue()
        self.worker = self.make_worker()

    def make_worker(self, sender=None):
        detector = SimpleNamespace(observe=lambda _: SimpleNamespace(label=self.label))
        return Worker(self.config, detector, sender, clock=lambda: self.now)

    def tearDown(self):
        self.worker.close()
        self.temp.cleanup()

    def frame(self, sequence, label='PRESENT', session='s', age=0):
        self.now += 1
        self.label = label
        marker = publish_frame(self.config, self.png, session, sequence, True,
                               captured_at=self.now-age, captured_monotonic=self.now-age)
        frame, image = read_frame(marker, self.config)
        self.worker.process(frame, image, marker)
        return frame, image, marker

    def notifications(self):
        return self.worker.db.execute('SELECT * FROM notifications').fetchall()

    def test_exact_reset_example_and_duplicate(self):
        for i in range(1, 6):
            self.frame(i)
        self.frame(6, 'EMPTY')
        self.assertEqual(self.worker.count, 0)
        self.assertEqual(len(self.notifications()), 0)
        for i in range(7, 16):
            self.frame(i)
        args = self.frame(16)
        self.assertEqual(len(self.notifications()), 1)
        self.worker.process(*args)
        self.assertEqual(self.worker.count, 10)
        self.assertEqual(len(self.notifications()), 1)

    def test_gap_unknown_late_and_adjustable_threshold(self):
        self.worker.config = replace(self.config, stuck_consecutive_frames=3)
        self.frame(1)
        self.frame(3)
        self.assertEqual(self.worker.count, 1)
        self.frame(4, 'UNKNOWN')
        self.frame(5, age=100)
        self.assertEqual(self.worker.live_count, 0)
        for i in range(6, 9):
            self.frame(i)
        self.assertEqual(len(self.notifications()), 1)
        self.now += 100
        self.worker.scan()
        self.assertEqual(len(self.notifications()), 1)

    def test_normal_history_and_safe_cleanup(self):
        self.worker.config = replace(self.config, deposit_history_hours=5 / 60)
        args = self.frame(1)
        self.frame(2, 'EMPTY')
        self.frame(3, 'EMPTY')
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'normal_visual')
        pending = publish_frame(self.config, self.png, 's', 4, True, captured_at=self.now)
        temporary = pending.parent / 'unknown.tmp'
        temporary.write_bytes(b'not-owned')
        outside = Path(self.temp.name) / 'original.png'
        outside.write_bytes(b'original')
        link = pending.parent / 'escape.png'
        try:
            link.symlink_to(outside)
        except OSError:
            # Windows may require Developer Mode/admin for symlinks.
            pass
        else:
            self.assertFalse(self.worker.unlink_owned(link, pending.parent))
        self.now += 400
        self.worker.cleanup()
        self.assertFalse(args[1].exists())
        self.assertTrue(pending.exists())
        self.assertTrue(temporary.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM events').fetchone()[0], 0)
        self.assertEqual(self.worker.db.execute('SELECT image FROM frames WHERE id=?', (args[0].frame_id,)).fetchone()[0], None)
        self.assertLess(event['first_clear'], event['closed'])

    def test_missing_image_fails_closed(self):
        self.worker.detector = SimpleNamespace(observe=lambda p: Image.open(p))
        marker = publish_frame(self.config, self.png, 's', 1, True, captured_at=self.now)
        frame, image = read_frame(marker, self.config)
        image.unlink()
        self.worker.process(frame, image, marker)
        self.assertEqual(self.worker.count, 0)

    def test_async_success_restart_and_no_stale_reminder(self):
        self.worker.close()
        self.config = replace(self.config, notify_enabled=True, stuck_consecutive_frames=2)
        sent = []
        self.worker = self.make_worker(lambda message: sent.append(message) or 'success')
        self.frame(1)
        self.frame(2)
        self.worker.drain(wait=True)
        success = self.worker.active()['last_success']
        self.assertIsNotNone(success)
        self.assertIn('Suspected banknote/object stuck', sent[0])
        self.worker.close()
        self.worker = self.make_worker(lambda _: self.fail('unexpected send'))
        self.assertEqual(self.worker.count, 0)
        self.frame(3)
        self.assertEqual(self.worker.active()['last_success'], success)
        self.now += 100
        self.worker.scan()
        self.assertEqual(len(self.notifications()), 1)

    def test_failed_and_ambiguous_are_not_success(self):
        for result in ('failed', 'ambiguous'):
            self.worker.close()
            self.config = replace(self.config, notify_enabled=True, stuck_consecutive_frames=1)
            self.worker = self.make_worker(lambda _: result)
            self.now += 61
            self.frame(1, session=result)
            self.worker.drain(wait=True)
            self.assertIsNone(self.worker.active()['last_success'])
            self.assertEqual(self.notifications()[-1]['status'], result)

    def test_stale_history_is_recorded_without_live_alert(self):
        self.worker.config = replace(self.config, stuck_consecutive_frames=3)
        for i in range(1, 5):
            self.frame(i, age=100)
        self.frame(5, 'EMPTY', age=100)
        self.frame(6, 'EMPTY', age=100)
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'stuck')
        self.assertEqual(len(self.notifications()), 0)

    def test_camera_cannot_reuse_another_cameras_database(self):
        self.worker.close()
        self.config = replace(self.config, camera_id='other-camera')
        with self.assertRaises(ValueError):
            self.make_worker()
        self.config = replace(self.config, camera_id='cabinet-1')
        self.worker = self.make_worker()


if __name__ == '__main__':
    unittest.main()
