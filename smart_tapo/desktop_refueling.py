"""View-only local refueling history. No listener, transport or visual-history mutation."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import queue
import re
import threading
import time
from tkinter import Toplevel, ttk

from PIL import Image, ImageTk

from .desktop_history import local_time
from .refueling_store import RefuelingStore


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000


def _safe(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Unsafe history path')
    return path


def _image_bytes(media, item):
    """Ignore supplied paths; accept only bounded, hash-verified owned media."""
    identifier = item.get('id', '')
    extension = {'image/png': 'png', 'image/jpeg': 'jpg'}.get(item.get('content_type'))
    size = item.get('byte_size')
    if (not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier)
            or extension is None or type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES):
        raise ValueError('Invalid evidence manifest')
    root = _safe(media)
    path = _safe(root / (identifier + '.' + extension))
    if path.resolve().parent != root.resolve() or path.stat().st_size != size:
        raise ValueError('Invalid evidence size')
    with path.open('rb') as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != item.get('sha256'):
        raise ValueError('Invalid evidence hash')
    with Image.open(io.BytesIO(data)) as source:
        if source.format != {'png': 'PNG', 'jpg': 'JPEG'}[extension] or source.width * source.height > MAX_PIXELS:
            raise ValueError('Invalid evidence image')
        source.verify()
    return data


def _query(directory, camera_id, cleanup, request):
    directory = _safe(directory)
    database = _safe(directory / 'refueling.sqlite3')
    if not database.is_file():
        return {'rows': [], 'counts': {}, 'cleanup': {}, 'image': None}
    store = RefuelingStore(directory, camera_id)
    try:
        report = store.cleanup() if cleanup else {}
        result = {'rows': store.list_history(limit=200), 'counts': store.delivery_counts(),
                  'cleanup': report, 'image': None}
        if request is not None:
            session_id, image_id = request
            session = store.get_session(session_id)
            if not session:
                raise ValueError('Evidence expired')
            item = next(item for item in session['evidence'] if item['id'] == image_id)
            result['image'] = _image_bytes(store.media, item)
        return result
    finally:
        store.close()


def _run(results, generation, directory, camera_id, cleanup, request):
    # No Tk object or bound panel method is held by this thread.
    try:
        results.put_nowait((generation, _query(directory, camera_id, cleanup, request), False))
    except Exception:
        results.put_nowait((generation, None, True))


_OBSERVATION_REASONS = {
    'completion_without_start': 'ไม่พบเสียงเริ่มที่จับคู่ได้',
    'awaiting_end_after_source_gap': 'เสียงขาดช่วง — ไม่จับคู่ข้ามช่วง',
    'expired_round_awaiting_completion': 'รอบก่อนหมดอายุ — ไม่จับคู่กับรอบเก่า',
    'duplicate_or_stale_observation': 'ข้อมูลซ้ำหรือย้อนหลัง — ไม่นับเพิ่ม',
    'overlapping_observation': 'เสียงซ้อนกับประกาศก่อน — ไม่นับเพิ่ม',
}


def session_values(row):
    if row.get('row_type') == 'observation_pair':
        return (local_time(row['observed_started_at']),local_time(row['observed_last_ready_at']),
                row['observed_ready_count'],local_time(row['observed_end_at']),
                'คู่เสียงพร้อมเติม→จบ / ข้อมูลรอบไม่ครบ • ไม่ยืนยันยอดรับเงิน')
    if row.get('row_type') == 'observation':
        reason = _OBSERVATION_REASONS.get(row.get('reason'), 'ไม่ได้รวมเป็นรอบเติม')
        observed = local_time(row['observed_started_at'])
        if row.get('kind') == 'paid_ready':
            return (observed, observed, '—', 'ไม่พบ', f'พบพร้อมเติม / ข้อมูลรอบไม่ครบ • {reason}')
        return ('ไม่พบ', 'ไม่พบ', '—', observed,
                f'พบเสียงจบอย่างเดียว • {reason}')
    issues = []
    if row.get('repeated_ready_at') is not None:
        issues.append('ประกาศพร้อมเติมซ้ำ')
    if row.get('missing_end_at') is not None:
        issues.append('ไม่พบเสียงจบภายใน 5 นาที')
    state = 'จบแล้ว' if row.get('ended_at') is not None else 'ยังไม่จบ'
    if row.get('source_interrupted_at') is not None:
        state = 'หลักฐานเสียงขาดช่วง / ไม่ทราบเวลาจบ'
    return (local_time(row['started_at']), local_time(row['last_ready_at']), row['ready_count'],
            local_time(row['ended_at']) if row.get('ended_at') is not None else 'ยังไม่พบเวลาจบ',
            ' • '.join([state, *issues]))


class RefuelingHistoryPanel(ttk.Frame):
    def __init__(self, parent, config_getter):
        super().__init__(parent, padding=12)
        self.get_config = config_getter
        self.closed = False
        self.pending = False
        self.generation = 0
        self.scope = None
        self.rows = {}
        self.results = queue.Queue(maxsize=1)
        self.next_refresh = self.next_cleanup = 0
        self.request = None
        self.viewer = self.photo = None
        self.deletion_warning = False
        ttk.Label(self, text='ตัวรวมรอบ/ส่งรูปทดสอบแล้ว แต่ยังไม่เปิดฟังจริงจนยืนยัน Live/Playback',
                  wraplength=850).pack(anchor='w')
        ttk.Label(self, text='ประวัติรอบเติม / เสียงประกาศที่ยืนยัน • ล่าสุด 200 รายการ • เวลาไทย UTC+7 • เก็บในเครื่อง 3 วัน • ไม่อนุมานจำนวนเงิน',
                  wraplength=850).pack(anchor='w', pady=5)
        actions = ttk.Frame(self)
        actions.pack(fill='x')
        ttk.Button(actions, text='รีเฟรช', command=self.refresh).pack(side='left')
        ttk.Button(actions, text='ดูภาพที่เลือก', command=self.show_image).pack(side='left', padx=8)
        self.tree = ttk.Treeview(self, columns=('start', 'ready', 'count', 'end', 'state'), show='headings', height=9)
        for column, title, width in zip(('start', 'ready', 'count', 'end', 'state'),
                                      ('เริ่ม', 'พร้อมล่าสุด', 'ครั้ง', 'จบ', 'สถานะ / ปัญหา'), (145,145,45,145,300)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=40, stretch=True)
        self.tree.pack(fill='both', expand=True, pady=8)
        self.tree.bind('<<TreeviewSelect>>', self._selection)
        self.evidence = ttk.Combobox(self, state='readonly')
        self.evidence.pack(fill='x')
        self.summary = ttk.Label(self, text='ยังไม่มีสรุปการส่ง', wraplength=850)
        self.summary.pack(anchor='w', pady=5)
        self.status = ttk.Label(self, text='ยังไม่มีประวัติ', wraplength=850)
        self.status.pack(anchor='w')

    @property
    def is_running(self):
        return self.pending

    @property
    def busy(self):
        return self.pending

    def refresh(self):
        self.next_refresh = 0

    def _selection(self, event=None):
        selected = self.tree.selection()
        row = self.rows.get(selected[0]) if selected else None
        items = row.get('evidence', []) if row and row.get('row_type','session') == 'session' else []
        self.evidence.configure(values=[f"ภาพ {i+1} • {local_time(item.get('captured_at'))}" for i,item in enumerate(items)])
        self.evidence.set('')
        if items:
            self.evidence.current(0)
        elif row:
            self.status.configure(text='ไม่มีภาพหลักฐานที่เชื่อมโยง — ไม่ได้ยืนยันชนิดเงินหรือว่าเป็นเหรียญเท่านั้น')

    def show_image(self):
        if self.closed:
            return
        selected = self.tree.selection()
        index = self.evidence.current()
        row = self.rows.get(selected[0]) if selected else None
        if row and row.get('row_type','session') == 'session' and 0 <= index < len(row.get('evidence', [])):
            self.request = (row['id'], row['evidence'][index]['id'])
        else:
            self.status.configure(text='เลือกรายการและภาพหลักฐานก่อน — ไม่มีภาพไม่ใช่การยืนยันชนิดเงิน')

    def _render(self, result):
        selected = self.tree.selection()
        self.rows = {row['id']: row for row in result['rows']}
        self.tree.delete(*self.tree.get_children())
        for identifier, row in self.rows.items():
            self.tree.insert('', 'end', iid=identifier, values=session_values(row))
        if selected and selected[0] in self.rows:
            self.tree.selection_set(selected[0])
        self.status.configure(text='ล่าสุด 200 รายการ • เลือกรายการเพื่อดูภาพหลักฐาน')
        self._selection()
        counts = result['counts']
        if counts.get('awaiting_end_after_expiry'):
            self.status.configure(text='รอบยังไม่จบหมดอายุแล้ว — รอเสียงขอบคุณก่อนนับรอบใหม่ ไม่เก็บข้อมูลรอบเก่าไว้เกิน 3 วัน')
        if counts.get('awaiting_end_after_gap'):
            self.status.configure(text='หลักฐานเสียงขาดช่วง / ไม่ทราบเวลาจบ — แสดงประกาศที่ยืนยัน แต่ไม่จับคู่ข้ามช่วง')
        self.deletion_warning = bool(result['cleanup'].get('media_delete_failed', self.deletion_warning))
        reasons = ', '.join(f'{code} ×{count}' for code, count in counts.get('failed_reasons', {}).items())
        self.summary.configure(text=f"ส่งแล้ว {counts.get('delivered',0)} • รอส่ง {counts.get('queued',0)} • ล้มเหลว {counts.get('failed',0)}"
                               + (f' ({reasons} — ดูรายละเอียดในแท็บ Refueling API v1)' if reasons else '')
                               + f" • หมดอายุก่อนยืนยันการส่ง {counts.get('expired_before_delivery',0)}"
                               + (' • คำเตือน: ลบภาพหมดอายุไม่สำเร็จ จะลองใหม่' if self.deletion_warning else ''))
        if not self.rows:
            self.status.configure(text='ยังไม่มีประวัติรอบเติมหรือเสียงประกาศที่ยืนยัน')
        if result['image'] is not None:
            self._viewer(result['image'])

    def _viewer(self, data):
        if self.viewer is not None and self.viewer.winfo_exists():
            self.viewer.destroy()
        self.viewer = Toplevel(self)
        self.viewer.title('ภาพหลักฐานรอบเติม — ไม่ยืนยันจำนวนเงิน')
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert('RGB')
        image.thumbnail((1000, 700), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image, master=self.viewer)
        ttk.Label(self.viewer, image=self.photo).pack()

    def poll(self, visible=False):
        if self.closed:
            try:
                self.results.get_nowait()
                self.pending = False
            except queue.Empty:
                pass
            return
        config = self.get_config()  # Snapshot only on Tk thread.
        scope = (Path(config.data_dir).absolute() / 'refueling', config.camera_id)
        if scope != self.scope:
            self.scope = scope
            self.generation += 1
            self.rows = {}
            self.tree.delete(*self.tree.get_children())
            self.evidence.configure(values=())
            self.evidence.set('')
            self.request = None
            self.deletion_warning = False
            self.summary.configure(text='ยังไม่มีสรุปการส่ง')
            self.status.configure(text='กำลังอ่านประวัติรอบเติมจริง')
            if self.viewer is not None and self.viewer.winfo_exists():
                self.viewer.destroy()
            self.viewer = self.photo = None
            self.next_refresh = self.next_cleanup = 0
        try:
            generation, result, error = self.results.get_nowait()
            self.pending = False
            if generation == self.generation:
                if error:
                    self.status.configure(text='อ่านประวัติ/ภาพหรือลบข้อมูลหมดอายุไม่ได้ กรุณารีเฟรช ตรวจพื้นที่และสิทธิ์')
                else:
                    self._render(result)
        except queue.Empty:
            pass
        now = time.monotonic()
        cleanup = now >= self.next_cleanup
        if not self.pending and (cleanup or self.request is not None or (visible and now >= self.next_refresh)):
            request, self.request = self.request, None
            self.next_refresh = now + 3
            if cleanup:
                self.next_cleanup = now + 60
            self.pending = True
            threading.Thread(target=_run, args=(self.results, self.generation, *scope, cleanup, request),
                             daemon=True, name='SmartTapo-RefuelingHistory').start()

    def close(self):
        self.closed = True
        self.generation += 1
        self.request = None
        if self.viewer is not None and self.viewer.winfo_exists():
            self.viewer.destroy()
        self.viewer = self.photo = None
