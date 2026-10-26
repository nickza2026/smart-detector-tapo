"""Bounded read-only event history shared by the desktop and loopback web UI."""
from __future__ import annotations

import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class HistoryUnavailable(RuntimeError):
    """Operator-safe error, without SQL, local paths or arbitrary stored values."""


@dataclass(frozen=True)
class HistoryEvent:
    id: str
    kind: str
    started: float
    last_present: float | None
    first_clear: float | None
    closed: float | None
    image_count: int
    emergency_state: str = ''


@dataclass(frozen=True)
class HistoryPage:
    events: tuple[HistoryEvent, ...]
    next_before: tuple[float, str] | None = None


@dataclass(frozen=True)
class HistoryImage:
    role: str
    path: Path


IMAGE_ROLES = ('first_full', 'first', 'threshold', 'clear')
KIND_LABELS = {'candidate': 'กำลังพบวัตถุ', 'normal_visual': 'ปกติ — วัตถุผ่านแล้ว',
               'stuck': 'ค้าง — ถึงเกณฑ์', 'incomplete': 'ข้อมูลไม่ครบ / ยืนยันไม่ได้'}
IMAGE_LABELS = {'first_full': 'ภาพเต็มเมื่อเริ่มพบ', 'first': 'กรอบตรวจแรก (จัดแนวแล้ว)',
                'threshold': 'กรอบเมื่อถึงเกณฑ์ค้าง', 'clear': 'กรอบว่างเมื่อจบเหตุการณ์'}
EMERGENCY_LABELS = {'sending':'Emergency: กำลังส่ง ยังไม่ยืนยัน',
    'active':'Emergency: รอรับทราบ',
    'acknowledged':'Emergency: รับทราบแล้ว (ไม่ใช่การยืนยันว่าแบงค์ออก)',
    'expired':'Emergency: หมดอายุ ไม่ถือว่ารับทราบ',
    'cancel_pending':'Emergency: ยังไม่ยืนยันยกเลิก อาจเตือนต่อ',
    'cancelled':'Emergency: ยกเลิกแล้ว',
    'ambiguous':'Emergency: ผลส่งไม่แน่ชัด ระงับส่งใหม่เพื่อป้องกันซ้ำ',
    'failed':'Emergency: ส่งไม่สำเร็จ', 'shadow':'Emergency: ทดสอบ ไม่ส่งจริง'}


def _emergency_column(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='emergency_notifications'").fetchone():
        return '(SELECT n.state FROM emergency_notifications n WHERE n.event=e.id ORDER BY n.created DESC,n.id DESC LIMIT 1) AS emergency_state, '
    return "'' AS emergency_state, "


def _event(row):
    values=dict(row)
    if values.get('emergency_state') not in EMERGENCY_LABELS:values['emergency_state']=''
    return HistoryEvent(**values)


def _eligible(config, now):
    # A normal passage gets a full TTL AFTER closure. Other event types keep
    # their existing worker retention; this reader never deletes any data.
    hours = getattr(config, 'deposit_history_hours', 24.0)
    return now - hours * 3600


@contextmanager
def _database(config):
    path = config.data_dir / 'worker.sqlite3'
    if not path.exists():
        yield None
        return
    if path.is_symlink() or config.data_dir.is_symlink():
        raise HistoryUnavailable('ไม่เปิดประวัติผ่านลิงก์ไฟล์ข้อมูล')
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        owner = db.execute("SELECT value FROM settings WHERE name='camera_id'").fetchone()
        if not owner or owner['value'] != config.camera_id:
            raise HistoryUnavailable('ฐานประวัติไม่ตรงกับรหัสกล้องที่ตั้งไว้')
        yield db
    except sqlite3.Error:
        raise HistoryUnavailable('อ่านประวัติไม่ได้ชั่วคราว กรุณารีเฟรชหลัง Worker เริ่มพร้อมแล้ว') from None
    finally:
        if db is not None:
            db.close()


def list_events(config, before=None, limit=100, now=None):
    """Newest event start first; use opaque tuple cursor, not shifting offsets."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('History limit must be between 1 and 100')
    if before is not None:
        if (not isinstance(before, (tuple, list)) or len(before) != 2
                or type(before[0]) not in (int, float) or not math.isfinite(before[0])
                or not isinstance(before[1], str) or len(before[1]) > 128):
            raise ValueError('Invalid history cursor')
    now = time.time() if now is None else now
    with _database(config) as db:
        if db is None:
            return HistoryPage(())
        where = "(e.kind != 'normal_visual' OR e.closed >= ?)"
        values = [_eligible(config, now)]
        if before is not None:
            where += ' AND (e.started < ? OR (e.started = ? AND e.id < ?))'
            values.extend((before[0], before[0], before[1]))
        values.append(limit + 1)
        rows = db.execute('SELECT e.id,e.kind,e.started,e.last_present,e.first_clear,e.closed,'+_emergency_column(db)+
                          '(SELECT count(*) FROM evidence v WHERE v.event=e.id) AS image_count '
                          'FROM events e WHERE ' + where + ' ORDER BY e.started DESC,e.id DESC LIMIT ?', values).fetchall()
        events = tuple(_event(row) for row in rows[:limit])
        cursor = (events[-1].started, events[-1].id) if len(rows) > limit else None
        return HistoryPage(events, cursor)


def get_event(config, event_id, now=None):
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        return None
    now = time.time() if now is None else now
    with _database(config) as db:
        if db is None:
            return None
        row = db.execute('SELECT e.id,e.kind,e.started,e.last_present,e.first_clear,e.closed,'+_emergency_column(db)+
                         '(SELECT count(*) FROM evidence v WHERE v.event=e.id) AS image_count '
                         "FROM events e WHERE e.id=? AND (e.kind!='normal_visual' OR e.closed>=?)",
                         (event_id, _eligible(config, now))).fetchone()
        return _event(row) if row else None


def _safe_image(config, event_id, role, value):
    if role not in IMAGE_ROLES or not isinstance(value, str):
        return None
    root = config.data_dir / 'events' / config.camera_id
    path = Path(value)
    if not path.is_absolute():
        # Worker paths are absolute for desktop profiles. Do not guess a cwd.
        return None
    base = config.data_dir.absolute()
    if (path.name != event_id + '-' + role + '.png' or path.is_symlink()
            or any(p.is_symlink() for p in (root, *root.parents) if p == base or base in p.parents)
            or path.resolve().parent != root.resolve() or not path.is_file()):
        return None
    return path


def get_images(config, event_id, now=None):
    """Return only existing event-owned PNGs; recheck on every open request."""
    now = time.time() if now is None else now
    if get_event(config, event_id, now) is None:
        return ()
    with _database(config) as db:
        if db is None:
            return ()
        rows = db.execute('SELECT role,path FROM evidence WHERE event=?', (event_id,)).fetchall()
        found = {}
        for row in rows:
            path = _safe_image(config, event_id, row['role'], row['path'])
            if path is not None:
                found[row['role']] = HistoryImage(row['role'], path)
        return tuple(found[role] for role in IMAGE_ROLES if role in found)
