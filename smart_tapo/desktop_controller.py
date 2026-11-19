"""Spawn-only desktop process supervision; no GUI or hardware work in the parent."""
from __future__ import annotations

import logging
import math
import multiprocessing
import os
import queue
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from smart_tapo.audit_log import AuditLog, valid_detail, safe_runtime_failure
from smart_tapo.history import HISTORY_TIMEZONE
from smart_tapo.runtime import ProcessLock
from smart_tapo.diagnostics import ALIGNMENT_HINTS, DIAGNOSTIC_BUILD, OBSERVATION_ERROR_HINTS


def _emit(messages, role, kind, detail=None, created=None, audit=None):
    created = time.time() if created is None else created
    persisted = audit is None or audit.write(kind, detail, created=created)
    if messages is not None:
        try:
            messages.put_nowait((role, kind, detail, created))
        except queue.Full:
            pass
    return persisted


def _dated(text, created=None):
    created = time.time() if created is None else created
    try:
        stamp = datetime.fromtimestamp(created, tz=HISTORY_TIMEZONE).strftime('%d/%m/%Y %H:%M:%S')
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return '[' + stamp + ' UTC+7] ' + text


def _detection_hint(detail):
    reasons = {
        'ok': 'ภาพผ่านการตรวจ',
        'not_fresh': 'นาฬิกาในภาพยังไม่เปลี่ยน จึงยังไม่นับเป็นภาพสด',
        'future': 'เวลาภาพอยู่ในอนาคต ตรวจนาฬิกาเครื่อง',
        'unhealthy': 'สถานะภาพไม่พร้อม จึงเริ่มนับใหม่',
        'stale': 'ภาพย้อนหลัง ไม่ใช้ส่งเตือนสด',
        'gap': 'ภาพขาดช่วงหรือเปลี่ยน session เริ่มนับใหม่',
        'alignment': 'จัดแนวภาพไม่มั่นใจ จึงยังยืนยันวัตถุไม่ได้',
        'detector_error': 'อ่านหรือวิเคราะห์ภาพไม่ได้',
        'unknown': 'ผลภาพไม่ชัดเจน จึงยังยืนยันวัตถุไม่ได้',
        'navigation_recovery': 'กำลังกู้/เปลี่ยนหน้า Live ไม่ใช้ภาพเก่านับต่อเนื่อง',
        'navigation_unavailable': 'ตรวจสถานะการกู้ Live ไม่ได้ พักการยืนยันภาพไว้ก่อน',
        'clock_epoch_changed': 'ภาพจากก่อนเริ่มระบบหรือไม่มีหลักฐานรอบระบบ ไม่ใช้แจ้งเตือนสด',
    }
    if not isinstance(detail, tuple) or len(detail) != 5:
        return None
    label, count, threshold, live, reason = detail
    if (type(label) is not str or type(reason) is not str
            or label not in ('PRESENT', 'EMPTY', 'UNKNOWN') or reason not in reasons
            or any(type(value) is not int or not 0 <= value <= 2_147_483_647
                   for value in (count, threshold, live))
            or threshold < 1 or live > count):
        return None
    if label == 'PRESENT':
        title = ('สงสัยแบงค์/วัตถุค้าง — ถึงเกณฑ์ภาพสด' if live >= threshold else
                 'พบวัตถุถึงเกณฑ์จากประวัติ แต่ภาพสดยังไม่ครบ' if count >= threshold else
                 'พบวัตถุในกรอบตรวจ')
    else:
        title = 'กรอบตรวจว่าง' if label == 'EMPTY' else 'ยังยืนยันภาพไม่ได้'
    return f'{title}: นับ {count}/{threshold} ภาพ; ภาพสด {live}/{threshold}; {reasons[reason]}'


def _quality_hint(detail):
    if (not isinstance(detail, tuple) or len(detail) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in detail)):
        return None
    score, threshold, ratio, ratio_threshold = detail
    if not (0 <= score <= 255 and 0 < threshold <= 1_000_000_000
            and 0 <= ratio <= 1 and 0 < ratio_threshold <= 1):
        return None
    return (f'ค่าความต่าง {score:.2f}/{threshold:.2f}; '
            f'สัดส่วนพิกเซลเปลี่ยน {ratio*100:.1f}%/{ratio_threshold*100:.1f}% '
            '(ต้องผ่านทั้งสองค่า)')


