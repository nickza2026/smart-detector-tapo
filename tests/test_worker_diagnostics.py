"""Operator diagnostics must distinguish proof, shadow mode and send results."""
from __future__ import annotations

from dataclasses import replace
import io
import logging
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from smart_tapo.config import Config, Rect
from smart_tapo.desktop_controller import DesktopController, _SafeLog
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.vision import Observation
from smart_tapo.worker import Worker


DETECTION = 'Detection status: label=%s count=%d/%d live=%d reason=%s'
NOTIFICATION = 'Notification status: %s'
QUALITY = 'Visual quality: score=%.3f threshold=%.3f changed=%.4f threshold=%.4f'


class WorkerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.config = replace(Config(), data_dir=Path(self.temp.name), expected_size=(4, 4),
                              video=Rect(0, 0, 4, 4), roi=Rect(0, 0, 2, 2), clock_roi=Rect(0, 0, 1, 1))
        self.detector = SimpleNamespace(observe=lambda path: SimpleNamespace(label='PRESENT'))
        content = io.BytesIO()
        Image.new('RGB', (4, 4)).save(content, format='PNG')
        self.png = content.getvalue()
        self.sender = Mock(return_value='success')
        self.worker = Worker(self.config, self.detector, self.sender, clock=lambda: self.now)
        self.addCleanup(self.worker.close)

    def frame(self, sequence, *, fresh=True, age=0, elapsed=1):
        self.now += elapsed
        marker = publish_frame(self.worker.config, self.png, 'diagnostic', sequence, fresh,
                               captured_at=self.now - age, captured_monotonic=self.now - age)
        frame, image = read_frame(marker, self.worker.config)
        self.worker.process(frame, image, marker)

    def test_observation_exception_reaches_real_gui_bridge_with_safe_typed_cause(self):
        import cv2
        for error, code in ((cv2.error('private-url-key'), 'opencv_error'),
                            (OSError('private-url-key'), 'image_io_error'),
                            (ValueError('private-url-key'), 'invalid_image_value'),
                            (RuntimeError('private-url-key'), 'unexpected_error')):
            with self.subTest(code=code):
                messages = queue.Queue()
                handler = _SafeLog(messages, 'worker')
                logger = logging.getLogger('smart_tapo.worker')
                old_level = logger.level
                logger.setLevel(logging.INFO)
                logger.addHandler(handler)
                try:
                    self.detector.observe = Mock(side_effect=error)
                    self.worker.count = self.worker.live_count = 3
                    self.frame(int(self.now))
                finally:
                    logger.removeHandler(handler)
                    logger.setLevel(old_level)
                controller = DesktopController()
                controller._roles['worker']['messages'] = messages
                text = '\n'.join(controller.drain_messages())
                self.assertIn('[observe/' + code + ']', text)
                self.assertIn('ตรวจภาพ', text)
                self.assertNotIn('private-url-key', text)
                self.assertNotIn('พบปัญหา ตรวจการตั้งค่า', text)
                self.assertEqual(self.worker.count, 0)
                self.assertEqual(self.worker.live_count, 0)
                self.sender.assert_not_called()

    def test_threshold_and_shadow_are_visible_without_sending(self):
        with patch('smart_tapo.worker.LOG.info') as log:
            for sequence in range(1, 11):
                self.frame(sequence)
        records = [call.args for call in log.call_args_list]
        self.assertIn((DETECTION, 'PRESENT', 10, 10, 10, 'ok'), records)
        self.assertIn((NOTIFICATION, 'shadow'), records)
        self.sender.assert_not_called()
        self.assertEqual(self.worker.db.execute('SELECT status FROM notifications').fetchone()[0], 'shadow')

    def test_freshness_unknown_and_historical_proof_are_distinguished(self):
        with patch('smart_tapo.worker.LOG.info') as log:
            self.frame(1, fresh=False)
            self.frame(2, age=100)
            self.detector.observe = lambda path: SimpleNamespace(label='UNKNOWN', alignment_reason='synthetic-private-value')
            self.frame(3)
        records = [call.args for call in log.call_args_list if call.args[0] == DETECTION]
        self.assertIn((DETECTION, 'UNKNOWN', 0, 10, 0, 'not_fresh'), records)
        self.assertIn((DETECTION, 'PRESENT', 1, 10, 0, 'stale'), records)
        self.assertIn((DETECTION, 'UNKNOWN', 0, 10, 0, 'alignment'), records)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def test_notification_result_visible_and_failed_is_not_success(self):
        self.worker.config = replace(self.config, notify_enabled=True, stuck_consecutive_frames=1)
        self.sender.return_value = 'failed'
        with patch('smart_tapo.worker.LOG.info') as log:
            self.frame(1)
            self.worker.drain(wait=True)
        records = [call.args for call in log.call_args_list]
        self.assertIn((NOTIFICATION, 'queued'), records)
        self.assertIn((NOTIFICATION, 'failed'), records)
        self.assertIsNone(self.worker.active()['last_success'])

    def test_unchanged_detection_status_is_rate_limited(self):
        self.worker.config = replace(self.config, stuck_consecutive_frames=10000)
        with patch('smart_tapo.worker.LOG.info') as log:
            for sequence in range(1, 101):
                self.frame(sequence, elapsed=.01)
        records = [call for call in log.call_args_list if call.args[0] == DETECTION]
        self.assertEqual(len(records), 1)

    def test_threshold_is_visible_when_one_scan_processes_a_burst(self):
        with patch('smart_tapo.worker.LOG.info') as log:
            for sequence in range(1, 11):
                self.frame(sequence, elapsed=.01)
        self.assertIn((DETECTION, 'PRESENT', 10, 10, 10, 'ok'),
                      [call.args for call in log.call_args_list])

    def test_empty_result_reports_measured_score_and_both_thresholds(self):
        self.detector.observe = lambda path: Observation('EMPTY', score=12.5, ratio=.208)
        with patch('smart_tapo.worker.LOG.info') as log:
            self.frame(1)
        self.assertIn((QUALITY, 12.5, 15.0, .208, .12), [call.args for call in log.call_args_list])
        self.assertEqual(self.worker.count, 0)
        self.sender.assert_not_called()

    def test_repeated_alignment_reason_is_rate_limited_with_detection(self):
        self.detector.observe = lambda path: Observation('UNKNOWN', alignment_reason='not_enough_anchor_matches')
        with patch('smart_tapo.worker.LOG.info') as log:
            for sequence in range(1, 101):
                self.frame(sequence, elapsed=.01)
        alignment = [call for call in log.call_args_list
                     if call.args[0] == 'Alignment unavailable: %s; consecutive proof reset']
        self.assertEqual(len(alignment), 1)

    def test_unknown_alignment_reason_does_not_echo_arbitrary_text(self):
        self.detector.observe = lambda path: Observation('UNKNOWN', alignment_reason='synthetic-private-token')
        with patch('smart_tapo.worker.LOG.info') as log:
            self.frame(1)
        records = [call.args for call in log.call_args_list]
        self.assertIn(('Alignment unavailable: %s; consecutive proof reset', 'unclassified_alignment_failure'), records)
        self.assertNotIn('synthetic-private-token', repr(records))

    def test_real_log_adapter_reports_progress_and_shadow_without_credentials(self):
        from smart_tapo.audit_log import AuditLog
        import json
        messages = queue.Queue()
        audit = AuditLog(self.config.data_dir, 'worker')
        self.addCleanup(audit.close)
        handler = _SafeLog(messages, 'worker', audit=audit)
        logger = logging.getLogger('smart_tapo.worker')
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, handler)
        for sequence in range(1, 11):
            self.frame(sequence)
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('ค้าง', text)
        self.assertIn('10/10', text)
        self.assertIn('shadow mode', text)
        self.assertNotIn(str(self.config.data_dir), text)
        rows = [json.loads(line) for line in (self.config.data_dir/'logs/worker.jsonl').read_text(encoding='utf-8').splitlines()]
        event_id = self.worker.active()['id']
        shadow = next(row for row in rows if row['kind'] == 'notification_shadow')
        self.assertEqual(shadow['detail']['event_id'], event_id)
        self.assertEqual(shadow['detail']['notification_id'], self.worker.db.execute('SELECT id FROM notifications').fetchone()[0])
        self.assertTrue(any(row['kind'] == 'frame_context' and row['detail'].get('event_id') == event_id for row in rows))
        self.sender.assert_not_called()


