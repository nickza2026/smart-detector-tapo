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
from .config import Rect
from .desktop_calibration import is_ready, mark_ready
from .desktop_controller import DesktopController, _dated
from .desktop_storage import DesktopStore,DesktopStorageError
from .desktop_history import HistoryPanel
from .desktop_rtsp_audio import RtspAudioPanel
from .desktop_startup import StartupPanel
from .desktop_notify_test import NotifyTestPanel
from .desktop_refueling import RefuelingHistoryPanel
from .desktop_webhook import WebhookPanel
from .diagnostics import APP_VERSION, DIAGNOSTIC_BUILD
from .frames import atomic_bytes
from .runtime import ProcessLock, hidden_console_options
from .vision import load_png


class DesktopApp:
    def __init__(self, root, store, controller=None):
        self.root, self.store = root, store
        self.controller = controller or DesktopController()
        self.config = store.load_or_create()
        from .desktop_rtsp import image_config
        self.config,self._source_migrated=image_config(self.config)
        self.jobs = queue.Queue(maxsize=20)
        self.busy = False
        self.operation_stop = threading.Event()
        self.closing = False
        self._run_all_starting = False
        self._run_all_generation = 0
        self._run_all_role_configs = {}
        self.tracking_popup = None
        self.reference_image = None
        self.reference_dirty = False
        self.reference_source = None
        self.photo = None
        self.drag_start = None
        self.selection = self.config.roi
        self.form = {}
        self.root.title('Smart Tapo ' + APP_VERSION + ' (' + DIAGNOSTIC_BUILD + ') — ควบคุมกล้องและตรวจวัตถุค้าง')
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
        ttk.Label(outer, text='RTSP ภาพ → ยืนยันภาพช่องว่าง → Capture / Worker • เสียง RTSP แยก process').pack(anchor='w', pady=(3, 12))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill='both', expand=True)
        self.update_notice=tk.StringVar(value='')
        self.update_banner=ttk.Frame(outer)
        ttk.Label(self.update_banner,textvariable=self.update_notice,wraplength=800).pack(side='left',padx=5,pady=5)
        ttk.Button(self.update_banner,text='ดูรุ่นใหม่ / อัปเดต',command=lambda:self.notebook.select(self.updates_panel)).pack(side='right',padx=5)
        self.settings = ttk.Frame(self.notebook, padding=14)
        self.calibration = ttk.Frame(self.notebook, padding=14)
        self.control = ttk.Frame(self.notebook, padding=14)
        for frame, name in ((self.settings, '1. ตั้งค่า'), (self.calibration, '2. ภาพช่องว่าง'), (self.control, '3. เริ่ม / หยุด')):
            self.notebook.add(frame, text=name)
        self._build_settings()
        self.root.bind('<Destroy>',self._root_destroyed,add='+')
        self._build_calibration()
        self._build_control()
        self.history_panel=HistoryPanel(self.notebook,lambda:self.config,self._remember_history_port)
        self.notebook.add(self.history_panel,text='ประวัติการหยอดแบงค์')
        self.refueling_panel=RefuelingHistoryPanel(self.notebook,lambda:self.config)
        self.notebook.add(self.refueling_panel,text='ประวัติการเติม')
        self.webhook_panel=WebhookPanel(self.notebook,lambda:self.config,self._save_webhook_config,self.store)
        self.notebook.add(self.webhook_panel,text='Refueling API v1')
        self.audio_panel=RtspAudioPanel(self.notebook,lambda:self.config,
            settings_current=lambda:self._candidate()==self.config and not self.busy)
        from .desktop_offline_audio import OfflineAudioPanel
        self.offline_audio_panel=OfflineAudioPanel(self.audio_panel.modes,lambda:self.config,self.audio_panel.listener_panel)
        self.audio_panel.modes.add(self.offline_audio_panel,text='ไฟล์ WAV (ทางเลือก)')
        self.notebook.add(self.audio_panel,text='เสียง RTSP / ฟังเติม')
        self.startup_panel=StartupPanel(self.notebook,self)
        self.notebook.add(self.startup_panel,text='Windows startup')
        from .desktop_updates import UpdatesPanel
        from .update_business import business_state
        self.updates_panel=UpdatesPanel(self.notebook,state_getter=lambda:business_state(self),
            install_request=self._request_update,on_release=self._notice_release)
        self.notebook.add(self.updates_panel,text='อัปเดตโปรแกรม')
        self.status = tk.StringVar(value='พร้อมตั้งค่า — ยังไม่เปิดกล้องและไม่ส่งแจ้งเตือน')
        ttk.Label(outer, textvariable=self.status, wraplength=940).pack(anchor='w', pady=(10, 3))
        ttk.Label(outer, text='บันทึกการตั้งค่าที่: ' + str(store.config_path), wraplength=940).pack(anchor='w')
        self._credential_error = False
        self._health_credentials = ('', self.config.notify_api_url)
        try:
            key = store.load_notify_key(self.config.notify_api_url, self.config.camera_id)
            self.notify_key.set(key)
            self._health_credentials = (key.strip(), self.config.notify_api_url)
        except Exception:
            self._credential_error = True
            self.status.set('อ่านคีย์ที่เข้ารหัสไม่ได้ กรุณากรอกคีย์ใหม่บนบัญชี Windows นี้; ไฟล์เดิมยังไม่ถูกลบ')
        if not self._source_migrated and self.config.baseline_path.is_file():
            try:
                self.load_image(load_png(self.config.baseline_path, self.config.expected_size), saved=True)
            except (OSError, ValueError):
                self.status.set('ภาพอ้างอิงเดิมอ่านไม่ได้ กรุณาตั้งภาพช่องว่างใหม่')
        self._refresh_calibration()
        if self._credential_error:
            self.status.set('อ่านคีย์ที่เข้ารหัสไม่ได้ กรุณากรอกคีย์ใหม่บนบัญชี Windows นี้; ไฟล์เดิมยังไม่ถูกลบ')
        from .desktop_health_alerts import HealthAlertSupervisor
        self.health_alerts = HealthAlertSupervisor()
        self.audio_panel.listener_panel.on_health_stop = self._poll_health
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
        self.settings.rowconfigure(0,weight=1)
        self.settings.columnconfigure(0,weight=1)
        canvas=self.settings_canvas=tk.Canvas(self.settings,highlightthickness=0,height=1)
        canvas.grid(row=0,column=0,sticky='nsew')
        scrollbar=ttk.Scrollbar(self.settings,orient='vertical',command=canvas.yview)
        scrollbar.grid(row=0,column=1,sticky='ns')
        canvas.configure(yscrollcommand=scrollbar.set)
        body=self.settings_body=ttk.Frame(canvas)
        window=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
        # Windows Tk can leave a Canvas window item unmapped on the second
        # notebook return even though the Canvas itself is visible. Reattach
        # the same body (not a rebuilt form) so values/scroll state survive.
        canvas.bind('<Map>',lambda event:canvas.itemconfigure(window,window=body),add='+')
        body.columnconfigure(1, weight=1)
        entries = [('camera_id','รหัสกล้อง (ตั้งเอง เช่น cabinet-1)'),
                   ('rtsp_url','RTSP URL สำหรับภาพและเสียง (relay ที่ไม่มีรหัสผ่าน/token)'),
                   ('stuck_consecutive_frames','จำนวนภาพต่อเนื่องก่อนเตือน'),
                   ('deposit_history_hours','เก็บประวัติหยอดปกติ (ชั่วโมง)')]
        for row, (key, label) in enumerate(entries):
            self._entry(body,row,key,label,getattr(self.config,key),
                        browse=key in ('adb_path','memu_executable'))
        ttk.Label(body,text='ภาพและเสียงจาก RTSP URL เดียวกัน แยก process • ไม่ใช้ MEmu / ADB / เสียง Windows\nยังไม่เปิดสตรีมจนกดเริ่มเอง • ความต่อเนื่อง transport ไม่ใช่หลักฐาน physical Live',wraplength=800).grid(row=4,column=0,columnspan=3,sticky='w')
        ttk.Separator(body).grid(row=7,column=0,columnspan=3,sticky='ew',pady=14)
        ttk.Label(body,text='Notify API',style='Subheading.TLabel').grid(row=8,column=0,sticky='w')
        self.notify_key=tk.StringVar()
        self.notify_base=tk.StringVar(value=self.config.notify_api_url)
        self.notify_importance=tk.StringVar(value=self.config.notify_importance)
        ttk.Label(body,text='API Base URL (HTTPS)').grid(row=9,column=0,sticky='w',pady=4)
        ttk.Entry(body,textvariable=self.notify_base).grid(row=9,column=1,sticky='ew',padx=8,pady=4)
        ttk.Label(body,text='Application API Key').grid(row=10,column=0,sticky='w',pady=4)
        ttk.Entry(body,textvariable=self.notify_key,show='*').grid(row=10,column=1,sticky='ew',padx=8,pady=4)
        notify_options=ttk.Frame(body)
        notify_options.grid(row=11,column=0,columnspan=3,sticky='w',pady=(4,8))
        importance=ttk.Frame(notify_options)
        importance.grid(row=0,column=0,sticky='w')
        ttk.Label(importance,text='วัตถุค้างแจ้งแบบ critical เสมอ • ระดับแจ้งระบบภาพ/เสียงขัดข้อง:').pack(side='left')
        for value,label in (('normal','normal — เงียบ ไม่ปลุก'),('critical','critical — ปลุกต่อเนื่องจนกดรับทราบในมือถือ')):
            ttk.Radiobutton(importance,text=label,value=value,variable=self.notify_importance).pack(side='left',padx=(8,0))
        self.notify = tk.BooleanVar(value=self.config.notify_enabled)
        ttk.Checkbutton(notify_options,text='เปิดแจ้งเตือน Notify API อัตโนมัติ: เหตุการณ์ Worker และระบบภาพ/เสียง (ไม่รวมปุ่มทดสอบส่งจริง)',
                        variable=self.notify).grid(row=1,column=0,sticky='w',pady=(8,0))
        health_settings = ttk.Frame(body)
        health_settings.grid(row=12,column=0,columnspan=3,sticky='ew')
        health_settings.columnconfigure(1,weight=1)
        ttk.Label(health_settings,text='คีย์บันทึกด้วย Windows DPAPI สำหรับบัญชีนี้ ผูกกับ Base URL และรหัสกล้อง ไม่อยู่ใน config หรือ log\nHTTP 202 คือผู้ให้บริการรับคำขอ ไม่ยืนยันว่ามือถือได้รับหรือมีคนรับทราบ • แจ้งกลับมาปกติส่งแบบ normal เสมอ',
                  wraplength=800).grid(row=0,column=0,columnspan=3,sticky='w')
        self.health_notify = tk.BooleanVar(value=self.config.health_notify_enabled)
        ttk.Checkbutton(health_settings,text='แจ้งระบบภาพ/เสียงขัดข้องและกลับมาปกติ (เมื่อเปิด Notify API และเริ่มเฝ้า)',
                        variable=self.health_notify).grid(row=1,column=0,columnspan=3,sticky='w',pady=4)
        self._entry(health_settings,2,'health_failure_seconds','ผิดปกติต่อเนื่องก่อนแจ้ง (วินาที)',self.config.health_failure_seconds)
        self._entry(health_settings,3,'health_repeat_seconds','แจ้งซ้ำขณะยังผิดปกติ (วินาที)',self.config.health_repeat_seconds)
        self.health_notification_status = tk.StringVar(value='สุขภาพระบบ: ยังไม่เริ่มเฝ้า')
        ttk.Label(health_settings,textvariable=self.health_notification_status,wraplength=800).grid(row=4,column=0,columnspan=3,sticky='w')
        actions = ttk.Frame(body)
        actions.grid(row=13,column=0,columnspan=3,sticky='w',pady=14)
        ttk.Button(actions,text='บันทึกการตั้งค่า',command=self.save_settings).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='ล้างคีย์ที่บันทึกไว้',command=self.clear_keys).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='เปิดโฟลเดอร์ข้อมูล',command=self.open_folder).pack(side='left')
        ttk.Button(actions,text='โฟลเดอร์ตั้งค่า',command=self.open_config_folder).pack(side='left')
        self.notify_test_panel=NotifyTestPanel(actions,lambda:(self.notify_key.get(),self.notify_base.get()))
        self.notify_test_panel.pack(side='left',padx=(10,0))

        self._entry(body,14,'capture_interval_seconds','ช่วงรับภาพ (วินาที)',self.config.capture_interval_seconds)
        ttk.Button(body,text='ใช้ 0.5 วินาที',
                   command=self._half_second_preset).grid(row=14,column=2)
        ttk.Label(body,text='ช่วงรับภาพเป็นรอบที่ขอ ไม่รับประกัน FPS • Sourcebank เป็นวัตถุตามภาพ ไม่ยืนยันรับเงิน\nรับ/ถอดภาพแยกจาก Worker และเสียง • stream ขาด/ค้าง/เกินอายุไม่ใช้ส่งแจ้งเตือนสด',
                  wraplength=800).grid(row=15,column=0,columnspan=3,sticky='w')
        self._entry(body,17,'data_dir','โฟลเดอร์ข้อมูล (เปลี่ยนแล้วต้องเปิดโปรแกรมใหม่)',self.config.data_dir)
        self.storage_picker=ttk.Button(body,text='เลือกโฟลเดอร์…',command=self.choose_data_folder)
        self.storage_picker.grid(row=17,column=2)
        self._entry(body,18,'max_image_storage_mb','โควตาภาพหลักฐาน (MB)',self.config.max_image_storage_mb)
        ttk.Label(body,text='โฟลเดอร์ใหม่ต้องว่าง • ไม่ย้าย/ลบข้อมูลเดิม ประวัติแต่ละโฟลเดอร์แยกกัน\nตั้งค่า คีย์เข้ารหัส และภาพอ้างอิงยังอยู่ที่เดิม • ต้องยืนยัน Windows startup ใหม่',wraplength=800).grid(row=19,column=0,columnspan=3,sticky='w')
        self.storage_estimate = tk.StringVar(value='ภาพสดผ่าน RAM แบบจำกัด • บันทึกเฉพาะเหตุการณ์/หลักฐาน')
        self.storage_estimate_label=ttk.Label(body,textvariable=self.storage_estimate,wraplength=800)
        self.storage_estimate_label.grid(row=16,column=0,columnspan=3,sticky='w')
        self._budget_timer = None
        for key in ('capture_interval_seconds','max_image_storage_mb','data_dir'):
            self.form[key].trace_add('write',self._schedule_storage_estimate)
        self._schedule_storage_estimate()
        self.clock_window = tk.BooleanVar(value=False)
        self.form['clock_check_interval_seconds']=tk.StringVar(value=str(self.config.clock_check_interval_seconds))
        def reveal(event):
            widget=event.widget
            top=widget.winfo_rooty()-body.winfo_rooty()
            height=max(1,body.winfo_height())
            visible=canvas.canvasy(0)
            bottom=top+widget.winfo_height()
            if top<visible:canvas.yview_moveto(top/height)
            elif bottom>visible+canvas.winfo_height():
                canvas.yview_moveto((bottom-canvas.winfo_height()+4)/height)
        def bind_controls(parent):
            for widget in parent.winfo_children():
                widget.bind('<FocusIn>',reveal,add='+')
                if not isinstance(widget,ttk.Combobox):
                    widget.bind('<MouseWheel>',lambda event:canvas.yview_scroll(
                        -1 if event.delta>0 else 1,'units'),add='+')
                bind_controls(widget)
        bind_controls(body)

    def _schedule_storage_estimate(self, *_):
        if getattr(self,'closing',False):return
        if self._budget_timer is not None:
            self.root.after_cancel(self._budget_timer)
        self._budget_timer = self.root.after(600,self._update_storage_estimate)

    def _root_destroyed(self,event):
        if event.widget is self.root:
            self._run_all_generation=getattr(self,'_run_all_generation',0)+1
            popup=getattr(self,'tracking_popup',None)
            if popup is not None:
                try:popup.close()
                except tk.TclError:pass
            self._cancel_storage_estimate()
            if hasattr(self,'health_alerts'):self.health_alerts.close()

    def _cancel_storage_estimate(self):
        timer=getattr(self,'_budget_timer',None)
        if timer is not None:
            try:self.root.after_cancel(timer)
            except tk.TclError:pass
            self._budget_timer=None

    def _update_storage_estimate(self):
        self._budget_timer = None
        if self.busy or self._active() or self._audio_active():
            return
        from .storage_budget import estimate_storage, event_storage_budget, budget_text
        try:
            estimate=estimate_storage if self._legacy_video_storage() else event_storage_budget
            self.storage_estimate.set(budget_text(estimate(self._candidate())))
        except (ValueError,OSError):
            self.storage_estimate.set('พื้นที่ภาพอัตโนมัติ: ตรวจช่วงรับภาพและสิทธิ์อ่านโฟลเดอร์ก่อนบันทึก')

    def _half_second_preset(self):
        self.form['capture_interval_seconds'].set('0.5')
        self.form['clock_check_interval_seconds'].set('2')
        self.clock_window.set(False)

    def _build_audio_settings(self):
        frame=ttk.Frame(self.audio_panel.modes,padding=12)
        self.audio_connection_settings=frame
        self.audio_panel.modes.add(frame,text='แหล่งเสียงเดิม / ADB')
        frame.columnconfigure(1,weight=1)
        entries=(('camera_name','ชื่อกล้องใน Tapo (เฉพาะแหล่งเสียง)'),
            ('adb_path','ไฟล์ adb.exe สำหรับยืนยันเสียง'),
            ('memu_executable','ไฟล์ MEmu.exe สำหรับเสียง (ถ้าต้องการ)'),
            ('adb_serial','อุปกรณ์ MEmu / ADB ของเสียง'),
            ('tapo_package','Android package ของ Tapo'),
            ('adb_timeout_seconds','ADB timeout สำหรับเสียง (วินาที)'),
            ('ui_check_interval_seconds','อายุยืนยันหน้า Live สำหรับเสียง (วินาที)'),
            ('startup_timeout_seconds','เวลารอแอปต้นทางเสียง (วินาที)'))
        for row,(key,label) in enumerate(entries):
            self._entry(frame,row,key,label,getattr(self.config,key),
                        browse=key in ('adb_path','memu_executable'))
        ttk.Button(frame,text='ค้นหาอุปกรณ์เสียงต้นทาง',command=self.find_devices).grid(row=3,column=2)
        ttk.Label(frame,text='ตั้งค่านี้ใช้เฉพาะเสียง Windows loopback และการยืนยันหน้า Tapo ของเสียง ไม่ใช้รับภาพ RTSP\nภาพ RTSP ไม่ใช่หลักฐานว่าเสียง Windows มาจากกล้องเดียวกัน • เสียงยังต้องเปิดแอปต้นทางตามเดิม',wraplength=780).grid(row=8,column=0,columnspan=3,sticky='w',pady=10)
        ttk.Button(frame,text='บันทึกการตั้งค่า',command=self.save_settings).grid(row=9,column=0,sticky='w')

    def _build_calibration(self):
        ttk.Label(self.calibration,text='ตั้งภาพอ้างอิงครั้งแรก — ไม่ต้องพิมพ์คำสั่งหรือหาไฟล์ empty เอง',
                  style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self.calibration,text='หยุด Capture/Worker และตรวจว่าช่องรับไม่มีแบงค์ค้างก่อนถ่ายภาพ',
                  wraplength=850).pack(anchor='w',pady=(4,8))
        actions=ttk.Frame(self.calibration)
        actions.pack(anchor='w')
        self.capture_ref_button=ttk.Button(actions,text='ถ่ายภาพจาก RTSP',command=self.capture_reference)
        self.capture_ref_button.pack(side='left',padx=(0,8))
        self.load_ref_button=ttk.Button(actions,text='เลือกภาพ PNG…',command=self.choose_image)
        self.load_ref_button.pack(side='left')
        coordinates=ttk.Frame(self.calibration)
        coordinates.pack(anchor='w',pady=8)
        ttk.Label(coordinates,text='พิกัดภาพเต็ม: ').pack(side='left')
        self.roi_values={name:tk.StringVar(value=str(getattr(self.selection,name)))
                         for name in ('x','y','width','height')}
        self.roi_entries=[]
        for name,label in (('x','X'),('y','Y'),('width','กว้าง'),('height','สูง')):
            ttk.Label(coordinates,text=label).pack(side='left',padx=(4,2))
            entry=ttk.Entry(coordinates,textvariable=self.roi_values[name],width=6)
            entry.pack(side='left')
            self.roi_entries.append(entry)
            self.roi_values[name].trace_add('write',self._roi_input_edited)
        self.apply_roi_button=ttk.Button(coordinates,text='ใช้พิกัดกรอบ',command=self.apply_roi_coordinates)
        self.apply_roi_button.pack(side='left',padx=8)
        ttk.Label(coordinates,text='หรือลากกรอบบนภาพกล้อง').pack(side='left')
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
        self.calibration.bind('<Configure>',self._resize_preview)

    def _build_control(self):
        ttk.Label(self.control,text='Capture และ Worker ทำงานแยกกัน ปิดหน้าต่างจะขอหยุดทั้งสองตัว',
                  style='Subheading.TLabel').pack(anchor='w',pady=(0,12))
        self.role_labels,self.start_buttons,self.stop_buttons={},{},{}
        for role,title in (('capture','Capture — รับภาพ RTSP (คนละ process กับเสียง)'),('worker','Worker — ตรวจแบงค์/วัตถุค้าง (ไม่รอเสียง)')):
            frame=ttk.LabelFrame(self.control,text=title,padding=12)
            frame.pack(fill='x',pady=6)
            var=tk.StringVar(value='หยุดอยู่')
            self.role_labels[role]=var
            ttk.Label(frame,textvariable=var).pack(side='left',expand=True,fill='x')
            self.start_buttons[role]=ttk.Button(frame,text='เริ่ม '+role.title(),command=lambda r=role:self.start_role(r))
            self.start_buttons[role].pack(side='left',padx=8)
            self.stop_buttons[role]=ttk.Button(frame,text='หยุด',command=lambda r=role:self.stop_role(r))
            self.stop_buttons[role].pack(side='left')
        actions=ttk.Frame(self.control)
        actions.pack(fill='x',pady=10)
        self.run_all_button=ttk.Button(actions,text='เริ่มทั้งหมด (Run All)',command=self.run_all)
        self.run_all_button.pack(side='left',padx=(0,8))
        ttk.Button(actions,text='หยุดทั้งหมด',command=self.stop_all).pack(side='left')
        ttk.Button(actions,text='เปิดโฟลเดอร์ log',command=self.open_logs_folder).pack(side='left',padx=8)
        ttk.Button(actions,text='ดูภาพ RTSP (ไม่บันทึก)',command=self.preview_rtsp).pack(side='left',padx=4)
        tracking=ttk.Frame(self.control)
        tracking.pack(fill='x',pady=(0,5))
        ttk.Button(tracking,text='ดูกรอบที่ติดตามจาก Worker',command=self.open_tracking_view).pack(side='left')
        ttk.Label(tracking,text='ภาพปัจจุบันใน RAM — ไม่เปลี่ยนภาพช่องว่าง/พิกัดที่บันทึก').pack(side='left',padx=8)
        ttk.Label(self.control,text='สถานะ / ข้อผิดพลาด').pack(anchor='w')
        self.log=tk.Text(self.control,height=16,wrap='word',state='disabled',font=('Consolas',10))
        self.log.pack(fill='both',expand=True,pady=5)

    def _active(self):
        return (any(self.controller.is_running(role) for role in ('capture','worker'))
                or (hasattr(self,'startup_panel') and self.startup_panel.is_running)
                or (hasattr(self,'health_alerts') and self.health_alerts.is_running))

    def stop_role(self,role):
        self._run_all_generation=getattr(self,'_run_all_generation',0)+1
        self.controller.stop(role)
        self._poll_health()

    def open_tracking_view(self):
        if self.closing:return
        getter=getattr(self.controller,'tracking_snapshot',None)
        if not callable(getter):
            self._error('ตัวควบคุมนี้ไม่มีภาพติดตามกรอบ');return
        from .desktop_tracking import DesktopTrackingPopup
        popup=getattr(self,'tracking_popup',None)
        if popup is None or popup.closed:
            self.tracking_popup=DesktopTrackingPopup(self.root,getter=getter,max_age=self.config.max_live_age_seconds)
            self.tracking_popup.window.deiconify()
        self.tracking_popup.poll()

    def run_all(self):
        if self.closing or self.busy or self._run_all_starting:return
        self._run_all_starting=True
        self.run_all_button.configure(state='disabled')
        try:
            from .desktop_run_all import RunAllError, run_all
            try:
                run_all(self)
            except RunAllError as error:
                self._error(str(error))
        except Exception:
            self._error('เริ่มทั้งหมดไม่ได้ ตรวจสถานะภาพ/เสียง/API — ไม่แสดงข้อมูลลับและไม่เริ่มใหม่เอง')
        finally:
            self._run_all_starting=False
            if not self.closing:self.run_all_button.configure(state='normal')

    def _audio_active(self):
        return hasattr(self,'audio_panel') and self.audio_panel.is_running

    def stop_all(self):
        self._run_all_generation=getattr(self,'_run_all_generation',0)+1
        if hasattr(self,'startup_panel'):self.startup_panel.stop()
        self.operation_stop.set()
        self.controller.stop_all()
        self.audio_panel.stop()
        if hasattr(self,'offline_audio_panel'):self.offline_audio_panel.stop()
        self.webhook_panel.stop()
        poll_health=getattr(self,'_poll_health',None)
        if callable(poll_health):poll_health()

    def _poll_health(self):
        """Typed live health only; credentials were snapshotted when saved on Tk."""
        supervisor=getattr(self,'health_alerts',None)
        if supervisor is None:return
        snapshots={}
        if not self.closing:
            for channel,owner in (('vision',self.controller),
                                   ('audio',self.audio_panel.listener_panel)):
                getter=getattr(owner,'health_snapshot',None)
                if callable(getter):snapshots[channel]=getter()
        supervisor.poll(self.config,getattr(self,'_health_credentials',('', '')),snapshots)
        labels={
            'disabled':'ปิดแจ้งสุขภาพระบบ', 'missing_credentials':'ไม่มีคีย์ Notify API ที่บันทึกไว้',
            'idle':'ยังไม่เริ่มเฝ้า', 'monitoring':'เฝ้าสุขภาพภาพ/เสียง',
            'queued':'รอส่งแจ้งสุขภาพระบบ', 'sending':'กำลังส่งแจ้งสุขภาพระบบ',
            'accepted':'Notify API รับคำขอแล้ว — ยังไม่ยืนยันว่ามือถือดัง',
            'failed':'Notify API ยังไม่ยืนยันรับคำขอ', 'ambiguous':'ไม่ทราบแน่ชัดว่า Notify API รับแล้วหรือไม่',
            'closed':'หยุดแจ้งสุขภาพระบบ',
        }
        if hasattr(self,'health_notification_status'):
            self.health_notification_status.set('สุขภาพระบบ: '+labels.get(supervisor.status,'ยังยืนยันการส่งไม่ได้'))

    def _error(self, text):
        self.status.set(text)
        messagebox.showerror('Smart Tapo',text,parent=self.root)

    def _browse(self, variable):
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('Windows application','*.exe'),('All files','*')])
        if path: variable.set(path)

    def _legacy_video_storage(self):
        # RAM is the GUI default. Legacy behavior requires explicit opt-in.
        return getattr(getattr(self,'controller',None),'video_storage_mode','ram') == 'legacy_spool'

    def _storage_busy(self, *, include_history=True):
        if getattr(self,'closing',False) or self.busy or self._active() or self._audio_active():
            return True
        for name in ('startup_panel','audio_panel','offline_audio_panel','webhook_panel',
                     'refueling_panel','notify_test_panel'):
            if name=='refueling_panel' and not include_history:continue
            panel=getattr(self,name,None)
            if panel is not None and panel.is_running:
                return True
        history=getattr(self,'history_panel',None)
        return bool(include_history and history is not None and history.pending)

    def choose_data_folder(self):
        if self._storage_busy():
            self._error('หยุดงานทั้งหมดและรอรายการเบื้องหลังให้เสร็จก่อนเลือกโฟลเดอร์')
            return
        path=filedialog.askdirectory(parent=self.root,title='เลือกโฟลเดอร์ข้อมูลใหม่ที่ว่าง (ไม่ย้ายข้อมูลเดิม)',mustexist=False)
        if not path:return
        if self._storage_busy():
            self._error('มีงานกำลังทำอยู่ ยังไม่เปลี่ยนโฟลเดอร์')
            return
        self.form['data_dir'].set(path)

    def _candidate(self):
        updates={key:var.get().strip() for key,var in self.form.items()}
        if 'data_dir' in updates:
            from .storage_location import normalize_location
            updates['data_dir']=normalize_location(updates['data_dir'],self.store.root)
        if 'max_image_storage_mb' in updates:
            try:updates['max_image_storage_mb']=float(updates['max_image_storage_mb'])
            except ValueError:raise ValueError('โควตาภาพหลักฐานต้องเป็นตัวเลขมากกว่า 0') from None
        try:updates['stuck_consecutive_frames']=int(updates['stuck_consecutive_frames'])
        except ValueError:raise ValueError('จำนวนภาพก่อนเตือนต้องเป็นจำนวนเต็มบวก') from None
        try:updates['deposit_history_hours']=float(updates['deposit_history_hours'])
        except ValueError:raise ValueError('ระยะเก็บประวัติต้องเป็นจำนวนชั่วโมงมากกว่า 0') from None
        if 'capture_interval_seconds' in updates:
            try: updates['capture_interval_seconds']=float(updates['capture_interval_seconds'])
            except ValueError: raise ValueError('ช่วงรับภาพต้องเป็นจำนวนวินาทีที่ถูกต้อง') from None
            if updates['capture_interval_seconds'] != self.config.capture_interval_seconds:
                updates['stuck_minimum_seconds']=max(self.config.stuck_minimum_seconds,
                    (updates['stuck_consecutive_frames']-1)*self.config.capture_interval_seconds)
                updates['clear_minimum_seconds']=max(self.config.clear_minimum_seconds,
                    (self.config.clear_consecutive_frames-1)*self.config.capture_interval_seconds)
        if 'clock_check_interval_seconds' in updates:
            try: updates['clock_check_interval_seconds']=float(updates['clock_check_interval_seconds'])
            except ValueError: raise ValueError('ช่วงตรวจนาฬิกาต้องเป็นวินาที') from None
        if hasattr(self, 'clock_window'):
            updates['clock_window_enabled']=False
        for key in ('adb_timeout_seconds','ui_check_interval_seconds','startup_timeout_seconds',
                    'health_failure_seconds','health_repeat_seconds'):
            if key in updates:
                try:updates[key]=float(updates[key])
                except ValueError:raise ValueError('เวลาตรวจแหล่งเสียงต้องเป็นจำนวนวินาทีที่ถูกต้อง') from None
        updates['capture_backend']='rtsp'
        updates['notify_enabled']=self.notify.get()
        if hasattr(self,'notify_base'):
            updates['notify_api_url']=self.notify_base.get().strip()
            updates['notify_importance']=self.notify_importance.get()
        updates['health_notify_enabled']=(self.health_notify.get() if hasattr(self,'health_notify')
                                          else self.config.health_notify_enabled)
        return replace(self.config,**updates).validate()

    def _save_webhook_config(self,config):
        # API controls never apply a stale snapshot of capture/audio/calibration fields.
        updated=replace(self.config,refueling_api_url=config.refueling_api_url,
                        refueling_api_enabled=config.refueling_api_enabled).validate()
        self.store.save(updated)
        self.config=updated

    def _remember_history_port(self,port):
        # This display-only setting does not reconfigure a running detector.
        updated=replace(self.config,history_web_port=port).validate()
        self.store.save(updated)
        self.config=updated

    def save_settings(self):
        if self._storage_busy(include_history=False):
            self._error('หยุดงานทั้งหมดและรอรายการเบื้องหลังให้เสร็จก่อนแก้การตั้งค่า')
            return False
        old=self.config
        changed_root=False
        try:
            config=self._candidate()
            from .storage_location import location_identity, preflight_location
            changed_root=location_identity(config.data_dir)!=location_identity(old.data_dir)
            if changed_root:
                if self._storage_busy():
                    raise ValueError('รอประวัติและงานเบื้องหลังให้จบก่อนเปลี่ยนโฟลเดอร์ข้อมูล')
                if not messagebox.askyesno('เปลี่ยนโฟลเดอร์ข้อมูลและปิดโปรแกรม',
                        'บันทึกแล้วโปรแกรมจะปิด ต้องเปิดใหม่เพื่อใช้โฟลเดอร์ใหม่\nไม่ย้าย/ลบข้อมูลเดิม ประวัติแยกกัน และต้องยืนยัน Windows startup ใหม่\nดำเนินการต่อ?',parent=self.root):return False
                if self._storage_busy():
                    raise ValueError('มีงานกำลังทำอยู่ ยังไม่บันทึกโฟลเดอร์ใหม่')
            # Keep raw text until admission, so resolve cannot hide link/../ paths.
            if 'data_dir' in getattr(self,'form',{}) or changed_root:
                raw=self.form['data_dir'].get() if 'data_dir' in getattr(self,'form',{}) else config.data_dir
                target=preflight_location(raw,self.store.root,old.data_dir,old.baseline_path)
                config=replace(config,data_dir=target)
            from .storage_budget import apply_storage_budget, apply_event_storage_budget, budget_text
            apply_budget=apply_storage_budget if self._legacy_video_storage() else apply_event_storage_budget
            config,budget=apply_budget(config)
            if changed_root:
                config=replace(config,windows_startup_enabled=False,windows_startup_audio=False,windows_startup_binding='')
            if hasattr(self,'storage_estimate'):
                self.storage_estimate.set(budget_text(budget))
            key=self.notify_key.get().strip()
            if config.notify_enabled and not key:
                raise ValueError('กรอก Notify API Key ก่อนเปิดส่งแจ้งเตือน')
            if key:
                self.store.save_notify_key(key,config.notify_api_url,config.camera_id)
                self._credential_error=False
            self.store.save(config)
            if changed_root:
                try:
                    if old.windows_startup_enabled or old.windows_startup_binding:
                        from .startup import manage_entry
                        manage_entry(False,config=self.store.config_path)
                except Exception:
                    self.store.save(old)
                    raise
            else:
                self.config=config
                self._health_credentials=(key,config.notify_api_url)
        except Exception as error:
            # Never include arbitrary exception text from a credential provider.
            text=str(error) if isinstance(error,ValueError) else 'บันทึกไม่ได้ ตรวจสิทธิ์โฟลเดอร์และการเข้ารหัส Windows ของบัญชีนี้'
            self._error(text)
            return False
        if changed_root:
            # Never publish the new root to old viewers, pending jobs or HTTP tokens.
            self.status.set('บันทึกโฟลเดอร์ใหม่แล้ว กำลังปิดอย่างปลอดภัย — เปิดโปรแกรมใหม่ ประวัติเดิมยังอยู่แยกกัน')
            self.close()
            # False means callers must not start/capture/calibrate in this process.
            return False
        self.status.set('บันทึกแล้ว — '+('เปิดแจ้งเตือนอัตโนมัติ' if config.notify_enabled else 'โหมดเก็บผล ปิดแจ้งเตือนอัตโนมัติ; ปุ่มทดสอบจริงยังต้องยืนยัน'))
        self._refresh_calibration()
        return True

    def clear_keys(self):
        if self._active() or self.busy or self._audio_active():
            self._error('หยุดการทำงานก่อนล้างคีย์')
            return
        if not messagebox.askyesno('ล้างคีย์','ล้างคีย์ที่บันทึกและปิดการแจ้งเตือนจริง?',parent=self.root): return
        try:
            self.notify.set(False)
            self.config=replace(self.config,notify_enabled=False)
            self.store.save(self.config)
            self.store.clear_notify_key()
            self.notify_key.set('')
            self._health_credentials=('',self.config.notify_api_url)
            self.status.set('ล้างคีย์แล้ว และปิดการแจ้งเตือนจริง')
        except Exception:
            self._error('ล้างคีย์ไม่สำเร็จ กรุณาตรวจสิทธิ์โฟลเดอร์ข้อมูล')

    def _async(self, work, done, *, error_text='ทำรายการไม่สำเร็จ ตรวจการตั้งค่าและการเชื่อมต่อก่อนลองใหม่'):
        if self.busy or self._active() or self._audio_active():
            self._error('หยุด Capture/Worker และรอรายการปัจจุบันก่อน')
            return
        self.busy=True
        self.operation_stop.clear()
        self.status.set('กำลังทำรายการที่ขอ… หน้าต่างยังใช้งานได้')
        def target():
            try: self.jobs.put_nowait((done,work(),None))
            except Exception: self.jobs.put_nowait((done,None,error_text))
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
        self._async(work,done,error_text='ค้นหาอุปกรณ์เสียงต้นทางไม่สำเร็จ ตรวจ adb.exe และ MEmu สำหรับเสียง')

    def capture_reference(self):
        if not self.save_settings(): return
        config=self.config
        def work():
            from .capture.rtsp import read_rtsp_reference
            return read_rtsp_reference(config,self.operation_stop)
        self._async(work,lambda image:self._rtsp_reference_loaded(image,config),
                    error_text='รับภาพ RTSP ไม่สำเร็จ ตรวจ relay/URL/การเชื่อมต่อ แล้วลองใหม่ ไม่มีการใช้ภาพเก่าหรือ ADB แทน')

    def _rtsp_reference_loaded(self,image,expected):
        if self.operation_stop.is_set() or self.closing:return
        try:
            candidate=self._candidate()
        except ValueError:
            self._error('การตั้งค่า RTSP ไม่ถูกต้องระหว่างรับภาพ — ไม่ใช้ภาพนี้ กรุณาบันทึกและถ่ายใหม่')
            return
        if candidate!=expected:
            self._error('การตั้งค่าเปลี่ยนระหว่างรับภาพ RTSP — ไม่ใช้ภาพนี้ กรุณาบันทึกและถ่ายใหม่')
            return
        from .desktop_rtsp import reference_config
        try:
            config=reference_config(expected,image,reset_roi=not is_ready(expected,self.store.root))
            self.store.save(config)
        except (OSError,ValueError,DesktopStorageError):
            self._error('ยังไม่ใช้ภาพ RTSP นี้ ตรวจขนาดภาพ/สิทธิ์/พื้นที่และบันทึกการตั้งค่าก่อนลองใหม่')
            return
        self.config=config
        self.selection=config.roi
        self.load_image(image)

    def preview_rtsp(self):
        if self._active() or self.busy:
            self._error('หยุด Capture/Worker ก่อนเปิดตัวอย่าง RTSP แยก')
            return
        if not self.save_settings():return
        config=self.config
        def work():
            from .capture.rtsp import read_rtsp_reference
            return read_rtsp_reference(config,self.operation_stop)
        def done(image):
            if self.closing or self.operation_stop.is_set():return
            window=tk.Toplevel(self.root);window.title('ตัวอย่างภาพ RTSP — ไม่บันทึก/ไม่ใช้แทนภาพอ้างอิง')
            sample=image.copy();sample.thumbnail((960,540))
            photo=ImageTk.PhotoImage(sample,master=window)
            label=ttk.Label(window,image=photo);label.image=photo;label.pack(padx=8,pady=8)
            ttk.Label(window,text='ขนาดภาพ '+str(image.size)+' • ภาพตัวอย่างหนึ่งเฟรม ไม่ใช่การยืนยันสตรีมสดต่อเนื่อง').pack(pady=6)
        self._async(work,done,error_text='เปิดภาพตัวอย่าง RTSP ไม่สำเร็จ ไม่มีการเรียก ADB หรือใช้ภาพเก่าแทน')

    def choose_image(self):
        if self._active() or self.busy or self._audio_active():
            self._error('หยุดการทำงานก่อนตั้งภาพช่องว่าง')
            return
        if not self.save_settings():return
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('PNG screenshot','*.png')])
        if not path:return
        try:
            with Image.open(path) as source:
                if source.format!='PNG' or source.width>7680 or source.height>4320:
                    raise ValueError('Invalid reference image')
                image=source.convert('RGB')
            self.operation_stop.clear()
            self._rtsp_reference_loaded(image,self.config)
        except (OSError,ValueError):self._error('ต้องเป็นภาพ PNG ของแหล่ง RTSP นี้ เลือกกรอบและยืนยันภาพช่องว่างใหม่ก่อนเริ่มตรวจ')

    def load_image(self, image, saved=False):
        self.reference_image=image.copy()
        self.reference_dirty=not saved
        self.reference_source=(self.config.camera_id,self.config.rtsp_url)
        self.confirm_empty.set(False)
        self._render_preview()
        self._draw_selection()
        self.status.set('ตรวจภาพช่องว่างและตำแหน่งกรอบก่อนกดยืนยัน')

    def _resize_preview(self,event):
        if self.reference_image is not None and not self.closing:
            self._render_preview(force=False)

    def _render_preview(self,force=True):
        video=self.reference_image.crop(self.config.video.box())
        available=self.preview_max_height
        if self.calibration.winfo_height()>1:
            fixed=28  # The tab frame has 14px padding on each side.
            for child in self.calibration.winfo_children():
                if child is self.canvas:
                    continue
                padding=child.pack_info().get('pady',0)
                padding=sum(int(p) for p in padding) if isinstance(padding,(tuple,list)) else 2*int(padding)
                fixed+=child.winfo_reqheight()+padding
            available=max(120,min(available,self.calibration.winfo_height()-fixed))
        self.preview_scale=min(720/video.width,available/video.height)
        size=(round(video.width*self.preview_scale),round(video.height*self.preview_scale))
        if not force and self.photo is not None and (self.photo.width(),self.photo.height())==size:
            return
        self.photo=ImageTk.PhotoImage(video.resize(size,Image.Resampling.LANCZOS),master=self.root)
        self.canvas.configure(width=size[0],height=size[1])
        self.canvas.delete('all')
        self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self._draw_selection(sync_inputs=False)

    def _roi_fields_match_selection(self):
        try:
            return all(int(value.get())==getattr(self.selection,name)
                       for name,value in self.roi_values.items())
        except ValueError:
            return False

    def _calibration_pending(self):
        return (self.reference_dirty or self.selection!=self.config.roi
                or not self._roi_fields_match_selection())

    def _roi_input_edited(self,*args):
        if hasattr(self,'confirm_empty'):
            self.confirm_empty.set(False)
        if hasattr(self,'calibration_status'):
            self._refresh_calibration()

    def apply_roi_coordinates(self):
        if self._active() or self.busy or self.closing:
            self._error('หยุด Capture/Worker และรอรายการปัจจุบันก่อนเปลี่ยนกรอบ')
            return False
        if self.reference_image is None:
            self._error('เลือกภาพช่องว่างก่อนตั้งพิกัดกรอบ')
            return False
        try:
            selection=Rect(**{name:int(value.get()) for name,value in self.roi_values.items()})
            if min(selection.width,selection.height)<12:
                raise ValueError('small ROI')
            replace(self.config,roi=selection).validate()
        except ValueError:
            self._error('พิกัดต้องเป็นจำนวนเต็ม กรอบอย่างน้อย 12×12 และอยู่ภายในภาพกล้อง')
            return False
        self.selection=selection
        self.confirm_empty.set(False)
        self._draw_selection()
        self.status.set('ใช้พิกัดบนภาพแล้ว ยังไม่บันทึก — ตรวจภาพช่องว่างและยืนยันก่อนเริ่ม Worker')
        return True

    def use_recorded_roi(self):
        if self._active() or self.busy or self._audio_active():
            self._error('หยุดทุกงานก่อนเลือกกรอบทดลอง');return False
        if (self.reference_image is None or self.config.expected_size!=(720,1280)
                or self.reference_image.size!=self.config.expected_size):
            self._error('กรอบจากคลิปใช้กับภาพ 720×1280 เท่านั้น เลือกภาพช่องว่างและตรวจตำแหน่งก่อน');return False
        for key,value in dict(x=249,y=272,width=100,height=80).items():self.roi_values[key].set(str(value))
        return self.apply_roi_coordinates()

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

    def _draw_selection(self,sync_inputs=True):
        if sync_inputs:
            for name,value in self.roi_values.items():
                text=str(getattr(self.selection,name))
                if value.get()!=text:
                    value.set(text)
        self.canvas.delete('roi')
        x,y,w,h=self.selection.x,self.selection.y,self.selection.width,self.selection.height
        vx,vy,s=self.config.video.x,self.config.video.y,self.preview_scale
        self.canvas.create_rectangle((x-vx)*s,(y-vy)*s,(x+w-vx)*s,(y+h-vy)*s,outline='#ff5252',width=3,tags='roi')

    def save_reference(self):
        if self._active() or self.busy:
            self._error('หยุด Capture/Worker ก่อนเปลี่ยนภาพอ้างอิง')
            return False
        if not self._roi_fields_match_selection():
            self._error('กดใช้พิกัดกรอบให้ตรงกับภาพก่อนยืนยันบันทึก')
            return False
        if self.reference_image is None or not self.confirm_empty.get():
            self._error('ต้องมีภาพ และติ๊กยืนยันว่าช่องรับว่างกับตำแหน่งกรอบถูกต้องก่อน')
            return False
        try:
            candidate=self._candidate()
            if self.reference_source!=(candidate.camera_id,candidate.rtsp_url):
                raise ValueError('source changed')
        except ValueError:
            self._error('แหล่ง RTSP เปลี่ยนหลังรับภาพนี้ กรุณาบันทึก URL และถ่ายภาพช่องว่างใหม่')
            return False
        if self.config.baseline_path.exists() and not messagebox.askyesno('เปลี่ยนภาพอ้างอิง','แทนที่ภาพอ้างอิงเดิมด้วยภาพนี้?',parent=self.root):return False
        if not self.save_settings():return False
        try:
            from .capture.rtsp import rtsp_source_fingerprint
            updated=replace(self.config,roi=self.selection,
                rtsp_source_binding=rtsp_source_fingerprint(self.config)).validate()
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
            self.reference_dirty=False
            self._source_migrated=False
        except Exception:
            self._error('บันทึกภาพอ้างอิงไม่สำเร็จ ตรวจสิทธิ์และพื้นที่ว่าง แล้วตั้งภาพอีกครั้งก่อนเริ่ม Worker')
            self._refresh_calibration()
            return False
        self.status.set('ภาพอ้างอิงพร้อม — ไปแท็บเริ่ม / หยุดได้ ไม่ต้องรันคำสั่งเพิ่มเติม')
        self._refresh_calibration()
        return True

    def _refresh_calibration(self):
        ready=is_ready(self.config,self.store.root) and not self._calibration_pending()
        self.calibration_status.set('ภาพอ้างอิงพร้อมใช้งาน' if ready else 'ยังไม่พร้อม: ใช้พิกัดและยืนยันภาพช่องว่างสำหรับภาพ/กรอบปัจจุบันก่อนเริ่ม Worker')

    def start_role(self,role):
        if self.busy:return
        # Saving settings is blocked while another role runs. Config changes
        # cannot be applied mid-run, including an audio-only run. Starting a
        # companion role uses the same frozen config, never a settings rewrite.
        if not self._active() and not self._audio_active() and not self.save_settings():return
        try:
            if self._candidate()!=self.config:
                raise ValueError('หยุดทั้งหมดและบันทึกการตั้งค่าที่เปลี่ยนก่อน')
            if role=='worker':
                if self._calibration_pending() or not is_ready(self.config,self.store.root):
                    self.notebook.select(self.calibration)
                    raise ValueError('ตั้งและยืนยันภาพช่องว่างในแท็บ 2 ก่อนเริ่ม Worker')
                self.controller.start_worker(self.config,self.notify_key.get().strip())
            else:
                if self._calibration_pending() or not is_ready(self.config,self.store.root):
                    self.notebook.select(self.calibration)
                    raise ValueError('รับภาพ RTSP และยืนยันภาพช่องว่างกับกรอบใหม่ก่อนเริ่ม Capture')
                self.controller.start_capture(self.config)
            if self.controller.is_running(role):
                bindings=getattr(self,'_run_all_role_configs',{})
                bindings[role]=self.config
                self._run_all_role_configs=bindings
            self.notebook.select(self.control)
        except Exception as error:
            self._error(str(error) if isinstance(error,ValueError) else 'เริ่มงานไม่ได้ ดูสถานะและตรวจการตั้งค่า')

    def open_folder(self):
        if os.name=='nt':os.startfile(self.config.data_dir)
        elif os.sys.platform=='darwin':subprocess.Popen(['open',str(self.config.data_dir)])
        else:self.status.set(str(self.config.data_dir))

    def open_config_folder(self):
        if os.name=='nt':os.startfile(self.store.root)
        elif os.sys.platform=='darwin':subprocess.Popen(['open',str(self.store.root)])
        else:self.status.set(str(self.store.root))

    def open_logs_folder(self):
        try:
            path=self.config.data_dir/'logs'
            path.mkdir(parents=True,exist_ok=True)
            if os.name=='nt':os.startfile(path)
            elif os.sys.platform=='darwin':subprocess.Popen(['open',str(path)])
            else:self.status.set(str(path))
        except OSError:
            self._error('เปิดโฟลเดอร์ log ไม่ได้ ตรวจสิทธิ์โฟลเดอร์ข้อมูล')

    def collect_page_signature(self,label):
        if self.closing:return
        if self._active() or self.busy or self.audio_panel.is_running:
            self._error('หยุด Capture/Worker/การรับเสียงก่อนเก็บข้อมูลหน้า Tapo')
            return
        try:
            if self._candidate()!=self.config or not self.config.adb_serial:
                raise ValueError
        except ValueError:
            self._error('บันทึกการตั้งค่าและเลือก ADB Serial ก่อนเก็บข้อมูลหน้า')
            return
        config=self.config
        def work():
            from .page_signature import collect_page_signature
            return collect_page_signature(config,label)
        def done(path):
            self.status.set('เก็บข้อมูลหน้าแล้ว (ยังไม่ใช่การรับรอง Live): '+str(path))
        self._async(work,done,error_text='อ่านโครงสร้างหน้าใหม่ไม่สำเร็จ จึงไม่ใช้ XML เก่า ตรวจ ADB Serial และลองพักวิดีโอชั่วครู่ก่อนเก็บใหม่')

    def open_page_reports(self):
        try:
            from .refueling_store import _safe
            path=_safe(self.config.data_dir/'diagnostics')
            if not path.is_dir():
                self.status.set('ยังไม่มีรายงาน กดเก็บหน้า Live/Playback ก่อน')
                return
            if os.name=='nt':os.startfile(path)
            else:self.status.set(str(path))
        except (OSError,ValueError):self._error('เปิดรายงานไม่ได้ ตรวจโฟลเดอร์ข้อมูล')

    def preview_adb_device(self):
        if self.closing:return
        if self._active() or self.busy or self.audio_panel.is_running:
            self._error('หยุด Capture/Worker/การรับเสียงก่อนดูภาพอุปกรณ์ ADB')
            return
        try:
            if self._candidate()!=self.config or not self.config.adb_serial:raise ValueError
        except ValueError:
            self._error('บันทึกการตั้งค่าและเลือก ADB Serial ก่อนดูภาพ')
            return
        config=self.config
        def work():
            from .page_signature import device_preview
            return device_preview(config)
        def done(image):
            previous=getattr(self,'device_preview_window',None)
            if previous is not None and previous.winfo_exists():previous.destroy()
            window=tk.Toplevel(self.root)
            self.device_preview_window=window
            window.title('อุปกรณ์ ADB: '+config.adb_serial+' — ไม่บันทึกภาพ')
            from .desktop_clock import ClockRegionEditor
            editor=ClockRegionEditor(window,image,config,lambda region:self._save_clock_roi(config,region))
            self.device_preview_photo=editor
            editor.pack()
            ttk.Label(window,text='ถ้าไม่ตรง MEmu ที่ดูอยู่ ให้แก้ ADB Serial ก่อนเก็บ Live/Playback').pack(padx=8,pady=8)
            def close():
                window.destroy();self.device_preview_window=self.device_preview_photo=None
            window.protocol('WM_DELETE_WINDOW',close)
            window.deiconify()
            self.status.set('ภาพจาก ADB Serial ที่ตั้งไว้เท่านั้น — ไม่เปิด/เปลี่ยนหน้า Tapo และไม่บันทึกภาพ')
        self._async(work,done,error_text='ดูภาพอุปกรณ์ไม่ได้ ตรวจ ADB Serial/ขนาดภาพที่ตั้งไว้ ไม่ได้บันทึกหรือเปลี่ยนหน้า Tapo')

    def _save_clock_roi(self,expected,region):
        try:
            if (self.closing or self.busy or self._active() or self._audio_active()
                    or self.config!=expected or self._candidate()!=self.config):return False
            region.validate(expected.expected_size)
            updated=replace(self.config,clock_roi=region).validate()
            self.store.save(updated)
        except (OSError,ValueError,DesktopStorageError):return False
        self.config=updated
        self.status.set('บันทึกเฉพาะกรอบนาฬิกาแล้ว ภาพช่องว่างและกรอบแบงก์ยังเดิม')
        return True

    def _append_log(self,text):
        if not re.match(r'^\[\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} UTC\+7\] ',text):
            text=_dated(text)
        secret=self.notify_key.get()
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
        active=any(self.controller.is_running(role) for role in states) or self._audio_active()
        labels={'stopped':'หยุดอยู่','starting':'กำลังเริ่ม','running':'กำลังทำงาน','stopping':'กำลังหยุดอย่างปลอดภัย','failed':'หยุดเพราะข้อผิดพลาด — ดูข้อความด้านล่าง'}
        health_getter=getattr(self.controller,'health_snapshot',None)
        vision_watch=(callable(health_getter) and health_getter().get('expected') is True)
        for role,value in states.items():
            self.role_labels[role].set(labels.get(value['state'],value['state']))
            running=self.controller.is_running(role)
            self.start_buttons[role].configure(state='disabled' if running or self.busy or self.closing else 'normal')
            self.stop_buttons[role].configure(state='normal' if (running or vision_watch) and value['state']!='stopping' and not self.closing else 'disabled')
        for text in self.controller.drain_messages():self._append_log(text)
        self.history_panel.poll(visible=self.notebook.select()==str(self.history_panel))
        self.refueling_panel.poll(visible=self.notebook.select()==str(self.refueling_panel))
        self.audio_panel.poll()
        self.offline_audio_panel.poll()
        self.webhook_panel.poll()
        self.startup_panel.poll()
        self._poll_health()
        active=active or self.startup_panel.is_running or self.health_alerts.is_running
        popup=getattr(self,'tracking_popup',None)
        if popup is not None:popup.poll()
        self.run_all_button.configure(state='disabled' if self.busy or self.closing or self._run_all_starting else 'normal')
        def lock_settings(parent):
            for widget in parent.winfo_children():
                if isinstance(widget,(ttk.Entry,ttk.Combobox,ttk.Checkbutton,ttk.Button)):
                    widget.configure(state='disabled' if active or self.busy or self.closing else 'normal')
                lock_settings(widget)
        lock_settings(self.settings)
        self.storage_picker.configure(state='disabled' if self._storage_busy() else 'normal')
        self.notify_test_panel.poll()
        for widget in (self.capture_ref_button,self.load_ref_button,self.save_ref_button,self.confirm_box,
                       self.apply_roi_button,*self.roi_entries):
            widget.configure(state='disabled' if active or self.busy or self.closing else 'normal')
        if self.closing:
            if not active and not self.startup_panel.is_running and not self.busy and not self.audio_panel.is_running and not self.refueling_panel.is_running and not self.webhook_panel.is_running and not self.offline_audio_panel.is_running and not self.notify_test_panel.is_running:
                self.controller.close()
                self.root.destroy()
                return
            self.status.set('กำลังหยุดและปิดข้อมูลอย่างปลอดภัย รอ RTSP/คำขอทดสอบ/งานที่กำลังทำอยู่สักครู่…')
        self.timer=self.root.after(200,self._poll)

    def _notice_release(self,version):
        self.update_notice.set(f'มีเวอร์ชันใหม่ V{version} กรุณาดาวน์โหลดและอัปเดต — ไม่ติดตั้งเอง')
        if not self.update_banner.winfo_manager():
            self.update_banner.pack(before=self.notebook,fill='x',pady=4)

    def _request_update(self,manifest,downloaded):
        # This runs only after the explicit second confirmation. Tk is paused
        # during the bounded arm wait: it cannot start another business action.
        import base64
        import sys
        from . import updates
        from .update_business import business_state
        from .update_install import prepare_update,start_helper,wait_for_arm
        if (not getattr(sys,'frozen',False) or updates.PINNED_PUBLIC_KEY is None
                or not updates.install_allowed(business_state(self))):
            return False
        raw=json.loads(manifest.payload)
        raw['signature']=base64.b64encode(manifest.signature).decode('ascii')
        plan=prepare_update(Path(sys.executable),json.dumps(raw).encode('utf-8'),downloaded,
            current_version=int(APP_VERSION.lower().lstrip('v')),config_path=self.store.config_path.absolute())
        self._update_helper=start_helper(plan)
        wait_for_arm(plan,helper=self._update_helper)
        if not updates.install_allowed(business_state(self)):
            raise updates.UpdateError('Business state changed; keep application open')
        self.close()
        return self.closing is True

    def close(self):
        if self.closing:return
        if (self._active() or self.busy or self.audio_panel.is_running) and not messagebox.askyesno('ปิด Smart Tapo','หยุด Capture/Worker/การรับเสียง แล้วปิดโปรแกรม?',parent=self.root):return
        self.closing=True
        self._run_all_generation=getattr(self,'_run_all_generation',0)+1
        popup=getattr(self,'tracking_popup',None)
        if popup is not None:popup.close()
        if hasattr(self,'health_alerts'):self.health_alerts.close()
        self.updates_panel.close()
        self._cancel_storage_estimate()
        self.startup_panel.stop()
        self.webhook_panel.close()
        self.history_panel.close()
        self.refueling_panel.close()
        self.audio_panel.close()
        self.offline_audio_panel.close()
        self.notify_test_panel.close()
        self.operation_stop.set()
        self.controller.stop_all()