def _rtsp_hint(detail):
    if not valid_detail('rtsp_status',detail):return None
    labels={'calibration_required':'URL/ขนาด RTSP ยังไม่ผูกกับภาพช่องว่างใหม่',
        'reconnecting':'รับภาพขาดหรือเปิดไม่ได้ กำลังเชื่อม RTSP ใหม่',
        'geometry_changed':'ขนาดภาพสตรีมเปลี่ยน พักส่งภาพและต้องยืนยันภาพช่องว่างใหม่',
        'stale':'ภาพค้างในคิวเกินอายุ พักใช้ภาพนี้',
        'timestamp_unconfirmed':'timestamp ยังไม่ยืนยันหรือกำลังสะสมหลักฐาน พักส่งภาพสด',
        'healthy':'รับภาพ RTSP ผ่านเกณฑ์เวลา/ขนาด ไม่ใช่ยืนยันสุขภาพเสียงหรือ Worker',
        'storage_limit':'พื้นที่ภาพถึงขีดจำกัด หยุดรับภาพใหม่',
        'overflow':'คิว RAM เต็ม Worker รับภาพไม่ทัน เริ่มสะสมหลักฐานใหม่ ไม่ต่อข้ามภาพที่ขาด',
        'starting':'กำลังเปิดตัวรับภาพ RTSP','waiting':'ยังไม่มีภาพ RTSP ใหม่',
        'stopped':'หยุดตัวรับภาพ RTSP แล้ว'}
    return (labels[detail['reason']]+' ['+detail['reason']+']; ถอดภาพสะสม='
        +str(detail['decoded'])+'; ข้ามภาพคิวเก่า='+str(detail['dropped'])
        +' (ไม่ใช่จำนวนหยอดแบงค์)')


def _passage_loss_hint(detail):
    if not valid_detail('passage_loss', detail):
        return None
    since = _dated('', detail['since'])
    if since is None:
        return None
    reasons = {'expired': 'ผลตัวติดตามแบงค์หมดอายุก่อนส่งถึง Worker (ภาพไม่พร้อมส่งนานเกินไป)',
               'reconnect': 'สตรีมหลุดก่อนส่งผลตัวติดตามแบงค์ถึง Worker',
               'worker_gap': 'Worker ไม่ได้รับภาพที่ใช้ได้นานเกินเวลาที่เก็บผลตัวติดตามแบงค์'}
    return (reasons[detail['reason']] + ' — แบงค์ที่ผ่านหรือดึงกลับตั้งแต่ ' + since.strip()
            + ' อาจไม่ถูกบันทึก ตรวจประวัติช่วงนี้ด้วยตา [' + detail['reason'] + ']')


def _ui_check_hint(detail):
    if not valid_detail('ui_check',detail):return None
    states={'valid':'ยืนยันหน้า Live สำเร็จ',
            'pending_expired':'หลักฐานหน้า Live หมดอายุขณะรอผลตรวจใหม่ — พักส่งเฟรม',
            'rejected':'อ่านหน้าแล้วไม่ยืนยัน Live — พักส่งเฟรม',
            'failed':'ตัวตรวจหน้า Live ล้มเหลว — พักส่งเฟรม'}
    reasons={'none':'ผ่าน','proof_expired':'รอผลตรวจเกินอายุหลักฐาน',
        'adb_timeout':'ADB หมดเวลารอ','adb_unavailable':'เรียก ADB ไม่ได้',
        'adb_nonzero':'ADB คืนสถานะล้มเหลว','ui_lock_busy':'รอ lock ไม่สำเร็จ',
        'ui_lock_unavailable':'เปิด lock สำหรับตรวจ UI ไม่สำเร็จ',
        'ui_dump_failed':'สร้าง UI dump ไม่สำเร็จ','ui_hierarchy_invalid':'โครงสร้าง UI ใช้ไม่ได้',
        'verification_exception':'ตัวตรวจผิดพลาดโดยยังไม่มีสาเหตุเฉพาะ',
        'legacy_rejected':'ตัวตรวจเดิมไม่ยืนยัน Live',
        'camera_not_confirmed':'ไม่ยืนยันชื่อกล้องที่เลือก',
        'ambiguous_camera_title':'ชื่อกล้องกำกวม','incomplete_live_layout':'องค์ประกอบหน้า Live ไม่ครบ',
        'observed_playback_layout':'พบหน้า Playback','incomplete_playback_layout':'พบองค์ประกอบ Playback ไม่ครบ',
        'playback_or_conflicting_controls':'พบปุ่ม Playback หรือหน้าขัดแย้ง',
        'live_inactivity_confirmation':'พบป๊อปอัปยืนยันดูต่อ','live_refresh_required':'พบหน้าที่ต้องรีเฟรช',
        'foreground_changed_or_unknown':'แอปด้านหน้าเปลี่ยนหรือไม่ยืนยัน',
        'unrecognized_foreground':'ไม่รู้จักแอปด้านหน้า','invalid_hierarchy':'โครงสร้าง UI ไม่ถูกต้อง',
        'sensitive_page':'อยู่หน้าที่ไม่อนุญาตให้ใช้ภาพ','unsupported_package':'แอปไม่ตรงที่ตั้งไว้',
        'navigation_in_progress':'กำลังกู้หรือเปลี่ยนหน้า',
        'selected_camera_authorization_missing':'หลักฐานอนุญาตกล้องเดิมไม่ครบ'}
    stages={'idle':'ยังไม่เริ่ม','lock':'รอ lock','foreground_before':'ตรวจแอปก่อนอ่าน',
        'ui_dump':'สร้าง UI dump','ui_read':'อ่าน UI dump','ui_cleanup':'ล้างไฟล์ UI ที่สร้างเอง',
        'foreground_after':'ตรวจแอปหลังอ่าน','classify':'จำแนกหน้า','unknown':'ยังไม่ทราบขั้นตอน'}
    def seconds(key):
        value=detail[key]
        return 'ยังไม่ทราบ' if value is None else f'{value:.3f}s'
    parts=[states.get(detail['state'],'ยังไม่ยืนยันผลตรวจ'),
        reasons.get(detail['reason'],'ยังไม่มีสาเหตุเฉพาะ')+' ['+detail['reason']+']',
        'ขั้น='+stages.get(detail['stage'],'ยังไม่ทราบขั้นตอน'),
        'นับจากขอตรวจ='+seconds('check_seconds'),'อายุหลักฐาน='+seconds('proof_age_seconds')]
    for key,label in (('lock_seconds','lock'),('foreground_seconds','foreground'),
                      ('dump_seconds','dump'),('read_seconds','read'),
                      ('cleanup_seconds','cleanup'),('parse_seconds','parse')):
        parts.append(label+'='+seconds(key))
    return '; '.join(parts)