class DesktopDiagnosticTests(unittest.TestCase):
    def test_live_reason_and_pending_frame_are_visible_without_raw_exception(self):
        from smart_tapo.capture.live_verifier import LiveVerificationError
        messages=queue.Queue()
        handler=_SafeLog(messages,'capture')
        handler.emit(self.record('Capture unavailable: %s',(LiveVerificationError('incomplete_live_layout'),)))
        handler.emit(self.record('Capture unavailable: %s',(ValueError('private-user-token'),)))
        controller=DesktopController();controller._roles['capture']['messages']=messages
        text='\n'.join(controller.drain_messages())
        self.assertIn('[incomplete_live_layout]',text);self.assertNotIn('private-user-token',text)
        messages=queue.Queue();_SafeLog(messages,'worker').emit(self.record('Frame decision deferred: navigation_busy',()))
        controller._roles['worker']['messages']=messages
        self.assertIn('เก็บเฟรมเดิม', '\n'.join(controller.drain_messages()))

    def record(self, message, args):
        return logging.LogRecord('smart_tapo.worker', logging.INFO, __file__, 1, message, args, None)

    def test_safe_adapter_and_controller_display_proof_and_notification_outcome(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        handler.emit(self.record(DETECTION, ('PRESENT', 10, 10, 10, 'ok')))
        handler.emit(self.record(NOTIFICATION, ('failed',)))
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('ค้าง', text)
        self.assertIn('10/10', text)
        self.assertIn('ส่งแจ้งเตือนไม่สำเร็จ', text)

    def test_ready_message_identifies_the_diagnostic_build(self):
        from smart_tapo.diagnostics import DIAGNOSTIC_BUILD
        messages = queue.Queue()
        messages.put(('worker', 'ready'))
        controller = DesktopController()
        controller._roles['worker'].update(state='starting', messages=messages)
        self.assertIn('[diagnostics ' + DIAGNOSTIC_BUILD + ']', '\n'.join(controller.drain_messages()))

    def test_observation_diagnostic_rejects_unbounded_fields_and_capture_role(self):
        template = 'Frame observation unavailable: stage=%s cause=%s'
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        for args in (('private-stage', 'opencv_error'), ('observe', 'private-secret'),
                     ('observe', ['invalid']), ('observe',), 'private-secret'):
            handler.emit(self.record(template, args))
        _SafeLog(messages, 'capture').emit(self.record(template, ('observe', 'opencv_error')))
        self.assertTrue(messages.empty())
        record = self.record('Unrelated worker warning', ())
        record.levelno = logging.WARNING
        handler.emit(record)
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('พบปัญหา', text)
        self.assertNotIn('[observe/', text)

    def test_adapter_does_not_echo_unvalidated_diagnostic_values(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        handler.emit(self.record(DETECTION, ('synthetic-private-token', 1, 10, 1, 'ok')))
        handler.emit(self.record(DETECTION, ('PRESENT', 'synthetic-private-token', 10, 1, 'ok')))
        handler.emit(self.record(DETECTION, ('PRESENT', 1, 10, 1, ['invalid'])))
        handler.emit(self.record(NOTIFICATION, ('synthetic-private-token',)))
        self.assertTrue(messages.empty())

    def test_success_and_ambiguous_do_not_claim_phone_delivery_or_ack(self):
        for status, expected in (('success', 'ไม่ใช่การยืนยันว่ามือถือดัง'),
                                 ('ambiguous', 'อาจส่งถึงแล้ว')):
            with self.subTest(status=status):
                messages = queue.Queue()
                _SafeLog(messages, 'worker').emit(self.record(NOTIFICATION, (status,)))
                controller = DesktopController()
                controller._roles['worker']['messages'] = messages
                self.assertIn(expected, '\n'.join(controller.drain_messages()))

    def test_alignment_reason_and_quality_are_visible_but_invalid_values_are_not(self):
        messages = queue.Queue()
        handler = _SafeLog(messages, 'worker')
        handler.emit(self.record('Alignment unavailable: %s; consecutive proof reset', ('not_enough_anchor_matches',)))
        handler.emit(self.record(QUALITY, (12.5, 15.0, .208, .12)))
        handler.emit(self.record(QUALITY, ('synthetic-private-token', 15.0, .208, .12)))
        handler.emit(self.record(QUALITY, (float('nan'), 15.0, .208, .12)))
        controller = DesktopController()
        controller._roles['worker']['messages'] = messages
        text = '\n'.join(controller.drain_messages())
        self.assertIn('จุดอ้างอิงตรงกันไม่พอ', text)
        self.assertIn('12.50/15.00', text)
        self.assertIn('20.8%/12.0%', text)
        self.assertNotIn('synthetic-private-token', text)
        self.assertNotIn('nan', text)


if __name__ == '__main__':
    unittest.main()
