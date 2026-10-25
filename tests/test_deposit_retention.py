from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from smart_tapo.config import Config, Rect, load_config
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.worker import Worker


class DepositRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.sequence = 0
        self.label = 'EMPTY'
        self.config = replace(Config(), data_dir=Path(self.temp.name), expected_size=(4, 4),
                              roi=Rect(0, 0, 2, 2), video=Rect(0, 0, 4, 4), clock_roi=Rect(0, 0, 1, 1))
        content = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(content, format='PNG')
        self.png = content.getvalue()
        self.detector = SimpleNamespace(observe=lambda _: SimpleNamespace(label=self.label))
        self.worker = Worker(self.config, self.detector, clock=lambda: self.now)

    def tearDown(self):
        self.worker.close()
        self.temp.cleanup()

    def frame(self, label):
        self.sequence += 1
        self.now += 1
        self.label = label
        marker = publish_frame(self.config, self.png, 'test', self.sequence, True,
                               captured_at=self.now, captured_monotonic=self.now)
        frame, image = read_frame(marker, self.config)
        self.worker.process(frame, image, marker)
        return image

    def passage(self):
        raw = self.frame('PRESENT')
        self.frame('PRESENT')
        self.frame('EMPTY')
        self.frame('EMPTY')
        return self.worker.db.execute('SELECT * FROM events').fetchone(), raw

    def evidence(self):
        return self.worker.db.execute('SELECT * FROM evidence').fetchall()

    def test_full_day_after_closure_and_raw_spool_independent(self):
        event, raw = self.passage()
        self.assertEqual(event['kind'], 'normal_visual')
        self.assertEqual(event['started'], 1001)
        self.assertEqual(event['closed'], 1004)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertEqual({r['role'] for r in self.evidence()}, {'first', 'first_full', 'clear'})
        self.now = event['closed'] + 23 * 3600 + 59 * 60
        self.worker.cleanup()
        self.assertFalse(raw.exists())
        for row in self.evidence():
            with Image.open(row['path']) as image:
                image.load()
                self.assertEqual(image.size, (4, 4) if row['role'] == 'first_full' else (2, 2))
            self.assertEqual(Path(row['path']).name, event['id'] + '-' + row['role'] + '.png')
        self.assertEqual(len(self.evidence()), 3)
        self.now = event['closed'] + 86400 + 0.01
        self.worker.cleanup()
        self.assertEqual(self.evidence(), [])
        self.assertIsNone(self.worker.db.execute('SELECT * FROM events').fetchone())

    def test_configurable_hour(self):
        self.worker.config = replace(self.config, deposit_history_hours=1)
        event, _ = self.passage()
        self.now = event['closed'] + 3599
        self.worker.cleanup()
        self.assertEqual(len(self.evidence()), 3)
        self.now += 2
        self.worker.cleanup()
        self.assertEqual(self.evidence(), [])
        self.assertIsNone(self.worker.db.execute('SELECT * FROM events').fetchone())

    def test_legacy_config_and_validation(self):
        path = Path(self.temp.name) / 'old.json'
        content = json.dumps({'non_alert_images_minutes': 5})
        path.write_text(content)
        self.assertEqual(load_config(path).deposit_history_hours, 24)
        self.assertEqual(path.read_text(), content)
        for value in (0, -1, float('nan'), float('inf'), True, '24'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(Config(), deposit_history_hours=value).validate()

    def test_empty_frames_and_contiguous_passage_not_per_frame(self):
        for _ in range(8):
            self.frame('EMPTY')
        self.assertEqual(self.evidence(), [])
        self.assertIsNone(self.worker.active())
        for _ in range(5):
            self.frame('PRESENT')
        self.assertEqual(len(self.evidence()), 2)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.frame('EMPTY')
        self.frame('EMPTY')
        for _ in range(5):
            self.frame('EMPTY')
        self.assertEqual(len(self.evidence()), 3)

    def test_active_and_unresolved_notifications_protected(self):
        self.frame('PRESENT')
        self.now += 40 * 86400
        self.worker.cleanup()
        self.assertIsNotNone(self.worker.active())
        self.assertEqual(len(self.evidence()), 2)
        event = self.worker.active()['id']
        self.worker.db.execute("UPDATE events SET kind='normal_visual',closed=? WHERE id=?", (1001, event))
        for status in ('pending', 'failed', 'ambiguous'):
            with self.subTest(status=status):
                self.worker.db.execute('DELETE FROM notifications')
                self.worker.db.execute('INSERT INTO notifications VALUES(?,?,?,?)', ('n', event, 1001, status))
                self.worker.cleanup()
                self.assertEqual(len(self.evidence()), 2)
                self.assertIsNotNone(self.worker.db.execute('SELECT * FROM events').fetchone())

    def test_failed_unlink_keeps_event_and_notifications(self):
        event, _ = self.passage()
        self.worker.db.execute('INSERT INTO notifications VALUES(?,?,?,?)', ('n', event['id'], self.now, 'shadow'))
        self.now += 86401
        with patch.object(self.worker, 'unlink_owned', return_value=False):
            self.worker.cleanup()
        self.assertEqual(len(self.evidence()), 3)
        self.assertIsNotNone(self.worker.db.execute('SELECT * FROM events').fetchone())
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 1)
        self.worker.cleanup()
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_unsafe_evidence_path_preserves_event_and_outside_file(self):
        event, _ = self.passage()
        outside = Path(self.temp.name) / 'outside.png'
        outside.write_bytes(self.png)
        self.worker.db.execute("UPDATE evidence SET path=? WHERE role='first_full'", (str(outside),))
        self.now += 86401
        self.worker.cleanup()
        self.assertTrue(outside.exists())
        self.assertEqual(len(self.evidence()), 1)
        self.assertIsNotNone(self.worker.db.execute('SELECT * FROM events').fetchone())

    def test_stuck_and_incomplete_keep_legacy_ttls(self):
        for _ in range(10):
            self.frame('PRESENT')
        self.frame('EMPTY')
        self.frame('EMPTY')
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'stuck')
        self.now = event['closed'] + 86401
        self.worker.cleanup()
        self.assertEqual(len(self.evidence()), 4)
        self.now = event['closed'] + 7 * 86400 + 1
        self.worker.cleanup()
        self.assertEqual(self.evidence(), [])
        self.assertIsNotNone(self.worker.db.execute('SELECT * FROM events').fetchone())
        self.now = event['closed'] + 30 * 86400 + 1
        self.worker.cleanup()
        self.assertIsNone(self.worker.db.execute('SELECT * FROM events').fetchone())
        self.frame('PRESENT')
        self.frame('UNKNOWN')
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'incomplete')
        self.now += 301
        self.worker.cleanup()
        self.assertEqual(self.evidence(), [])
        self.assertIsNotNone(self.worker.db.execute('SELECT * FROM events').fetchone())

    def test_restart_candidate_is_incomplete(self):
        self.frame('PRESENT')
        self.worker.close()
        self.worker = Worker(self.config, self.detector, clock=lambda: self.now)
        event = self.worker.db.execute('SELECT * FROM events').fetchone()
        self.assertEqual(event['kind'], 'incomplete')
        self.assertEqual(len(self.evidence()), 2)

    def test_full_snapshot_is_immutable_and_rejects_symlink(self):
        raw = self.frame('PRESENT')
        event = self.worker.active()['id']
        full = next(Path(r['path']) for r in self.evidence() if r['role'] == 'first_full')
        original = full.read_bytes()
        raw.write_bytes(b'not a png')
        self.worker.snapshot(event, 'first_full', raw, full_frame=True)
        self.assertEqual(full.read_bytes(), original)
        link = raw.with_name('link.png')
        try:
            link.symlink_to(raw)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            self.worker.snapshot(event, 'unsafe_full', link, full_frame=True)
        with self.assertRaises(OSError):
            self.worker.snapshot(event, 'invalid_full', raw, full_frame=True)
