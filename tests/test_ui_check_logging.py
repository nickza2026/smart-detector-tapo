"""Synthetic, credential-free Live UI diagnostic bridge; no ADB or GUI capture."""
import json
import logging
from pathlib import Path
import queue
import tempfile
import unittest

from smart_tapo.audit_log import AuditLog, valid_detail
from smart_tapo.desktop_controller import DesktopController, _SafeLog


def detail(**changes):
    result=dict(state='pending_expired',reason='proof_expired',stage='ui_dump',
        check_seconds=6.25,proof_age_seconds=11.25,lock_seconds=.125,
        foreground_seconds=.25,dump_seconds=None,read_seconds=None,
        cleanup_seconds=None,parse_seconds=None)
    result.update(changes)
    return result


class UICheckLoggingTests(unittest.TestCase):
    def test_diagnostics_persist_as_bounded_public_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            log=AuditLog(Path(directory),'capture')
            self.assertTrue(log.write('ui_check',detail()))
            log.close()
            record=json.loads((Path(directory)/'logs/capture.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(record['kind'],'ui_check')
            self.assertIsNone(record['detail']['dump_seconds'])
            self.assertEqual(record['detail']['check_seconds'],6.25)

    def test_handler_forwards_only_validated_fixed_template(self):
        channel=queue.Queue()
        record=logging.LogRecord('synthetic',logging.INFO,'',0,
            'Live UI check diagnostics',(),None)
        record.ui_check_context=detail()
        handler=_SafeLog(channel,'capture')
        handler.emit(record)
        self.assertFalse(channel.empty(),'UI check diagnostics disappeared before the UI queue')
        payload=channel.get_nowait()
        self.assertEqual(payload[:3],('capture','ui_check',detail()))
        for bad in (detail(xml='SECRET XML'),detail(reason='SECRET TOKEN'),
                    detail(check_seconds=float('nan')),detail(lock_seconds=True),
                    detail(proof_age_seconds=-1)):
            record.ui_check_context=bad
            handler.emit(record)
            self.assertTrue(channel.empty())
            self.assertFalse(valid_detail('ui_check',bad))

    def test_controller_renders_distinct_timeout_and_expired_pending(self):
        controller=DesktopController()
        channel=queue.Queue()
        controller._roles['capture']['messages']=channel
        channel.put(('capture','ui_check',detail()))
        channel.put(('capture','ui_check',detail(state='failed',reason='adb_timeout')))
        channel.put(('capture','ui_check',detail(xml='SECRET XML')))
        text='\n'.join(controller.drain_messages())
        self.assertIn('หมดอายุขณะรอ',text)
        self.assertIn('ADB หมดเวลารอ',text)
        self.assertIn('6.250',text)
        self.assertNotIn('SECRET',text)

    def test_unknown_proof_age_is_not_rendered_as_measured_zero(self):
        controller=DesktopController()
        channel=queue.Queue()
        controller._roles['capture']['messages']=channel
        channel.put(('capture','ui_check',detail(state='failed',reason='ui_lock_busy',
            proof_age_seconds=None)))
        text='\n'.join(controller.drain_messages())
        self.assertIn('อายุหลักฐาน=ยังไม่ทราบ',text)
        self.assertIn('รอ lock ไม่สำเร็จ',text)
