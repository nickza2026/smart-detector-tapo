"""Bounded, RAM-only operator preview. Never evidence or money verification."""
import io
import math
import re
from PIL import Image
from .diagnostics import ALIGNMENT_HINTS

REASONS = frozenset(('ok', 'unknown', 'alignment', 'quality')) | frozenset(ALIGNMENT_HINTS)
KEYS = frozenset(('image', 'size', 'corners', 'session', 'sequence', 'generation',
                  'captured_monotonic', 'observed_monotonic', 'alignment_ok', 'reason'))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_packet(packet, *, now, max_age, size=None, generation=None):
    """Return a defensive copy, or None; inspect image headers before decoding."""
    try:
        if type(packet) is not dict or set(packet) != KEYS:
            return None
        p = packet.copy()
        if not _number(now) or not _number(max_age) or max_age <= 0:
            return None
        if type(p['size']) not in (tuple, list) or len(p['size']) != 2:
            return None
        w, h = p['size']
        if any(type(v) is not int or not 1 <= v <= 16384 for v in (w, h)):
            return None
        if size is not None and tuple(size) != (w, h):
            return None
        for key in ('generation', 'sequence'):
            if type(p[key]) is not int or p[key] < 0:
                return None
        if generation is not None and p['generation'] != generation:
            return None
        if type(p['session']) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', p['session']) is None:
            return None
        c, o = p['captured_monotonic'], p['observed_monotonic']
        if not _number(c) or not _number(o) or not 0 <= c <= o <= now or now - c > max_age:
            return None
        if type(p['alignment_ok']) is not bool or type(p['reason']) is not str or p['reason'] not in REASONS:
            return None
        corners = p['corners']
        if type(corners) not in (tuple, list) or len(corners) not in (0, 8):
            return None
        if any(not _number(v) or not 0 <= v <= (w if i % 2 == 0 else h) for i, v in enumerate(corners)):
            return None
        if p['alignment_ok'] != (len(corners) == 8) or (p['alignment_ok'] and p['reason'] != 'ok'):
            return None
        data = p['image']
        if type(data) is not bytes or not 0 < len(data) <= 128 * 1024:
            return None
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG') or not 1 <= image.width <= 640 or not 1 <= image.height <= 360:
                return None
            if abs(image.width / image.height - w / h) > .03:
                return None
            image.verify()
        p['size'], p['corners'] = (w, h), tuple(corners)
        return p
    except Exception:
        return None


def make_packet(current, frame, observation, *, generation, observed_monotonic, safe):
    """Encode the exact decoded observation frame, not a warped reference."""
    image = current.convert('RGB')
    image.thumbnail((640, 360))
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=75)
    corners = tuple(getattr(observation, 'view_corners', ())) if safe else ()
    reason=getattr(observation,'alignment_reason','')
    reason=reason if type(reason) is str and reason in ALIGNMENT_HINTS else 'unknown'
    return dict(image=output.getvalue(), size=current.size, corners=corners,
                session=frame.session_id, sequence=frame.sequence, generation=generation,
                captured_monotonic=frame.captured_monotonic, observed_monotonic=observed_monotonic,
                alignment_ok=bool(safe and len(corners) == 8),
                reason='ok' if safe and len(corners) == 8 else reason)


class DesktopTrackingPopup:
    """Explicitly opened view; parent owns polling and worker lifecycle."""
    def __init__(self, parent, getter, *, max_age=10.):
        import tkinter as tk
        self.getter = getter
        self.max_age = max_age
        self.closed = False
        self._key = self._photo = None
        self.window = tk.Toplevel(parent)
        self.window.title('ติดตามกรอบจากWorker')
        self.canvas = tk.Canvas(self.window, width=640, height=360, background='black', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.label = tk.Label(self.window, text='UNKNOWN — ดูกรอบเท่านั้น ไม่ใช่การยืนยันเงิน')
        self.label.pack()
        self.window.protocol('WM_DELETE_WINDOW', self.close)

    def poll(self):
        if self.closed:
            return
        try:
            packet = self.getter()
            if packet is None:
                self.canvas.delete('all')
                self._key = self._photo = None
                self.label.configure(text='UNKNOWN — ไม่มีภาพปัจจุบัน')
                return
            import time
            packet = validate_packet(packet, now=time.monotonic(), max_age=self.max_age)
            if packet is None:
                self.canvas.delete('all')
                self._key = self._photo = None
                self.label.configure(text='UNKNOWN — ภาพไม่ผ่านเกณฑ์')
                return
            width = max(1, min(640, self.canvas.winfo_width()))
            height = max(1, min(360, self.canvas.winfo_height()))
            key = (packet['session'], packet['sequence'], packet['generation'], width, height)
            if key == self._key:
                return
            with Image.open(io.BytesIO(packet['image'])) as source:
                image = source.convert('RGB')
            image.thumbnail((width, height))
            from PIL import ImageTk
            self._photo = ImageTk.PhotoImage(image, master=self.window)
            self.canvas.delete('all')
            self.canvas.create_image(0, 0, anchor='nw', image=self._photo)
            if packet['alignment_ok']:
                w, h = packet['size']
                coords = [v * (image.width / w if i % 2 == 0 else image.height / h)
                          for i, v in enumerate(packet['corners'])]
                self.canvas.create_polygon(*coords, fill='', outline='red', width=2)
            text='ติดตามกรอบ' if packet['alignment_ok'] else 'UNKNOWN — '+ALIGNMENT_HINTS.get(packet['reason'],'ยังไม่มั่นใจในภาพ/การจัดแนว')
            self.label.configure(text=text+' — ไม่ใช่การยืนยันเงิน')
            self._key = key
        except Exception:
            self.canvas.delete('all')
            self._key = self._photo = None

    def close(self):
        if not self.closed:
            self.closed = True
            self._photo = self._key = None
            self.window.destroy()
