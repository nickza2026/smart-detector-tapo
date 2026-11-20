"""Bounded local audit records from validated, credential-free protocol events.

Construct only after acquiring the role's ProcessLock. This is deliberately not
an application logging handler: arbitrary messages and exceptions do not belong
in this file.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
import errno
import pickle
from datetime import datetime
from pathlib import Path

from .diagnostics import ALIGNMENT_HINTS, DIAGNOSTIC_BUILD
from .history import HISTORY_TIMEZONE
from .capture.live_verifier import SAFE_REASONS

_KINDS = frozenset(('ready', 'stopped', 'failed', 'warning', 'frame_rejected', 'connection',
                    'screenshot', 'quota', 'view_changed', 'alignment',
                    'alignment_detail', 'detection', 'quality',
                    'notification_queued', 'notification_shadow',
                    'notification_success', 'notification_failed',
                    'notification_ambiguous', 'notification_deferred', 'frame_context',
                    'ui_unverified', 'ui_verified','ui_reason','frame_deferred',
                    'ui_recovery_started','ui_recovery_restored','ui_check','rtsp_status','runtime_failure',
                    'passage_loss','analysis_expired'))
_REASONS = frozenset(('ok', 'not_fresh', 'future', 'unhealthy', 'stale',
                      'gap', 'alignment', 'detector_error', 'unknown',
                      'navigation_recovery','navigation_unavailable','clock_epoch_changed'))
_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_UUID = re.compile(r'[0-9a-fA-F]{32}\Z')


def _number(value, minimum=0, maximum=1e12):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


_FAILURE_STAGES = frozenset(('configuration', 'process_lock', 'capture', 'worker',
                             'decoder_ipc', 'frame_publish', 'unknown'))
_FAILURE_KINDS = frozenset(('disk_full', 'permission', 'decoder_ipc', 'process_lock', 'unknown'))


def safe_runtime_failure(error, stage, *, recoverable=False):
    stage = stage if type(stage) is str and stage in _FAILURE_STAGES else 'unknown'
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
        kind = 'disk_full'
    elif isinstance(error, PermissionError) or (isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM)):
        kind = 'permission'
    elif stage == 'decoder_ipc' and (isinstance(error, (EOFError, pickle.UnpicklingError))
            or (isinstance(error, OSError) and error.errno in
                (None, errno.EPIPE, errno.EBADF, errno.ECONNRESET, errno.ECONNABORTED,
                 errno.ENOTCONN, errno.EINVAL, errno.EIO))):
        kind = 'decoder_ipc'
    elif stage == 'process_lock':
        kind = 'process_lock'
    else:
        kind = 'unknown'
    return dict(stage=stage, kind=kind, recoverable=bool(recoverable and kind == 'decoder_ipc'))


def valid_detail(kind, detail):
    if kind == 'runtime_failure':
        return (type(detail) is dict and set(detail) == {'stage', 'kind', 'recoverable'}
                and type(detail['stage']) is str and detail['stage'] in _FAILURE_STAGES
                and type(detail['kind']) is str and detail['kind'] in _FAILURE_KINDS
                and type(detail['recoverable']) is bool
                and (not detail['recoverable'] or
                     (detail['stage'] == 'decoder_ipc' and detail['kind'] == 'decoder_ipc')))
    if kind=='rtsp_status':
        return (type(detail) is dict and set(detail)=={'reason','session_id','decoded','dropped'}
            and type(detail['reason']) is str and detail['reason'] in
            {'calibration_required','reconnecting','geometry_changed','stale',
             'timestamp_unconfirmed','healthy','storage_limit','overflow','starting','waiting','stopped'}
            and (detail['session_id'] is None or (type(detail['session_id']) is str
                and len(detail['session_id'])<=64 and _ID.fullmatch(detail['session_id'])))
            and all(type(detail[k]) is int and 0<=detail[k]<2**63 for k in ('decoded','dropped')))
    if kind == 'passage_loss':
        return (type(detail) is dict and set(detail) == {'reason', 'since'}
                and type(detail['reason']) is str and detail['reason'] in ('expired', 'reconnect', 'worker_gap')
                and type(detail['since']) is float and _number(detail['since']))
    if kind == 'analysis_expired':
        return type(detail) is float and _number(detail, maximum=86400)
    if kind=='ui_check':
        from .capture.ui_diagnostics import valid_ui_check
        return valid_ui_check(detail)
    if kind=='ui_reason':return type(detail) is str and detail in SAFE_REASONS|{'unrecognized_reason'}
    if kind == 'detection':
        if type(detail) is not tuple or len(detail) != 5:
            return False
        label, count, threshold, live, reason = detail
        return (type(label) is str and label in ('PRESENT', 'EMPTY', 'UNKNOWN')
                and type(reason) is str and reason in _REASONS
                and all(type(v) is int and 0 <= v <= 2_147_483_647
                        for v in (count, threshold, live))
                and threshold > 0 and live <= count)
    if kind == 'quality':
        return (type(detail) is tuple and len(detail) == 4
                and all(_number(v) for v in detail)
                and detail[0] <= 255 and 0 < detail[1] <= 1e9
                and detail[2] <= 1 and 0 < detail[3] <= 1)
    if kind == 'alignment_detail':
        return type(detail) is str and detail in ALIGNMENT_HINTS
    if kind == 'frame_context':
        if type(detail) is not dict or not detail or len(detail) > 14:
            return False
        for key, value in detail.items():
            if type(key) is not str:
                return False
            if key in ('frame_id', 'session_id', 'previous_session_id'):
                if (type(value) is not str or not _ID.fullmatch(value)
                        or (key != 'frame_id' and len(value) > 64)):
                    return False
            elif key in ('sequence', 'previous_sequence', 'missed_slots'):
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    return False
            elif key in ('captured_at', 'captured_monotonic', 'previous_captured_at', 'previous_captured_monotonic'):
                if not _number(value):
                    return False
            elif key == 'event_id':
                if type(value) is not str or not _UUID.fullmatch(value):
                    return False
            elif key == 'fresh':
                if type(value) is not bool:
                    return False
            elif key == 'elapsed_seconds':
                if not _number(value, maximum=86400):
                    return False
            else:
                return False
        return True
    if kind == 'notification_deferred':
        return (type(detail) is dict and set(detail) == {'event_id', 'reason'}
                and type(detail['event_id']) is str and _UUID.fullmatch(detail['event_id'])
                and type(detail['reason']) is str
                and detail['reason'] in ('not_live', 'cooldown', 'retry_wait', 'in_flight'))
    if kind.startswith('notification_'):
        return (detail is None or (type(detail) is dict and len(detail) <= 2
                and all(type(k) is str and k in ('event_id', 'notification_id')
                        and type(v) is str and _UUID.fullmatch(v)
                        for k, v in detail.items())))
    return detail is None


class AuditLog:
    def __init__(self, data_dir, role, max_bytes=2 * 1024 * 1024, backup_count=4, scope=None):
        if type(role) is not str or role not in ('capture', 'worker'):
            raise ValueError('Invalid audit role')
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('Invalid audit size')
        if type(backup_count) is not int or not 0 <= backup_count <= 100:
            raise ValueError('Invalid audit backup count')
        if scope is not None and (type(scope) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', scope)):
            raise ValueError('Invalid audit scope')
        self.path = Path(data_dir) / 'logs' / (role + ('-' + scope if scope is not None else '') + '.jsonl')
        self.root = Path(data_dir).resolve()
        self.role = role
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.run_id = uuid.uuid4().hex
        self._stream = None
        self._closed = False

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def _rotate(self):
        self._close_stream()
        if self.backup_count:
            oldest = self.path.with_name(self.path.name + '.' + str(self.backup_count))
            oldest.unlink(missing_ok=True)
            for index in range(self.backup_count - 1, 0, -1):
                source = self.path.with_name(self.path.name + '.' + str(index))
                if source.exists():
                    source.replace(self.path.with_name(self.path.name + '.' + str(index + 1)))
            self.path.replace(self.path.with_name(self.path.name + '.1'))
        else:
            self.path.unlink(missing_ok=True)

    def write(self, kind, detail=None, created=None, text=None):
        # text is intentionally ignored, never serialized or interpolated.
        if (self._closed or type(kind) is not str or kind not in _KINDS
                or not valid_detail(kind, detail)):
            return False
        created = time.time() if created is None else created
        if not _number(created, minimum=-62135596800, maximum=253402214399):
            return False
        try:
            if (self.path.parent.is_symlink() or self.path.is_symlink()
                    or self.path.parent.resolve().parent != self.root):
                return False
            record = {'created': created,
                      'timestamp': datetime.fromtimestamp(created, HISTORY_TIMEZONE).isoformat(),
                      'run_id': self.run_id, 'pid': os.getpid(),
                      'role': self.role, 'kind': kind, 'build': DIAGNOSTIC_BUILD}
            if detail is not None:
                record['detail'] = detail
            line = (json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                    + '\n').encode('utf-8')
            if len(line) > self.max_bytes:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._stream is None:
                self._stream = self.path.open('ab')
            if self._stream.tell() + len(line) > self.max_bytes:
                self._rotate()
                self._stream = self.path.open('ab')
            self._stream.write(line)
            self._stream.flush()
            return True
        except (OSError, ValueError, OverflowError):
            try:
                self._close_stream()
            except OSError:
                pass
            return False

    def close(self):
        self._closed = True
        try:
            self._close_stream()
        except OSError:
            pass