class _SafeLog(logging.Handler):
    def __init__(self, messages, role, audit=None):
        super().__init__(logging.INFO)
        self.messages, self.role = messages, role
        self.audit = audit
        self._audit_warned = False
        self._failure_reported = False

    def _send(self, kind, detail, created):
        if not _emit(self.messages, self.role, kind, detail, created, self.audit) and not self._audit_warned:
            self._audit_warned = True
            _emit(self.messages, self.role, 'log_unavailable', created=created)

    def emit(self, record):
        # Translate known app templates to safe operator hints, never arbitrary
        # exception strings, camera UI XML, paths or credential-bearing args.
        template = str(record.msg)
        if template == 'Safe runtime failure':
            detail = getattr(record, 'runtime_failure', None)
            if valid_detail('runtime_failure', detail):
                self._send('runtime_failure', detail, record.created)
                if not detail['recoverable']:
                    self._runtime_failure_reported = True
            return
        if template == 'Rejected frame marker; new proof required':
            if self.role == 'worker':self._send('frame_rejected',None,record.created)
            return
        if template == 'Frame observation unavailable: stage=%s cause=%s':
            args = record.args
            if (self.role == 'worker' and type(args) is tuple and len(args) == 2
                    and args[0] == 'observe' and type(args[1]) is str
                    and args[1] in OBSERVATION_ERROR_HINTS):
                # The existing audit schema only supports a detail-free warning.
                # Keep that durable warning and carry typed, bounded GUI detail
                # separately rather than pretending unsupported audit data saved.
                if self.audit is not None and not self.audit.write('warning', created=record.created):
                    if not self._audit_warned:
                        self._audit_warned = True
                        _emit(self.messages, self.role, 'log_unavailable', created=record.created)
                _emit(self.messages, self.role, 'observation_error', args, record.created)
            return
        if template == 'Passage verdicts may be lost':
            detail = getattr(record, 'passage_loss', None)
            if valid_detail('passage_loss', detail):
                self._send('passage_loss', detail, record.created)
            return
        if template == 'RAM frame expired during analysis':
            detail = getattr(record, 'analysis_seconds', None)
            if valid_detail('analysis_expired', detail):
                self._send('analysis_expired', detail, record.created)
            return
        if template == 'RTSP image status: %s':
            context=getattr(record,'rtsp_context',None)
            if valid_detail('rtsp_status',context):self._send('rtsp_status',context,record.created)
            return
        if template == 'Live UI check diagnostics':
            context=getattr(record,'ui_check_context',None)
            if valid_detail('ui_check',context):self._send('ui_check',context,record.created)
            return
        if template in ('Live overlay recovery started','Live overlay recovery restored'):
            self._send('ui_recovery_started' if template.endswith('started') else 'ui_recovery_restored',None,record.created)
            return
        context = getattr(record, 'frame_context', None)
        if context is not None and valid_detail('frame_context', context):
            self._send('frame_context', context, record.created)
        if template == 'Capture sample':
            return
        if template == 'Frame decision deferred: navigation_busy':
            self._send('frame_deferred',None,record.created)
            return
        if template == 'Detection status: label=%s count=%d/%d live=%d reason=%s':
            if _detection_hint(record.args) is not None:
                self._send('detection', record.args, record.created)
            return
        if template == 'Notification status: %s':
            if (isinstance(record.args, tuple) and len(record.args) == 1
                    and record.args[0] in ('queued', 'shadow', 'success', 'failed', 'ambiguous')):
                detail = getattr(record, 'notification_context', None)
                if valid_detail('notification_' + record.args[0], detail):
                    self._send('notification_' + record.args[0], detail, record.created)
            return
        if template == 'Notification deferred':
            detail = getattr(record, 'notification_context', None)
            if valid_detail('notification_deferred', detail):
                self._send('notification_deferred', detail, record.created)
            return
        if template == 'Visual quality: score=%.3f threshold=%.3f changed=%.4f threshold=%.4f':
            if _quality_hint(record.args) is not None:
                self._send('quality', record.args, record.created)
            return
        if template.startswith('Alignment unavailable:'):
            if (isinstance(record.args, tuple) and len(record.args) == 1
                    and type(record.args[0]) is str and record.args[0] in ALIGNMENT_HINTS):
                self._send('alignment_detail', record.args[0], record.created)
                return
            kind = 'alignment'
        elif template.startswith('Camera view changed:'):
            kind = 'view_changed'
        elif template.startswith('Image quota full'):
            kind = 'quota'
        elif template.startswith('Live UI verification pending:'):
            kind = 'ui_unverified'
        elif template == 'Live UI verification restored':
            kind = 'ui_verified'
        elif template.startswith('Capture unavailable:'):
            kind = 'connection'
            reason=getattr(record.args[0],'reason',None) if record.args else None
            if valid_detail('ui_reason',reason):self._send('ui_reason',reason,record.created)
            if record.args and str(record.args[0]).startswith('Screenshot'):
                kind = 'screenshot'
            elif record.args and str(record.args[0]).startswith(('Live view', 'Live UI', 'Invalid UI hierarchy')):
                kind = 'ui_unverified'
        elif record.levelno >= logging.WARNING:
            kind = 'warning'
        else:
            return
        self._send(kind, None, record.created)


