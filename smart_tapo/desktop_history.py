"""Read-only event list, evidence viewer and explicit loopback web controls."""
from __future__ import annotations

import queue
import threading
import time
import webbrowser
from datetime import datetime
from tkinter import Toplevel, ttk, StringVar, messagebox

from PIL import Image, ImageTk

from .history import IMAGE_LABELS, KIND_LABELS, get_images, list_events


def local_time(value):
    if value is None:
        return '—'
    try:
        return datetime.fromtimestamp(value).astimezone().strftime('%d/%m/%Y %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return '—'


class HistoryPanel(ttk.Frame):
    def __init__(self, parent, config_provider, save_port):
        super().__init__(parent, padding=14)
        self.config_provider, self.save_port = config_provider, save_port
        self.server = None
        self.closed = False
        self.pending = False
        self.results = queue.Queue(maxsize=2)
        self.generation = 0
        self.scope = None
        self.cursors = [None]
        self.next_before = None
        self.next_refresh = 0
        self.image_request = None
        self.rows = {}
        self.viewer = None
        self.photo = None
        ttk.Label(self, text='ประวัติการหยอดแบงค์', style='Subheading.TLabel').pack(anchor='w')
        self.retention = StringVar()
        ttk.Label(self, textvariable=self.retention, wraplength=900).pack(anchor='w', pady=4)
        ttk.Label(self, text='เหตุการณ์จากภาพ ไม่ยืนยันว่าเป็นแบงค์หรือว่าตู้รับเงิน/เพิ่มยอดสำเร็จ; '
                  'วัตถุที่ผ่านระหว่างภาพทุก 1 วินาทีอาจตรวจไม่ทัน', wraplength=900).pack(anchor='w')
        web = ttk.LabelFrame(self, text='เว็บประวัติ — เฉพาะเครื่องนี้ (127.0.0.1)', padding=8)
        web.pack(fill='x', pady=8)
        ttk.Label(web, text='Port').pack(side='left')
        self.port_var = StringVar(value=str(getattr(config_provider(), 'history_web_port', 8080)))
        self.port_entry = ttk.Entry(web, textvariable=self.port_var, width=7)
        self.port_entry.pack(side='left', padx=6)
        self.start_web_button = ttk.Button(web, text='เริ่มเว็บ', command=self.start_web)
        self.start_web_button.pack(side='left', padx=4)
        self.stop_web_button = ttk.Button(web, text='หยุดเว็บ', command=self.stop_web, state='disabled')
        self.stop_web_button.pack(side='left', padx=4)
        self.open_web_button = ttk.Button(web, text='เปิดในเบราว์เซอร์', command=self.open_web, state='disabled')
        self.open_web_button.pack(side='left', padx=4)
        self.web_status = StringVar(value='ยังไม่เปิดเว็บ — ไม่เริ่มอัตโนมัติเมื่อเปิดโปรแกรม')
        ttk.Label(self, textvariable=self.web_status, wraplength=900).pack(anchor='w')
        table = ttk.Frame(self)
        table.pack(fill='both', expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=('started','last','kind','images'), show='headings', height=7)
        for key, text, width in (('started','เวลาเริ่ม (ล่าสุดก่อน)',170),('last','เวลาที่เห็นล่าสุด',170),
                                 ('kind','สถานะจากภาพ',230),('images','ภาพหลักฐาน',90)):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, minwidth=70, stretch=key=='kind')
        self.tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<Double-1>', self.show_images)
        actions = ttk.Frame(self)
        actions.pack(fill='x')
        ttk.Button(actions, text='รีเฟรช / กลับรายการล่าสุด', command=self.refresh).pack(side='left')
        self.previous_button = ttk.Button(actions, text='ก่อนหน้า', command=self.previous_page, state='disabled')
        self.previous_button.pack(side='left', padx=6)
        self.next_button = ttk.Button(actions, text='ถัดไป', command=self.next_page, state='disabled')
        self.next_button.pack(side='left')
        ttk.Button(actions, text='ดูภาพที่เลือก', command=self.show_images).pack(side='right')
        self.status = StringVar(value='เปิดแท็บนี้เพื่ออ่านประวัติจาก Worker')
        ttk.Label(self, textvariable=self.status, wraplength=900).pack(anchor='w', pady=6)

    def refresh(self):
        self.generation += 1
        self.cursors = [None]
        self.next_before = None
        self.next_refresh = 0

    def previous_page(self):
        if len(self.cursors)>1:
            self.cursors.pop()
            self.generation += 1
            self.next_refresh = 0

    def next_page(self):
        if self.next_before is not None:
            self.cursors.append(self.next_before)
            self.generation += 1
            self.next_refresh = 0

    def _job(self, kind, work):
        self.pending = True
        generation = self.generation
        def target():
            try:
                result = work()
                error = False
            except Exception:
                result, error = None, True
            self.results.put_nowait((kind,generation,result,error))
        threading.Thread(target=target, daemon=True, name='SmartTapo-HistoryRead').start()

    def poll(self, visible=False):
        if self.closed:
            return
        config = self.config_provider()
        scope = (config.data_dir, config.camera_id, config.deposit_history_hours)
        self.retention.set('เวลาตามเครื่อง • รายการและภาพเหตุการณ์ปกติเก็บ '
                           + str(config.deposit_history_hours) + ' ชั่วโมงหลังจบ; ภาพทั่วไปวนลบ '
                           + str(config.rolling_frames_minutes) + ' นาที')
        if scope != self.scope:
            self.scope = scope
            self.rows = {}
            self.tree.delete(*self.tree.get_children())
            self.image_request = None
            self.refresh()
        try:
            kind,generation,result,error = self.results.get_nowait()
            self.pending = False
            if generation == self.generation:
                if error:
                    self.status.set('อ่านประวัติ/ภาพไม่ได้ชั่วคราว ตรวจว่า Worker เริ่มพร้อมแล้วและกดรีเฟรช')
                elif kind == 'events':
                    self._render(result)
                elif kind == 'images':
                    self._show_viewer(*result)
        except queue.Empty:
            pass
        if not self.pending:
            if self.image_request is not None:
                event_id = self.image_request
                self.image_request = None
                self._job('images',lambda:(event_id,get_images(config,event_id)))
            elif visible and time.monotonic() >= self.next_refresh:
                before = self.cursors[-1]
                self.next_refresh = time.monotonic()+3
                self._job('events',lambda:list_events(config,before=before))

    def _render(self, page):
        selection = self.tree.selection()
        rows = {event.id:event for event in page.events}
        if rows != self.rows:
            self.tree.delete(*self.tree.get_children())
            for event in page.events:
                self.tree.insert('', 'end', iid=event.id, values=(local_time(event.started),
                    local_time(event.last_present), KIND_LABELS.get(event.kind,'ไม่ทราบสถานะ'), event.image_count))
            if selection and selection[0] in rows:
                self.tree.selection_set(selection[0])
            self.rows = rows
        self.next_before = page.next_before
        self.previous_button.configure(state='normal' if len(self.cursors)>1 else 'disabled')
        self.next_button.configure(state='normal' if self.next_before else 'disabled')
        self.status.set(('ยังไม่มีประวัติ — ต้องให้ Worker ตรวจพบวัตถุก่อน' if not rows else
                         'หน้า '+str(len(self.cursors))+' • '+str(len(rows))+' รายการ • ดับเบิลคลิกเพื่อดูภาพ'))

    def show_images(self, event=None):
        selected = self.tree.selection()
        if not selected:
            self.status.set('เลือกรายการที่ต้องการดูภาพก่อน')
            return
        self.image_request = selected[0]
        self.status.set('กำลังอ่านภาพหลักฐาน…')

    def _show_viewer(self, event_id, images):
        if self.viewer is not None and self.viewer.winfo_exists():
            self.viewer.destroy()
        self.viewer=None
        self.photo=None
        if not images:
            self.status.set('ไม่มีภาพแล้ว อาจหมดระยะเก็บหรือถูกลบ กดรีเฟรชรายการได้')
            return
        self.viewer = Toplevel(self)
        self.viewer.title('ภาพหลักฐาน — ประวัติการหยอดแบงค์')
        self.viewer.geometry('800x650')
        outer = ttk.Frame(self.viewer,padding=12)
        outer.pack(fill='both',expand=True)
        event = self.rows.get(event_id)
        ttk.Label(outer,text='เวลาเริ่ม: '+local_time(event.started if event else None)+
                  ' • เป็นหลักฐานภาพ ไม่ยืนยันการรับเงิน',wraplength=760).pack(anchor='w')
        picker = ttk.Combobox(outer,state='readonly',values=[IMAGE_LABELS[image.role] for image in images])
        picker.pack(fill='x',pady=8)
        picture = ttk.Label(outer,anchor='center')
        picture.pack(fill='both',expand=True)
        def display(_event=None):
            image = images[picker.current()]
            try:
                # Revalidate ownership/expiry immediately before opening it.
                safe = {item.role:item for item in get_images(self.config_provider(),event_id)}
                if image.role not in safe:
                    raise ValueError()
                with Image.open(safe[image.role].path) as source:
                    width,height = self.config_provider().expected_size
                    if source.format!='PNG' or source.width>width or source.height>height:
                        raise ValueError()
                    frame=source.convert('RGB')
                frame.thumbnail((760,530),Image.Resampling.LANCZOS)
                self.photo=ImageTk.PhotoImage(frame,master=self.viewer)
                picture.configure(image=self.photo,text='')
            except Exception:
                self.photo=None
                picture.configure(image='',text='ภาพนี้ไม่มีแล้วหรือเปิดไม่ได้ กรุณารีเฟรชรายการ')
        picker.bind('<<ComboboxSelected>>',display)
        picker.current(0)
        display()

    def start_web(self):
        try:
            port=int(self.port_var.get())
            if not 1<=port<=65535:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Port ไม่ถูกต้อง','กรอก Port จำนวนเต็มระหว่าง 1–65535',parent=self)
            return False
        if self.server is not None and self.server.running:
            return True
        try:
            self.save_port(port)
            from .history_web import HistoryWebServer
            self.server=HistoryWebServer(self.config_provider)
            self.server.start(port)
        except Exception:
            self.web_status.set('เปิดเว็บไม่ได้ พอร์ตอาจถูกใช้งานหรือบันทึกการตั้งค่าไม่ได้ ลองเลือก Port อื่น')
            return False
        self.web_status.set('เปิดที่ http://127.0.0.1:'+str(self.server.port)+'/ — กดเปิดในเบราว์เซอร์เพื่อเข้าเว็บ')
        self.start_web_button.configure(state='disabled')
        self.port_entry.configure(state='disabled')
        self.stop_web_button.configure(state='normal')
        self.open_web_button.configure(state='normal')
        return True

    def stop_web(self):
        if self.server is not None:
            self.server.stop()
        self.web_status.set('เว็บหยุดแล้ว')
        self.start_web_button.configure(state='normal')
        self.port_entry.configure(state='normal')
        self.stop_web_button.configure(state='disabled')
        self.open_web_button.configure(state='disabled')

    def open_web(self):
        if self.server is not None and self.server.running:
            webbrowser.open(self.server.url)

    def close(self):
        self.closed=True
        if self.server is not None:
            self.server.stop()
