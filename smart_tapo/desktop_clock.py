"""Explicit RAM-only clock-region calibration from the selected ADB image."""
import math
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageTk
from .config import Rect


def display_region(start,end,native_size,display_size):
    """Map both axes from the actual rendered size, not assumed desktop DPI."""
    if min(*native_size,*display_size)<=0:raise ValueError('Invalid image size')
    points=[]
    for x,y in (start,end):
        points.append((max(0,min(native_size[0],x*native_size[0]/display_size[0])),
                       max(0,min(native_size[1],y*native_size[1]/display_size[1]))))
    left,top=map(math.floor,(min(p[0] for p in points),min(p[1] for p in points)))
    right,bottom=map(math.ceil,(max(p[0] for p in points),max(p[1] for p in points)))
    region=Rect(left,top,right-left,bottom-top);region.validate(native_size)
    return region


class ClockRegionEditor(ttk.Frame):
    def __init__(self,parent,image,config,on_save):
        super().__init__(parent,padding=8)
        if image.size!=config.expected_size:raise ValueError('Image does not match selected device')
        self.image=image.copy();self.config=config;self.on_save=on_save
        self.selection=config.clock_roi;self.drag_start=None;self.edited=False
        self.confirm=tk.BooleanVar(value=False)
        ttk.Label(self,text='ฟ้า = กรอบนาฬิกา • แดง = กรอบตรวจแบงก์ (ไม่เปลี่ยนในหน้านี้)').pack()
        ttk.Label(self,text='ลากครอบตัวเลขวัน/เวลาในวิดีโอทั้งหมด ไม่เลือกหัว Tapo หรือพื้นที่ภาพที่เคลื่อนไหว',
                  wraplength=760).pack()
        scale=min(1.,max(100,parent.winfo_screenwidth()-160)/image.width,
                  max(100,parent.winfo_screenheight()-340)/image.height)
        self.display_size=(max(1,round(image.width*scale)),max(1,round(image.height*scale)))
        self.photo=ImageTk.PhotoImage(self.image.resize(self.display_size,Image.Resampling.LANCZOS),master=self)
        self.canvas=tk.Canvas(self,width=self.display_size[0],height=self.display_size[1],highlightthickness=0)
        self.canvas.pack();self.canvas.create_image(0,0,anchor='nw',image=self.photo)
        self.canvas.bind('<ButtonPress-1>',self._begin)
        self.canvas.bind('<B1-Motion>',self._move)
        self.canvas.bind('<ButtonRelease-1>',self._move)
        self.position=tk.StringVar();ttk.Label(self,textvariable=self.position).pack()
        self.crop_label=ttk.Label(self);self.crop_label.pack()
        ttk.Checkbutton(self,text='ฉันตรวจแล้วว่ากรอบครอบวัน/เวลาในวิดีโอ ไม่ใช่บริเวณอื่น',
                        variable=self.confirm,command=self._buttons).pack()
        self.save_button=ttk.Button(self,text='บันทึกเฉพาะกรอบนาฬิกา',command=self.save)
        self.save_button.pack(pady=4)
        self.status=tk.StringVar(value='ภาพเดียวบอกไม่ได้ว่านาฬิกาเดินหรือไม่ • ไม่มีการบันทึกภาพหรือเริ่มรับเสียง')
        ttk.Label(self,textvariable=self.status,wraplength=760).pack()
        self._render();self._buttons()

    def _box(self,region):
        sx=self.display_size[0]/self.image.width;sy=self.display_size[1]/self.image.height
        left,top,right,bottom=region.box()
        return left*sx,top*sy,right*sx,bottom*sy

    def _render(self):
        self.canvas.delete('regions')
        self.canvas.create_rectangle(*self._box(self.config.roi),outline='#ff4040',width=2,tags='regions')
        self.canvas.create_rectangle(*self._box(self.selection),outline='#00bfff',width=2,tags='regions')
        region=self.selection
        self.position.set(f'ภาพต้นทาง {self.image.width}×{self.image.height} • clock: X={region.x}, Y={region.y}, กว้าง={region.width}, สูง={region.height}')
        crop=self.image.crop(region.box())
        scale=min(760/max(1,crop.width),54/max(1,crop.height))
        crop=crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.NEAREST)
        self.crop_photo=ImageTk.PhotoImage(crop,master=self)
        self.crop_label.configure(image=self.crop_photo)

    def _begin(self,event):self.drag_start=(event.x,event.y)

    def _move(self,event):
        if self.drag_start is None:return
        try:region=display_region(self.drag_start,(event.x,event.y),self.image.size,self.display_size)
        except ValueError:return
        self.selection=region;self.edited=True;self.confirm.set(False)
        self._render();self._buttons()

    def _buttons(self):
        self.save_button.configure(state='normal' if self.edited and self.confirm.get() else 'disabled')

    def save(self):
        if not self.edited or not self.confirm.get():return False
        if self.on_save(self.selection):
            self.status.set('บันทึกกรอบนาฬิกาแล้ว — ไม่แก้กรอบแบงก์หรือภาพช่องว่าง')
            self.edited=False;self.confirm.set(False);self._buttons();return True
        self.status.set('ยังไม่บันทึก ตรวจว่าหยุดทุกงานและยังใช้กล้อง/การตั้งค่าชุดเดิม')
        return False
