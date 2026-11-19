"""Explicit, cancellable CRITICAL Notify API test; no startup delivery."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from uuid import uuid4
from .notify_api import NotifyTarget, AlertMessage, NotifyResult, deliver_alert

CONFIRMATION = ('ส่ง Notify API จริงหนึ่งรายการแบบ critical?\n'
                'นี่เป็นการทดสอบ ไม่ใช่เหตุการณ์กล้องจริง มือถืออาจปลุกต่อเนื่องจนกดรับทราบในมือถือ\n'
                'การยกเลิกหรือปิดหน้าต่างนี้ไม่หยุดเสียง และคำขอที่เริ่มแล้วอาจเรียกคืนไม่ได้')
RESULTS = {
    'success': 'ผู้ให้บริการรับคำขอแล้ว ไม่ยืนยันว่ามือถือได้รับ/เสียง/การรับทราบ',
    'failed': 'ผู้ให้บริการยังไม่ยืนยันรับคำขอ Notify API',
    'ambiguous': 'ไม่ทราบแน่ชัดว่าได้รับหรือไม่ อาจส่งแล้ว โปรดตรวจสอบก่อนกดอีกครั้ง',
}
CLIENT_ERROR = RESULTS['ambiguous'] + ' (client_error)'


def format_result(result):
    """Render only the transport's validated enum/integer; never raw errors."""
    if type(result) is not NotifyResult:
        return CLIENT_ERROR
    try:
        safe = NotifyResult(result.state, result.code, http_status=result.http_status)
    except Exception:
        return CLIENT_ERROR
    warning = ' • บันทึกประวัติไม่สำเร็จ' if result.history_saved is False else ''
    if safe.state == 'success':
        return RESULTS['success'] + warning
    status = f'HTTP {safe.http_status}; ' if safe.http_status is not None else ''
    return RESULTS[safe.state] + f' ({status}{safe.code})' + warning


class NotifyTestPanel(ttk.Frame):
    def __init__(self, parent, credentials_getter, *, sender=deliver_alert, confirm=None, history=None):
        super().__init__(parent)
        self.credentials_getter = credentials_getter
        self.sender = sender
        self.history = history
        self.confirm = confirm if confirm is not None else self._confirm
        self.status = tk.StringVar(self, '')
        self.button = ttk.Button(self, text='ทดสอบ Notify API จริง (critical)', command=self.start)
        self.button.pack(anchor='w')
        ttk.Label(self, textvariable=self.status, wraplength=300).pack(anchor='w')
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='notify-test')
        self._future = None
        self._confirming = self._closing = self._shutdown = False
        self._cancel = threading.Event()

    def _confirm(self):
        return messagebox.askyesno('ยืนยันทดสอบ Notify API จริง', CONFIRMATION, parent=self)

    @property
    def is_running(self):
        return self._confirming or self._future is not None

    def start(self):
        if self._closing or self.is_running:
            return
        self._confirming = True
        self.button.state(['disabled'])
        try:
            try:
                key, base = self.credentials_getter()
                target = NotifyTarget(base.strip(), key.strip(), history=self.history, channel='manual_test')
            except Exception:
                self.status.set('กรุณาระบุ Notify API Base URL และ API Key ที่ถูกต้อง')
                return
            if not self.confirm():
                self.status.set('ยกเลิก ยังไม่ได้ส่งคำขอ')
                return
            if self._closing:
                return
            alert = AlertMessage(str(uuid4()), str(uuid4()), 'SmartTapo TEST',
                'TEST ทดสอบเท่านั้น ไม่ใช่เหตุการณ์กล้องจริง กดรับทราบในมือถือเพื่อหยุดเสียง', 'critical')
            self._cancel = threading.Event()
            self.status.set('กำลังส่งรายการทดสอบจริงแบบ critical…')
            self._future = self._executor.submit(self.sender, target, alert, self._cancel)
        except Exception:
            self.status.set('เริ่มทดสอบไม่สำเร็จ (client_error)')
        finally:
            self._confirming = False
            if not self._closing and self._future is None:
                self.button.state(['!disabled'])

    def poll(self):
        future = self._future
        if future is not None and future.done():
            try:
                text = format_result(future.result())
            except Exception:
                text = CLIENT_ERROR
            self._future = None
            self.status.set(text)
        self.button.state(['disabled'] if self._closing or self.is_running else ['!disabled'])

    def close(self):
        self._closing = True
        self._cancel.set()
        self.button.state(['disabled'])
        if self._future is not None:
            if self._future.cancel():
                self._future = None
            else:
                self.status.set('ยกเลิกการรอ คำขอที่เริ่มแล้วอาจส่งไปแล้วและเรียกคืนไม่ได้')
        if not self._shutdown:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._shutdown = True
        self.poll()


def smoke_notify_test(root, panel):
    """Real UI control flow with synthetic credentials and injected sender only."""
    from .notify_api import NotifyResult
    if panel.is_running or panel._closing:
        raise RuntimeError('Notify smoke requires an idle open panel')
    saved = panel.credentials_getter, panel.sender, panel.confirm, panel.status.get()
    calls = []
    def fake_sender(target, alert, cancel):
        calls.append((target, alert))
        return NotifyResult('success', 'provider_accepted') if len(calls) == 1 else NotifyResult('ambiguous', 'network_unknown')
    try:
        panel.credentials_getter = lambda: ('synthetic-smoke-key', 'https://notify.invalid')
        panel.sender = fake_sender
        panel.confirm = lambda: True
        results = []
        for _ in range(2):
            panel.button.invoke()
            panel.start()
            deadline = time.monotonic() + 5
            while panel.is_running and time.monotonic() < deadline:
                root.update()
                panel.poll()
                time.sleep(.001)
            if panel.is_running:
                raise RuntimeError('Fake Notify smoke did not drain')
            results.append(panel.status.get())
        return dict(notify_test_gui=results == [RESULTS['success'], format_result(NotifyResult('ambiguous', 'network_unknown'))],
                    notify_test_single_action=len(calls) == 2,
                    notify_test_redacted_result=all('synthetic-smoke' not in s for s in results),
                    notify_test_fake_only=len(calls) == 2 and all(t.key == 'synthetic-smoke-key' and t.base_url == 'https://notify.invalid' and a.importance == 'critical' for t, a in calls))
    finally:
        panel.credentials_getter, panel.sender, panel.confirm, status = saved
        panel.status.set(status)
