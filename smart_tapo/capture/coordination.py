"""Durable navigation fences shared by capture, audio and inference workers."""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from smart_tapo.capture.ui_lock import ui_lock


@dataclass(frozen=True)
class NavigationSnapshot:
    generation: str
    recovering: bool
    changed_monotonic: float


def _path(config):
    root = Path(config.data_dir).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Unsafe navigation state path')
    key = hashlib.sha256((config.adb_serial + '\0' + config.camera_name).encode()).hexdigest()
    path = root / ('navigation-' + key + '.json')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Unsafe navigation state file')
    return path


def navigation_snapshot(config):
    path = _path(config)
    try:
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096: raise ValueError
        data = json.loads(raw)
        if set(data) != {'generation', 'recovering', 'changed_monotonic'}: raise ValueError
        generation, recovering, cutoff = data['generation'], data['recovering'], data['changed_monotonic']
        if not isinstance(generation, str) or len(generation) != 32 or uuid.UUID(hex=generation).hex != generation: raise ValueError
        if type(recovering) is not bool or type(cutoff) not in (int, float) or not math.isfinite(cutoff) or cutoff < 0: raise ValueError
        return NavigationSnapshot(generation, recovering, float(cutoff))
    except FileNotFoundError:
        return NavigationSnapshot('initial', False, 0.0)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('Invalid navigation state') from None


def _write(config, snapshot):
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(asdict(snapshot), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def navigation_lock(serial, timeout):
    """Decision/mutation lock, separate from slow read-only UI collection.

    Lock order when both are needed: UI lock, THEN navigation lock. Audio and
    worker commits use only this lock and never wait for a healthy XML dump.
    """
    with ui_lock('navigation:' + serial, timeout):
        yield


@contextmanager
def navigation_scope(config):
    with ui_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)):
        with navigation_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)):
            navigation_snapshot(config)  # Never overwrite malformed evidence.
            snapshot = NavigationSnapshot(uuid.uuid4().hex, True, time.monotonic())
            _write(config, snapshot)
            try:
                yield snapshot
            finally:
                _write(config, NavigationSnapshot(snapshot.generation, False, snapshot.changed_monotonic))
