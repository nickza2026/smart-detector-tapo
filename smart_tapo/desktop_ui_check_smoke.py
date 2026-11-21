"""Fake-only bridge check for the real bounded desktop log widget."""
import json
import logging
from pathlib import Path
import queue
import tempfile


def smoke_ui_check_diagnostics(root,app):
    if app.config.adb_serial!='synthetic-device' or app.config.notify_enabled or app._active() or app._audio_active():
        raise ValueError('UI check smoke requires idle synthetic configuration')
    from .audit_log import AuditLog
    from .desktop_controller import _SafeLog
    record=app.controller._roles['capture']
    original=record['messages']
    channel=queue.Queue(maxsize=16)
    base=dict(state='pending_expired',reason='proof_expired',stage='unknown',
        check_seconds=6.25,proof_age_seconds=11.25,lock_seconds=None,
        foreground_seconds=None,dump_seconds=None,read_seconds=None,
        cleanup_seconds=None,parse_seconds=None)
    samples=[base,dict(base,state='failed',reason='adb_timeout',stage='ui_dump'),
        dict(base,state='failed',reason='ui_lock_busy',stage='lock',proof_age_seconds=None),
        dict(base,state='rejected',reason='camera_not_confirmed',stage='classify'),
        dict(base,state='valid',reason='none',stage='classify',proof_age_seconds=3.)]
    try:
        record['messages']=channel
        with tempfile.TemporaryDirectory(prefix='smart-tapo-ui-check-smoke-') as directory:
            audit=AuditLog(Path(directory),'capture')
            try:
                handler=_SafeLog(channel,'capture',audit)
                for detail in [*samples,dict(base,xml='SECRET UI XML')]:
                    message=logging.LogRecord('synthetic',logging.INFO,'',0,
                        'Live UI check diagnostics',(),None)
                    message.ui_check_context=detail
                    handler.emit(message)
            finally:audit.close()
            rows=[json.loads(line) for line in (Path(directory)/'logs/capture.jsonl').read_text(encoding='utf-8').splitlines()]
            if len(rows)!=5 or any(row['kind']!='ui_check' for row in rows):
                raise RuntimeError('UI diagnostic audit bridge failed')
            for text in app.controller.drain_messages():app._append_log(text)
            root.update()
            text=app.log.get('1.0','end')
            if not all(value in text for value in ('หมดอายุขณะรอ','ADB หมดเวลารอ',
                'รอ lock ไม่สำเร็จ','ไม่ยืนยันชื่อกล้อง','อายุหลักฐาน=ยังไม่ทราบ','6.250')):
                raise RuntimeError('UI diagnostic rendering incomplete')
            if 'SECRET' in text or 'SECRET' in repr(rows):
                raise RuntimeError('Unsafe UI diagnostic data escaped')
        return dict(ui_check_diagnostics_gui=True,ui_check_diagnostics_redacted=True,
                    ui_check_diagnostics_fake_only=True)
    finally:record['messages']=original
