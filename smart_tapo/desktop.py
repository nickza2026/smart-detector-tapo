"""Double-click desktop control panel. No console is required for normal use."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .calibrate import save_calibration
from .capture_diagnostics import describe_capture
from .config import Rect
from .desktop_calibration import is_ready, mark_ready
from .desktop_controller import DesktopController
from .desktop_storage import DesktopStore
from .desktop_history import HistoryPanel
from .frames import atomic_bytes
from .runtime import ProcessLock, hidden_console_options
from .vision import load_png


class DesktopApp:
    def __init__(self, root, store, controller=None):
        self.root, self.store = root, store
        self.controller = controller or DesktopController()
        self.config = store.load_or_create()
        self.jobs = queue.Queue(maxsize=20)
        self.busy = False
        self.operation_stop = threading.Event()
        self.closing = False
        self.reference_image = None
        self.photo = None
        self.drag_start = None
        self.selection = self.config.roi
        self.form = {}
        self.root.title('Smart Tapo — ควบคุมกล้องและตรวจวัตถุค้าง')
        self.root.geometry('1000x' + str(min(820, max(650, root.winfo_screenheight()-90))))
        self.root.minsize(850, 650)
        self.preview_max_height=max(220,min(330,root.winfo_screenheight()-420))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        if os.name == 'nt' and 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Heading.TLabel', font=('Segoe UI', 16, 'bold'))
        style.configure('Subheading.TLabel', font=('Segoe UI', 11, 'bold'))
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Smart Tapo', style='Heading.TLabel').pack(anchor='w')
        ttk.Label(outer, text='ตั้งค่า → ยืนยันภาพช่องว่าง → เริ่ม Capture / Worker').pack(anchor='w', pady=(3, 12))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill='both', expand=True)
        self.settings = ttk.Frame(self.notebook, padding=14)
        self.calibration = ttk.Frame(self.notebook, padding=14)
        self.control = ttk.Frame(self.notebook, padding=14)
        for frame, name in ((self.settings, '1. ตั้งค่า'), (self.calibration, '2. ภาพช่องว่าง'), (self.control, '3. เริ่ม / หยุด')):
            self.notebook.add(frame, text=name)
        self._build_settings()
        self._build_calibration()
        self._build_control()
        self.history_panel=HistoryPanel(self.notebook,lambda:self.config,self._remember_history_port)
        self.notebook.add(self.history_panel,text='ประวัติการหยอดแบงค์')
        self.notifications=ttk.Frame(self.notebook,padding=14)
        self.notebook.add(self.notifications,text='แจ้งเตือน Pushover')
        self._build_notifications()
        self.status = tk.StringVar(value='พร้อมตั้งค่า — ยังไม่เปิดกล้องและไม่ส่งแจ้งเตือน')
        ttk.Label(outer, textvariable=self.status, wraplength=940).pack(anchor='w', pady=(10, 3))
        ttk.Label(outer, text='บันทึกการตั้งค่าที่: ' + str(store.config_path), wraplength=940).pack(anchor='w')
        self._credential_error = False
        try:
            token, user = store.load_credentials()
            self.token.set(token)
            self.user.set(user)
        except Exception:
            self._credential_error = True
            self.status.set('อ่านคีย์ที่เข้ารหัสไม่ได้ กรุณากรอกคีย์ใหม่บนบัญชี Windows นี้; ไฟล์เดิมยังไม่ถูกลบ')
        if self.config.baseline_path.is_file():
            try:
                self.load_image(load_png(self.config.baseline_path, self.config.expected_size))
            except (OSError, ValueError):
                self.status.set('ภาพอ้างอิงเดิมอ่านไม่ได้ กรุณาตั้งภาพช่องว่างใหม่')
        self._refresh_calibration()
        if self._credential_error:
            self.status.set('อ่านคีย์ที่เข้ารหัสไม่ได้ กรุณากรอกคีย์ใหม่บนบัญชี Windows นี้; ไฟล์เดิมยังไม่ถูกลบ')
        self.timer = self.root.after(200, self._poll)

    def _entry(self, parent, row, key, label, value, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=4)
        variable = tk.StringVar(value=str(value))
        self.form[key] = variable
        if key == 'adb_serial':
            widget = ttk.Combobox(parent, textvariable=variable)
            self.device_picker = widget
        else:
            widget = ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky='ew', padx=8, pady=4)
        if browse:
            ttk.Button(parent, text='เลือกไฟล์…', command=lambda: self._browse(variable)).grid(row=row, column=2)
        return widget

    def _build_settings(self):
        self.settings.columnconfigure(1, weight=1)
        entries = [('camera_id','รหัสกล้อง (ตั้งเอง เช่น cabinet-1)'),
                   ('camera_name','ชื่อกล้องตามที่แสดงใน Tapo'),
                   ('adb_path','ไฟล์ adb.exe'), ('memu_executable','ไฟล์ MEmu.exe (ไม่จำเป็นถ้าเปิดอยู่แล้ว)'),
                   ('adb_serial','อุปกรณ์ MEmu / ADB Serial'),
                   ('stuck_consecutive_frames','จำนวนภาพต่อเนื่องก่อนเตือน'),
                   ('deposit_history_hours','เก็บประวัติหยอดปกติ (ชั่วโมง)'),
                   ('capture_interval_seconds','ช่วงรับภาพเป้าหมาย (วินาที เช่น 1 หรือ 0.2)')]
        for row, (key, label) in enumerate(entries):
            self._entry(self.settings,row,key,label,getattr(self.config,key),
                        browse=key in ('adb_path','memu_executable'))
        ttk.Button(self.settings, text='ค้นหาอุปกรณ์', command=self.find_devices).grid(row=4,column=2)
        ttk.Label(self.settings,text='ช่วงรับภาพเป็นเป้าหมาย ไม่รับประกัน FPS จริงของ ADB; '
                  'การรับภาพเร็วขึ้นทำให้ครบจำนวนภาพก่อนเตือนเร็วขึ้นด้วย เช่น 0.2 วินาที / 50 ภาพ ≈ 10 วินาที '
                  'และภาพนาฬิกาไม่เปลี่ยนอาจยังไม่ผ่านการยืนยันภาพใหม่ ไม่ใช่การแก้ detector ด้วยการลดช่วงเวลาอย่างเดียว',
                  wraplength=700).grid(row=8,column=0,columnspan=3,sticky='w',pady=8)
        actions=ttk.Frame(self.settings)
        actions.grid(row=9,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Button(actions,text='บันทึกการตั้งค่า',command=self.save_settings).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='เปิดโฟลเดอร์ข้อมูล',command=self.open_folder).pack(side='left')

    def _build_notifications(self):
        self.notification_actions=ttk.Frame(self.notifications)
        self.notification_actions.pack(side='bottom',fill='x',pady=(8,0))
        wrap=ttk.Frame(self.notifications)
        wrap.pack(fill='both',expand=True)
        canvas=tk.Canvas(wrap,highlightthickness=0)
        canvas.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(wrap,orient='vertical',command=canvas.yview)
        scroll.pack(side='right',fill='y')
        canvas.configure(yscrollcommand=scroll.set)
        panel=ttk.Frame(canvas)
        window=canvas.create_window(0,0,window=panel,anchor='nw')
        panel.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=max(1,event.width)))
        panel.columnconfigure(1,weight=1)
        ttk.Label(panel,text='Pushover — High / Emergency',style='Subheading.TLabel').grid(row=0,column=0,columnspan=3,sticky='w')
        self.token, self.user = tk.StringVar(), tk.StringVar()
        for row,label,var in ((1,'App Token',self.token),(2,'User / Group Key',self.user)):
            ttk.Label(panel,text=label).grid(row=row,column=0,sticky='w',pady=4)
            ttk.Entry(panel,textvariable=var,show='*').grid(row=row,column=1,sticky='ew',padx=8,pady=4)
        self.notify = tk.BooleanVar(value=self.config.notify_enabled)
        ttk.Checkbutton(panel,text='เปิดส่งแจ้งเตือนจริง (ปิด = ทดสอบ ไม่เรียก API)',variable=self.notify).grid(row=3,column=0,columnspan=3,sticky='w',pady=6)
        ttk.Label(panel,text='โหมด').grid(row=4,column=0,sticky='w')
        self.priority=tk.StringVar(value='2 — Emergency' if self.config.notification_priority==2 else '1 — High')
        self.priority_widget=ttk.Combobox(panel,textvariable=self.priority,values=('1 — High','2 — Emergency'),state='readonly')
        self.priority_widget.grid(row=4,column=1,sticky='ew',padx=8)
        self.emergency_form={}
        for row,key,label in ((5,'emergency_retry_seconds','เตือนซ้ำโดย Pushover (วินาที ≥ 30)'),
                              (6,'emergency_expire_seconds','หมดอายุที่ขอ (วินาที ≤ 10800)'),
                              (7,'emergency_poll_seconds','ตรวจรับทราบ (วินาที ≥ 5)')):
            ttk.Label(panel,text=label).grid(row=row,column=0,sticky='w',pady=4)
            var=tk.StringVar(value=str(getattr(self.config,key)))
            self.emergency_form[key]=var
            ttk.Entry(panel,textvariable=var).grid(row=row,column=1,sticky='ew',padx=8,pady=4)
        ttk.Label(panel,text='Emergency: รับทราบแล้วเงียบจนเหตุการณ์จบ; ช่องว่างที่ยืนยันแล้วจะขอยกเลิกบน Pushover '
                  'ไม่ส่ง High reminder ซ้อน ถ้าหมดอายุไม่ถือว่ารับทราบและไม่เปิดรอบใหม่เองในเหตุเดิม',wraplength=700).grid(row=8,column=0,columnspan=3,sticky='w',pady=6)
        ttk.Label(panel,text='Pushover จำกัด 50 retries แม้ขอหมดอายุ 3 ชั่วโมง; ตั้ง retry 60 วินาทีจึงอาจหยุดราว 50 นาที '
                  'หยุด Worker/ปิดโปรแกรมไม่หยุดการเตือนที่ cloud ส่งไปแล้ว ต้องรับทราบใน Pushover หรือรอหมดอายุ',wraplength=700).grid(row=9,column=0,columnspan=3,sticky='w',pady=6)
        ttk.Label(panel,text='คีย์ใช้ Windows DPAPI ไม่ลง config/log; config เก่ายังเป็น High จนเลือก Emergency และบันทึก',wraplength=700).grid(row=10,column=0,columnspan=3,sticky='w')
        actions=self.notification_actions
        ttk.Button(actions,text='บันทึกการตั้งค่า',command=self.save_settings).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='ล้างคีย์ที่บันทึกไว้',command=self.clear_keys).pack(side='left',padx=(0,8))

    def _build_calibration(self):
        ttk.Label(self.calibration,text='ตั้งภาพอ้างอิงครั้งแรก — ไม่ต้องพิมพ์คำสั่งหรือหาไฟล์ empty เอง',
                  style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self.calibration,text='หยุด Capture/Worker และตรวจว่าช่องรับไม่มีแบงค์ค้างก่อนถ่ายภาพ',
                  wraplength=850).pack(anchor='w',pady=(4,8))
        actions=ttk.Frame(self.calibration)
        actions.pack(anchor='w')
        self.capture_ref_button=ttk.Button(actions,text='ถ่ายภาพจาก MEmu',command=self.capture_reference)
        self.capture_ref_button.pack(side='left',padx=(0,8))
        self.load_ref_button=ttk.Button(actions,text='เลือกภาพ PNG…',command=self.choose_image)
        self.load_ref_button.pack(side='left')
        ttk.Label(self.calibration,text='ลากกรอบสีแดงให้ครอบจุดที่ต้องตรวจแบงค์ค้าง (เฉพาะภาพกล้อง ไม่ใช่ UI)').pack(anchor='w',pady=8)
        self.canvas=tk.Canvas(self.calibration,width=720,height=self.preview_max_height,background='#202830',highlightthickness=0)
        self.canvas.pack(anchor='center')
        self.canvas.bind('<ButtonPress-1>',self._drag_begin)
        self.canvas.bind('<B1-Motion>',self._drag_move)
        self.canvas.bind('<ButtonRelease-1>',self._drag_end)
        self.confirm_empty=tk.BooleanVar(value=False)
        self.confirm_box=ttk.Checkbutton(self.calibration,text='ฉันตรวจแล้วว่าภาพนี้ไม่มีแบงค์ค้าง และกรอบอยู่ตรงจุดตรวจ',variable=self.confirm_empty)
        self.confirm_box.pack(anchor='w',pady=8)
        self.save_ref_button=ttk.Button(self.calibration,text='ยืนยันและบันทึกภาพช่องว่าง',command=self.save_reference)
        self.save_ref_button.pack(anchor='w')
        self.calibration_status=tk.StringVar()
        ttk.Label(self.calibration,textvariable=self.calibration_status,wraplength=850).pack(anchor='w',pady=6)

    def _build_control(self):
        ttk.Label(self.control,text='Capture และ Worker ทำงานแยกกัน ปิดหน้าต่างจะขอหยุดทั้งสองตัว',
                  style='Subheading.TLabel').pack(anchor='w',pady=(0,12))
        self.role_labels,self.start_buttons,self.stop_buttons={},{},{}
        for role,title in (('capture','Capture — รับภาพจาก MEmu'),('worker','Worker — ตรวจวัตถุค้าง')):
            frame=ttk.LabelFrame(self.control,text=title,padding=12)
            frame.pack(fill='x',pady=6)
            var=tk.StringVar(value='หยุดอยู่')
            self.role_labels[role]=var
            ttk.Label(frame,textvariable=var).pack(side='left',expand=True,fill='x')
            self.start_buttons[role]=ttk.Button(frame,text='เริ่ม '+role.title(),command=lambda r=role:self.start_role(r))
            self.start_buttons[role].pack(side='left',padx=8)
            self.stop_buttons[role]=ttk.Button(frame,text='หยุด',command=lambda r=role:self.controller.stop(r))
            self.stop_buttons[role].pack(side='left')
        ttk.Button(self.control,text='หยุดทั้งหมด',command=self.controller.stop_all).pack(anchor='w',pady=10)
        self.capture_metrics=tk.StringVar(value='ยังไม่มีข้อมูลจังหวะรับภาพ')
        self.metrics_due=0
        ttk.Label(self.control,textvariable=self.capture_metrics,wraplength=700).pack(anchor='w',pady=4)
        ttk.Label(self.control,text='สถานะ / ข้อผิดพลาด').pack(anchor='w')
        self.log=tk.Text(self.control,height=16,wrap='word',state='disabled',font=('Consolas',10))
        self.log.pack(fill='both',expand=True,pady=5)

    def _active(self):
        return any(self.controller.is_running(role) for role in ('capture','worker'))

    def _error(self, text):
        self.status.set(text)
        messagebox.showerror('Smart Tapo',text,parent=self.root)

    def _browse(self, variable):
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('Windows application','*.exe'),('All files','*')])
        if path: variable.set(path)

    def _candidate(self):
        updates={key:var.get().strip() for key,var in self.form.items()}
        try:updates['stuck_consecutive_frames']=int(updates['stuck_consecutive_frames'])
        except ValueError:raise ValueError('จำนวนภาพก่อนเตือนต้องเป็นจำนวนเต็มบวก') from None
        try:updates['deposit_history_hours']=float(updates['deposit_history_hours'])
        except ValueError:raise ValueError('ระยะเก็บประวัติต้องเป็นจำนวนชั่วโมงมากกว่า 0') from None
        try:updates['capture_interval_seconds']=float(updates['capture_interval_seconds'])
        except ValueError:raise ValueError('ช่วงรับภาพต้องเป็นจำนวนวินาทีมากกว่า 0') from None
        priorities={'1 — High':1,'2 — Emergency':2}
        if self.priority.get() not in priorities:raise ValueError('เลือกโหมด High หรือ Emergency')
        updates['notification_priority']=priorities[self.priority.get()]
        for key,var in self.emergency_form.items():
            try:updates[key]=int(var.get().strip())
            except ValueError:raise ValueError('เวลา Emergency ต้องเป็นจำนวนเต็มวินาทีตามขอบเขตที่แสดง') from None
        updates['notify_enabled']=self.notify.get()
        return replace(self.config,**updates).validate()

    def _remember_history_port(self,port):
        # This display-only setting does not reconfigure a running detector.
        updated=replace(self.config,history_web_port=port).validate()
        self.store.save(updated)
        self.config=updated

    def save_settings(self):
        if self._active() or self.busy:
            self._error('หยุด Capture/Worker และรอรายการปัจจุบันให้เสร็จก่อนแก้การตั้งค่า')
            return False
        try:
            config=self._candidate()
            token,user=self.token.get().strip(),self.user.get().strip()
            if bool(token)!=bool(user) or (config.notify_enabled and not (token and user)):
                raise ValueError('กรอก App Token และ User Key ให้ครบก่อนเปิดส่งแจ้งเตือน')
            if token and user:
                self.store.save_credentials(token,user)
                self._credential_error=False
            self.store.save(config)
            self.config=config
        except Exception as error:
            # Never include arbitrary exception text from a credential provider.
            text=str(error) if isinstance(error,ValueError) else 'บันทึกไม่ได้ ตรวจสิทธิ์โฟลเดอร์และการเข้ารหัส Windows ของบัญชีนี้'
            self._error(text)
            return False
        self.status.set('บันทึกแล้ว — '+('เปิดแจ้งเตือนจริง' if config.notify_enabled else 'โหมดเก็บผล ไม่ส่งแจ้งเตือน'))
        self._refresh_calibration()
        return True

    def clear_keys(self):
        if self._active() or self.busy:
            self._error('หยุดการทำงานก่อนล้างคีย์')
            return
        if not messagebox.askyesno('ล้างคีย์','ล้างคีย์ที่บันทึกและปิดการแจ้งเตือนจริง?',parent=self.root): return
        try:
            self.notify.set(False)
            self.config=replace(self.config,notify_enabled=False)
            self.store.save(self.config)
            self.store.clear_credentials()
            self.token.set('');self.user.set('')
            self.status.set('ล้างคีย์แล้ว และปิดการแจ้งเตือนจริง')
        except Exception:
            self._error('ล้างคีย์ไม่สำเร็จ กรุณาตรวจสิทธิ์โฟลเดอร์ข้อมูล')

    def _async(self, work, done):
        if self.busy or self._active():
            self._error('หยุด Capture/Worker และรอรายการปัจจุบันก่อน')
            return
        self.busy=True
        self.operation_stop.clear()
        self.status.set('กำลังติดต่อ MEmu… หน้าต่างยังใช้งานได้')
        def target():
            try: self.jobs.put_nowait((done,work(),None))
            except Exception: self.jobs.put_nowait((done,None,'ติดต่อ MEmu ไม่สำเร็จ ตรวจ adb.exe, อุปกรณ์, การล็อกอิน Tapo และหน้า Live'))
        threading.Thread(target=target,daemon=True).start()

    def find_devices(self):
        if not self.save_settings(): return
        path=self.config.adb_path
        def work():
            result=subprocess.run([path,'devices'],capture_output=True,timeout=15,check=True,
                                  **hidden_console_options())
            devices=[]
            for line in result.stdout.decode('utf-8','replace').splitlines():
                parts=line.split()
                if len(parts)>=2 and parts[1]=='device' and re.fullmatch(r'[A-Za-z0-9_.:\[\]-]{1,80}',parts[0]): devices.append(parts[0])
            return devices
        def done(devices):
            self.device_picker.configure(values=devices)
            if devices:
                if len(devices)==1:self.form['adb_serial'].set(devices[0])
                self.status.set('พบอุปกรณ์ เลือก Serial แล้วกดบันทึก')
            else:self.status.set('ไม่พบอุปกรณ์ online — เปิด MEmu และเปิด ADB ใน MEmu ก่อน')
        self._async(work,done)

    def capture_reference(self):
        if not self.save_settings(): return
        config=self.config
        def work():
            from .capture import Adb
            from io import BytesIO
            adb=Adb(config)
            adb.connect(self.operation_stop)
            if self.operation_stop.is_set():raise RuntimeError('Cancelled')
            adb.ensure_live()
            if self.operation_stop.is_set():raise RuntimeError('Cancelled')
            return load_png(BytesIO(adb.run('exec-out','screencap','-p')),config.expected_size)
        self._async(work,self.load_image)

    def choose_image(self):
        if self._active() or self.busy:
            self._error('หยุดการทำงานก่อนตั้งภาพช่องว่าง')
            return
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('PNG screenshot','*.png')])
        if not path:return
        try:self.load_image(load_png(path,self.config.expected_size))
        except (OSError,ValueError):self._error('ต้องเป็นภาพ PNG จาก MEmu ขนาด '+str(self.config.expected_size)+' ไม่ใช่ภาพเต็มจอ Windows')

    def load_image(self, image):
        self.reference_image=image.copy()
        self.confirm_empty.set(False)
        video=image.crop(self.config.video.box())
        self.preview_scale=min(720/video.width,self.preview_max_height/video.height)
        size=(round(video.width*self.preview_scale),round(video.height*self.preview_scale))
        self.photo=ImageTk.PhotoImage(video.resize(size,Image.Resampling.LANCZOS),master=self.root)
        self.canvas.configure(width=size[0],height=size[1])
        self.canvas.delete('all')
        self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self._draw_selection()
        self.status.set('ตรวจภาพช่องว่างและตำแหน่งกรอบก่อนกดยืนยัน')

    def _point(self,event):
        scale=self.preview_scale
        return (max(0,min(self.config.video.width,event.x/scale))+self.config.video.x,
                max(0,min(self.config.video.height,event.y/scale))+self.config.video.y)

    def _drag_begin(self,event):
        if self.reference_image is not None and not self._active() and not self.busy:
            self.drag_start=self._point(event)

    def _drag_move(self,event):
        if self.drag_start is None:return
        x,y=self.drag_start;right,bottom=self._point(event)
        left,top=round(min(x,right)),round(min(y,bottom))
        width,height=round(abs(right-x)),round(abs(bottom-y))
        if width>=12 and height>=12:
            self.selection=Rect(left,top,width,height)
            self.confirm_empty.set(False)
            self._draw_selection()

    def _drag_end(self,event):
        self._drag_move(event)
        self.drag_start=None

    def _draw_selection(self):
        self.canvas.delete('roi')
        x,y,w,h=self.selection.x,self.selection.y,self.selection.width,self.selection.height
        vx,vy,s=self.config.video.x,self.config.video.y,self.preview_scale
        self.canvas.create_rectangle((x-vx)*s,(y-vy)*s,(x+w-vx)*s,(y+h-vy)*s,outline='#ff5252',width=3,tags='roi')

    def save_reference(self):
        if self._active() or self.busy:
            self._error('หยุด Capture/Worker ก่อนเปลี่ยนภาพอ้างอิง')
            return False
        if self.reference_image is None or not self.confirm_empty.get():
            self._error('ต้องมีภาพ และติ๊กยืนยันว่าช่องรับว่างกับตำแหน่งกรอบถูกต้องก่อน')
            return False
        if self.config.baseline_path.exists() and not messagebox.askyesno('เปลี่ยนภาพอ้างอิง','แทนที่ภาพอ้างอิงเดิมด้วยภาพนี้?',parent=self.root):return False
        if not self.save_settings():return False
        try:
            updated=replace(self.config,roi=self.selection).validate()
            if updated.alignment_enabled:
                from .alignment import Aligner
                if not Aligner(updated,self.reference_image).align(self.reference_image).ok:
                    self._error('ภาพนี้ยังไม่มีจุดอ้างอิงรอบกรอบเพียงพอ ตรวจความชัด/แสง และเลือกกรอบเฉพาะช่องรับ ไม่ครอบภาพทั้งหมด')
                    return False
            # Persist geometry first; only a matching final manifest permits
            # a worker start after any interrupted/partially failed save.
            self.store.save(updated)
            self.config=updated
            with tempfile.TemporaryDirectory(dir=self.store.root) as temporary:
                source=Path(temporary)/'source.png'
                self.reference_image.save(source)
                save_calibration(updated,source,confirm_empty=True,replace=True)
            mark_ready(updated,self.store.root)
        except Exception:
            self._error('บันทึกภาพอ้างอิงไม่สำเร็จ ตรวจสิทธิ์และพื้นที่ว่าง แล้วตั้งภาพอีกครั้งก่อนเริ่ม Worker')
            self._refresh_calibration()
            return False
        self.status.set('ภาพอ้างอิงพร้อม — ไปแท็บเริ่ม / หยุดได้ ไม่ต้องรันคำสั่งเพิ่มเติม')
        self._refresh_calibration()
        return True

    def _refresh_calibration(self):
        ready=is_ready(self.config,self.store.root)
        self.calibration_status.set('ภาพอ้างอิงพร้อมใช้งาน' if ready else 'ยังไม่พร้อม: ต้องยืนยันภาพช่องว่างสำหรับกล้องและกรอบปัจจุบัน')

    def start_role(self,role):
        if self.busy:return
        # Saving settings is blocked while another role runs. Config changes
        # cannot be applied mid-run, but starting the second role is allowed.
        if not self._active() and not self.save_settings():return
        try:
            if self._candidate()!=self.config:
                raise ValueError('หยุดทั้งหมดและบันทึกการตั้งค่าที่เปลี่ยนก่อน')
            if role=='worker':
                if not is_ready(self.config,self.store.root):
                    self.notebook.select(self.calibration)
                    raise ValueError('ตั้งและยืนยันภาพช่องว่างในแท็บ 2 ก่อนเริ่ม Worker')
                self.controller.start_worker(self.config,self.token.get().strip(),self.user.get().strip())
            else:
                if not self.config.adb_serial:
                    raise ValueError('เลือกอุปกรณ์ MEmu / ADB Serial ในแท็บตั้งค่าก่อน')
                self.controller.start_capture(self.config)
            self.notebook.select(self.control)
        except Exception as error:
            self._error(str(error) if isinstance(error,ValueError) else 'เริ่มงานไม่ได้ ดูสถานะและตรวจการตั้งค่า')

    def open_folder(self):
        if os.name=='nt':os.startfile(self.store.root)
        elif os.sys.platform=='darwin':subprocess.Popen(['open',str(self.store.root)])
        else:self.status.set(str(self.store.root))

    def _append_log(self,text):
        for secret in (self.token.get(),self.user.get()):
            if secret:text=text.replace(secret,'[redacted]')
        self.log.configure(state='normal')
        self.log.insert('end',text[:2000]+'\n')
        if int(self.log.index('end-1c').split('.')[0])>500:self.log.delete('1.0','101.0')
        self.log.see('end');self.log.configure(state='disabled')

    def _poll(self):
        try:
            while True:
                done,value,error=self.jobs.get_nowait()
                self.busy=False
                if self.closing:continue
                if error:self._error(error)
                else:done(value)
        except queue.Empty:pass
        states=self.controller.snapshot()
        now=time.monotonic()
        if now>=self.metrics_due:
            self.capture_metrics.set(describe_capture(self.config))
            self.metrics_due=now+1
        active=any(self.controller.is_running(role) for role in states)
        labels={'stopped':'หยุดอยู่','starting':'กำลังเริ่ม','running':'กำลังทำงาน','stopping':'กำลังหยุดอย่างปลอดภัย','failed':'หยุดเพราะข้อผิดพลาด — ดูข้อความด้านล่าง'}
        for role,value in states.items():
            self.role_labels[role].set(labels.get(value['state'],value['state']))
            running=self.controller.is_running(role)
            self.start_buttons[role].configure(state='disabled' if running or self.busy or self.closing else 'normal')
            self.stop_buttons[role].configure(state='normal' if running and value['state']!='stopping' else 'disabled')
        for text in self.controller.drain_messages():self._append_log(text)
        self.history_panel.poll(visible=self.notebook.select()==str(self.history_panel))
        def lock_settings(parent):
            for widget in parent.winfo_children():
                if isinstance(widget,(ttk.Entry,ttk.Combobox,ttk.Checkbutton,ttk.Button)):
                    widget.configure(state='disabled' if active or self.busy or self.closing else 'normal')
                lock_settings(widget)
        lock_settings(self.settings)
        lock_settings(self.notifications)
        if not (active or self.busy or self.closing):self.priority_widget.configure(state='readonly')
        for widget in (self.capture_ref_button,self.load_ref_button,self.save_ref_button,self.confirm_box):
            widget.configure(state='disabled' if active or self.busy or self.closing else 'normal')
        if self.closing:
            if not active and not self.busy:
                self.root.destroy()
                return
            self.status.set('กำลังหยุดและปิดข้อมูลอย่างปลอดภัย รอ ADB/งานที่กำลังทำอยู่สักครู่…')
        self.timer=self.root.after(200,self._poll)

    def close(self):
        if self.closing:return
        if (self._active() or self.busy) and not messagebox.askyesno('ปิด Smart Tapo','หยุด Capture/Worker แล้วปิดโปรแกรม?',parent=self.root):return
        self.closing=True
        self.history_panel.close()
        self.operation_stop.set()
        self.controller.stop_all()


def _smoke_gui_flow(root, app):
    """Synthetic first run only. Never open live devices or send notifications."""
    from PIL import ImageDraw
    from .frames import publish_frame
    from tests.test_alignment_regression import textured_reference
    import time
    store=app.store
    result={'gui_created':True,'auto_config_created':store.config_path.is_file()}
    # Nonexistent adapter path is intentional: even the capture child cannot
    # contact real ADB, start MEmu or use an operator's existing configuration.
    app.form['adb_path'].set(str(store.root/'nonexistent-smoke-adb.exe'))
    app.form['adb_serial'].set('synthetic-device')
    app.form['memu_executable'].set('')
    app.priority.set('2 — Emergency')
    app.form['stuck_consecutive_frames'].set('1')
    app.config=replace(app.config,startup_timeout_seconds=.3,max_live_age_seconds=120)
    if not app.save_settings():raise RuntimeError('GUI settings did not save')
    image=textured_reference(app.config)
    app.load_image(image)
    app.confirm_empty.set(True)
    if not app.save_reference():raise RuntimeError('GUI calibration did not complete')
    app.start_role('worker')
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        root.update()
        state=app.controller.snapshot()['worker']['state']
        if state=='running':break
        if state=='failed':raise RuntimeError('GUI worker startup failed')
        time.sleep(.05)
    else:raise RuntimeError('GUI worker startup timed out')
    occupied=image.copy()
    roi=app.config.roi
    ImageDraw.Draw(occupied).rectangle((roi.x+10,roi.y+15,roi.x+roi.width-10,roi.y+roi.height-15),fill='white')
    from io import BytesIO
    encoded=BytesIO();occupied.save(encoded,format='PNG')
    publish_frame(app.config,encoded.getvalue(),'gui-smoke',1,True)
    import sqlite3
    from contextlib import closing
    found=False
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        root.update()
        try:
            with closing(sqlite3.connect(app.config.data_dir/'worker.sqlite3')) as db:
                found=db.execute("SELECT count(*) FROM emergency_notifications WHERE state='shadow'").fetchone()[0]==1
        except sqlite3.Error:pass
        if found:break
        time.sleep(.05)
    # Start the second role without stopping/reconfiguring the running worker.
    app.start_role('capture')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        root.update()
        state=app.controller.snapshot()['capture']['state']
        if state=='running':break
        if state=='failed':raise RuntimeError('GUI capture startup failed')
        time.sleep(.05)
    else:raise RuntimeError('GUI capture startup timed out')
    result['capture_process_started']=True
    app.controller.stop_all()
    deadline=time.monotonic()+20
    while time.monotonic()<deadline and app._active():root.update();time.sleep(.05)
    result.update(worker_stopped=not app._active(),shadow_notification=found,emergency_shadow=found,
                  calibration_ready=is_ready(app.config,store.root),real_notifications=0)
    if not all(result[key] for key in ('worker_stopped','shadow_notification','calibration_ready')):
        raise RuntimeError('GUI worker flow did not complete safely')
    from .history import get_images,list_events
    from .history_web import HistoryWebServer
    from http.cookiejar import CookieJar
    from urllib.request import HTTPCookieProcessor,build_opener
    from urllib.parse import urlencode
    page=list_events(app.config)
    if not page.events:raise RuntimeError('History did not contain the synthetic event')
    app.history_panel._render(page)
    event_id=page.events[0].id
    result['history_menu_created']=app.notebook.tab(app.history_panel,'text')=='ประวัติการหยอดแบงค์'
    result['history_event_visible']=event_id in app.history_panel.tree.get_children()
    result['history_full_image']=any(image.role=='first_full' for image in get_images(app.config,event_id))
    app.history_panel.server=HistoryWebServer(lambda:app.config)
    web=app.history_panel.server
    web.start(0)  # Ephemeral loopback only. Never changes a user's saved port.
    try:
        client=build_opener(HTTPCookieProcessor(CookieJar()))
        with client.open(web.url,timeout=5) as response:
            body=response.read(1024*1024).decode('utf-8')
        image_url='http://127.0.0.1:'+str(web.port)+'/image?'+urlencode(dict(id=event_id,role='first_full'))
        with client.open(image_url,timeout=5) as response:
            image_bytes=response.read(16*1024*1024+1)
        result['history_web_served']='ประวัติการหยอดแบงค์' in body and image_bytes.startswith(b'\x89PNG\r\n\x1a\n')
        result['thai_time']='+07:00' in body and 'UTC+7' in body
    finally:
        web.stop()
    if not all(result[key] for key in ('history_menu_created','history_event_visible','history_full_image','history_web_served','emergency_shadow','thai_time')):
        raise RuntimeError('GUI history/web smoke did not complete')
    if os.name=='nt':
        store.save_credentials('synthetic-smoke-token','synthetic-smoke-user')
        if store.load_credentials()!=('synthetic-smoke-token','synthetic-smoke-user'):
            raise RuntimeError('Windows credential round trip failed')
        result['windows_dpapi_roundtrip']=True
    return result


def smoke_gui(report_path):
    """Bounded synthetic smoke with cleanup even when a GUI assertion fails."""
    import time
    with tempfile.TemporaryDirectory(prefix='smart-tapo-gui-smoke-') as directory:
        root=tk.Tk()
        root.withdraw()
        app=None
        try:
            app=DesktopApp(root,DesktopStore(Path(directory)/'config.json'))
            result=_smoke_gui_flow(root,app)
            atomic_bytes(report_path,json.dumps(result).encode('utf-8'))
            return result
        finally:
            if app is not None:
                app.history_panel.close()
                app.controller.stop_all()
                deadline=time.monotonic()+20
                while time.monotonic()<deadline and app._active():
                    app.controller.snapshot()
                    time.sleep(.05)
                # Test-only fail-safe. Never terminate production jobs this way.
                for record in app.controller._roles.values():
                    process=record['process']
                    if process is not None:
                        process.terminate()
                        process.join(timeout=5)
                app.controller.snapshot()
                try:root.after_cancel(app.timer)
                except tk.TclError:pass
            root.destroy()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,help='Optional explicit portable config; not required for normal use')
    parser.add_argument('--smoke-report',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.smoke_report:
        try:smoke_gui(args.smoke_report);return 0
        except Exception as error:
            # This mode exclusively uses fresh synthetic config/keys, not user data.
            atomic_bytes(args.smoke_report,json.dumps({'passed':False,'error':type(error).__name__,
                                                       'detail':str(error)[:300]}).encode('utf-8'))
            return 1
    root=tk.Tk()
    root.withdraw()
    app=None
    try:
        store=DesktopStore(args.config)
        store.root.mkdir(parents=True,exist_ok=True)
        with ProcessLock(store.root/'desktop.lock'):
            app=DesktopApp(root,store)
            root.deiconify()
            root.mainloop()
        return 0
    except Exception:
        messagebox.showerror('Smart Tapo','เปิดโปรแกรมไม่ได้: ตรวจว่าไม่มีโปรแกรมอีกหน้าต่างทำงานอยู่ และโฟลเดอร์ config อ่าน/เขียนได้ ไฟล์เดิมยังไม่ถูกลบ',parent=root)
        try:root.destroy()
        except tk.TclError:pass
        return 1
    finally:
        if app is not None:
            # Also cover OS/window destruction or an unexpected GUI exception,
            # not only the ordinary close-button path.
            app.controller.stop_all()
            app.history_panel.close()


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
