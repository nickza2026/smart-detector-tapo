"""Explicit, bounded Windows output-loopback diagnostics; never coin inference."""
from __future__ import annotations

import math
import os
from pathlib import Path
from tkinter import BooleanVar, StringVar, ttk

from .audio import AudioProbe,prepare_sample_directory
from .desktop_listener import ListenerPanel


ERRORS = {
    'unsupported_platform':'การรับเสียงแบบนี้รองรับ Windows เท่านั้น',
    'dependency_unavailable':'โหลดตัวรับเสียงไม่ได้ ใช้ EXE ที่รวม WASAPI backend แล้ว',
    'dependency_missing':'โหลดตัวรับเสียงไม่ได้ ใช้ EXE ที่รวม WASAPI backend แล้ว',
    'no_loopback_devices':'ไม่พบเสียงขาออก WASAPI ตรวจอุปกรณ์เสียง Windows/Remote Desktop',
    'device_changed':'อุปกรณ์เปลี่ยนหรือถูกตัดการเชื่อมต่อ กดรีเฟรชแล้วเลือกใหม่ ไม่มีการสลับไปไมโครโฟน',
    'invalid_device':'อุปกรณ์ไม่ใช่ output-loopback ที่เลือกไว้ กดรีเฟรชแล้วเลือกใหม่',
    'quota_exceeded':'พื้นที่ตัวอย่างเสียงถึงขีดจำกัด เปิดโฟลเดอร์และจัดการไฟล์ที่ไม่ต้องใช้ก่อน',
    'unsafe_directory':'โฟลเดอร์เสียงไม่ปลอดภัยหรือเป็นลิงก์ เปลี่ยนโฟลเดอร์ข้อมูลก่อน',
    'timeout':'รับเสียงเกินเวลาหรืออุปกรณ์ไม่ตอบสนอง จบรอบทดสอบแล้ว ตรวจอุปกรณ์เสียง/RDP',
    'worker_exited':'ตัวรับเสียงหยุดผิดปกติ ยังไม่ถือว่าบันทึกสำเร็จ',
    'capture_failed':'รับเสียงไม่สำเร็จ ตรวจอุปกรณ์ที่ MEmu ใช้และการเชื่อมต่อ RDP',
    'audio_unavailable':'อุปกรณ์เสียงหรือ driver ไม่พร้อม ตรวจเสียงขาออกของ MEmu และ RDP แล้วรีเฟรช',
    'storage_limit':'โฟลเดอร์ถึงขีดจำกัด 20 ไฟล์/64 MiB (มีการสำรองที่สำหรับรอบใหม่) จัดการไฟล์ก่อนบันทึก',
    'storage_error':'บันทึกไฟล์เสียงไม่สำเร็จ ยังไม่ถือว่าได้ WAV สมบูรณ์ ตรวจพื้นที่และสิทธิ์',
    'unsafe_or_full_directory':'โฟลเดอร์เสียงไม่พร้อม เป็นลิงก์ หรือถึงขีดจำกัด ตรวจพื้นที่และสิทธิ์',
    'clip_limit':'รูปแบบเสียงนี้จะทำให้ไฟล์เกินขีดจำกัด 32 MiB จึงไม่เริ่มบันทึก',
    'invalid_pcm':'ข้อมูลเสียงขาดหรือรูปแบบผิด รอบนี้ไม่ถือว่าบันทึกสำเร็จ',
    'empty_capture':'รอบนี้ยังอ่านข้อมูลเสียงไม่ได้ ไม่ใช่การยืนยันว่าเสียงเงียบหรือไม่มีเหรียญ',
    'worker_failed':'ตัวรับเสียงหยุดผิดปกติ ยังไม่ถือว่าบันทึกสำเร็จ',
    'stopped':'หยุดตัวรับเสียงที่ไม่ตอบสนองแล้ว ไม่ถือว่าได้ไฟล์ WAV สำเร็จ',
}


