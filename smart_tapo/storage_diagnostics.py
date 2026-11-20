"""Allowlisted local storage diagnostics without exception text or paths.

Recovery codes permit a bounded reopen attempt, not destructive repair. They
are categories that *may* recover after external conditions change; they do not
promise that any particular failure is transient.
"""
from __future__ import annotations

import errno
import sqlite3
import stat
import sys


class StoragePathUnsafeError(ValueError):
    """A store path violates the existing link/root safety policy."""

    def __init__(self, *args, guard=None):
        super().__init__(*args)
        self.guard = validate_guard_evidence(guard)


class StorageMetadataBusyError(StoragePathUnsafeError):
    """An ephemeral zero-link leaf is NOT safe to use; defer this operation."""


_GUARD_REASONS = frozenset({'linked_path', 'non_single_link', 'filesystem_root'})
_GUARD_COMPONENTS = frozenset({'path', 'directory', 'source_root', 'media',
                               'database', 'journal', 'wal', 'shm'})
_GUARD_FIELDS = frozenset({'reason', 'component', 'depth', 'mode', 'links',
                          'attributes', 'reparse_tag'})


def validate_guard_evidence(value):
    """Copy only bounded enums/integers, never paths or exception text."""
    if type(value) is not dict or set(value) != _GUARD_FIELDS:
        return None
    if (type(value['reason']) is not str or value['reason'] not in _GUARD_REASONS
            or type(value['component']) is not str or value['component'] not in _GUARD_COMPONENTS
            or type(value['depth']) is not int or not 0 <= value['depth'] <= 512):
        return None
    for key in ('mode', 'links', 'attributes', 'reparse_tag'):
        item = value[key]
        lower = -1 if key == 'links' else 0
        if item is not None and (type(item) is not int or not lower <= item <= 0xffffffff):
            return None
    return dict(value)


def guard_evidence(reason, component, depth, info=None):
    """Capture the rejecting lstat result itself; do not query a changing file again."""
    return validate_guard_evidence(dict(reason=reason, component=component, depth=depth,
        mode=getattr(info, 'st_mode', None), links=getattr(info, 'st_nlink', None),
        attributes=getattr(info, 'st_file_attributes', None),
        reparse_tag=getattr(info, 'st_reparse_tag', None)))


def format_guard_evidence(value):
    evidence = validate_guard_evidence(value)
    if evidence is None:
        return ''
    def number(key, hexadecimal=False):
        item = evidence[key]
        return '?' if item is None else (hex(item) if hexadecimal else str(item))
    return (f"Storage guard: {evidence['reason']} | component={evidence['component']} "
            f"depth={evidence['depth']} | mode={number('mode', True)} "
            f"links={number('links')} attrs={number('attributes', True)} "
            f"tag={number('reparse_tag', True)} | "
            f"python={'.'.join(map(str, sys.version_info[:3]))} "
            f"sqlite={'.'.join(map(str, sqlite3.sqlite_version_info))}")


class StorageCameraMismatchError(ValueError):
    """An existing store belongs to a different camera."""


class StorageSchemaError(ValueError):
    """The store has a deterministically unsupported schema."""


STORAGE_RECOVERY_CODES = frozenset({
    'storage_busy', 'storage_full', 'storage_permission', 'storage_io_error',
})
_STORAGE_STAGES = frozenset({
    'storage_open', 'storage_gap', 'storage_commit', 'storage_cleanup', 'storage_close',
})
_SQLITE_CODES = {
    sqlite3.SQLITE_BUSY: 'storage_busy',
    sqlite3.SQLITE_LOCKED: 'storage_busy',
    sqlite3.SQLITE_FULL: 'storage_full',
    sqlite3.SQLITE_READONLY: 'storage_permission',
    sqlite3.SQLITE_PERM: 'storage_permission',
    sqlite3.SQLITE_AUTH: 'storage_permission',
    sqlite3.SQLITE_CORRUPT: 'storage_corrupt',
    sqlite3.SQLITE_NOTADB: 'storage_corrupt',
    sqlite3.SQLITE_SCHEMA: 'storage_schema',
    sqlite3.SQLITE_IOERR: 'storage_io_error',
}
_OS_CODES = {
    errno.ENOSPC: 'storage_full',
    errno.EACCES: 'storage_permission',
    errno.EPERM: 'storage_permission',
    errno.EROFS: 'storage_permission',
    errno.EBUSY: 'storage_busy',
    errno.EAGAIN: 'storage_busy',
    errno.EIO: 'storage_io_error',
}
_WINDOWS_CODES = {
    5: 'storage_permission',       # ERROR_ACCESS_DENIED
    19: 'storage_permission',      # ERROR_WRITE_PROTECT
    32: 'storage_busy',            # ERROR_SHARING_VIOLATION
    33: 'storage_busy',            # ERROR_LOCK_VIOLATION
    39: 'storage_full',            # ERROR_HANDLE_DISK_FULL
    112: 'storage_full',           # ERROR_DISK_FULL
    1117: 'storage_io_error',      # ERROR_IO_DEVICE
}


def classify_storage_error(exc: BaseException) -> str:
    """Classify structured metadata only; never parse untrusted error messages.

    SQLITE_ERROR is deliberately unknown: it can mean SQL bugs, missing columns,
    or other failures and must not be universally labelled schema corruption.
    """
    if isinstance(exc, StorageMetadataBusyError):
        evidence = validate_guard_evidence(getattr(exc, 'guard', None))
        if (evidence is not None and evidence['reason'] == 'non_single_link'
                and evidence['component'] in ('journal', 'wal', 'shm')
                and evidence['depth'] == 0 and evidence['links'] == 0
                and evidence['mode'] is not None and stat.S_ISREG(evidence['mode'])
                and evidence['attributes'] is not None and not evidence['attributes'] & 0x400
                and evidence['reparse_tag'] in (None, 0)):
            return 'storage_busy'
        return 'storage_path_unsafe'
    if isinstance(exc, StoragePathUnsafeError):
        return 'storage_path_unsafe'
    if isinstance(exc, StorageCameraMismatchError):
        return 'storage_camera_mismatch'
    if isinstance(exc, StorageSchemaError):
        return 'storage_schema'
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', None)
        if isinstance(code, int):
            return _SQLITE_CODES.get(code & 0xff, 'storage_error')
        return 'storage_error'
    if isinstance(exc, OSError):
        windows = getattr(exc, 'winerror', None)
        if windows in _WINDOWS_CODES:
            return _WINDOWS_CODES[windows]
        if exc.errno in _OS_CODES:
            return _OS_CODES[exc.errno]
        if isinstance(exc, PermissionError):
            return 'storage_permission'
        if isinstance(exc, BlockingIOError):
            return 'storage_busy'
    return 'storage_error'


def storage_failure_event(exc: BaseException, stage: str) -> dict:
    """Return only safe constants; reject invalid stages rather than echo them."""
    if not isinstance(stage, str) or stage not in _STORAGE_STAGES:
        raise ValueError('Invalid storage failure stage')
    event = {'code': classify_storage_error(exc), 'stage': stage}
    if isinstance(exc, StoragePathUnsafeError):
        evidence = validate_guard_evidence(getattr(exc, 'guard', None))
        if evidence is not None:
            event['guard'] = evidence
    return event