def _smoke_gui_flow(root, app):
    from .desktop_rtsp_smoke import smoke_rtsp_gui
    return smoke_rtsp_gui(root,app)


def smoke_gui(report_path,*,audio_cases=None):
    """Bounded synthetic smoke with cleanup even when a GUI assertion fails."""
    import time
    from .smoke_window import install
    window_policy=install()
    with tempfile.TemporaryDirectory(prefix='smart-tapo-gui-smoke-') as directory:
        root=tk.Tk()
        root.withdraw()
        app=None
        try:
            app=DesktopApp(root,DesktopStore(Path(directory)/'config.json'))
            result=_smoke_gui_flow(root,app)
            if os.name=='nt':
                url,camera=app.config.notify_api_url,app.config.camera_id
                app.store.save_notify_key('synthetic-smoke-key',url,camera)
                if app.store.load_notify_key(url,camera)!='synthetic-smoke-key':
                    raise RuntimeError('Windows credential round trip failed')
                result['windows_dpapi_roundtrip']=True
            from .storage_recovery_smoke import smoke_storage_recovery
            result.update(smoke_storage_recovery(root, Path(directory)/'storage-recovery'))
            result['offscreen_policy']=window_policy
        finally:
            if app is not None:
                app._cancel_storage_estimate()
                app.health_alerts.close()
                app.webhook_panel.close()
                app.history_panel.close()
                app.refueling_panel.close()
                app.audio_panel.close()
                app.offline_audio_panel.close()
                app.controller.stop_all()
                deadline=time.monotonic()+20
                while time.monotonic()<deadline and (app._active() or app.audio_panel.is_running):
                    app.controller.snapshot()
                    app.audio_panel.poll()
                    app.health_alerts.poll(app.config,('', ''),{})
                    time.sleep(.05)
                # Test-only fail-safe. Never terminate production jobs this way.
                for record in app.controller._roles.values():
                    process=record['process']
                    if process is not None:
                        process.terminate()
                        process.join(timeout=5)
                app.controller.snapshot()
                app.controller.close()
                try:root.after_cancel(app.timer)
                except tk.TclError:pass
            root.destroy()

        # Only publish a successful artifact after all owned IPC/process cleanup.
        manager=app.controller._frame_manager._process
        try:alive=manager.is_alive()
        except ValueError:alive=False  # Process.close itself refuses a live child.
        if alive:raise RuntimeError('RAM manager was not reaped')
        result['ram_manager_reaped_gui']=True
        from .storage_restart_smoke import smoke_storage_restart
        result.update(smoke_storage_restart())
        if audio_cases is not None:
            from .audio_case_smoke import smoke_audio_cases
            private_report=Path(report_path).with_name(Path(report_path).stem+'-offline-audio.json')
            result.update(smoke_audio_cases(audio_cases,private_report,Path(directory)/'offline-audio'))
        atomic_bytes(report_path,json.dumps(result).encode('utf-8'))
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,help='Optional explicit portable config; not required for normal use')
    parser.add_argument('--windows-login',action='store_true',help='Run the explicitly saved Windows-login startup binding')
    parser.add_argument('--smoke-report',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--offline-audio-cases',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--endpoint-metadata-report',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--update-restart',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--update-startup-plan',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--update-token',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.update_restart and args.windows_login:parser.error('Update restart cannot start automatic work')
    if bool(args.update_startup_plan)!=bool(args.update_token):parser.error('Incomplete update startup request')
    if args.update_startup_plan and not args.update_restart:parser.error('Update restart required')
    if args.offline_audio_cases and not args.smoke_report:parser.error('Offline audio cases require smoke-report')
    if args.endpoint_metadata_report:
        from .audio_endpoint import render_endpoint_ids
        endpoints=render_endpoint_ids()
        atomic_bytes(args.endpoint_metadata_report,json.dumps(dict(
            kind='WINDOWS_RENDER_METADATA_ONLY',diagnostic_build=DIAGNOSTIC_BUILD,
            unique_render_endpoints=len(endpoints),capture_started=False,
            config_loaded=False,startup_registered=False)).encode('utf-8'))
        return 0 if endpoints else 1
    if args.smoke_report:
        try:smoke_gui(args.smoke_report,audio_cases=args.offline_audio_cases);return 0
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
            app.updates_panel.start_periodic()
            if args.windows_login:
                app.startup_panel.begin()
            root.deiconify()
            if args.update_startup_plan:
                root.update_idletasks()
                from .update_install import acknowledge_startup
                acknowledge_startup(args.update_startup_plan,args.update_token)
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
            app.updates_panel.close()
            app.health_alerts.close()
            app.startup_panel.close()
            app.webhook_panel.close()
            app.controller.stop_all()
            app.history_panel.close()
            app.refueling_panel.close()
            app.audio_panel.close()
            deadline=time.monotonic()+20
            while app._active() and time.monotonic()<deadline:
                app.controller.snapshot()
                app.health_alerts.poll(app.config,('', ''),{})
                time.sleep(.05)
            app.controller.close()


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
