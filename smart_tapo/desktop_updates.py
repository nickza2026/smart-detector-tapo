"""Tk public updater view. Background work never calls Tk or business services."""
from __future__ import annotations
import queue
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox,ttk
from . import updates
from .diagnostics import APP_VERSION


def _blocked_message(state):
    """Name only verified booleans; unknown state never fabricates a cause."""
    prefix='ยังอัปเดตไม่ได้: '
    if (type(state) is not dict or set(state)!=set(updates.BUSINESS_FLAGS)
            or any(type(value) is not bool for value in state.values())):
        return prefix+'ตรวจสถานะไม่ได้ หรือการตั้งค่ายังไม่บันทึก — ไม่ติดตั้งและไม่หยุดงานให้เอง'
    labels={
        'workers':'process/งานเบื้องหลังยังทำงาน (workers)',
        'capture':'ยังรับภาพอยู่ (capture)',
        'listener':'ยังฟังเสียงหรือรอลองใหม่อยู่ (listener)',
        'calibration':'ยังตั้งภาพช่องว่างหรือทำงานตั้งค่าอยู่ (calibration)',
        'offline':'ยังประมวลผลไฟล์เสียงอยู่ (offline)',
        'webhook':'ยังเปิดตัวส่ง API หรือทดสอบ Notify API อยู่ (webhook)',
        'open_round':'มีรอบเติมที่ยังไม่มีเสียงจบ (open_round)',
        'interrupted_round':'ยังรอเสียงจบยืนยันขอบเขตหลังข้อมูลเสียงขาด/หมดอายุ หยุด process อย่างเดียวไม่ยืนยันขอบเขตนี้ (interrupted_round)',
        'outstanding_writes':'ยังมีงานประวัติ/งานเบื้องหลังค้างอยู่ (outstanding_writes)',
    }
    return prefix+'; '.join(labels[key] for key in updates.BUSINESS_FLAGS if state[key])


