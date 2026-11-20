"""Stopped-desktop storage planning; never allocates image storage or deletes files."""
from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
import shutil
import stat
from PIL import Image

MB = 1_000_000
MAX_SAMPLES = 64
MAX_ENTRIES = 100_000


@lru_cache(maxsize=8)
def _capture_reference_bytes(path, version, expected_size):
    """One local encode per reference version, never in candidate/active polls.

    Saving calibration through Pillow can compress much smaller than the
    capture encoder. File length alone would under-budget the first run.
    Cache only a byte count, never pixels; never opens a stream.
    """
    try:
        with Path(path).open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':return None
        import cv2
        import numpy as np
        with Image.open(path) as image:
            if image.size!=expected_size or image.width*image.height>16_777_216:return None
            pixels=cv2.cvtColor(np.asarray(image.convert('RGB')),cv2.COLOR_RGB2BGR)
            ok,encoded=cv2.imencode('.png',pixels)
        return len(encoded) if ok else None
    except (OSError,ValueError,Image.DecompressionBombError):
        return None


@dataclass(frozen=True)
class StorageBudget:
    recommended_mb: int | None
    frames: int
    png_bytes: int | None
    existing_bytes: int = 0


def _regular_size(path):
    # Reject linked ancestors too (including Windows junctions).
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            return None
    try:
        info = path.stat(follow_symlinks=False)
        return info.st_size if stat.S_ISREG(info.st_mode) and info.st_size > 0 else None
    except FileNotFoundError:
        return None


def estimate_storage(config):
    values = (config.capture_interval_seconds, config.rolling_frames_minutes,
              config.cleanup_interval_seconds, config.max_image_storage_mb)
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('ช่วงรับภาพ ระยะเก็บภาพ และโควตาต้องเป็นจำนวนมากกว่า 0')
    frames = math.ceil((config.rolling_frames_minutes*60 + config.cleanup_interval_seconds)
                       / config.capture_interval_seconds) + 1
    samples = []
    baseline = _regular_size(Path(config.baseline_path))
    if baseline:
        samples.append(baseline)
        info=Path(config.baseline_path).stat(follow_symlinks=False)
        version=(info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_ino)
        encoded=_capture_reference_bytes(str(config.baseline_path),version,tuple(config.expected_size))
        if encoded:samples.append(encoded)
    existing = protected = entries = 0
    recent = []
    for name in ('frames', 'events', 'pending'):
        root = Path(config.data_dir)/name
        if not root.exists(): continue
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (root, *root.parents)):
            raise ValueError('ประเมินพื้นที่ไม่ได้: โฟลเดอร์ภาพเป็นลิงก์')
        stack = [root]
        while stack:
            with os.scandir(stack.pop()) as items:
                for item in items:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        raise ValueError('ภาพมีจำนวนมากเกินขอบเขตประเมินพื้นที่ กรุณาตรวจพื้นที่ก่อนบันทึก')
                    if item.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(item.path)):
                        continue
                    if item.is_dir(follow_symlinks=False):
                        stack.append(Path(item.path))
                    elif item.is_file(follow_symlinks=False) and Path(item.name).suffix.lower() in ('.png', '.jpg', '.tmp'):
                        info = item.stat(follow_symlinks=False)
                        existing += info.st_size
                        if name != 'frames': protected += info.st_size
                        elif item.name.lower().endswith('.png') and info.st_size > 0:
                            recent.append((info.st_mtime_ns, info.st_size))
                            recent.sort(reverse=True)
                            del recent[MAX_SAMPLES:]
    samples.extend(size for _, size in recent)
    if not samples: return StorageBudget(None, frames, None, existing)
    size = max(samples)
    recommended = math.ceil((frames*size*1.25 + protected)/MB)
    return StorageBudget(recommended, frames, size, existing)


