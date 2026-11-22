"""Explicit, private, offline schema-1 announcement profiles; never discovered.

Example (replace hashes with SHA-256 of operator-reviewed PCM16 WAV files)::

    {"schema_version": 1, "profile_id": "pump_a", "camera_id": "camera_a",
     "templates": [
       {"kind": "paid_ready", "file": "ready.wav", "sha256": "<64 hex digits>",
        "start": 0.2, "end": 2.2, "threshold": 0.85},
       {"kind": "end", "file": "end.wav", "sha256": "<64 hex digits>",
        "start": 0.2, "end": 2.2, "threshold": 0.85}]}

Boundaries and thresholds require operator confirmation. Acceptance is NOT
measured classification accuracy. No originals, .tools artifacts, or alternate
profiles are searched or promoted. Errors deliberately omit private inputs.
An explicit optional template field ``temporal_threshold`` in (0, 1] adds
band-centered temporal-order evidence. It is fingerprint-bound and preserved
by private import. Absent means legacy behavior; older readers fail closed.
Optional root ``feature_version`` is strictly integer 1 or 2, independent of
schema_version=1. Absent keeps the original features and canonical fingerprint;
2 selects frame-local spectral contrast for every template and analyzed window.
Optional paid_ready-only ``alternates`` contains exactly one {start, end} view
of the same pinned WAV, with the same verified width and inherited evidence
thresholds/version. It changes the fingerprint, never adds an announcement kind.
Absent retains the legacy canonical bytes and matching behavior.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat

import numpy as np

from .announcement import PhraseTemplate, read_wav, _feature_version
from .audio import lock_directory, unlock_directory


@dataclass(frozen=True)
class AnnouncementProfile:
    id: str
    camera_id: str
    templates: tuple[PhraseTemplate, ...]
    fingerprint: str

    @property
    def feature_version(self):
        return self.templates[0].feature_version if self.templates else 1


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _regular(path, limit):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or getattr(info, 'st_nlink', 1) != 1 or not 0 < info.st_size <= limit):
        raise ValueError
    return info


@contextmanager
def _pinned_file(path, limit):
    """Deny Windows writes/deletes while hashing and the bounded decoder read."""
    _regular(path, limit)
    handle = None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ValueError
    try:
        before = _regular(path, limit)
        yield
        after = _regular(path, limit)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError
    finally:
        if handle is not None:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(handle)


def _hash(path):
    digest = hashlib.sha256()
    total = 0
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            total += len(chunk)
            if total > 64 * 1024 * 1024:
                raise ValueError
            digest.update(chunk)
    return digest.hexdigest()


def load_announcement_profile(path, expected_camera_id) -> AnnouncementProfile:
    """Load only this explicit manifest, failing closed with a constant ValueError."""
    try:
        return _load(path, expected_camera_id)
    except Exception:
        raise ValueError('invalid_announcement_profile') from None


def _load(path, expected_camera_id):
    if not _identifier(expected_camera_id):
        raise ValueError
    path = Path(os.path.abspath(path))
    with ExitStack() as stack:
        handles = lock_directory(path.parent, check_contents=False)
        stack.callback(unlock_directory, handles)
        stack.enter_context(_pinned_file(path, 32 * 1024))
        with path.open('rb') as source:
            raw = source.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            raise ValueError
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
        fields = {'schema_version', 'profile_id', 'camera_id', 'templates'}
        if (type(data) is not dict or set(data) not in (fields, fields | {'feature_version'})
                or type(data['schema_version']) is not int or data['schema_version'] != 1
                or not _identifier(data['profile_id']) or not _identifier(data['camera_id'])
                or data['camera_id'] != expected_camera_id
                or type(data['templates']) is not list or len(data['templates']) != 2):
            raise ValueError
        feature_version = _feature_version(data.get('feature_version', 1))
        templates = []
        hashes = []
        kinds = set()
        for entry in data['templates']:
            fields={'kind', 'file', 'sha256', 'start', 'end', 'threshold'}
            if (type(entry) is not dict or not fields<=set(entry)
                    or set(entry)-fields-{'temporal_threshold','alternates'}):
                raise ValueError
            kind = entry['kind']
            if type(kind) is not str or kind not in ('paid_ready', 'end') or kind in kinds:
                raise ValueError
            kinds.add(kind)
            name = entry['file']
            if (type(name) is not str or not 1 <= len(name) <= 240 or '\\' in name
                    or ':' in name or PureWindowsPath(name).is_absolute()
                    or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/'))
                    or any(p.endswith((' ', '.')) for p in name.split('/'))
                    or Path(name).suffix.lower() != '.wav'):
                raise ValueError
            if type(entry['sha256']) is not str or re.fullmatch('[0-9a-f]{64}', entry['sha256']) is None:
                raise ValueError
            for key in ('start', 'end', 'threshold'):
                if type(entry[key]) not in (int, float) or not math.isfinite(entry[key]):
                    raise ValueError
            start, end, threshold = entry['start'], entry['end'], entry['threshold']
            temporal=entry.get('temporal_threshold')
            if 'temporal_threshold' in entry and (type(temporal) not in (float,int)
                    or not math.isfinite(temporal) or not 0<temporal<=1):
                raise ValueError
            if not (0 <= start < end <= 20 and .8 <= end - start <= 10 and .5 <= threshold <= 1):
                raise ValueError
            alternate_spans=[]
            if 'alternates' in entry:
                alternates=entry['alternates']
                if kind!='paid_ready' or type(alternates) is not list or len(alternates)!=1:
                    raise ValueError
                for alternate in alternates:
                    if type(alternate) is not dict or set(alternate)!={'start','end'}:
                        raise ValueError
                    alternate_spans.append((alternate['start'],alternate['end']))
            wav = path.parent / name
            directory_handles = lock_directory(wav.parent, check_contents=False)
            stack.callback(unlock_directory, directory_handles)
            with _pinned_file(wav, 64 * 1024 * 1024):
                verified_hash = _hash(wav)
                if verified_hash != entry['sha256']:
                    raise ValueError
                samples, rate = read_wav(wav)
                if _hash(wav) != verified_hash:
                    raise ValueError
            template = PhraseTemplate.from_recording(kind, samples, rate, start, end, threshold,temporal,
                                                     feature_version=feature_version,alternate_spans=alternate_spans)
            # Immutable bytes backing also prevents re-enabling ndarray writes.
            frozen = np.frombuffer(template.features.tobytes(), dtype='float32').reshape(template.features.shape)
            object.__setattr__(template, 'features', frozen)
            templates.append(template)
            hashes.append(verified_hash)
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        fingerprint = hashlib.sha256(canonical + b'\n' + '\n'.join(hashes).encode('ascii')).hexdigest()
        return AnnouncementProfile(data['profile_id'], data['camera_id'], tuple(templates), fingerprint)
