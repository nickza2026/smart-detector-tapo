"""Opt-in, bounded non-PCM listener metadata. Never starts or polls a listener.

Single writer per data directory. Failures return False; snapshot()['io_error']
lets the UI disclose unavailable diagnostics without interrupting listening.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

# Frozen protocol codes from continuous_listener / desktop_listener. No GUI imports.
ERROR_CODES = frozenset(('profile_invalid live_check_failed lease_invalid lease_expired '
    'audio_unavailable audio_availability_failed device_changed invalid_device '
    'unsupported_stream_format audio_timeout storage_error stream_fault '
    'audio_clock_discontinuity audio_sequence_gap invalid_audio_chunk audio_short_read '
    'live_provenance_lost acoustic_analysis_failed acoustic_result_limit '
    'invalid_acoustic_result invalid_acoustic_span invalid_stream_configuration '
    'invalid_stream_profile profile_too_long_for_stream stream_already_failed '
    'unsupported_profile_span profile_changed live_check_unavailable initial_check_expired '
    'live_check_expired listener_start_failed initial_check_failed live_control_backlog '
    'live_proof_invalid live_playback_detected live_mode_unconfirmed '
    'live_live_refresh_required live_live_inactivity_confirmation live_navigation_changed '
    'live_camera_not_confirmed live_incomplete_live_layout live_unrecognized_foreground '
    'live_foreground_changed_or_unknown live_playback_or_conflicting_controls '
    'live_unsupported_package live_invalid_hierarchy live_sensitive_page '
    'live_incomplete_playback_layout close_failed stopped worker_failed listener_failed '
    'listener_failed invalid_stream_configuration').split())
PHASES = frozenset(('preparing', 'attempt_begin', 'attempt_end', 'receiving',
    'waiting_audio', 'awaiting_boundary', 'retry_scheduled', 'rechecking', 'error', 'stopped'))
KINDS = frozenset(('listener_progress', 'listener_retry', 'listener_rechecking',
    'error', 'finished', 'attempt_begin', 'attempt_end', 'state'))
COUNTERS = ('frames', 'observations', 'observations_total', 'ambiguous_candidates', 'ambiguous_total')
TIMINGS = ('check_seconds', 'lag_seconds', 'read_seconds', 'work_seconds',
           'max_work_seconds', 'audio_seconds', 'wall_seconds', 'rate', 'channels')
SCHEMA = 'listener_health_v1'


def _number(value, low=0, high=1e12):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _fingerprint(value):
    if type(value) is not str or not 0 < len(value) <= 4096:
        raise ValueError('invalid_identity')
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class ListenerHealthLog:
    def __init__(self, data_dir, *, clock=time.time, monotonic=time.monotonic,
                 max_bytes=2 * 1024 * 1024, backup_count=2):
        if type(max_bytes) is not int or not 256 <= max_bytes <= 2 * 1024 * 1024:
            raise ValueError('invalid_size')
        if type(backup_count) is not int or not 0 <= backup_count <= 2:
            raise ValueError('invalid_backups')
        self.root = Path(data_dir).absolute()
        self.path = self.root / 'logs' / 'listener-health.jsonl'
        self.run_id = uuid.uuid4().hex
        self.clock, self.monotonic = clock, monotonic
        self.max_bytes, self.backup_count = max_bytes, backup_count
        self._closed = self._io_error = False
        self._last = None
        self._progress_at = None
        self._written = 0

    def snapshot(self):
        return {'closed': self._closed, 'io_error': self._io_error,
                'records_written': self._written, 'run_id': self.run_id,
                'phase': self._last.get('phase') if self._last else None}

    def _safe_paths(self):
        # Check every ancestor too: neither junctions nor symlinked roots are owned.
        for path in (self.root, *self.root.parents, self.path.parent):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or
                        getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise OSError('unsafe_path')
        for index in range(self.backup_count + 1):
            path = self.path if index == 0 else self.path.with_name(self.path.name + '.' + str(index))
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > self.max_bytes):
                raise OSError('unsafe_file')
            with path.open('rb') as source:
                for line in source:
                    record = json.loads(line)
                    if (type(record) is not dict or record.get('schema') != SCHEMA or
                            type(record.get('run_id')) is not str or
                            not re.fullmatch('[0-9a-f]{32}', record['run_id'])):
                        raise ValueError('foreign_file')

    def record_whitelisted(self, event, *, phase=None, child_pid=None,
                           device_identity=None, profile_identity=None, gap_seconds=None):
        """Record a protocol dict; omit unavailable counters. False also means suppressed.

        Identities are explicit producer inputs, SHA256 hashed before persistence.
        Do not supply credentials even though original identities are never saved.
        """
        # Latch I/O failure: never hammer a full/unsafe disk from the UI poll.
        # An explicit new logger instance is required to retry logging.
        if self._closed or self._io_error:
            return False
        try:
            allowed = {'kind', 'code', 'heartbeat', 'waiting_audio', 'awaiting_boundary',
                       'retry_seconds', 'diagnostics', 'mode', *COUNTERS}
            if type(event) is not dict or set(event) - allowed or event.get('kind') not in KINDS:
                return False
            kind = event['kind']
            if phase is None:
                phase = {'listener_retry': 'retry_scheduled', 'listener_rechecking': 'rechecking',
                         'error': 'error', 'finished': 'stopped', 'state': 'preparing'}.get(kind, kind)
                if kind == 'listener_progress':
                    phase = ('waiting_audio' if event.get('waiting_audio') is True else
                             'awaiting_boundary' if event.get('awaiting_boundary') is True else 'receiving')
            if phase not in PHASES:
                return False
            row = {'kind': kind, 'phase': phase}
            for key in COUNTERS:
                if key in event:
                    if type(event[key]) is not int or not 0 <= event[key] <= 2**63 - 1:
                        return False
                    row[key] = event[key]
            for key in ('waiting_audio', 'awaiting_boundary'):
                if key in event:
                    if type(event[key]) is not bool:
                        return False
                    row[key] = event[key]
            if 'code' in event:
                code = event['code']
                row['code'] = code if type(code) is str and code in ERROR_CODES else 'unknown'
            if 'retry_seconds' in event:
                if not _number(event['retry_seconds'], high=30):
                    return False
                row['retry_seconds'] = event['retry_seconds']
            if 'diagnostics' in event:
                detail = event['diagnostics']
                if type(detail) is not dict or set(detail) - set(TIMINGS):
                    return False
                for key, value in detail.items():
                    if not _number(value, -86400 if key == 'lag_seconds' else 0, 192000):
                        return False
                row['diagnostics'] = dict(detail)
            if child_pid is not None:
                if type(child_pid) is not int or not 1 <= child_pid <= 2**32 - 1:
                    return False
                row['child_pid'] = child_pid
            if gap_seconds is not None:
                if not _number(gap_seconds, high=86400 * 365):
                    return False
                row['gap_seconds'] = gap_seconds
            for key, identity in (('device_fingerprint', device_identity), ('profile_fingerprint', profile_identity)):
                if identity is not None:
                    row[key] = _fingerprint(identity)
            now, mono = self.clock(), self.monotonic()
            if not _number(now, high=253402214399) or not _number(mono):
                return False
            transition = self._last is None or any(row.get(k) != self._last.get(k)
                for k in ('phase', 'kind', 'code', 'child_pid', 'device_fingerprint', 'profile_fingerprint'))
            if kind == 'listener_progress' and not transition:
                if self._progress_at is not None and mono < self._progress_at:
                    self._progress_at = mono  # reset rate limiter on monotonic rollback
                    return False
                if self._progress_at is not None and mono - self._progress_at < 5:
                    return False
            elif kind == 'state' and row == self._last:
                return False
            record = {'schema': SCHEMA, 'run_id': self.run_id, 'created': now,
                      'monotonic': mono, 'pid': os.getpid(), **row}
            line = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
            if len(line) > self.max_bytes:
                self._io_error = True
                return False
            self._safe_paths()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + len(line) > self.max_bytes:
                if self.backup_count:
                    oldest = self.path.with_name(self.path.name + '.' + str(self.backup_count))
                    oldest.unlink(missing_ok=True)
                    for index in range(self.backup_count - 1, 0, -1):
                        source = self.path.with_name(self.path.name + '.' + str(index))
                        if source.exists():
                            source.replace(self.path.with_name(self.path.name + '.' + str(index + 1)))
                    self.path.replace(self.path.with_name(self.path.name + '.1'))
                else:
                    self.path.unlink()
            with self.path.open('ab') as output:
                output.write(line)
                output.flush()
            self._written += 1
            self._last = row
            if kind == 'listener_progress':
                self._progress_at = mono
            return True
        except (OSError, ValueError, TypeError, OverflowError):
            self._io_error = True
            return False

    def close(self):
        self._closed = True