def apply_storage_budget(config):
    """Raise the configured floor only after explicit Save/start; check free disk first."""
    budget = estimate_storage(config)
    if budget.recommended_mb is None: return config, budget
    quota = max(config.max_image_storage_mb, budget.recommended_mb)
    target = Path(config.data_dir)
    while not target.exists(): target = target.parent
    free = shutil.disk_usage(target).free
    reserve = max(1_000_000_000, math.ceil(free*.10))
    additional = max(0, quota*MB-budget.existing_bytes)
    if additional + reserve > free:
        raise ValueError('พื้นที่ดิสก์ไม่พอสำหรับช่วงรับภาพและระยะเก็บที่เลือก (รวมพื้นที่สำรอง) กรุณาเพิ่มพื้นที่ก่อนบันทึก; ไม่ลดระยะเก็บอัตโนมัติ')
    from dataclasses import replace
    return replace(config, max_image_storage_mb=quota), budget


@dataclass(frozen=True)
class EventStorageBudget:
    quota_mb: float


def event_storage_budget(config):
    quota = config.max_image_storage_mb
    if not math.isfinite(quota) or quota <= 0:
        raise ValueError('โควตาภาพหลักฐานต้องเป็นจำนวนมากกว่า 0')
    return EventStorageBudget(quota)


def _stored_image_bytes(config):
    """Save-only metadata accounting, bounded and without following links."""
    total=entries=0
    for name in ('frames','events','pending'):
        root=Path(config.data_dir)/name
        if not root.exists():continue
        if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())
               for p in (root,*root.parents)):
            raise ValueError('ประเมินพื้นที่ไม่ได้: โฟลเดอร์ภาพเป็นลิงก์')
        stack=[root]
        while stack:
            with os.scandir(stack.pop()) as items:
                for item in items:
                    entries+=1
                    if entries>MAX_ENTRIES:raise ValueError('ภาพมีจำนวนมากเกินขอบเขตประเมินพื้นที่')
                    if item.is_symlink() or (hasattr(os.path,'isjunction') and os.path.isjunction(item.path)):continue
                    if item.is_dir(follow_symlinks=False):stack.append(Path(item.path))
                    elif item.is_file(follow_symlinks=False) and Path(item.name).suffix.lower() in ('.png','.jpg','.tmp'):
                        total+=item.stat(follow_symlinks=False).st_size
    return total


def apply_event_storage_budget(config):
    """RAM video persists evidence only; never expand quota from frame rate."""
    budget = event_storage_budget(config)
    target = Path(config.data_dir)
    while not target.exists():
        target = target.parent
    free = shutil.disk_usage(target).free
    reserve = max(1_000_000_000, math.ceil(free*.10))
    additional=max(0,budget.quota_mb*MB-_stored_image_bytes(config))
    if additional + reserve > free:
        raise ValueError('พื้นที่ดิสก์ไม่พอสำหรับโควตาภาพหลักฐานและพื้นที่สำรอง กรุณาลดโควตาหรือเลือกไดรฟ์ใหม่')
    return config, budget


def budget_text(budget):
    if isinstance(budget, EventStorageBudget):
        return (f'ภาพสดผ่าน RAM แบบจำกัด ไม่เก็บภาพดิบทุกเฟรม/ไม่สำรองภาพ 5 นาทีบนดิสก์ • '
                f'บันทึกเฉพาะเหตุการณ์/หลักฐาน โควตาภาพ {budget.quota_mb:,.0f} MB (ไม่ยืนยันรับเงิน)')
    if budget.recommended_mb is None:
        return 'พื้นที่ภาพอัตโนมัติ: ยังไม่ทราบขนาด PNG — รอภาพอ้างอิง แล้วบันทึก/เริ่มใหม่เพื่อคำนวณ'
    return f'พื้นที่ภาพประมาณ {budget.recommended_mb:,} MB ({budget.frames:,} ภาพ รวมรอบล้างและเผื่อ 25%) • เพิ่มโควตาเมื่อบันทึก/เริ่มเท่านั้น ไม่จองพื้นที่'
