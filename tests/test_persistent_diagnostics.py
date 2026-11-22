import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import queue
import tempfile
import unittest

from smart_tapo.audit_log import AuditLog
from smart_tapo.desktop_controller import DesktopController, _SafeLog


class PersistentDiagnosticTests(unittest.TestCase):
    def test_rejected_marker_hint_is_durable_and_not_generic(self):
        messages=queue.Queue()
        handler=_SafeLog(messages,'worker',audit=self.audit)
        record=logging.LogRecord('smart_tapo.worker',logging.WARNING,__file__,1,
            'Rejected frame marker; new proof required',(),None)
        handler.emit(record)
        rows=[json.loads(line) for line in (self.root/'logs/worker.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[-1]['kind'],'frame_rejected')
        controller=DesktopController();controller._roles['worker']['messages']=messages
        text='\n'.join(controller.drain_messages())
        self.assertIn('ปฏิเสธ marker',text)
        self.assertNotIn('log_unavailable',text)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = AuditLog(self.root, 'worker')
        self.addCleanup(self.audit.close)

    def record(self):
        record = logging.LogRecord('smart_tapo.worker', logging.INFO, __file__, 1,
                                   'Detection status: label=%s count=%d/%d live=%d reason=%s',
                                   ('PRESENT', 2, 10, 2, 'ok'), None)
        record.created = datetime(2026, 10, 27, 17, 30, tzinfo=timezone.utc).timestamp()
        return record

    def test_file_persists_before_queue_and_ui_uses_original_timestamp(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        handler.audit = self.audit
        record = self.record()
        handler.emit(record)
        path = self.root / 'logs' / 'worker.jsonl'
        self.assertTrue(path.is_file(), 'Safe diagnostic never reached the persistent log')
        row = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(row['created'], record.created)
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('[28/10/2026 00:30:00 UTC+7]', text)
        self.assertIn('2/10', text)

    def test_full_ui_queue_does_not_discard_disk_record(self):
        messages = queue.Queue(maxsize=1)
        messages.put(('occupied', 'occupied'))
        handler = _SafeLog(messages, 'worker')
        handler.audit = self.audit
        handler.emit(self.record())
        path = self.root / 'logs' / 'worker.jsonl'
        self.assertTrue(path.is_file(), 'A full UI queue discarded the audit record too')
        self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()), 1)

    def test_arbitrary_warning_args_are_not_written_to_disk(self):
        handler = _SafeLog(queue.Queue(), 'worker')
        handler.audit = self.audit
        record = logging.LogRecord('arbitrary', logging.WARNING, __file__, 1,
                                   'provider failed: %s', ('synthetic-secret-token',), None)
        handler.emit(record)
        path = self.root / 'logs' / 'worker.jsonl'
        self.assertTrue(path.is_file())
        self.assertNotIn('synthetic-secret-token', path.read_text(encoding='utf-8'))

    def test_disk_failure_is_visible_without_discarding_ui_detection(self):
        blocked = self.root / 'blocked'
        blocked.write_text('file', encoding='utf-8')
        audit = AuditLog(blocked, 'worker')
        self.addCleanup(audit.close)
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker', audit=audit)
        handler.emit(self.record())
        handler.emit(self.record())
        items = []
        while not messages.empty():
            items.append(messages.get_nowait())
        self.assertEqual([item[1] for item in items].count('log_unavailable'), 1)
        self.assertEqual([item[1] for item in items].count('detection'), 2)

    def test_notification_correlation_and_deferral_are_persisted(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker', audit=self.audit)
        context = {'event_id': 'a'*32, 'notification_id': 'b'*32}
        record = logging.LogRecord('worker', logging.INFO, __file__, 1,
                                   'Notification status: %s', ('failed',), None)
        record.notification_context = context
        handler.emit(record)
        deferred = logging.LogRecord('worker', logging.INFO, __file__, 1, 'Notification deferred', (), None)
        deferred.notification_context = {'event_id': 'a'*32, 'reason': 'retry_wait'}
        handler.emit(deferred)
        rows = [json.loads(line) for line in (self.root / 'logs/worker.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['detail'], context)
        self.assertEqual(rows[1]['detail']['reason'], 'retry_wait')
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        self.assertIn('รอเวลาลองส่งใหม่', '\n'.join(controller.drain_messages()))

    def test_ui_proof_expiry_and_recovery_are_persisted_and_displayed(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker', audit=self.audit)
        for template, level in (('Live UI verification pending: capture publication paused', logging.WARNING),
                                ('Live UI verification restored', logging.INFO)):
            handler.emit(logging.LogRecord('capture',level,__file__,1,template,(),None))
        rows = [json.loads(line) for line in (self.root/'logs/worker.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['kind'] for row in rows], ['ui_unverified','ui_verified'])
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('หยุดปล่อยภาพให้ Worker', text)
        self.assertIn('ยืนยันหน้า Live ได้อีกครั้ง', text)


if __name__ == '__main__':
    unittest.main()
