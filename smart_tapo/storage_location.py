"""Explicit-Save storage admission. Never adopt or migrate another data set."""
import os
from pathlib import Path, PureWindowsPath
import secrets
import stat


def normalize_location(value, config_root):
    """Lexical only: safe for Tk candidate comparisons, no filesystem metadata."""
    text = str(value).strip()
    if not text:
        raise ValueError('กรุณาเลือกโฟลเดอร์ข้อมูลเฉพาะ ไม่ใช้ค่าว่าง')
    win = PureWindowsPath(text)
    if win.drive and not win.root:
        raise ValueError('กรุณาระบุไดรฟ์และโฟลเดอร์แบบเต็ม')
    path = Path(text)
    if not path.is_absolute():
        path = Path(config_root) / path
    return Path(os.path.abspath(path))


def location_identity(value):
    return os.path.normcase(os.path.abspath(value))


def _canonical(path):
    # Windows 8.3 aliases (C:\Users\RUNNER~1) must not defeat overlap checks.
    return Path(os.path.realpath(path))


def _check_ancestry(path):
    # Do this BEFORE resolve/abspath: link/../destination must not hide a link.
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (hasattr(part, 'is_junction') and part.is_junction())):
            raise ValueError('โฟลเดอร์ข้อมูลและโฟลเดอร์แม่ต้องไม่เป็นลิงก์หรือ junction')
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('ตำแหน่งข้อมูลต้องเป็นโฟลเดอร์')


def preflight_location(value, config_root, current, baseline_path=None):
    """Save-only validation + exclusive owned write probe; never opens a database."""
    text = str(value).strip()
    target = normalize_location(text, config_root)
    raw = Path(text)
    if not raw.is_absolute():
        raw = Path(config_root) / raw
    _check_ancestry(raw)
    if target == Path(target.anchor):
        raise ValueError('เลือกโฟลเดอร์เฉพาะ ไม่ใช้รากไดรฟ์')
    canonical = _canonical(target)
    root = _canonical(normalize_location(config_root, config_root))
    old = _canonical(normalize_location(current, config_root))
    changed = location_identity(canonical) != location_identity(old)
    if changed:
        if canonical == root or canonical in root.parents or old in canonical.parents or canonical in old.parents:
            raise ValueError('ใช้โฟลเดอร์ข้อมูลแยกจากโฟลเดอร์เดิมและโฟลเดอร์ตั้งค่า')
        if baseline_path is not None:
            calibration = _canonical(normalize_location(baseline_path, config_root).parent)
            if (canonical == calibration or canonical in calibration.parents
                    or (calibration != root and calibration in canonical.parents)):
                raise ValueError('ไม่ใช้โฟลเดอร์ภาพอ้างอิงเป็นโฟลเดอร์ข้อมูล')
        if target.exists() and next(target.iterdir(), None) is not None:
            raise ValueError('โฟลเดอร์ใหม่ต้องว่าง ไม่รับฐานข้อมูลหรือประวัติเดิมโดยอัตโนมัติ')
    target.mkdir(parents=True, exist_ok=True)
    _check_ancestry(raw)
    # O_EXCL means cleanup can only remove the file this call created.
    probe = target / ('.smart-tapo-write-' + secrets.token_hex(16))
    descriptor = None
    created = False
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        created = True
        payload=b'Smart Tapo storage write probe\n'
        if os.write(descriptor, payload) != len(payload):
            raise OSError('Incomplete storage write probe')
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            probe.unlink()
    return target
