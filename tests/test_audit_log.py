import json
import os
import tempfile
import unittest
from pathlib import Path

from smart_tapo.audit_log import AuditLog


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def records(self):
        return [json.loads(line) for line in (self.root / 'logs/worker.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_append_run_ids_thai_time_and_immediate_flush(self):
        first = AuditLog(self.root, 'worker')
        self.assertTrue(first.write('ready', created=0, text='secret'))
        row = self.records()[0]
        self.assertEqual(row['created'], 0)
        self.assertEqual(row['timestamp'], '1970-01-01T07:00:00+07:00')
        self.assertEqual(row['pid'], os.getpid())
        self.assertEqual(row['role'], 'worker')
        self.assertNotIn('text', row)
        first.close()
        second = AuditLog(self.root, 'worker')
        self.assertTrue(second.write('stopped'))
        second.close()
        self.assertNotEqual(self.records()[0]['run_id'], self.records()[1]['run_id'])
        self.assertFalse(second.write('ready'))

    def test_valid_details(self):
        with_log = AuditLog(self.root, 'worker')
        for kind, detail in [('detection', ('PRESENT', 2, 3, 1, 'ok')),
                             ('quality', (2.5, 3, .1, .2)),
                             ('alignment_detail', 'unclassified_alignment_failure'),
                             ('frame_context', {'frame_id': 'frame-1', 'sequence': 2, 'captured_at': 1.5}),
                             ('notification_success', {'event_id': 'a' * 32})]:
            self.assertTrue(with_log.write(kind, detail))
        with_log.close()
        self.assertEqual(len(self.records()), 5)

    def test_rejects_untrusted_fields(self):
        log = AuditLog(self.root, 'worker')
        for kind, detail in [('secret', None), ('ready', 'password'),
                             ('detection', ('PRESENT', 2, 3, 3, 'ok')),
                             ('detection', ('PRESENT', 2, 3, 1, 'secret')),
                             ('quality', (float('nan'), 3, .1, .2)),
                             ('alignment_detail', 'provider secret'),
                             ('frame_context', {'path': 'secret'}),
                             ('frame_context', {'frame_id': '../secret'}),
                             ('notification_failed', {'event_id': 'password'})]:
            self.assertFalse(log.write(kind, detail))
        self.assertFalse(log.write('ready', created=float('inf')))
        log.close()
        path = self.root / 'logs/worker.jsonl'
        self.assertTrue(not path.exists() or path.read_bytes() == b'')
        with self.assertRaises(ValueError):
            AuditLog(self.root, '../secret')

    def test_bounded_rotation(self):
        log = AuditLog(self.root, 'worker', max_bytes=400, backup_count=2)
        for _ in range(30):
            self.assertTrue(log.write('ready'))
        log.close()
        files = list((self.root / 'logs').iterdir())
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 400)
            for line in path.read_text(encoding='utf-8').splitlines():
                json.loads(line)

    def test_unwritable_path_and_oversized_record(self):
        blocked = self.root / 'blocked'
        blocked.write_text('file', encoding='utf-8')
        log = AuditLog(blocked, 'capture')
        self.assertFalse(log.write('ready'))
        log.close()
        tiny = AuditLog(self.root, 'worker', max_bytes=10)
        self.assertFalse(tiny.write('ready'))
        tiny.close()

    def test_capture_streams_are_separate_and_bounded_metadata_is_accepted(self):
        first = AuditLog(self.root, 'capture', scope='camera_a')
        second = AuditLog(self.root, 'capture', scope='camera_b')
        self.assertTrue(first.write('frame_context', {'frame_id': 'session_a-'+'1'*100,
                        'session_id': 'session_a', 'sequence': 2, 'fresh': True,
                        'elapsed_seconds': .25, 'missed_slots': 0}))
        self.assertTrue(second.write('ready'))
        first.close()
        second.close()
        self.assertTrue((self.root/'logs/capture-camera_a.jsonl').is_file())
        self.assertTrue((self.root/'logs/capture-camera_b.jsonl').is_file())
        with self.assertRaises(ValueError):
            AuditLog(self.root, 'capture', scope='../escape')
