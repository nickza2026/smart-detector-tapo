"""Bounded, numeric-only capture timing diagnostics; not evidence of live video."""
from __future__ import annotations

import json
import math
import os
import stat
import time


_FIELDS = {'target_interval_seconds', 'last_interval_seconds', 'cycle_seconds',
           'frames', 'fresh_frames', 'skipped_slots', 'captured_at'}
_UNAVAILABLE = 'ยังไม่มีข้อมูลจังหวะรับภาพ'


def describe_capture(config, now=None):
    """Read a small fixed snapshot, returning only fixed text and validated numbers."""
    try:
        path = config.data_dir / 'capture_status.json'
        if path.is_symlink():
            return _UNAVAILABLE
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                     getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return _UNAVAILABLE
            raw = source.read(4097)
        if len(raw) > 4096:
            return _UNAVAILABLE
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != _FIELDS:
            return _UNAVAILABLE
        for key, value in data.items():
            if key == 'last_interval_seconds' and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e15:
                return _UNAVAILABLE
        for key in ('frames', 'fresh_frames', 'skipped_slots'):
            if type(data[key]) is not int:
                return _UNAVAILABLE
        if data['target_interval_seconds'] <= 0 or data['fresh_frames'] > data['frames']:
            return _UNAVAILABLE
        current = time.time() if now is None else now
        stale = not 0 <= current-data['captured_at'] <= config.max_live_age_seconds
        last = data['last_interval_seconds']
        actual = '—' if last is None else f'{last:.3f}s'
        return (f"เป้าหมาย {data['target_interval_seconds']:.3f}s / จริงล่าสุด {actual} / "
                f"รอบ {data['cycle_seconds']:.3f}s / พิกเซลนาฬิกาเปลี่ยน "
                f"{data['fresh_frames']}/{data['frames']} / ข้าม {data['skipped_slots']}"
                + (' / ข้อมูลเก่า' if stale else '')
                + ' (ไม่ยืนยันภาพสด)')
    except (OSError, ValueError, TypeError, OverflowError, AttributeError):
        return _UNAVAILABLE
