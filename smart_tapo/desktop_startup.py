"""Opt-in RTSP image/audio login startup; no source access on window open."""
from dataclasses import replace
import sys
from pathlib import Path
import json
from tkinter import BooleanVar, StringVar, messagebox, ttk
from .desktop_calibration import is_ready
from .startup import bind, manage_entry, validate_binding


class StartupPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=14)
        self.app = app
        self.coordinator = None
        self.await_audio = False
        self._started = False
        self.enabled = BooleanVar(master=self, value=app.config.windows_startup_enabled)
        self.audio = BooleanVar(master=self, value=app.config.windows_startup_audio)
        self.status = StringVar(master=self, value='ยังไม่ลงทะเบียนหรือเปิดสตรีม — ต้องยินยอมและผูก RTSP ใหม่')
        ttk.Label(self, text='เริ่มหลังเข้าสู่ Windows (บัญชีนี้)', style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self, text='ภาพและเสียงใช้ RTSP URL เดียวกัน แยก process ไม่เปิด MEmu / ADB / เสียง Windows\nผูกกล้อง แหล่ง RTSP และโปรไฟล์ เปลี่ยนแหล่ง/โปรไฟล์ต้องยินยอมใหม่ ไม่เปิด Webhook sender อัตโนมัติ', wraplength=800).pack(anchor='w', pady=8)
        ttk.Checkbutton(self, text='ยินยอมเริ่ม Capture/Worker หลัง Windows login', variable=self.enabled).pack(anchor='w')
        ttk.Checkbutton(self, text='ยินยอมฟังเสียง RTSP ต่อเนื่องด้วยโปรไฟล์ที่เลือก (ไม่ใช่ยืนยันรับเงิน)', variable=self.audio).pack(anchor='w')
        ttk.Button(self, text='บันทึก / ผูกการยินยอม Windows startup', command=self.save).pack(anchor='w', pady=8)
        ttk.Button(self, text='ลองเริ่มจากค่าที่ผูกไว้', command=self.begin).pack(anchor='w')
        ttk.Label(self, textvariable=self.status, wraplength=800).pack(anchor='w', pady=8)
        # Preserve a user's previous profile selection, never its legacy consent.
        try:
            previous = json.loads(app.config.windows_startup_binding)
            if isinstance(previous, dict) and isinstance(previous.get('profile'), str):
                app.audio_panel.listener_panel.profile_path.set(previous['profile'])
        except (ValueError, TypeError):
            pass
        try:
            saved = validate_binding(app.config)
            if saved.get('profile'):
                app.audio_panel.listener_panel.profile_path.set(saved['profile'])
            self.status.set('มีการยินยอม RTSP ที่ผูกไว้ — ยังไม่เปิดสตรีมในรอบนี้')
        except Exception:
            pass

    @property
    def is_running(self): return self.await_audio

    def save(self):
        app = self.app
        if app.closing or app.busy or app._active() or app._audio_active() or self.is_running:
            self.status.set('หยุดงานทั้งหมดก่อนเปลี่ยน startup'); return
        old = app.config
        try:
            if not self.enabled.get():
                updated = replace(old, windows_startup_enabled=False, windows_startup_audio=False, windows_startup_binding='')
            else:
                if not getattr(sys, 'frozen', False): raise ValueError('installed_exe_required')
                if app._candidate() != old or not is_ready(old, app.store.root) or app._calibration_pending():
                    raise ValueError('settings_not_ready')
                selected = replace(old, windows_startup_audio=self.audio.get())
                binding = bind(selected, app.audio_panel.listener_panel.profile_path.get().strip(), None)
                updated = replace(selected, windows_startup_enabled=True, windows_startup_binding=binding).validate()
                if not messagebox.askyesno('ยืนยัน Windows startup', 'ยินยอมเริ่มภาพและเสียง RTSP ตามรายการที่ผูกไว้ทุกครั้งหลัง login?\nไม่ยืนยัน physical Live / รับเงิน และไม่เปิด Webhook sender', parent=self): return
            app.store.save(updated)
            try:
                manage_entry(self.enabled.get(), Path(sys.executable) if self.enabled.get() else None, app.store.config_path)
            except Exception:
                app.store.save(old)
                raise
            app.config = updated
            self.status.set('บันทึก Windows startup แล้ว — ยังไม่เริ่มรับภาพ/เสียงในรอบนี้')
        except Exception:
            self.status.set('ยังไม่เปลี่ยน startup — ตรวจ EXE ถาวร/ค่าที่บันทึก/ภาพช่องว่าง/โปรไฟล์/สิทธิ์โฟลเดอร์')

    def begin(self):
        app = self.app
        if app.closing or self._started or self.is_running or app._active() or app._audio_active() or app.busy: return
        try:
            if not app.config.windows_startup_enabled or app._candidate() != app.config: raise ValueError
            saved = validate_binding(app.config)
            if app.config.windows_startup_audio:
                selected = app.audio_panel.listener_panel.profile_path.get().strip()
                if Path(selected).absolute() != Path(saved['profile']).absolute(): raise ValueError
            if not is_ready(app.config, app.store.root) or app._calibration_pending(): raise ValueError
        except Exception:
            self.status.set('พัก startup — แหล่งหรือโปรไฟล์เปลี่ยน/ยังไม่ยินยอม/ภาพช่องว่างไม่พร้อม ต้องผูกใหม่'); return
        # Audio and video starts are independent: failure of one does not cancel the other.
        self._started = True
        video_ok = True
        try:
            if app.controller.start_capture(app.config) is False and not app.controller.is_running('capture'):
                raise ValueError
            if app.controller.start_worker(app.config, app.notify_key.get().strip()) is False and not app.controller.is_running('worker'):
                raise ValueError
        except Exception:
            video_ok = False
        bindings=getattr(app,'_run_all_role_configs',{})
        for role in ('capture','worker'):
            if app.controller.is_running(role) is True:bindings[role]=app.config
        app._run_all_role_configs=bindings
        audio_ok = not app.config.windows_startup_audio
        if app.config.windows_startup_audio:
            listener = app.audio_panel.listener_panel
            try:
                listener.profile_path.set(saved['profile'])
                listener.start_bound(app.config, saved['profile'], expected_fingerprint=saved['fingerprint'])
                listener._active, listener._stopping = True, False
                listener.status.set('Windows startup: กำลังเริ่มเสียง RTSP ที่ผูกไว้')
                audio_ok = True
            except Exception:
                listener.status.set('เริ่มเสียง RTSP ที่ผูกไว้ไม่สำเร็จ — ภาพยังทำงานแยกต่างหาก')
        self.status.set('สั่งเริ่ม RTSP แล้ว — ดูสถานะจริงแต่ละ process; ไม่ใช่หลักฐาน physical Live' if video_ok and audio_ok else 'บาง process เริ่มไม่สำเร็จ — ตรวจภาพ/เสียงแยกกัน ไม่มี fallback')

    def stop(self):
        self.await_audio = False
        self.status.set('หยุด startup รอบนี้แล้ว ไม่ลองใหม่เอง')

    def close(self): self.stop()
    def poll(self): pass
