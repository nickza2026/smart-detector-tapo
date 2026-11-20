"""Manual direct Playback TEST panel; constructing/polling never opens audio."""
from copy import deepcopy
import json
import tkinter as tk
from tkinter import ttk, messagebox

from .playback_test import PlaybackTest


class PlaybackTestPanel(ttk.Frame):
    def __init__(self, parent, config_getter, device_getter, listener, *, other_running=lambda:False, controller=None):
        super().__init__(parent, padding=8)
        self.get_config, self.get_device, self.listener = config_getter, device_getter, listener
        self.other_running = other_running
        self.controller = controller if controller is not None else PlaybackTest()
        self.closing = False
        self.selection = None
        self.status = tk.StringVar(self, 'ยังไม่ได้รับข้อมูลเสียง (unknown/unreceived) — TEST เท่านั้น')
        self.device_summary = tk.StringVar(self, 'ยังไม่ได้เลือกเสียงขาออก')
        ttk.Label(self, text='ทดสอบเสียงจาก Tapo Playback ใน MEmu (TEST — ไม่ใช่ Live)', style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self, text='เปิด Playback ใน MEmu ด้วยตัวเอง แล้วเลือก Windows output ในแท็บเสียงก่อนเริ่ม\nไม่ต้องเลือก/อัปโหลดไฟล์ ไม่ควบคุม MEmu ไม่บันทึก PCM ไม่สร้างประวัติ/ธุรกรรม/ส่งแจ้งเตือน\nรับเสียงทั้งหมดบน output ที่เลือก อาจรวมโปรแกรมอื่น ไม่พิสูจน์กล้องหรือเงินจริง', wraplength=740).pack(anchor='w', pady=6)
        ttk.Label(self,textvariable=self.device_summary,wraplength=740).pack(anchor='w')
        ttk.Button(self,text='เลือกเสียงขาออกในแท็บแรก…',command=self.choose_output).pack(anchor='w',pady=4)
        row = ttk.Frame(self); row.pack(anchor='w')
        self.start_button = ttk.Button(row, text='เริ่มทดสอบเสียงจาก Playback', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(row, text='หยุดทดสอบ', command=self.stop)
        self.stop_button.pack(side='left', padx=8)
        ttk.Label(self, textvariable=self.status, wraplength=740).pack(anchor='w', pady=6)
        self.output = tk.Text(self, height=13, wrap='word', state='disabled')
        self.output.pack(fill='both', expand=True)
        self._buttons()

    @property
    def is_running(self): return self.controller.is_running

    def _buttons(self):
        device=self.get_device()
        name=''.join(c for c in str(device.get('name','')) if c.isprintable())[:110] if isinstance(device,dict) else ''
        self.device_summary.set('เสียงขาออกที่เลือก: '+name if name else 'ยังไม่เลือกเสียงขาออก — ไปแท็บแรกเพื่อค้นหาและเลือก ไม่ใช้ default แทน')
        self.start_button.configure(state='disabled' if self.closing or self.is_running or self.other_running() else 'normal')
        self.stop_button.configure(state='normal' if self.is_running and not self.closing else 'disabled')

    def choose_output(self):
        select=getattr(self.master,'select',None)
        if callable(select):select(0)

    def _profile(self, config):
        selected = self.listener.profile_path.get().strip()
        if selected: return selected
        try:
            saved = json.loads(config.windows_startup_binding)
            if saved['camera_id'] == config.camera_id: return saved.get('profile', '')
        except (ValueError, KeyError, TypeError): pass
        return ''

    def _selection(self):
        config = self.get_config()
        return (deepcopy(self.get_device()), config.camera_id, str(config.data_dir), self._profile(config))

    def start(self):
        if self.closing or self.is_running or self.other_running(): return
        try:
            from .playback_test import validate_device
            selection = self._selection()
            validate_device(selection[0])
        except (ValueError, TypeError, KeyError):
            self.status.set('เลือก output WASAPI loopback จริงพร้อม endpoint GUID ก่อน — ไม่ใช้ default/mic แทน')
            return
        if not messagebox.askyesno('ยืนยัน TEST ครั้งนี้', 'เริ่มรับเสียงจาก Windows output ที่เลือกตอนนี้หรือไม่?\nอาจรวมเสียงจากแอปอื่น ไม่บันทึกเสียง ไม่ใช่ Live/หลักฐานกล้อง/เงินจริง', parent=self): return
        try:
            if self.other_running(): raise ValueError('busy')
            self.controller.start(deepcopy(self.get_config()), selection[0], selection[3],
                                  self.listener.prepared_manifest, consent=True, other_running=self.other_running())
            self.selection = selection
            self.status.set('กำลังรอ PCM จริง — unknown/unreceived; เปิด Playback ด้วยตัวเอง')
            self._text('')
        except Exception:
            self.status.set('เริ่ม TEST ไม่ได้ — ไม่มีการใช้ default output แทน')
        self._buttons()

    def _text(self, text):
        self.output.configure(state='normal'); self.output.delete('1.0', 'end')
        self.output.insert('end', text); self.output.configure(state='disabled')

    def stop(self):
        self.controller.stop()
        self.status.set('หยุด TEST — ช่วงที่ยังไม่พบวลีจบยังไม่ครบ ไม่สร้าง end สมมติ')
        self._buttons()

    def poll(self):
        if self.is_running and self._selection() != self.selection: self.stop()
        for event in self.controller.poll():
            if event.get('kind') == 'playback_test':
                self._render(event)
            elif event.get('kind') == 'error':
                self.status.set('TEST หยุด/ข้อมูลขาดช่วง — ผลที่ค้างยังไม่ครบ: '+event.get('code', 'capture_failed'))
            elif event.get('kind') == 'stopped':
                if event.get('snapshot'): self._render(event['snapshot'])
                self.status.set('หยุด TEST แล้ว — ไม่ใช่ Live; ช่วงค้างยังไม่ครบ')
        self._buttons()

    def _render(self, state):
        if state['received']:
            self.status.set(f"รับ PCM จริง {state['elapsed']:.1f}s | RMS {state['rms']:.6f} | peak {state['peak']:.6f} — TEST")
        else: self.status.set('ยังไม่ได้รับ PCM — unknown/unreceived ไม่ใช่ระดับ 0')
        rounds = state['rounds']; counts = rounds['counts']
        lines = [f"TEST เท่านั้น | วลีเริ่ม {counts['ready']} | วลีจบ {counts['end']} | พบวลีครบ {counts['complete']}"]
        for label, score in state['scores'].items():
            lines.append(f"{label}: NCC {score['score']:.4f} / threshold {score['threshold']:.4f} @ {score['onset']:.2f}s (accepted TEST)")
        from .audio_diagnostic_summary import number, REASONS
        for label,score in state.get('maxima',{}).items():
            reason=REASONS.get(score['reason'],'ไม่มีรายละเอียดสาเหตุที่ยืนยันได้')
            lines.append(f"window ล่าสุด {label}: NCC {number(score['score'])} / เกณฑ์ {number(score['threshold'])} — {reason} (ไม่ใช่ผลยอมรับ)")
        if rounds['pending']: lines.append(f"รอวลีจบตั้งแต่ {rounds['pending']['start']:.2f}s — ยังไม่ครบ")
        for record in rounds['records'][-20:]:
            if record['status'] == 'complete':
                lines.append(f"พบวลีครบ TEST: {record['start']:.2f}–{record['end']:.2f}s ระยะ {record['duration']:.2f}s")
            else: lines.append(f"TEST {record['status']} — ไม่เป็นช่วงครบ")
        lines.append('ไม่มีการสร้างรายการเงินจริง/ประวัติ/แจ้งเตือน; NCC ไม่ใช่ความน่าจะเป็น')
        self._text('\n'.join(lines))

    def close(self):
        self.closing = True
        self.controller.close()
        self._buttons()


def smoke_playback_test(root, panel, config):
    """Fake-only GUI check. Never enumerates or opens an audio endpoint."""
    if config.adb_serial != 'synthetic-device' or config.notify_enabled or panel.is_running:
        raise ValueError('Playback smoke requires idle synthetic configuration')
    from .offline_test_rounds import TestRoundTracker
    from .offline_audio import DiagnosticRow
    tracker = TestRoundTracker()
    for label, onset in [('paid_ready', 19.), ('end', 145.)]:
        tracker.consume(DiagnosticRow(onset+3, label, onset, onset+2, .99, (.99,.99), .95,
                                      'accepted_test_candidate', 'synthetic'))
    class FakeController:
        is_running = False
        def start(self, *args, **kwargs): self.is_running = True
        def stop(self): self.is_running = False
        def close(self): self.stop()
        def poll(self):
            return [dict(kind='playback_test', received=True, elapsed=150., rms=.1, peak=.3,
                         rounds=tracker.snapshot(), scores={})] if self.is_running else []
    from unittest.mock import patch
    original, original_getter = panel.controller, panel.get_device
    try:
        # Replace the capture owner BEFORE exercising the real Start action.
        panel.controller = FakeController()
        panel.get_device = lambda: dict(index=4, name='Synthetic output [Loopback]',
            rate=8000, channels=1, endpoint_id='{11111111-2222-3333-4444-555555555555}')
        with patch.object(messagebox, 'askyesno', return_value=True): panel.start()
        if not panel.is_running: raise RuntimeError('Synthetic Playback Start failed')
        panel.poll(); root.update()
        if 'พบวลีครบ TEST' not in panel.output.get('1.0', 'end'): raise RuntimeError('Playback TEST summary missing')
        panel.stop(); panel.poll()
        return dict(playback_test_gui=True, playback_direct_fake_capture=True,
                    playback_minutes_state=True, playback_no_real_outputs=True)
    finally:
        panel.controller.close()
        panel.controller = original
        panel.get_device = original_getter
        panel.selection = None
        panel._buttons()