class UpdatesPanel(ttk.Frame):
    def __init__(self,parent,*,state_getter=lambda:None,install_request=None,on_release=lambda version:None):
        super().__init__(parent,padding=14)
        self.stop=threading.Event()
        self.results=queue.Queue(maxsize=1)
        self.client=updates.ReleaseClient()
        self.thread=None
        self.release=None
        self.directory=None
        self.closed=False
        self.state_getter,self.install_request,self.on_release=state_getter,install_request,on_release
        self.downloaded=None
        self.installing=False
        self.periodic_timer=None
        self.periodic_enabled=False
        self.current=int(APP_VERSION.lower().lstrip('v'))
        self.status=tk.StringVar(value='ตรวจสอบรุ่นใหม่จาก GitHub สาธารณะ — ไม่เปิดกล้อง / API และไม่ติดตั้งอัตโนมัติ')
        ttk.Label(self,textvariable=self.status,wraplength=800).pack(anchor='w',pady=8)
        self.check_button=ttk.Button(self,text='ตรวจสอบอัปเดต',command=self.check)
        self.check_button.pack(anchor='w',pady=4)
        ttk.Button(self,text='รายละเอียด / เปิดหน้า Releases',command=self.details).pack(anchor='w',pady=4)
        self.download_button=ttk.Button(self,text='ดาวน์โหลดอัปเดตที่ตรวจลายเซ็นแล้ว',command=self.download,state='disabled')
        self.download_button.pack(anchor='w',pady=4)
        self.install_button=ttk.Button(self,text='ยืนยันติดตั้งและเริ่มใหม่',command=self.install,state='disabled')
        self.install_button.pack(anchor='w',pady=4)
        ttk.Button(self,text='ไว้ภายหลัง',command=self.later).pack(anchor='w',pady=4)
        ttk.Label(self,text='ต้องตรวจลายเซ็นก่อนติดตั้ง — หากยังไม่ตั้ง public key จะปิดการติดตั้งอย่างปลอดภัย\nกรุณาบันทึกการตั้งค่า หยุดงานฟัง / Capture / API และรอรอบเติมจบก่อน\nไม่มีการติดตั้งหรือรีสตาร์ตจนกว่าคุณจะกดยืนยัน',wraplength=800).pack(anchor='w',pady=12)
        self.timer=self.after(200,self.poll)
        self.bind('<Destroy>',lambda event:self.close() if event.widget is self else None,add='+')
        # Source-mode tests and synthetic packaged smoke must not call GitHub.
        # The application explicitly schedules check() for normal startup.

    @property
    def is_running(self):return self.thread is not None and self.thread.is_alive()

    def start_periodic(self):
        if self.closed or self.periodic_enabled:return
        self.periodic_enabled=True
        self.periodic_timer=self.after(1500,self._periodic_check)

    def _periodic_check(self):
        if self.periodic_timer is not None:
            try:self.after_cancel(self.periodic_timer)
            except tk.TclError:pass
            self.periodic_timer=None
        if self.closed or not self.periodic_enabled:return
        if not self.is_running and not self.installing:self.check()
        self.periodic_timer=self.after(6*60*60*1000,self._periodic_check)

    def _start(self,operation):
        if self.closed or self.is_running or self.installing:return
        self.check_button.configure(state='disabled')
        self.download_button.configure(state='disabled')
        self.install_button.configure(state='disabled')
        results, stop, directory = self.results, self.stop, self.directory
        def run():
            try:result=operation();error=None
            except updates.UpdateError as exc:result=None;error=str(exc)
            except Exception:result=None;error='Update operation failed safely'
            if not stop.is_set():
                try:results.put_nowait((result,error))
                except queue.Full:pass
            elif directory:
                try:directory.cleanup()
                except OSError:pass
        self.thread=threading.Thread(target=run,name='public-release-check',daemon=True)
        self.thread.start()

    def check(self):
        if self.closed:return
        self.status.set('กำลังตรวจสอบรุ่นใหม่ (ไม่มีการดาวน์โหลดหรือติดตั้งอัตโนมัติ)…')
        client, current, stop = self.client, self.current, self.stop
        self._start(lambda:('release',client.check(current,stop)))

    def poll(self):
        if self.closed:return
        try:self.after_cancel(self.timer)
        except tk.TclError:pass
        try:
            result,error=self.results.get_nowait()
            self.check_button.configure(state='normal')
            if error:self.status.set('ตรวจสอบไม่ได้ / ปิดติดตั้งอย่างปลอดภัย: '+error)
            elif result[0]=='release':
                self.release=result[1]
                self.downloaded=None
                self.status.set('มีรุ่นใหม่ที่ตรวจลายเซ็นแล้ว: V'+str(self.release[0].version) if self.release else 'เป็นรุ่นล่าสุดแล้ว')
                if self.release:self.on_release(self.release[0].version)
            else:
                self.downloaded=result[1]
                self.status.set('ดาวน์โหลดและตรวจ SHA256 แล้ว — กดยืนยันติดตั้งเมื่อหยุดงานและไม่มีรอบเติมค้าง')
            self.download_button.configure(state='normal' if self.release else 'disabled')
            self.install_button.configure(state='normal' if self._install_ready() else 'disabled')
        except queue.Empty:pass
        self.timer=self.after(200,self.poll)

    def download(self):
        if not self.release or self.is_running:return
        if not messagebox.askyesno('ดาวน์โหลดอัปเดต','ดาวน์โหลดไฟล์ที่ตรวจลายเซ็นแล้ว? ยังไม่ปิดโปรแกรมและไม่ติดตั้ง',parent=self):return
        self.downloaded=None
        if self.directory:self.directory.cleanup()
        self.directory=tempfile.TemporaryDirectory(prefix='smart-tapo-update-')
        manifest,url=self.release
        destination=Path(self.directory.name)/manifest.filename
        stop = self.stop
        self._start(lambda:('download',updates.download(manifest,url,destination,stop)))

    def _install_ready(self):
        return (not self.closed and not self.installing and self.release is not None
            and self.downloaded is not None and self.downloaded.is_file()
            and updates.PINNED_PUBLIC_KEY is not None and getattr(sys,'frozen',False)
            and self.install_request is not None)

    def install(self):
        if not self._install_ready() or self.is_running:return
        try:state=self.state_getter()
        except Exception:state=None
        if not updates.install_allowed(state):
            self.status.set(_blocked_message(state))
            return
        if not messagebox.askyesno('ติดตั้งและเริ่มใหม่',
            'ยืนยันปิดโปรแกรมชั่วคราวเพื่อติดตั้งรุ่นใหม่?\nเก็บการตั้งค่า โปรไฟล์ ประวัติ และคิวเดิม\nกรุณาบันทึกสิ่งที่แก้ไขก่อน และเริ่มงานฟัง/ส่ง API เองหลังอัปเดต',parent=self):return
        try:
            if not updates.install_allowed(self.state_getter()):
                self.status.set('สถานะงานเปลี่ยนไป — ยกเลิกการติดตั้งโดยไม่หยุดงาน')
                return
            accepted=self.install_request(self.release[0],self.downloaded)
        except Exception:
            self.status.set('เริ่มตัวช่วยอัปเดตไม่ได้ — ยังไม่ยืนยันว่าปิดงานหรือเปลี่ยนไฟล์ กรุณาตรวจสถานะ')
            return
        if accepted is True:
            self.installing=True
            self.install_button.configure(state='disabled')
            self.status.set('กำลังเตรียมสลับรุ่นและรอโปรแกรมปิดอย่างปลอดภัย…')

    def details(self):
        if messagebox.askyesno('GitHub Releases','เปิดหน้า GitHub สาธารณะในเบราว์เซอร์? การติดตั้งด้วยตนเองต้องหยุดงานและสำรองไฟล์ EXE ก่อน',parent=self):
            webbrowser.open(updates.RELEASES_URL)

    def later(self):self.status.set('ไว้ภายหลัง — ไม่มีการปิดโปรแกรมหรือติดตั้งอัตโนมัติ')

    def close(self):
        self.closed=True;self.stop.set()
        self.periodic_enabled=False
        try:
            self.after_cancel(self.timer)
            if self.periodic_timer is not None:
                self.after_cancel(self.periodic_timer)
                self.periodic_timer=None
            if getattr(self,'startup_timer',None):self.after_cancel(self.startup_timer)
        except tk.TclError:pass
        # Never delete a directory being written by a background download.
        if self.directory and not self.is_running:self.directory.cleanup()