def _report_runtime_failure(handler, error):
    channel = getattr(handler, 'failure_channel', None)
    if channel is not None and not getattr(handler, '_capture_invalidated', False):
        handler._capture_invalidated = True
        channel.invalidate()
    if not getattr(handler, '_runtime_failure_reported', False):
        detail = safe_runtime_failure(error, getattr(handler, 'failure_stage', 'unknown'))
        handler._send('runtime_failure', detail, time.time())
        handler._runtime_failure_reported = True


@contextmanager
def audit_session(data_dir, role, messages=None, handler=None, scope=None):
    """Caller holds the role lock until this session flushes and closes."""
    root = logging.getLogger()
    own_handler = handler is None
    handler = handler or _SafeLog(messages, role)
    audit = AuditLog(data_dir, role, scope=scope)
    handler.audit = audit
    if own_handler:
        root.addHandler(handler)
    try:
        yield handler
    except BaseException as error:
        _report_runtime_failure(handler, error)
        handler._failure_reported = True
        handler._send('failed', None, time.time())
        raise
    finally:
        handler.audit = None
        audit.close()
        if own_handler:
            root.removeHandler(handler)


def _child(role, config, stop, messages, notify_key='', frame_channel=None):
    root = logging.getLogger()
    handler = _SafeLog(messages, role)
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    handler.failure_stage = 'configuration'
    handler.failure_channel = frame_channel if role == 'capture' else None
    try:
        config.validate()
        # Do not inherit ambient credentials, even when notifications are disabled.
        os.environ.pop('NOTIFY_API_KEY', None)
        if role == 'worker' and config.notify_enabled:
            if not notify_key:
                raise ValueError('Missing credentials')
            os.environ['NOTIFY_API_KEY'] = notify_key
        lock = 'worker.lock' if role == 'worker' else 'capture-' + config.camera_id + '.lock'
        handler.failure_stage = 'process_lock'
        with ProcessLock(config.data_dir / lock), audit_session(
                config.data_dir, role, messages, handler, scope=config.camera_id if role == 'capture' else None):
            handler.failure_stage = 'capture' if role == 'capture' else 'worker'
            if role == 'capture':
                from smart_tapo.capture import capture
                handler._send('ready', None, time.time())
                if frame_channel is not None:
                    if config.capture_backend != 'rtsp':
                        raise ValueError('RAM video requires RTSP')
                    from smart_tapo.capture.rtsp import capture_rtsp
                    result = capture_rtsp(config, stop, channel=frame_channel)
                else:
                    result = capture(config, stop)
                if result:
                    raise RuntimeError('Capture failed')
            else:
                from smart_tapo.worker import Worker
                worker = Worker(config)
                worker.health_observer = lambda detail: _emit(messages, role, 'health_observation', detail)
                worker.preview_observer = lambda detail: _emit(messages, role, 'tracking_preview', detail)
                try:
                    handler._send('ready', None, time.time())
                    while not stop.is_set():
                        if frame_channel is None:
                            worker.scan()
                            stop.wait(config.poll_interval_seconds)
                        else:
                            generation = frame_channel.generation
                            worker.memory_generation(generation)
                            envelope = frame_channel.receive()
                            if envelope is not None:
                                generation, fresh_after = frame_channel.snapshot()
                                worker.process_memory(envelope, generation=generation, fresh_after=fresh_after,
                                                      scope_check=frame_channel.snapshot,
                                                      generation_guard=frame_channel.epoch.get_lock())
                            worker.drain()
                            if time.time() - worker.last_cleanup >= config.cleanup_interval_seconds:
                                worker.cleanup()
                                worker.last_cleanup = time.time()
                except OSError:
                    _emit(messages, role, 'evidence_unavailable')
                    handler._send('warning', None, time.time())
                    raise
                except Exception as error:
                    from smart_tapo.evidence_budget import EvidenceStorageError
                    if isinstance(error, EvidenceStorageError):
                        _emit(messages, role, 'evidence_unavailable')
                        handler._send('warning', None, time.time())
                    raise
                finally:
                    worker.close()
            handler._send('stopped', None, time.time())
    except BaseException as error:
        if not handler._failure_reported:
            _report_runtime_failure(handler, error)
            handler._send('failed', None, time.time())
        # Exit status signals failure even if the bounded message queue is full.
        raise SystemExit(1) from None


