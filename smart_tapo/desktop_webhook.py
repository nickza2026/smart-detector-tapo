"""Explicitly armed desktop Refueling API v1 sender; never starts on launch."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from copy import deepcopy


class PreparedStartError(ValueError):
    """Fixed user-facing preparation error, never an underlying exception."""


@dataclass(frozen=True, repr=False)
class PreparedStart:
    """Click-scoped capability; deliberately never displays credential material."""
    config: object = field(repr=False)
    target: object = field(repr=False)
    scope: tuple = field(repr=False)
    stop_event: object = field(repr=False)
    active: bool = False
    stopped: bool = False

from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from .refueling_api_log import ApiAttemptLog, describe
from .refueling_store import RefuelingStore
from .storage_diagnostics import (classify_storage_error, STORAGE_RECOVERY_CODES,
                                  validate_guard_evidence, format_guard_evidence)
from .refueling_webhook import WebhookTarget, RefuelingWebhookClient, DeliveryResult, dispatch_one


class _QueueFailure(Exception):
    def __init__(self, exc, phase, *, operation=None):
        self.code = classify_storage_error(exc)
        self.phase = phase if type(phase) is str and phase in ('queue_open', 'queue_dispatch', 'queue_close') else 'queue_dispatch'
        self.operation = operation if type(operation) is str and operation in (
            'open', 'close', 'cleanup', 'pending_jobs', 'image_uploaded',
            'mark_image_uploaded', 'forget_image_uploads', 'mark_delivery') else None
        self.guard = validate_guard_evidence(getattr(exc, 'guard', None))
        super().__init__(self.code + ':' + self.phase)


class _QueueBoundary:
    """Classify only store calls, never transport/client exceptions."""
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        value = getattr(self.store, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except Exception as exc:
                raise _QueueFailure(exc, 'queue_dispatch', operation=name) from None
        return call


def _attempt(directory, target, stop, client_factory, log=None):
    # Connection and all SQLite operations belong to this worker, never Tk.
    if stop.is_set():
        return None
    try:
        store = RefuelingStore(directory, target.camera_id)
    except Exception as exc:
        raise _QueueFailure(exc, 'queue_open', operation='open') from None
    failed = False
    terminal = None
    def remember(result):
        nonlocal terminal
        if getattr(result, 'error_code', None) in ('receiver_auth_rejected', 'receiver_firewall_blocked'):
            terminal = result
        return result
    try:
        client = client_factory(target, store.media)
        if log is not None and hasattr(client, 'log'):
            client.log = log
        class Cancellable:
            target = client.target
            def record(self, *args, **kwargs):
                if hasattr(client, 'record'):
                    client.record(*args, **kwargs)
            def upload_image(self, image):
                return DeliveryResult('retry', 'transport_unconfirmed') if stop.is_set() else remember(client.upload_image(image))
            def send_event(self, payload, event_id):
                return DeliveryResult('retry', 'transport_unconfirmed') if stop.is_set() else remember(client.send_event(payload, event_id))
            def send_observation(self, payload, event_id):
                return DeliveryResult('retry', 'transport_unconfirmed') if stop.is_set() else remember(client.send_observation(payload, event_id))
        return dispatch_one(_QueueBoundary(store), Cancellable())
    except BaseException as exc:
        failed = True
        # A rejected credential/firewall must stop even if recording that result
        # fails locally; storage recovery must never hide the receiver's refusal.
        if isinstance(exc, Exception) and terminal is not None:
            return terminal
        raise
    finally:
        try:
            store.close()
        except Exception as exc:
            if not failed and terminal is None:
                raise _QueueFailure(exc, 'queue_close', operation='close') from None


def _recover(directory, target, stop, expected_ids=None):
    if stop.is_set():
        return None
    store = RefuelingStore(directory, target.camera_id)
    try:
        if stop.is_set():
            return None
        return store.rebind_unconfirmed(target.destination_key, expected_ids=expected_ids,
                                       consent=expected_ids is not None)
    finally:
        store.close()


class Sender:
    """One owned future, reaped even after stop; no overlapping configurations."""
    RESTART_LIMIT = 3
    RESTART_DELAY = 15.0

    def __init__(self, client_factory=RefuelingWebhookClient):
        self.client_factory = client_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='RefuelingAPI')
        self.future = None
        self.scope = None
        self.stop_event = threading.Event()
        self.closed = False
        self.next_attempt = 0
        self.storage_failures = 0
        self.restart_attempts = 0
        self._restart_pending = False
        self.status = 'หยุดอยู่ — เปิดโปรแกรมไม่ส่งเอง'
        self.api_log = None

    @property
    def busy(self):
        return self.future is not None

    def start(self, directory, target, *, consent=False):
        if self.closed or self.busy or not consent:
            raise ValueError('sender_not_ready_or_no_consent')
        self.scope = (Path(directory).absolute(), target)
        path = self.scope[0].parent / 'logs' / 'refueling-api.jsonl'
        if self.api_log is None or self.api_log.path != path:
            self.api_log = ApiAttemptLog(path)
        self.stop_event = threading.Event()
        self.next_attempt = 0
        self.storage_failures = 0
        self.restart_attempts = 0
        self._restart_pending = False
        self.status = 'เปิดส่งแล้ว • คิวเดิมที่ผูกปลายทางอื่นจะไม่ถูกย้าย'

    def stop(self):
        self.scope = None
        self._restart_pending = False
        self.stop_event.set()
        self.status = 'กำลังหยุด / รอคำตอบงานที่เริ่มแล้ว' if self.busy else 'หยุดอยู่'

    def poll(self):
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                result = future.result()
                if ((self.scope is None or self.stop_event.is_set() or self.closed)
                        and getattr(result, 'error_code', None) not in
                        ('receiver_auth_rejected', 'receiver_firewall_blocked')):
                    return
                self.storage_failures = 0
                self.restart_attempts = 0
                self._restart_pending = False
                self.status = 'เปิดส่งอยู่ • รอคิวที่เข้าเกณฑ์'
                if result is not None:
                    self.status = {'delivered': 'ยืนยันรับแล้วจาก receiver ACK',
                                   'retry': 'ยังไม่ยืนยันการส่ง • รอ retryAt',
                                   'failed': 'ส่งล้มเหลว • ตรวจการตั้งค่า/สิทธิ์'}.get(result.status, 'รอส่ง')
                    if result.error_code == 'receiver_auth_rejected':
                        self.stop()
                        self.status = 'หยุดส่ง: receiver ปฏิเสธสิทธิ์'
                    elif result.error_code == 'receiver_firewall_blocked':
                        self.stop()
                        self.status = ('หยุดส่ง: Cloudflare/firewall หน้า API บล็อกก่อนถึง API (ไม่ใช่คีย์ผิด) '
                                       '— ให้เจ้าของ API ยกเว้น path /v1/refueling จาก Browser Integrity Check/WAF')
            except Exception as exc:
                if self.scope is None or self.stop_event.is_set() or self.closed:
                    return
                code = exc.code if isinstance(exc, _QueueFailure) else 'sender_error'
                phase = exc.phase if isinstance(exc, _QueueFailure) else 'queue_dispatch'
                if code in STORAGE_RECOVERY_CODES:
                    self.storage_failures += 1
                    base, cap = (2, 60) if code == 'storage_busy' else (30, 300)
                    delay = min(cap, base * 2 ** min(self.storage_failures - 1, 8))
                    self.next_attempt = time.monotonic() + delay
                    self.status = f'เปิดส่งอยู่ • รอลองใหม่ใน {delay} วินาที [{code}/{phase}]'
                elif (code == 'storage_path_unsafe' or
                      (code == 'sender_error' and not isinstance(exc, (ValueError, PermissionError)))):
                    if self.restart_attempts >= self.RESTART_LIMIT:
                        self.stop()
                        self.status = f'หยุดส่ง: ลองเริ่มใหม่ครบ {self.RESTART_LIMIT} ครั้ง [{code}/{phase}]'
                    else:
                        self._restart_pending = True
                        self.next_attempt = time.monotonic() + self.RESTART_DELAY
                        self.status = (f'รอเริ่มตัวส่งใหม่ครั้งที่ {self.restart_attempts + 1}/{self.RESTART_LIMIT} '
                                       f'ใน 15 วินาที [{code}/{phase}] — ตรวจ guard เดิมทุกครั้ง')
                else:
                    self.stop()
                    self.status = f'หยุดส่ง: [{code}/{phase}] (ไม่บันทึกข้อมูลลับ)'
                if isinstance(exc, _QueueFailure):
                    detail = format_guard_evidence(exc.guard)
                    if detail:
                        self.status += f'\n{detail} | operation={exc.operation or "unknown"}'
        if not self.closed and self.scope and not self.busy and time.monotonic() >= self.next_attempt:
            self.next_attempt = time.monotonic() + 1
            try:
                self.future = self.executor.submit(_attempt, *self.scope, self.stop_event, self.client_factory, self.api_log)
                if self._restart_pending:
                    self.restart_attempts += 1
                    self._restart_pending = False
                    self.status = f'กำลังเริ่มตัวส่งใหม่ครั้งที่ {self.restart_attempts}/{self.RESTART_LIMIT}'
            except Exception:
                # A failed submission has unknown acceptance; do not create an
                # overlapping request by replacing the pool or blindly retrying.
                self.stop()
                self.status = 'หยุดส่ง: เริ่ม worker ไม่สำเร็จ [sender_submit_error] — ตรวจและกดเริ่มใหม่'

    def close(self):
        self.closed = True
        self.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)


class WebhookPanel(ttk.Frame):
    def __init__(self, parent, get_config, save_config, storage):
        super().__init__(parent, padding=12)
        self.get_config, self.save_config, self.storage = get_config, save_config, storage
        self.sender = Sender()
        self.recovery = None
        self.recovery_scope = None
        self.recovery_stop = threading.Event()
        config = get_config()
        self.url = tk.StringVar(value=config.refueling_api_url)
        self.key = tk.StringVar()
        self.enabled = tk.BooleanVar(value=config.refueling_api_enabled)
        self.consent = tk.BooleanVar(value=False)
        self.scope = None
        ttk.Label(self, text='Refueling API v1 /v1/refueling').pack(anchor='w')
        ttk.Label(self, text='Base URL (HTTPS) • PUT รูปก่อน POST JSON • ไม่ส่ง WAV • แยกจาก Notify API').pack(anchor='w')
        ttk.Entry(self, textvariable=self.url, width=80).pack(fill='x')
        ttk.Label(self, text='ตัวอย่าง base URL: https://dashboard.example/api (ไม่เติม /v1/refueling เอง)\nPUT https://dashboard.example/api/v1/refueling/images/{image_id}\nPOST https://dashboard.example/api/v1/refueling/events', wraplength=850).pack(anchor='w')
        ttk.Label(self, text='Application API key • Windows CURRENT USER DPAPI • เว้นว่างเพื่อใช้คีย์ที่บันทึก').pack(anchor='w')
        ttk.Entry(self, textvariable=self.key, show='*', width=60).pack(fill='x')
        ttk.Checkbutton(self, text='อนุญาตการส่ง Refueling API (ยังต้องกดเริ่มทุกครั้ง)', variable=self.enabled).pack(anchor='w')
        ttk.Checkbutton(self, text='ยืนยันผูกคิวที่ยังไม่มีปลายทางทั้งหมดของกล้องนี้ รวมเหตุใหม่ ไป URL/คีย์นี้; ไม่ย้ายคิวที่ผูกแล้ว', variable=self.consent).pack(anchor='w')
        ttk.Button(self, text='บันทึก URL / คีย์ / สิทธิ์ (ไม่ส่ง)', command=self.save).pack(anchor='w', pady=5)
        ttk.Button(self, text='เริ่มส่งคิวที่ยืนยันแล้ว', command=self.start).pack(anchor='w')
        ttk.Button(self, text='หยุดส่ง', command=self.stop).pack(anchor='w', pady=5)
        ttk.Button(self, text='กู้คิวที่ยังไม่ยืนยัน: ตรวจจำนวนและยืนยันย้ายปลายทาง…', command=self.recover).pack(anchor='w')
        ttk.Label(self, text='เสียงประกาศไม่ยืนยันยอดเงินจริง • เก็บข้อมูล/รูปใหม่ 3 วัน • หมดอายุอาจยังไม่ยืนยันการรับ').pack(anchor='w')
        self.label = ttk.Label(self, text=self.sender.status, wraplength=850)
        self.label.pack(anchor='w')
        ttk.Label(self, text='การยิง API ล่าสุด (ใหม่สุดอยู่บน) • URL / HTTP status / คำตอบของ API • ไม่บันทึกคีย์ '
                             '• เก็บเต็มใน data/logs/refueling-api.jsonl', wraplength=850).pack(anchor='w', pady=(8, 0))
        self.attempts = tk.Text(self, height=8, wrap='word', state='disabled')
        self.attempts.pack(fill='both', expand=True)
        self._shown_attempts = None
        self.enabled.trace_add('write', self._enabled_changed)
        self.url.trace_add('write', self._destination_edited)
        self.key.trace_add('write', self._destination_edited)

    def _destination_edited(self, *args):
        if self.scope is not None or self.sender.scope is not None or self.sender.busy:
            self.stop()

    def _enabled_changed(self, *args):
        if not self.enabled.get():
            self.stop()

    @property
    def is_running(self):
        return self.sender.busy or self.sender.scope is not None or self.recovery is not None

    def _feedback(self, message):
        # One status owner: idle polls preserve action feedback; worker progress
        # and authentication failures can still replace it in Sender.poll().
        self.sender.status = message
        self.label.configure(text=message)

    def save(self):
        self.stop()
        if self.sender.busy or self.recovery is not None:
            self._feedback('รอการส่งที่เริ่มแล้วหยุดก่อนบันทึก')
            return False
        try:
            config = replace(self.get_config(), refueling_api_url=self.url.get().strip(),
                             refueling_api_enabled=self.enabled.get()).validate()
            key = self.key.get()
            if key:
                WebhookTarget(config.refueling_api_url, key, config.camera_id)
                self.storage.save_refueling_key(key, config.refueling_api_url, config.camera_id)
                self.key.set('')
            self.save_config(config)
            self._feedback('บันทึกแล้ว — ยังไม่ส่ง ต้องยืนยันและกดเริ่ม')
            return True
        except Exception:
            self._feedback('บันทึกไม่ได้: ตรวจ HTTPS URL / คีย์ / DPAPI (ไม่แสดงข้อมูลลับ)')
            return False

    def start(self):
        try:
            if self.sender.scope is not None:
                self._feedback('ตัวส่งเปิดอยู่แล้ว — รอและส่งคิวใหม่อัตโนมัติ กดหยุดก่อนเปลี่ยนการตั้งค่า')
                return
            try:
                plan = self.prepare_start(consent=self.consent.get())
            except PreparedStartError as exc:
                self._feedback('ยังไม่เริ่ม: ' + str(exc))
                return
            self.start_prepared(plan)
        except Exception:
            self._feedback('ยังไม่เริ่ม: บันทึกการตั้งค่า เปิดสิทธิ์ และยืนยันผูกคิวก่อน (หรือรองานหยุด)')

    def prepare_start(self, *, consent=False):
        """Validate saved permission and load only the key for this saved scope."""
        config = self.get_config()
        reason = None
        if self.recovery is not None or self.sender.closed:
            reason = 'รองานเดิมหยุดก่อนเริ่มส่ง'
        elif not self.enabled.get():
            reason = 'เปิดสิทธิ์การส่งและบันทึกก่อน'
        elif not config.refueling_api_enabled:
            reason = 'สิทธิ์การส่งยังไม่ได้บันทึก กรุณากดบันทึกก่อน'
        elif config.refueling_api_url != self.url.get().strip():
            reason = 'URL ที่แก้ไขยังไม่ได้บันทึก'
        elif self.key.get():
            reason = 'คีย์ที่กรอกยังไม่ได้บันทึก'
        elif not consent:
            reason = 'กรุณายืนยันผูกคิวก่อนเริ่มส่ง'
        scope = (Path(config.data_dir).absolute(), config.camera_id, config.refueling_api_url)
        if reason:
            raise PreparedStartError(reason)
        if self.sender.scope is not None:
            target = self.sender.scope[1]
            if (self.scope != scope or self.sender.scope[0] != scope[0] / 'refueling'
                    or target.camera_id != config.camera_id
                    or target.base_url != config.refueling_api_url.rstrip('/')):
                raise PreparedStartError('หยุดตัวส่งก่อนเปลี่ยนการตั้งค่า')
            return PreparedStart(deepcopy(config), self.sender.scope[1], scope,
                                 self.sender.stop_event, True, self.sender.stop_event.is_set())
        if self.sender.busy:
            raise PreparedStartError('รองานเดิมหยุดก่อนเริ่มส่ง')
        try:
            target = WebhookTarget(config.refueling_api_url,
                self.storage.load_refueling_key(config.refueling_api_url, config.camera_id), config.camera_id)
        except Exception:
            raise PreparedStartError('ไม่มีคีย์ที่บันทึก อ่านคีย์ไม่ได้ หรือคีย์ไม่ตรง URL/กล้องนี้ กรุณาบันทึกคีย์ใหม่') from None
        return PreparedStart(deepcopy(config), target, scope, self.sender.stop_event,
                             stopped=self.sender.stop_event.is_set())

    def validate_prepared(self, plan):
        if (not isinstance(plan, PreparedStart) or self.get_config() != plan.config
                or not self.enabled.get() or self.key.get()
                or self.url.get().strip() != plan.config.refueling_api_url
                or self.recovery is not None or self.sender.closed
                or self.sender.stop_event is not plan.stop_event
                or self.sender.stop_event.is_set() != plan.stopped
                or (plan.active and (self.scope != plan.scope or self.sender.scope is None))
                or (not plan.active and (self.sender.scope is not None or self.sender.busy))):
            raise PreparedStartError('สถานะเปลี่ยน กรุณาตรวจและกดเริ่มใหม่')

    def start_prepared(self, plan):
        self.validate_prepared(plan)
        if plan.active:
            return False
        self.scope = plan.scope
        try:
            self.sender.start(plan.scope[0] / 'refueling', plan.target, consent=True)
        except Exception:
            self.scope = None
            self.sender.stop()
            raise PreparedStartError('เริ่มตัวส่งไม่ได้ กรุณาตรวจสถานะแล้วลองใหม่') from None
        self.consent.set(False)
        self.label.configure(text=self.sender.status)
        return True

    def recover(self):
        if self.is_running or self.sender.closed:
            self._feedback('หยุดส่งและรองานเดิมก่อนกู้คิว')
            return
        try:
            config = self.get_config()
            if self.key.get() or self.url.get().strip() != config.refueling_api_url:
                raise ValueError()
            target = WebhookTarget(config.refueling_api_url,
                                   self.storage.load_refueling_key(config.refueling_api_url, config.camera_id),
                                   config.camera_id)
            self.recovery_scope = (Path(config.data_dir).absolute(), target)
            self.recovery_stop = threading.Event()
            self.recovery = self.sender.executor.submit(_recover, self.recovery_scope[0]/'refueling', target, self.recovery_stop)
        except Exception:
            self._feedback('กู้คิวไม่ได้: บันทึก URL และคีย์สำหรับกล้องนี้ก่อน')

    def _poll_recovery(self):
        if self.recovery is None or not self.recovery.done():
            return
        future, self.recovery = self.recovery, None
        try:
            result = future.result()
            config = self.get_config()
            directory, target = self.recovery_scope
            if (self.sender.closed or self.recovery_stop.is_set() or
                    (Path(config.data_dir).absolute(), config.camera_id, config.refueling_api_url.rstrip('/')) !=
                    (directory, target.camera_id, target.base_url)):
                return
            if isinstance(result, tuple):
                if result and messagebox.askyesno('ยืนยันย้ายคิวที่ยังไม่ยืนยัน',
                        f'กล้อง {target.camera_id} → {target.base_url}\nจำนวน {len(result)} events ที่ยังไม่ยืนยัน (รวม failed)\nปลายทางนี้อาจได้รับประวัติ/รูปย้อนหลังไม่เกิน 3 วัน\nปลายทางเดิมอาจรับแล้ว; ใช้ IDs/payload/เวลาหมดอายุเดิม ไม่ส่งซ้ำรายการ delivered\nยืนยันผูกคิวเหล่านี้ใหม่? ยังไม่เริ่มส่งจนกดเริ่ม', parent=self):
                    self.recovery = self.sender.executor.submit(_recover, directory/'refueling', target, self.recovery_stop, result)
                else:
                    self.sender.status = 'ไม่ย้ายคิว / ไม่มีคิวที่เข้าเกณฑ์'
            elif result is not None:
                self.sender.status = f'ย้ายคิวที่ยังไม่ยืนยัน {result} events แล้ว — ยังไม่เริ่มส่ง'
        except Exception:
            self.sender.status = 'กู้คิวไม่ได้ / คิวเปลี่ยน กรุณาตรวจจำนวนและยืนยันใหม่'

    def stop(self):
        self.scope = None
        self.recovery_stop.set()
        self.sender.stop()
        self.consent.set(False)
        self.label.configure(text=self.sender.status)

    def poll(self):
        config = self.get_config()
        current = (Path(config.data_dir).absolute(), config.camera_id, config.refueling_api_url)
        if self.scope is not None and (self.scope != current or not config.refueling_api_enabled):
            self.stop()
            self.scope = None
        self.sender.poll()
        self._poll_recovery()
        self.label.configure(text=self.sender.status)
        self._render_attempts()

    def _render_attempts(self):
        records = self.sender.api_log.recent() if self.sender.api_log is not None else []
        marker = (len(records), records[-1].get('at') if records else None)
        if marker == self._shown_attempts:
            return
        self._shown_attempts = marker
        self.attempts.configure(state='normal')
        self.attempts.delete('1.0', 'end')
        self.attempts.insert('1.0', '\n'.join(describe(record) for record in reversed(records))
                             or 'ยังไม่มีการยิง API ในรอบเปิดโปรแกรมนี้')
        self.attempts.configure(state='disabled')

    def close(self):
        self.recovery_stop.set()
        self.sender.close()
