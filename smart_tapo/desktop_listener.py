"""Explicit experimental listener controls; construction never starts capture."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import math
from tkinter import BooleanVar, StringVar, filedialog, ttk

from .prepared_profile import default_prepared_manifest
from .watch_listener import WatchListener


ERRORS = {
    'profile_invalid': 'โปรไฟล์ไม่พร้อมหรือไม่ตรงกับกล้อง ตรวจโปรไฟล์ส่วนตัวก่อนเริ่มใหม่',
    'live_check_failed': 'ตรวจโปรไฟล์หรือหน้า Live ไม่ผ่าน ต้องเป็น Live และกล้องที่ตรงกัน',
    'lease_invalid': 'ยืนยันหน้า Live ไม่ได้ หยุดรับเสียงแล้ว',
    'lease_expired': 'การยืนยันหน้า Live หมดอายุ หยุดรับเสียงแล้ว',
    'audio_unavailable': 'อุปกรณ์เสียงไม่พร้อม ตรวจเสียงขาออก Windows และ RDP',
    'audio_availability_failed':'อ่านจำนวนข้อมูลจาก driver เสียงไม่สำเร็จ พักและรออุปกรณ์เดิม ไม่สลับไปไมโครโฟน',
    'device_changed': 'อุปกรณ์เสียงเปลี่ยน เลือกเสียงขาออกในแท็บทดสอบเสียงใหม่',
    'invalid_device': 'เลือกอุปกรณ์เสียงขาออกในแท็บทดสอบเสียงก่อน',
    'unsupported_stream_format': 'รูปแบบเสียงใช้หน่วยความจำเกินขอบเขตหรือไม่รองรับ เลือก output-loopback แบบสเตอริโอ 44.1/48 kHz ไม่สลับให้อัตโนมัติ',
    'audio_timeout': 'ตัวรับเสียงไม่ตอบสนอง หยุดรอบนี้แล้ว',
    'storage_error': 'เก็บผลประกาศไม่สำเร็จ ตรวจพื้นที่และสิทธิ์ข้อมูล',
    'stream_fault': 'ข้อมูลเสียงไม่ต่อเนื่อง หยุดรอบนี้แล้ว',
    'audio_clock_discontinuity':'เวลาเสียงกับเวลาระบบคลาดเกิน 2 วินาที หยุดเพื่อไม่จับคู่เสียงผิดช่วง',
    'audio_sequence_gap':'ลำดับชุดเสียงขาดช่วง หยุดรอบนี้แล้ว',
    'invalid_audio_chunk':'ชุดข้อมูลเสียงผิดรูปหรือมีค่าที่ใช้ไม่ได้ หยุดรอบนี้แล้ว',
    'audio_short_read':'driver ส่งข้อมูลเสียงมาไม่ครบชุด หยุดรอบนี้แล้ว',
    'live_provenance_lost':'ช่วงเวลาของเสียงไม่อยู่ในผลยืนยัน Live ที่ยังใช้ได้ หยุดรอบนี้แล้ว',
    'acoustic_analysis_failed':'ตัววิเคราะห์รูปแบบเสียงล้มเหลว หยุดโดยไม่ยืนยันประกาศใหม่',
    'acoustic_result_limit':'ผลวิเคราะห์เสียงมากเกินขอบเขต หยุดโดยไม่เลือกประกาศเอง',
    'invalid_acoustic_result':'ผลวิเคราะห์เสียงผิดรูป หยุดรอบนี้แล้ว',
    'invalid_acoustic_span':'ช่วงเวลาของวลีจากตัววิเคราะห์ใช้ไม่ได้ หยุดรอบนี้แล้ว',
    'invalid_stream_configuration':'ค่ารูปแบบ stream ใช้ไม่ได้ ยังไม่รับเสียง',
    'invalid_stream_profile':'ชนิดวลีของโปรไฟล์ไม่ครบ ยังไม่รับเสียง',
    'profile_too_long_for_stream':'วลียาวเกินขอบเขต stream ยังไม่รับเสียง',
    'stream_already_failed':'stream นี้เคยล้มเหลวแล้ว ไม่ต่อเสียงข้ามช่วงที่หาย',
    'unsupported_profile_span':'วลีในโปรไฟล์ยาวเกิน 9.5 วินาที ยังไม่รับเสียง',
    'profile_changed':'โปรไฟล์เปลี่ยนจากตอนเริ่ม ไม่สลับต้นแบบระหว่างเฝ้า รอไฟล์เดิมหรือกดหยุดเพื่อตั้งใหม่',
    'live_check_unavailable':'อ่านหน้า Tapo ผ่าน ADB ไม่สำเร็จ (เชื่อมต่อ/timeout/UI dump) ยังไม่ยืนยัน Live',
    'initial_check_expired':'ตรวจเริ่มนานจนผล Live เกินอายุ 10 วินาที ไม่ใช้ผลเก่าเริ่มรับเสียง',
    'live_check_expired':'ผลตรวจ Live รอบใหม่กลับมาช้าเกินอายุ ไม่ใช้ผลเก่าต่อเสียง',
    'listener_start_failed':'ตรวจ Live แล้ว แต่สร้างตัวรับเสียงแยก process ไม่สำเร็จ',
    'initial_check_failed':'ขั้นเตรียมรับเสียงล้มเหลวโดยยังระบุสาเหตุย่อยไม่ได้',
    'live_control_backlog':'คิวผลยืนยัน Live เต็ม ไม่ใช้หลักฐานที่ส่งไม่ทัน',
    'live_proof_invalid':'รูปแบบหลักฐาน Live ไม่ถูกต้อง ยังไม่รับเสียง',
    'live_playback_detected':'ตรวจพบหน้า Playback ไม่เริ่ม/ไม่รับเสียงย้อนหลังเป็นเหตุการณ์จริง',
    'live_mode_unconfirmed':'ยังระบุโหมดหน้าปัจจุบันไม่ได้ ไม่เริ่มรับเสียง',
    'live_live_refresh_required':'หน้า Live รอ refresh — กำลังกู้กล้องเดิมโดยไม่ใช้เสียงช่วงขาด',
    'live_live_inactivity_confirmation':'Tapo ถามดูต่อครบ 5 นาที — ต้องมีหลักฐานกล้องเดิมล่าสุดก่อนกู้ ไม่ใช้เสียงช่วงขาด',
    'live_navigation_changed':'หน้า Tapo กำลังเปลี่ยน/กู้ Live — พักเสียงและตรวจหลักฐานใหม่ก่อนรับต่อ',
    'live_camera_not_confirmed':'ชื่อกล้องที่อ่านได้ไม่ยืนยันกับค่าที่ตั้งไว้ ยังไม่รับเสียง',
    'live_incomplete_live_layout':'อ่านองค์ประกอบหน้า Live ไม่ครบ ยังไม่ยืนยัน Live',
    'live_unrecognized_foreground':'หน้าที่อยู่เบื้องหน้าไม่ใช่หน้ากล้อง Live ที่รองรับ',
    'live_foreground_changed_or_unknown':'หน้าที่อยู่เบื้องหน้าเปลี่ยนระหว่างตรวจหรืออ่านไม่ได้',
    'live_playback_or_conflicting_controls':'พบปุ่ม/แถบ Playback ขัดกับหน้า Live ไม่รับเสียงต่อ',
    'live_unsupported_package':'แอปกล้องไม่ตรงกับแอปที่รองรับ',
    'live_invalid_hierarchy':'ข้อมูลโครงสร้างหน้า Tapo ใช้ไม่ได้',
    'live_sensitive_page':'พบหน้าที่มีช่องรหัสผ่าน ไม่เก็บหรือใช้หน้านี้',
    'live_incomplete_playback_layout':'โครงสร้างหน้า Playback ไม่ครบ ไม่ยืนยันว่าเป็น Live',
    'close_failed': 'ปิดตัวรับเสียงไม่สมบูรณ์ ผลรอบนี้อาจไม่ครบ',
    'stopped': 'บังคับหยุดตัวรับเสียงแล้ว ผลรอบนี้อาจไม่ครบ',
    'worker_failed': 'ตัวรับเสียงหยุดผิดปกติ ผลรอบนี้อาจไม่ครบ',
    'listener_failed': 'รับเสียงไม่สำเร็จ ผลรอบนี้อาจไม่ครบ',
}
GENERIC_ERROR = 'เริ่มหรือรับเสียงไม่สำเร็จ ตรวจโปรไฟล์ หน้า Live และอุปกรณ์ก่อนลองใหม่'


def error_text(event):
    code=event.get('code')
    if type(code) is not str or code not in ERRORS:return GENERIC_ERROR
    text=ERRORS[code]+' ['+code+']'
    detail=event.get('diagnostics')
    if type(detail) is dict:
        parts=[]
        for key,label in (('check_seconds','check'),('lag_seconds','lag'),('read_seconds','read'),
                          ('work_seconds','work'),('max_work_seconds','maxwork'),('audio_seconds','audio'),('wall_seconds','wall')):
            value=detail.get(key)
            if type(value) in (int,float) and math.isfinite(value) and abs(value)<=86400 and (key=='lag_seconds' or value>=0):
                parts.append(f'{label}={value:.2f}s')
        if parts:text+='\n'+' | '.join(parts)
    return text


def _prepare_provided(config,manifest):
    from .prepared_profile import prepare_profile
    return prepare_profile(config.data_dir,config.camera_id,manifest)


class ListenerPanel(ttk.Frame):
    def __init__(self, parent, config_getter, device_getter, *, runtime=None,
                 other_running=lambda: False,prepared_manifest=None):
        super().__init__(parent, padding=12)
        self.get_config, self.get_device = config_getter, device_getter
        self.runtime = runtime if runtime is not None else WatchListener()
        self.other_running = other_running
        self.closing = self._stopping = self._active = False
        self.profile_dialog=None
        base=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
        self.prepared_manifest=Path(prepared_manifest) if prepared_manifest is not None else default_prepared_manifest(base)
        self._prepare_future=self._prepare_executor=None
        self._announcements = 0
        self.profile_path = StringVar(master=self)
        self.consent = BooleanVar(master=self, value=False)
        self.status = StringVar(master=self, value='ยังไม่รับเสียง — เลือกโปรไฟล์ส่วนตัวและยินยอมแต่ละรอบก่อน')
        self.device_summary = StringVar(master=self)
        self.count = StringVar(master=self, value='ประกาศที่ยอมรับ: 0 (ไม่ใช่รายการจ่ายค่าน้ำมัน)')
        self.health_status = StringVar(master=self,value='ประวัติสุขภาพ: ยังไม่มีข้อมูล — ไม่เก็บไฟล์เสียง')
        ttk.Label(self, text='ฟังประกาศต่อเนื่อง (ทดลอง)', style='Subheading.TLabel').pack(anchor='w')
        for text in (
            'ต้องเปิดหน้า Live และกล้องตรงกับโปรไฟล์ — การตรวจหน้าไม่ได้แยกเสียงเฉพาะ Tapo',
            'ไม่เก็บไฟล์เสียง ไม่ส่งเสียงผ่านเครือข่าย ไม่คำนวณจำนวนเงิน/จำนวนเหรียญ และยังไม่เชื่อมหลักฐานอัตโนมัติ',
        ):
            ttk.Label(self, text=text, wraplength=700).pack(anchor='w', pady=(4, 0))
        row = ttk.Frame(self)
        row.pack(fill='x', pady=8)
        ttk.Label(row, text='โปรไฟล์ JSON ส่วนตัว:').pack(side='left')
        self.path_entry = ttk.Entry(row, textvariable=self.profile_path)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.browse_button = ttk.Button(row, text='เลือกไฟล์…', command=self.choose_profile)
        self.browse_button.pack(side='left')
        self.prepared_button=ttk.Button(self,text='ใช้โปรไฟล์จากเสียงที่เตรียมให้',command=self.use_prepared_profile)
        self.prepared_button.pack(anchor='w',pady=(0,5))
        self.create_button=ttk.Button(self,text='ตั้งโปรไฟล์เอง (ขั้นสูง)…',command=self.create_profile)
        self.create_button.pack(anchor='w',pady=(0,5))
        ttk.Label(self, textvariable=self.device_summary, wraplength=700).pack(anchor='w', pady=4)
        self.consent_box = ttk.Checkbutton(
            self, text='ยินยอมเฝ้าจนกดหยุด รวมกู้คืนอุปกรณ์เดิม: เสียง Windows แบบผสมอาจมีแอปอื่น (ทดลอง)',
            variable=self.consent, command=self._buttons)
        self.consent_box.pack(anchor='w', pady=6)
        row = ttk.Frame(self)
        row.pack(anchor='w', pady=6)
        self.start_button = ttk.Button(row, text='เริ่มฟังต่อเนื่อง', command=self.start)
        self.start_button.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(row, text='หยุดฟัง', command=self.stop)
        self.stop_button.pack(side='left')
        ttk.Label(self, textvariable=self.status, wraplength=700).pack(anchor='w', pady=6)
        ttk.Label(self, textvariable=self.count, wraplength=700).pack(anchor='w')
        ttk.Label(self,textvariable=self.health_status,wraplength=700).pack(anchor='w',pady=(4,0))
        self.profile_path.trace_add('write', lambda *_: self._buttons())
        self.consent.trace_add('write', lambda *_: self._buttons())
        self._buttons()

    @property
    def is_running(self):
        return (self.runtime.is_running or self._prepare_future is not None
                or (self.profile_dialog is not None and bool(self.profile_dialog.winfo_exists())))

    def _selection(self):
        try:
            device = self.get_device()
            return device if isinstance(device, dict) and device else None
        except Exception:
            return None

    def _other_busy(self):
        try:
            return bool(self.other_running())
        except Exception:
            return True

    def _buttons(self):
        # close() must still release runtime/futures after Tk destroyed its
        # windows. Pure Tcl command lookup remains valid after Tk teardown.
        if not self.tk.call('info','commands',self._w):return
        busy = self.is_running or self.closing
        device = self._selection()
        name = ''.join(c for c in str(device.get('name', '')) if c.isprintable())[:110] if device else ''
        self.device_summary.set('เสียงขาออกร่วมกับแท็บทดสอบเสียง: ' + name if device else
                                'ยังไม่ได้เลือกเสียงขาออก — เลือกในแท็บทดสอบเสียงก่อน')
        allowed = (not busy and not self._other_busy() and device is not None
                   and bool(self.profile_path.get().strip()) and self.consent.get())
        self.start_button.configure(state='normal' if allowed else 'disabled')
        self.stop_button.configure(state='normal' if self.is_running and not self.closing else 'disabled')
        for widget in (self.path_entry, self.browse_button, self.consent_box,self.create_button,self.prepared_button):
            widget.configure(state='disabled' if busy else 'normal')

    def use_prepared_profile(self):
        if self.closing or self.is_running or self._other_busy():return
        self._stopping=False
        self.profile_path.set('')  # Failed preparation must never retain stale selection.
        self.consent.set(False)
        try:
            self._prepare_config=deepcopy(self.get_config())
            self._prepare_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='prepared-profile')
            self._prepare_future=self._prepare_executor.submit(_prepare_provided,self._prepare_config,self.prepared_manifest)
            self.status.set('กำลังจัดโปรไฟล์จากเสียงที่เตรียมให้และผูกกับกล้องนี้ — ยังไม่เริ่มรับเสียง')
        except Exception:
            if self._prepare_executor is not None:self._prepare_executor.shutdown(wait=False,cancel_futures=True)
            self._prepare_executor=self._prepare_future=None
            self.status.set('เตรียมโปรไฟล์ไม่สำเร็จ แตก ZIP ทั้งชุดให้มีโฟลเดอร์ prepared-profile ข้าง EXE และตรวจพื้นที่/สิทธิ์')
        self._buttons()

    def create_profile(self):
        if self.closing or self.is_running or self._other_busy():return
        self._stopping=False
        try:
            from .profile_setup import open_profile_setup
            self.profile_dialog=open_profile_setup(self,self.get_config(),lambda path:self.profile_path.set(str(path)))
        except Exception:self.status.set('เปิดหน้าสร้างโปรไฟล์ไม่ได้ ตรวจการตั้งค่าและโฟลเดอร์ข้อมูล')
        self._buttons()

    def choose_profile(self):
        if self.closing or self.is_running:
            return
        path = filedialog.askopenfilename(parent=self, title='เลือกโปรไฟล์ JSON ส่วนตัว',
                                         filetypes=[('JSON profile', '*.json')])
        if path:
            self.profile_path.set(path)

    def start(self):
        if self.closing or self.is_running or self._other_busy():
            return
        device = self._selection()
        path = self.profile_path.get().strip()
        if not device or not path or not self.consent.get():
            self.status.set('เลือกโปรไฟล์ อุปกรณ์เสียง และยินยอมรอบนี้ก่อน')
            self._buttons()
            return
        try:
            config = deepcopy(self.get_config())
            self.runtime.start(config, deepcopy(device), path, consent=True)
        except Exception:
            self.status.set(GENERIC_ERROR)
        else:
            self._active = True
            self._stopping = False
            self._announcements = 0
            self.count.set('ประกาศที่ยอมรับ: 0 (ไม่ใช่รายการจ่ายค่าน้ำมัน)')
            self.consent.set(False)
            self.status.set('กำลังเตรียมโปรไฟล์และตรวจหน้า Live — ยังไม่ยืนยันว่าเริ่มรับเสียง')
        self._buttons()

    def stop(self):
        self.runtime.stop()
        if self._prepare_future is not None:self._prepare_future.cancel()
        self.consent.set(False)
        self._stopping = True
        if self.is_running:
            self.status.set('กำลังหยุดรับเสียง ผลรอบนี้อาจไม่ครบ — รอปิดตัวรับเสียง')
        self._buttons()

    def poll(self):
        if self._prepare_future is not None and self._prepare_future.done():
            future,self._prepare_future=self._prepare_future,None
            executor,self._prepare_executor=self._prepare_executor,None
            try:
                path=future.result()
                current=self.get_config()
                same=(current.camera_id,current.data_dir)==(self._prepare_config.camera_id,self._prepare_config.data_dir)
                if not self.closing and not self._stopping and same:
                    self.profile_path.set(str(path))
                    self.status.set('พร้อมใช้โปรไฟล์จากเสียงที่คุณส่งแล้ว (ทดลอง) — เลือกอุปกรณ์/ยินยอมแล้วกดเริ่มเอง')
                elif not self.closing:self.status.set('หยุดเตรียมหรือการตั้งค่าเปลี่ยนแล้ว ยังไม่ได้เริ่มรับเสียง')
            except Exception:
                if not self.closing:
                    self.status.set('เตรียมโปรไฟล์ไม่สำเร็จ ตรวจ ZIP ทั้งชุด/พื้นที่/สิทธิ์ ไม่ใช้โปรไฟล์เก่าแทนผลที่ล้มเหลว')
            finally:
                if executor is not None:executor.shutdown(wait=False,cancel_futures=True)
        if (self.closing or self._stopping) and self.profile_dialog is not None and self.profile_dialog.winfo_exists():
            self.profile_dialog.request_close()
        # Explicit watch consent permits recovery; Stop/Close cancels it while
        # still owning/reaping all pending SDK and audio work.
        for event in self.runtime.poll():
            kind = event.get('kind')
            if kind == 'listener_progress':
                frames = event.get('frames')
                if not self._stopping and not self.closing:
                    if event.get('waiting_audio') is True:
                        self.status.set('ยังเฝ้าอยู่ — อุปกรณ์ยังไม่ส่งข้อมูลเสียง รอให้อัตโนมัติ ไม่ใช่การยืนยันว่าไม่มีการเติม')
                    elif event.get('awaiting_boundary') is True:
                        self.status.set('กำลังฟังต่อหลังเสียงขาด — รอประกาศสิ้นสุดก่อนเปิดรอบใหม่ ไม่ผูกลูกค้าข้ามช่วงที่หาย')
                    elif type(frames) is int and frames>0:
                        self.status.set('กำลังรับเสียงขาออกแบบผสม (ยังไม่ยืนยันต้นทางเฉพาะ Tapo)')
                observations = event.get('observations')
                total=event.get('observations_total')
                if type(total) is int and total>=0:
                    self._announcements=total
                    self.count.set(f'ประกาศที่ยอมรับ: {self._announcements} (ไม่ใช่รายการจ่ายค่าน้ำมัน)')
                elif type(observations) is int and observations >= 0:
                    self._announcements += observations
                    self.count.set(f'ประกาศที่ยอมรับ: {self._announcements} (ไม่ใช่รายการจ่ายค่าน้ำมัน)')
            elif kind == 'error':
                self.status.set(error_text(event))
            elif kind == 'listener_retry' and not self._stopping and not self.closing:
                delay=event.get('retry_seconds')
                seconds=format(delay,'g') if type(delay) in (int,float) and 0<=delay<=30 else '30'
                self.status.set('ยังเฝ้าอยู่ — พักรับเสียงและจะตรวจ Live/ลองอุปกรณ์เดิมอัตโนมัติใน '+str(seconds)+' วินาที ไม่ต้องกดเริ่มใหม่\n'+error_text(event))
            elif kind == 'listener_rechecking' and not self._stopping and not self.closing:
                self.status.set('ยังเฝ้าอยู่ — กำลังตรวจ Live ใหม่ก่อนกู้คืนการรับเสียงจากอุปกรณ์เดิม')
            elif kind == 'finished':
                self.status.set('หยุดรับเสียงแล้ว ผลรอบนี้อาจไม่ครบ ไม่ใช่การยืนยันรายการจ่ายค่าน้ำมัน')
        if self._active and not self.is_running:
            self._active = False
            if self._stopping and 'กำลังหยุด' in self.status.get():
                self.status.set('หยุดรับเสียงแล้ว ผลรอบนี้อาจไม่ครบ')
        health=getattr(self.runtime,'health_snapshot',None)
        if type(health) is dict:
            if health.get('io_error') is True:
                text='บันทึกประวัติสุขภาพไม่ได้ — สถานะการรับเสียงให้ดูข้อความด้านบน'
            elif health.get('records_written',0)>0:
                text='บันทึกประวัติสุขภาพแล้ว (ไม่เก็บเสียง/ข้อความพูด) — logs/listener-health.jsonl'
            else:text='ประวัติสุขภาพ: ยังไม่มีข้อมูล — ไม่เก็บไฟล์เสียง'
            if self.health_status.get()!=text:self.health_status.set(text)
        self._buttons()

    def close(self):
        if not self.closing:
            self.closing = True
            self._stopping = True
            self.consent.set(False)
            self.runtime.close()
            if self._prepare_future is not None:self._prepare_future.cancel()
            if self._prepare_executor is not None:self._prepare_executor.shutdown(wait=False,cancel_futures=True)
            self.status.set('กำลังปิดตัวรับเสียง — ผลรอบนี้อาจไม่ครบ')
        self._buttons()
