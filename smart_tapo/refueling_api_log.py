"""Bounded local record of Refueling API attempts: URL, HTTP status and response excerpt.

The bearer token only ever travels in a request header and is never recorded;
a token echoed back in a response body or error text is masked before storage.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import threading
import time

from .history import HISTORY_TIMEZONE

MAX_EXCERPT = 500
MAX_BYTES = 1_000_000


def excerpt(data, secret):
    """Printable, bounded and secret-masked text of a receiver body or error."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data[:MAX_EXCERPT * 4]).decode('utf-8', 'replace')
    text = ''.join(c if c.isprintable() else ' ' for c in str(data))
    if secret:
        text = text.replace(secret, '***')
    return text[:MAX_EXCERPT].strip()


class ApiAttemptLog:
    """Thread-safe: the sender thread writes, the Tk thread reads ``recent``."""
    def __init__(self, path, *, keep=50, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._recent = deque(maxlen=keep)
        self._lock = threading.Lock()

    def __call__(self, record):
        record = dict(record, at=self.clock())
        with self._lock:
            self._recent.append(record)
            try:
                if self.path.parent.is_symlink() or self.path.is_symlink():
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_name(self.path.name + '.1'))
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            except (OSError, ValueError):
                record['file_error'] = True

    def recent(self):
        with self._lock:
            return list(self._recent)


def describe(record):
    """One operator line, newest information first: what was sent where and what came back."""
    try:
        stamp = datetime.fromtimestamp(record['at'], HISTORY_TIMEZONE).strftime('%d/%m/%Y %H:%M:%S')
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        stamp = '?'
    if record.get('url'):
        target = f"{record.get('method')} {record['url']}"
        answer = (f"HTTP {record['http_status']}" if record.get('http_status') is not None
                  else 'ไม่ได้รับคำตอบ HTTP')
    else:
        target, answer = 'ไม่ได้ยิง API', 'ตรวจในเครื่องไม่ผ่าน'
    parts = [f'[{stamp}] {target} → {answer}', f"ผล {record.get('status')}/{record.get('error_code') or '-'}"]
    if record.get('elapsed_ms') is not None:
        parts.append(f"{record['elapsed_ms']} ms")
    if record.get('error_detail'):
        parts.append('ข้อผิดพลาด: ' + record['error_detail'])
    if record.get('response'):
        parts.append('API ตอบ: ' + record['response'])
    if record.get('file_error'):
        parts.append('(เขียนไฟล์ log ไม่ได้)')
    return ' • '.join(parts)