class AudioPanel(ttk.Frame):
    def __init__(self,parent,config_getter,probe=None):
        super().__init__(parent,padding=14)
        self.get_config=config_getter
        self.probe=probe if probe is not None else AudioProbe()
        self.closing=False
        self.devices=[]
        self.output_directory=None
        self.last_file=None
        self.consent=BooleanVar(master=self,value=False)
        self.status=StringVar(master=self,value='ยังไม่รับเสียง — กดรีเฟรชเพื่อเลือกเสียงขาออกก่อน')
        self.level=StringVar(master=self,value='ยังไม่มีตัวอย่างเสียง')
        self.saved=StringVar(master=self,value='ยังไม่มีไฟล์เสียง ไม่มีการอัดเมื่อเปิดโปรแกรมหรือเปิดแท็บ')
        self.modes=ttk.Notebook(self);self.modes.pack(fill='both',expand=True)
        content=ttk.Frame(self.modes,padding=6);self.modes.add(content,text='ทดสอบ 10 วินาที / เลือกอุปกรณ์')
        ttk.Label(content,text='ทดสอบเสียงจาก Tapo ผ่าน MEmu',style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(content,text='เปิดหน้า Live และเปิดลำโพงใน Tapo (ไม่ใช่ปุ่มพูดคุย/ไมค์) แล้วเลือกเสียงขาออกที่ MEmu ใช้',
                  wraplength=750).pack(anchor='w',pady=(5,6))
        ttk.Label(content,text='Windows Loopback รับเสียงขาออกทั้งอุปกรณ์ อาจรวมเสียงโปรแกรมอื่น ไม่ได้แยกเฉพาะ Tapo และไม่ใช้ไมโครโฟนเป็นตัวสำรอง',
                  wraplength=750).pack(anchor='w')
        row=ttk.Frame(content)
        row.pack(fill='x',pady=10)
        self.refresh_button=ttk.Button(row,text='รีเฟรชอุปกรณ์เสียง',command=self.refresh)
        self.refresh_button.pack(side='left',padx=(0,8))
        self.device_box=ttk.Combobox(row,state='readonly',width=60)
        self.device_box.pack(side='left',fill='x',expand=True)
        self.device_box.bind('<<ComboboxSelected>>',lambda event:self._buttons())
        self.consent_box=ttk.Checkbutton(content,text='ยอมรับว่ารอบนี้อาจรับ/บันทึกเสียงแอปอื่นที่ใช้ช่องเสียงเดียวกัน',
                                       variable=self.consent,command=self._buttons)
        self.consent_box.pack(anchor='w')
        actions=ttk.Frame(content)
        actions.pack(anchor='w',pady=10)
        self.monitor_button=ttk.Button(actions,text='ดูระดับเสียง 10 วินาที (ไม่เก็บไฟล์)',command=lambda:self.start(False))
        self.monitor_button.pack(side='left',padx=(0,8))
        self.record_button=ttk.Button(actions,text='บันทึก WAV 10 วินาที',command=lambda:self.start(True))
        self.record_button.pack(side='left',padx=(0,8))
        self.stop_button=ttk.Button(actions,text='หยุดรับเสียง',command=self.stop)
        self.stop_button.pack(side='left')
        ttk.Label(content,textvariable=self.level,wraplength=750).pack(anchor='w')
        self.meter=ttk.Progressbar(content,maximum=100)
        self.meter.pack(fill='x',pady=(4,8))
        ttk.Label(content,textvariable=self.status,wraplength=750).pack(anchor='w')
        ttk.Label(content,textvariable=self.saved,wraplength=750).pack(anchor='w',pady=8)
        self.folder_button=ttk.Button(content,text='เปิดโฟลเดอร์ตัวอย่างเสียง',command=self.open_folder)
        self.folder_button.pack(anchor='w')
        ttk.Label(content,text='แต่ละรอบหยุดเองภายในเวลาจำกัด ไฟล์อยู่เฉพาะเครื่อง ไม่ส่งเสียงขึ้นเว็บ ไม่ตรวจเหรียญ/จำนวนเงิน และไม่สั่งแจ้งเตือน',
                  wraplength=750).pack(anchor='w',pady=(10,3))
        ttk.Label(content,text='ถ้า VPS ไม่มี sound device หรือเสียงหายเมื่อออกจาก RDP จะรับเสียงไม่ได้ ต้องทดสอบสภาพใช้งานนั้นจริง ไม่ถือว่าไม่มีเหรียญ',
                  wraplength=750).pack(anchor='w')
        self.listener_panel=ListenerPanel(self.modes,self.get_config,self._selected_device,
            other_running=lambda:self.probe.is_running or (hasattr(self,'playback_panel') and self.playback_panel.is_running))
        self.modes.add(self.listener_panel,text='ฟังประวัติเติมต่อเนื่อง (ทดลอง)')
        from .desktop_playback_test import PlaybackTestPanel
        self.playback_panel=PlaybackTestPanel(self.modes,self.get_config,self._selected_device,
            self.listener_panel,other_running=lambda:self.probe.is_running or self.listener_panel.is_running)
        self.modes.add(self.playback_panel,text='ทดสอบฟัง Playback (TEST)')
        self._buttons()

    @property
    def is_running(self):
        return self.probe.is_running or self.listener_panel.is_running or self.playback_panel.is_running

    def _selected_device(self):
        index=self.device_box.current()
        return dict(self.devices[index]) if 0<=index<len(self.devices) else None

    def _buttons(self):
        busy=self.is_running or self.closing
        selected=0<=self.device_box.current()<len(self.devices)
        self.refresh_button.configure(state='disabled' if busy else 'normal')
        self.device_box.configure(state='disabled' if busy else 'readonly')
        self.consent_box.configure(state='disabled' if busy else 'normal')
        allowed=not busy and selected and self.consent.get()
        for button in (self.monitor_button,self.record_button):
            button.configure(state='normal' if allowed else 'disabled')
        self.stop_button.configure(state='normal' if self.is_running and not self.closing else 'disabled')
        self.folder_button.configure(state='disabled' if self.closing else 'normal')

    def refresh(self):
        if self.closing or self.is_running:return
        try:
            self.probe.start_list()
            self.devices=[]
            self.device_box.set('')
            self.device_box.configure(values=())
            self.status.set('กำลังค้นหาเสียงขาออกเท่านั้น ยังไม่เปิดรับเสียง')
        except Exception:
            self.status.set('เริ่มค้นหาอุปกรณ์ไม่ได้ รอรอบปัจจุบันให้จบแล้วลองใหม่')
        self._buttons()

    def start(self,record):
        if self.closing or self.is_running:return
        index=self.device_box.current()
        if not self.consent.get() or not 0<=index<len(self.devices):
            self.status.set('เลือกเสียงขาออกและยอมรับการรับเสียงทั้งอุปกรณ์ก่อน')
            return
        try:
            directory=prepare_sample_directory(Path(self.get_config().data_dir)) if record else None
            self.probe.start_capture(self.devices[index],directory=directory,duration=10)
            if record:self.output_directory=directory
            self.last_file=None
            self.meter['value']=0
            self.level.set('กำลังรอตัวอย่างเสียง ไม่ได้ยืนยันว่าเป็นเสียงจาก Tapo')
            self.status.set('กำลังบันทึกเสียงขาออก สูงสุด 10 วินาที' if record else 'กำลังดูระดับเสียง สูงสุด 10 วินาที — ไม่บันทึกไฟล์')
            self.saved.set('ยังไม่ถือว่ามีไฟล์สำเร็จจนตัวรับเสียงจบรอบ' if record else 'รอบนี้ไม่เก็บไฟล์เสียง')
        except Exception as error:
            self.status.set(ERRORS.get(str(error),'เริ่มรับเสียงไม่ได้ ตรวจอุปกรณ์และรอบทดสอบปัจจุบัน'))
        self._buttons()

    def stop(self):
        self.probe.stop()
        self.listener_panel.stop()
        self.playback_panel.stop()
        if self.is_running:self.status.set('กำลังหยุดตัวรับเสียง รอให้ปิดอุปกรณ์และไฟล์ให้เรียบร้อย')
        self._buttons()

    @staticmethod
    def _number(value,default=None):
        if type(value) not in (int,float) or not math.isfinite(value):return default
        return float(value)

    def poll(self):
        self.listener_panel.poll()
        self.playback_panel.poll()
        for event in self.probe.poll():
            kind=event.get('kind')
            if kind=='devices':
                self.devices=event.get('devices',[])[:64]
                labels=[]
                for device in self.devices:
                    name=''.join(c for c in str(device.get('name','')) if c.isprintable())[:110]
                    labels.append(f"{device.get('index','?')}: {name}")
                self.device_box.configure(values=labels)
                self.device_box.set('')  # No default/microphone fallback.
                try:
                    from .startup import matching_device, validate_binding
                    config=self.get_config()
                    if config.windows_startup_enabled and config.windows_startup_audio:
                        saved=validate_binding(config)
                        selected=matching_device(saved['device'],self.devices)
                        self.device_box.current(self.devices.index(selected))
                except Exception:
                    pass  # Missing/changed binding remains unselected, never guesses.
                self.status.set('เลือกอุปกรณ์ที่ MEmu เล่นเสียงออก แล้วกดทดสอบ' if self.devices else
                                'ไม่พบ output-loopback ตรวจอุปกรณ์เสียง Windows/RDP ไม่ใช่หลักฐานว่ากล้องไม่มีเสียง')
            elif kind=='level':
                rms=self._number(event.get('rms_dbfs'))
                peak=self._number(event.get('peak_dbfs'))
                elapsed=self._number(event.get('elapsed'),0)
                self.meter['value']=0 if rms is None else max(0,min(100,100+rms))
                text='เงียบในช่วงที่อ่านได้' if rms is None else f'RMS {rms:.1f} dBFS'
                text+=' / peak '+('เงียบ' if peak is None else f'{peak:.1f} dBFS')
                if event.get('clipped'):text+=' — สัญญาณเต็มสเกล/อาจแตก'
                self.level.set(f'{text} · อ่านแล้ว {elapsed:.1f} วินาที (ไม่ใช่ความมั่นใจว่าเป็นเหรียญ)')
            elif kind=='finished':
                if event.get('mode')=='list':
                    self.status.set('เลือกอุปกรณ์ที่ MEmu เล่นเสียงออก แล้วกดทดสอบ' if self.devices else
                                    'ไม่พบ output-loopback ตรวจอุปกรณ์เสียง Windows/RDP ไม่ใช่หลักฐานว่ากล้องไม่มีเสียง')
                    continue
                seconds=self._number(event.get('duration'),0)
                self.status.set(f'จบรอบรับเสียง {seconds:.1f} วินาที ไม่ได้ตรวจเหรียญหรือส่งแจ้งเตือน')
                path=event.get('path')
                if event.get('mode')=='record' and path and self._valid_saved_path(path):
                    self.last_file=Path(path)
                    suffix=' — ไฟล์เป็นเสียงเงียบ' if event.get('silent') else ' — ยังไม่ยืนยันต้นทางว่าเป็น Tapo'
                    self.saved.set('บันทึกแล้ว: '+str(self.last_file)+suffix)
                elif event.get('mode')=='record':
                    self.saved.set('ยังไม่มีไฟล์ WAV สำเร็จ ตรวจโฟลเดอร์และสถานะตัวรับเสียง')
            elif kind=='error':
                self.status.set(ERRORS.get(event.get('code'),'รับเสียงไม่สำเร็จ ตรวจเสียงขาออก Windows/RDP แล้วรีเฟรชอุปกรณ์'))
        self._buttons()

    def _valid_saved_path(self,path):
        try:
            file=Path(path)
            return (self.output_directory is not None and file.suffix.lower()=='.wav'
                    and not file.is_symlink() and not file.is_junction() and file.is_file()
                    and file.resolve().parent==self.output_directory.resolve())
        except (OSError,ValueError):return False

    def open_folder(self):
        directory=self.output_directory or Path(self.get_config().data_dir)/'audio-samples'
        try:
            if os.name!='nt' or not directory.is_dir():
                self.status.set('ยังไม่มีโฟลเดอร์เสียง จะสร้างเมื่อกดบันทึกตัวอย่างเท่านั้น')
                return
            if directory.is_symlink() or directory.is_junction():
                self.status.set('ไม่เปิดโฟลเดอร์เสียงที่เป็นลิงก์')
                return
            os.startfile(directory)
        except OSError:self.status.set('เปิดโฟลเดอร์เสียงไม่ได้ ตรวจสิทธิ์โฟลเดอร์ข้อมูล')

    def close(self):
        self.closing=True
        self.probe.close()
        self.listener_panel.close()
        self.playback_panel.close()
        self._buttons()
