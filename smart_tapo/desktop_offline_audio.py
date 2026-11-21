"""Explicit file-first TEST panel. Worker receives immutable selection only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event, Lock
import tkinter as tk
from tkinter import ttk, filedialog

from .offline_audio import diagnose_file, DiagnosticCancelled
from .audio_diagnostic_summary import format_report


class OfflineAudioPanel(ttk.Frame):
    def __init__(self,parent,config_getter,listener,*,runner=diagnose_file):
        super().__init__(parent,padding=8)
        self.get_config,self.listener,self.runner=config_getter,listener,runner
        self.future=self.executor=None
        self.stop_event=Event()
        self.closing=False
        self.report=None
        self._progress_lock=Lock()
        self._progress_state=None
        self.selection=tk.StringVar(master=self)
        self.status=tk.StringVar(master=self,value='TEST ออฟไลน์ — ยังไม่เลือกไฟล์ ไม่เปิดเสียง/กล้อง')
        ttk.Label(self,text='ทดสอบไฟล์เสียง (TEST — ไม่ใช่ Live)',style='Subheading.TLabel').pack(anchor='w')
        ttk.Label(self,text='เลือก WAV PCM16 ในเครื่อง: อ่านต่อเนื่องทีละช่วง ไม่จำกัดคลิปไว้ที่ 20 วินาที\nเก็บสถานะวลีเริ่ม–สิ้นสุดข้ามช่วง ไม่เล่นเสียง/รับเสียง Windows/สร้างรายการจริง MP4 ยังไม่รองรับ',wraplength=740).pack(anchor='w',pady=5)
        row=ttk.Frame(self);row.pack(fill='x')
        self.entry=ttk.Entry(row,textvariable=self.selection);self.entry.pack(side='left',fill='x',expand=True)
        self.browse=ttk.Button(row,text='เลือก WAV…',command=self.choose);self.browse.pack(side='left',padx=5)
        actions=ttk.Frame(self);actions.pack(anchor='w',pady=6)
        self.start_button=ttk.Button(actions,text='ทดสอบไฟล์',command=self.start);self.start_button.pack(side='left')
        self.stop_button=ttk.Button(actions,text='ยกเลิกทดสอบ',command=self.stop);self.stop_button.pack(side='left',padx=8)
        ttk.Label(self,textvariable=self.status,wraplength=740).pack(anchor='w')
        self.output=tk.Text(self,height=13,wrap='word',state='disabled')
        scroll=ttk.Scrollbar(self,command=self.output.yview);scroll.pack(side='right',fill='y')
        self.output.configure(yscrollcommand=scroll.set);self.output.pack(fill='both',expand=True)
        ttk.Label(self,text='NCC คือ correlation ไม่ใช่โอกาสถูกต้อง ผลผ่านพิสูจน์ matcher ของไฟล์เท่านั้น\nไม่พิสูจน์ว่าโปรแกรมรับ endpoint ของ MEmu ได้ หรือแก้เหตุเสียงจริงตรวจไม่พบแล้ว',wraplength=740).pack(anchor='w',pady=4)
        self.selection.trace_add('write',lambda *_:self.invalidate())
        self._buttons()

    @property
    def is_running(self):
        return self.future is not None

    def _buttons(self):
        busy=self.is_running or self.closing
        self.start_button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if self.is_running and not self.closing else 'disabled')
        for widget in (self.entry,self.browse):widget.configure(state='disabled' if busy else 'normal')

    def _text(self,text):
        self.output.configure(state='normal');self.output.delete('1.0','end')
        self.output.insert('end',text);self.output.configure(state='disabled')

    def invalidate(self):
        self.stop_event.set()
        with self._progress_lock:self._progress_state=None
        self.report=None
        self._text('')
        if not self.closing:self.status.set('เลือกไฟล์ใหม่แล้ว — ยังไม่มีผลทดสอบ')

    def choose(self):
        if self.is_running or self.closing:return
        path=filedialog.askopenfilename(parent=self,title='เลือก WAV ทดสอบออฟไลน์',filetypes=[('PCM16 WAV','*.wav')])
        if path:self.selection.set(path)

    def _profile(self,config):
        selected=self.listener.profile_path.get().strip()
        if selected:return selected
        try:
            saved=json.loads(config.windows_startup_binding)
            if saved['camera_id']==config.camera_id and saved.get('profile'):return saved['profile']
        except (ValueError,KeyError,TypeError):pass
        return ''

    def _snapshot(self):
        config=self.get_config()
        return (self.selection.get().strip(),self._profile(config),config.camera_id,str(config.data_dir))

    def start(self):
        if self.closing or self.is_running:return
        self.invalidate()
        self.stop_event=Event()
        config=deepcopy(self.get_config())
        self.snapshot=self._snapshot()
        path,profile,camera,_=self.snapshot
        if not path:
            self.status.set('เลือกไฟล์ WAV หนึ่งครั้งก่อน');return
        # Reuse explicit selected/saved profile; otherwise prepare provided
        # references privately without modifying listener selection or settings.
        manifest=Path(self.listener.prepared_manifest)
        stop=self.stop_event
        runner=self.runner
        def progress(update):
            if stop.is_set():return
            with self._progress_lock:
                if not stop.is_set():self._progress_state=dict(update)
        def work():
            from .offline_audio import check_cancel
            check_cancel(stop)
            chosen=profile
            if not chosen:
                from .prepared_profile import prepare_profile
                chosen=prepare_profile(config.data_dir,camera,manifest)
            check_cancel(stop)
            if runner is diagnose_file:
                from .offline_test_rounds import TestRoundTracker
                tracker=TestRoundTracker()
                report=runner(path,chosen,camera,stop,progress=progress,on_candidate=tracker.consume)
                check_cancel(stop)
                return replace(report,test_rounds=tracker.snapshot())
            return runner(path,chosen,camera,stop)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='offline-audio-test')
        self.future=self.executor.submit(work)
        self.status.set('กำลังทดสอบไฟล์ TEST ออฟไลน์ — ไม่เปิดเสียงจริง')
        self._buttons()

    def stop(self):
        self.stop_event.set()
        self.report=None
        self._text('')
        if not self.closing:self.status.set('ยกเลิกแล้ว — รอจบงานที่เป็นเจ้าของ ไม่ใช้ผลที่มาช้า')
        self._buttons()

    def poll(self):
        with self._progress_lock:
            update,self._progress_state=self._progress_state,None
        if update is not None and self.is_running and not self.stop_event.is_set() and not self.closing:
            done,total,rate=(update.get(key) for key in ('processed_frames','total_frames','rate'))
            if (type(done) is int and type(total) is int and type(rate) is int
                    and 0<=done<=total and rate>0 and total>0):
                self.status.set(f'กำลังทดสอบ TEST: {done/rate:.1f} / {total/rate:.1f} วินาที ({100*done/total:.1f}%) — ยกเลิกได้')
        if self.report is not None and self._snapshot()!=self.snapshot:
            self.invalidate()
        if self.future is not None and self._snapshot()!=self.snapshot:
            self.stop()
        if self.future is not None and self.future.done():
            future,self.future=self.future,None
            executor,self.executor=self.executor,None
            try:
                report=future.result()
                if not self.closing and not self.stop_event.is_set() and self._snapshot()==self.snapshot:
                    self.report=report
                    self._text(format_report(report))
                    self.status.set('ทดสอบไฟล์เสร็จ (ไม่ใช่การยืนยันตรวจพบ Live) — ดูเหตุผล/เวลา offset ด้านล่าง')
            except DiagnosticCancelled:
                if not self.closing:self.status.set('ยกเลิกทดสอบแล้ว ไม่มีผลสำเร็จ')
            except Exception:
                self.report=None
                self._text('')
                if not self.closing:self.status.set('ทดสอบไม่ได้: ต้องเป็น WAV PCM16 รูปแบบที่รองรับและโปรไฟล์กล้องเดิม ไฟล์ห้ามเปลี่ยน/เสีย/เป็นลิงก์; MP4 ยังไม่รองรับ ไม่ใช้ผลเก่า')
            finally:
                executor.shutdown(wait=True,cancel_futures=True)
        self._buttons()

    def close(self):
        self.closing=True
        self.stop()


def smoke_offline_audio(root,panel,config):
    """Explicit isolated smoke only; genuine synthetic WAV -> GUI -> real matcher."""
    if config.adb_serial!='synthetic-device' or config.notify_enabled or panel.is_running:
        raise ValueError('Offline smoke requires idle synthetic configuration')
    import tempfile
    import time
    import wave
    import numpy as np
    from .profile_setup import create_private_profile
    from tests.test_announcement import synthetic_phrase
    original=panel.listener.profile_path.get()
    def wait():
        deadline=time.monotonic()+45
        while panel.is_running and time.monotonic()<deadline:
            root.update()
            panel.poll()
            time.sleep(.01)
        if panel.is_running:raise RuntimeError('Offline synthetic job timeout')
    with tempfile.TemporaryDirectory(prefix='smart-tapo-offline-smoke-') as directory:
        base=Path(directory)
        def write(name,samples):
            path=base/name
            with wave.open(str(path),'wb') as target:
                target.setnchannels(1);target.setsampwidth(2);target.setframerate(8000)
                target.writeframes((samples*32767).astype('<i2').tobytes())
            return path
        y=synthetic_phrase()
        ready=write('ready.wav',y);end=write('end.wav',y[::-1])
        manifest=create_private_profile(base/'private',config.camera_id,[
            dict(kind='paid_ready',file=ready,start=.95,end=3.7,threshold=.95),
            dict(kind='end',file=end,start=2.3,end=5.1,threshold=.95)])
        long_audio=np.zeros(180*8000,dtype='float32')
        long_audio[18*8000:18*8000+len(y)]=y
        long_audio[145*8000:145*8000+len(y)]=y[::-1]
        clip=write('test.wav',long_audio)
        try:
            panel.listener.profile_path.set(str(manifest))
            panel.selection.set(str(clip));panel.start();wait()
            report=panel.report
            if report is None:raise RuntimeError('Offline report missing')
            accepted=[row.label for row in report.rows if row.reason=='accepted_test_candidate']
            if accepted!=['paid_ready','end']:raise RuntimeError('Offline synthetic candidates incorrect')
            if report.duration!=180 or report.max_buffer_frames>20*8000:
                raise RuntimeError('Long file was truncated or buffer grew with duration')
            if (not report.test_rounds or report.test_rounds['counts']['complete']!=1
                    or report.test_rounds['records'][0]['duration']<=120):
                raise RuntimeError('Test phrase state did not survive minutes apart')
            if 'พบวลีครบ' not in panel.output.get('1.0','end'):
                raise RuntimeError('Thai test interval summary missing')
            panel.start();panel.stop();wait()
            if panel.report is not None:raise RuntimeError('Cancelled offline report survived')
            return dict(offline_file_matcher_gui=True,offline_file_cancel_fenced=True,
                        offline_file_synthetic_only=True,offline_long_file_gui=True,
                        offline_minutes_apart_test_pair=True,offline_bounded_window=True)
        finally:
            panel.stop()
            wait()
            panel.listener.profile_path.set(original)
            panel.selection.set('')