class DesktopController:
    video_storage_mode = 'ram'

    def __init__(self):
        self._context = multiprocessing.get_context('spawn')
        from .ram_frames import FrameChannel
        self._frame_manager = self._context.Manager()
        self._frame_channel = FrameChannel(self._context, self._frame_manager.Queue(maxsize=2))
        self._roles = {role: dict(state='stopped', process=None, stop=None, messages=None)
                       for role in ('capture', 'worker')}
        self._messages = deque(maxlen=128)
        self._transport_closed = False
        self._health_expected = False
        self._health_generation = 0
        self._health_since = time.monotonic()
        self._health_good = self._health_observed = None
        self._health_reason = 'starting'
        self._health_max_age = 10.
        self._tracking_packet = None
        self._tracking_order = None
        self._tracking_size = None
        self._tracking_since = self._health_since

    def close(self):
        """Release owned IPC after roles have stopped and been reaped.

        Ordinary stop_all intentionally keeps IPC alive for later role restarts.
        Callers must finish their existing orderly shutdown/poll loop first.
        """
        if self._transport_closed:
            return
        self.snapshot()
        if any(record['process'] is not None for record in self._roles.values()):
            raise RuntimeError('Stop and reap video roles before closing transport')
        self._tracking_packet = self._tracking_order = None
        self._frame_manager.shutdown()
        self._transport_closed = True

    def shutdown_transport(self):
        self.close()

    def _message(self, text, created=None):
        result = _dated(text, created)
        if result is not None:
            self._messages.append(result)

    def _role(self, role):
        if role not in self._roles:
            raise ValueError('Unknown process role')
        return self._roles[role]

    def start_capture(self, config):
        return self._start('capture', config)

    def start_worker(self, config, notify_key=''):
        return self._start('worker', config, notify_key)

    def _start(self, role, config, notify_key=''):
        if self._transport_closed:
            raise RuntimeError('Video transport is closed')
        self.snapshot()
        record = self._role(role)
        if self.is_running(role):
            return False
        try:
            config.validate()
        except Exception:
            raise ValueError('Invalid process configuration') from None
        if role == 'worker' and config.notify_enabled and not notify_key:
            raise ValueError('Notifications require a Notify API key')
        if role == 'worker' or self._health_expected:
            self._health_expected = True
            self._health_generation += 1
            self._health_since = time.monotonic()
            self._health_good = self._health_observed = None
            self._health_sequence = None
            self._health_reason = 'starting'
            self._health_max_age = config.max_live_age_seconds
        self._tracking_packet = self._tracking_order = None
        self._tracking_size = tuple(config.expected_size)
        self._tracking_since = time.monotonic()
        self._health_max_age = config.max_live_age_seconds
        stop = self._context.Event()
        messages = self._context.Queue(maxsize=64)
        try:
            channel = self._frame_channel if self.video_storage_mode == 'ram' else None
            if channel is not None:
                channel.fence()
            process = self._context.Process(target=_child,
                        args=(role, config, stop, messages, notify_key, channel), name='SmartTapo-' + role)
            record.update(state='starting', process=process, stop=stop, messages=messages)
            process.start()
        except Exception:
            record.update(state='failed', process=None, stop=None, messages=None)
            messages.close()
            self._message(role + ': failed to start')
            return False
        self._message(role + ': starting')
        return True

    def stop(self, role):
        self._tracking_packet = self._tracking_order = None
        record = self._role(role)
        self._health_expected = False
        self._health_good = None
        if record['state'] in ('starting', 'running', 'stopping'):
            self._frame_channel.fence()
            record['stop'].set()
            record['state'] = 'stopping'

    def stop_all(self):
        for role in self._roles:
            self.stop(role)

    def snapshot(self):
        for role, record in self._roles.items():
            messages = record['messages']
            if messages is not None:
                # Bounded per poll as well as bounded at source.
                for _ in range(64):
                    try:
                        item = messages.get_nowait()
                    except queue.Empty:
                        break
                    if not isinstance(item, tuple) or len(item) not in (2, 3, 4):
                        continue
                    created = time.time()
                    if len(item) == 4:
                        created = item[3]
                        if type(created) not in (int, float) or not math.isfinite(created) or _dated('', created) is None:
                            continue
                        item = item[:2] if item[2] is None else item[:3]
                    sender, kind = item[:2]
                    if type(kind) is not str or sender != role:
                        continue
                    if kind == 'tracking_preview':
                        if role == 'worker' and len(item) == 3:
                            self._accept_tracking(item[2], time.monotonic())
                        continue
                    if kind == 'health_observation':
                        if role == 'worker' and len(item) == 3:
                            self._accept_health(item[2], time.monotonic())
                        continue
                    if kind in ('failed', 'evidence_unavailable', 'frame_rejected', 'alignment', 'alignment_detail',
                                'analysis_expired', 'connection', 'ui_unverified', 'ui_recovery_started', 'observation_error'):
                        self._health_good = None
                        self._health_reason = ('vision_worker_failed' if kind in ('failed', 'evidence_unavailable') else
                                               'vision_alignment' if kind in ('alignment', 'alignment_detail') else 'vision_quality')
                    if kind == 'runtime_failure':
                        self._health_good = None
                        self._health_reason = 'vision_worker_failed'
                        detail = item[2] if len(item) == 3 else None
                        if valid_detail(kind, detail):
                            hints = {'disk_full': 'พื้นที่เต็มหรือโควตาระบบไฟล์หมด',
                                     'permission': 'ไม่มีสิทธิ์เข้าถึงทรัพยากร',
                                     'decoder_ipc': 'ช่องส่งข้อมูลจากตัวถอดรหัสขัดข้อง',
                                     'process_lock': 'เปิดล็อกงานไม่สำเร็จ',
                                     'unknown': 'ยังไม่ทราบสาเหตุ ไม่ยืนยันว่าเป็นปัญหากล้องหรือพื้นที่'}
                            action = ' — เก็บตัวถอดรหัสเดิมแล้วรอเชื่อมต่อใหม่' if detail['recoverable'] else ' — หยุดงาน ตรวจสอบก่อนเริ่มใหม่'
                            self._message(role + ': [' + detail['stage'] + '/' + detail['kind'] + '] '
                                          + hints[detail['kind']] + action, created)
                        continue
                    if kind == 'observation_error':
                        detail = item[2] if len(item) == 3 else None
                        if (role == 'worker' and type(detail) is tuple and len(detail) == 2
                                and detail[0] == 'observe' and type(detail[1]) is str
                                and detail[1] in OBSERVATION_ERROR_HINTS):
                            self._message('worker: วิเคราะห์ภาพไม่สำเร็จ [observe/' + detail[1]
                                          + '] — ' + OBSERVATION_ERROR_HINTS[detail[1]]
                                          + '; ไม่ถือเป็นช่องว่างและไม่ส่งแจ้งเตือน', created)
                        continue
                    if kind == 'detection':
                        text = _detection_hint(item[2]) if len(item) == 3 else None
                        if text is not None:
                            self._message(role + ': ' + text, created)
                        continue
                    if kind == 'quality':
                        text = _quality_hint(item[2]) if len(item) == 3 else None
                        if text is not None:
                            self._message(role + ': ' + text, created)
                        continue
                    if kind == 'ui_check':
                        text=_ui_check_hint(item[2]) if len(item)==3 else None
                        if text is not None:self._message(role+': '+text,created)
                        continue
                    if kind == 'passage_loss':
                        text = _passage_loss_hint(item[2]) if len(item) == 3 else None
                        if text is not None:
                            self._message(role + ': ' + text, created)
                        continue
                    if kind == 'analysis_expired':
                        if len(item) == 3 and valid_detail(kind, item[2]):
                            self._message(role + ': วิเคราะห์ภาพไม่ทัน ใช้เวลา ' + f'{item[2]:.2f}'
                                          + ' วินาที ภาพหมดอายุหรือคิวถูกล้างระหว่างวิเคราะห์ — ทิ้งภาพนี้และเริ่มหลักฐานใหม่;'
                                          + ' ถ้าเกิดบ่อย ให้เพิ่มรอบถ่ายภาพหรือตรวจ CPU', created)
                        continue
                    if kind == 'rtsp_status':
                        text=_rtsp_hint(item[2]) if len(item)==3 else None
                        if text is not None:self._message(role+': '+text,created)
                        continue
                    if kind == 'alignment_detail':
                        reason = item[2] if len(item) == 3 else None
                        if type(reason) is str and reason in ALIGNMENT_HINTS:
                            self._message(role + ': จัดแนวไม่ผ่าน — '
                                          + ALIGNMENT_HINTS[reason] + ' [' + reason + ']', created)
                        continue
                    if kind == 'frame_context':
                        if len(item) == 3 and role == 'capture' and valid_detail(kind, item[2]):
                            detail = item[2]
                            self._message('capture: รับภาพลำดับ ' + str(detail.get('sequence', '?'))
                                          + '; ผ่านเกณฑ์ความสด=' + str(detail.get('fresh', False))
                                          + ' (โหมด window = ยืนยันสุขภาพทั้งช่วง)'
                                          + '; รอบใช้เวลา ' + str(round(detail.get('elapsed_seconds', 0), 3))
                                          + ' วินาที; missed slots=' + str(detail.get('missed_slots', 0)), created)
                        continue
                    if kind=='ui_reason':
                        if len(item)==3 and valid_detail(kind,item[2]):
                            self._message('capture: ตรวจหน้า Live ไม่ผ่าน ['+item[2]+'] — พักจนยืนยันกล้องเดิมได้',created)
                        continue
                    if kind == 'notification_deferred':
                        if len(item) == 3 and valid_detail(kind, item[2]):
                            reason = item[2]['reason']
                            hints = {'not_live': 'ภาพสดยังไม่ครบ', 'cooldown': 'ยังไม่ครบช่วงเตือนซ้ำ',
                                     'retry_wait': 'รอเวลาลองส่งใหม่', 'in_flight': 'การส่งก่อนหน้ายังไม่จบ'}
                            self._message('worker: ยังไม่ส่งแจ้งเตือน — ' + hints[reason], created)
                        continue
                    if kind.startswith('notification_') and len(item) == 3 and valid_detail(kind, item[2]):
                        item = item[:2]
                    hints = {
                        'ready': 'กำลังทำงาน (สถานะ process; ดูข้อความกล้องด้านล่าง) [diagnostics ' + DIAGNOSTIC_BUILD + ']',
                        'warning': 'พบปัญหา ตรวจการตั้งค่าและข้อมูลภาพก่อนใช้งานจริง',
                        'frame_rejected': 'ปฏิเสธ marker ภาพเก่า/metadata ไม่ตรงหรืออ่านไม่ได้ — ไม่นับเป็นภาพว่าง ต้องเริ่มหลักฐานใหม่; ไฟล์เดิมที่ไม่เปลี่ยนจะไม่รีเซ็ตเฟรมใหม่ซ้ำ',
                        'alignment': 'จัดแนวภาพไม่มั่นใจ จึงหยุดนับต่อเนื่องและไม่เดาแจ้งเตือน',
                        'view_changed': 'มุมกล้องเปลี่ยน เริ่มนับภาพต่อเนื่องใหม่',
                        'quota': 'พื้นที่เก็บภาพถึงขีดจำกัด หยุดรับภาพใหม่และรอ Worker cleanup',
                        'evidence_unavailable': 'Worker หยุด: บันทึกหลักฐานไม่ได้ ตรวจโควตา พื้นที่ว่าง และสิทธิ์โฟลเดอร์ — ไม่ส่งแจ้งเตือนโดยไม่มีหลักฐาน',
                        'connection': 'รับ/ยืนยันภาพไม่ผ่าน — อาจเป็น ADB, หน้า Live, clock window หรือไฟล์; ยังไม่ทราบสาเหตุแน่ชัด จะลองใหม่',
                        'screenshot': 'ภาพไม่ตรงขนาดที่ตั้งไว้หรืออ่าน PNG ไม่ได้ ตรวจความละเอียด MEmu',
                        'failed': 'เริ่มงานไม่สำเร็จ ตรวจการตั้งค่า ภาพช่องว่าง และโปรแกรมอื่นที่ใช้ข้อมูลชุดนี้',
                        'stopped': 'หยุดแล้ว',
                        'log_unavailable': 'เขียน log ย้อนหลังไม่ได้ ตรวจพื้นที่และสิทธิ์โฟลเดอร์ข้อมูล; อย่าถือว่าข้อความบนจอถูกเก็บแล้ว',
                        'ui_unverified': 'ยังยืนยันหน้า Live ไม่ได้หรือหลักฐานหมดอายุ หยุดปล่อยภาพให้ Worker โดยไม่ข้ามการตรวจ',
                        'ui_verified': 'ยืนยันหน้า Live ได้อีกครั้ง เฟรมใหม่ยังต้องผ่าน clock/freshness ตามเดิม',
                        'ui_recovery_started': 'พบป๊อปอัป/รีเฟรชที่รู้จัก กู้บนการเชื่อมต่อเดิมก่อน ไม่รอ reconnect',
                        'ui_recovery_restored': 'กู้อัตโนมัติและยืนยัน Live ใหม่แล้ว เฟรมยังต้องผ่าน clock/freshness',
                        'frame_deferred': 'รอ decision lock — เก็บเฟรมเดิมไว้ลองใหม่ ไม่เพิ่ม/รีเซ็ตชุดหลักฐานเพราะรออย่างเดียว',
                        'notification_queued': 'กำลังส่งแจ้งเตือน Notify API — ยังไม่ยืนยันว่าส่งสำเร็จ',
                        'notification_shadow': 'พบวัตถุค้างแต่ปิดส่งจริงไว้ (shadow mode) จึงบันทึกอย่างเดียว',
                        'notification_success': 'Notify API รับคำขอแล้ว ไม่ใช่การยืนยันว่ามือถือดังหรือผู้ใช้รับทราบ',
                        'notification_failed': 'ส่งแจ้งเตือนไม่สำเร็จ ตรวจคีย์และเครือข่าย; รออย่างน้อย 60 วินาทีและต้องมีภาพสดยืนยันก่อนลองใหม่',
                        'notification_ambiguous': 'ผลส่งไม่แน่นอน อาจส่งถึงแล้ว; รออย่างน้อย 60 วินาทีและต้องมีภาพสดยืนยันก่อนลองใหม่',
                    }
                    if len(item) != 2 or kind not in hints:
                        continue
                    if kind == 'ready' and record['state'] == 'starting':
                        record['state'] = 'running'
                    text = hints[kind]
                    self._message(role + ': ' + text, created)
            process = record['process']
            if process is not None and process.exitcode is not None:
                failed = process.exitcode != 0 or record['state'] == 'failed'
                record['state'] = 'failed' if failed else 'stopped'
                process.join(timeout=0)
                process.close()
                messages.close()
                record.update(process=None, stop=None, messages=None)
        return {role: dict(state=record['state'], pid=record['process'].pid
                          if record['process'] is not None else None)
                for role, record in self._roles.items()}

    def _tracking_running(self):
        return not self._transport_closed and all(
            record['state'] == 'running' and record['process'] is not None
            and record['process'].is_alive() for record in self._roles.values())

    def _accept_tracking(self, detail, now):
        from .desktop_tracking import validate_packet
        packet = validate_packet(detail, now=now, max_age=self._health_max_age,
                                 size=self._tracking_size, generation=self._frame_channel.generation)
        if (packet is None or not self._tracking_running()
                or packet['captured_monotonic'] <= self._tracking_since):
            self._tracking_packet = None
            return
        previous = self._tracking_order
        if previous is not None and (packet['captured_monotonic'] <= previous[2]
                or (packet['session'] == previous[0] and packet['sequence'] <= previous[1])):
            self._tracking_packet = None
            return
        self._tracking_order = (packet['session'], packet['sequence'], packet['captured_monotonic'])
        self._tracking_packet = packet

    def tracking_snapshot(self, now=None):
        """Latest validated RAM preview only; never consumes the frame receiver."""
        from .desktop_tracking import validate_packet
        if not self._tracking_running():
            self._tracking_packet = None
            return None
        packet = validate_packet(self._tracking_packet, now=time.monotonic() if now is None else now,
                                 max_age=self._health_max_age, size=self._tracking_size,
                                 generation=self._frame_channel.generation)
        if packet is None:
            self._tracking_packet = None
        return packet

    def _accept_health(self, detail, now):
        if type(detail) is not dict or type(detail.get('healthy')) is not bool:
            return
        observed, captured = detail.get('observed_at'), detail.get('captured_at')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (observed, captured)):
            return
        if not self._health_since <= captured <= observed <= now:
            return
        if self._health_observed is not None and observed <= self._health_observed:
            return
        sequence, session = detail.get('sequence'), detail.get('session')
        if (type(sequence) is not int or not 0 <= sequence <= 2_147_483_647
                or type(session) is not str or not 1 <= len(session) <= 128):
            return
        reason = detail.get('reason')
        if reason not in ('unknown', 'vision_alignment', 'vision_quality'):
            return
        previous = getattr(self, '_health_sequence', None)
        if previous is not None and previous[0] == session and sequence <= previous[1]:
            return
        self._health_sequence = (session, sequence)
        self._health_observed = observed
        self._health_reason = reason
        self._health_good = captured if detail['healthy'] else None

    def health_snapshot(self, now=None):
        """Bounded, credential-free vision analysis health; never polls GUI text."""
        now = time.monotonic() if now is None else now
        expected = self._health_expected
        dead = any(self._roles[r]['state'] in ('stopped', 'failed') for r in ('capture', 'worker'))
        good = self._health_good
        healthy = bool(expected and not dead and good is not None
                       and 0 <= now - good <= self._health_max_age)
        reason = ('vision_worker_failed' if dead else
                  'vision_no_frames' if (good is not None and not healthy) or
                  (self._health_reason == 'starting' and now - self._health_since > self._health_max_age)
                  else self._health_reason)
        if healthy:
            reason = 'ok'
        return dict(expected=expected, healthy=healthy, reason=reason,
                    observed_at=self._health_observed, generation=self._health_generation,
                    last_good_at=good)

    def drain_messages(self):
        self.snapshot()
        result = list(self._messages)
        self._messages.clear()
        return result

    def is_running(self, role):
        return self._role(role)['state'] in ('starting', 'running', 'stopping')
