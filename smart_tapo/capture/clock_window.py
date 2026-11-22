"""Bounded, unpublished PNG windows. Clock pixels are health, not video proof."""
from __future__ import annotations
import hashlib
import math
import uuid
from pathlib import Path
from smart_tapo.config import Config
from smart_tapo.frames import atomic_bytes
from smart_tapo.clock_epoch import current_clock_epoch


def config_token(config):
    return hashlib.sha256(repr(tuple((name, getattr(config, name, field.default))
        for name, field in Config.__dataclass_fields__.items())).encode()).hexdigest()


def valid_window(frame, config, navigation, now, mono):
    """Validate end-confirmation coverage, not a freshness TTL per subframe."""
    proof = frame.window_proof
    if not isinstance(proof, dict): return False
    required = {'session', 'epoch', 'config', 'generation', 'start_at', 'end_at',
                'start_mono', 'end_mono', 'health_start_at', 'health_start_mono',
                'ui_healthy', 'clock_progress', 'ui_at', 'ui_until'}
    if set(proof) != required: return False
    if (proof['session'] != frame.session_id or proof['epoch'] != frame.clock_epoch
            or proof['epoch'] != current_clock_epoch() or proof['config'] != config_token(config)
            or proof['generation'] != navigation.generation or navigation.recovering
            or proof['ui_healthy'] is not True or proof['clock_progress'] is not True): return False
    for key in ('start_at', 'end_at', 'start_mono', 'end_mono', 'health_start_at',
                'health_start_mono', 'ui_at', 'ui_until'):
        if type(proof[key]) not in (float, int) or not math.isfinite(proof[key]): return False
    interval = config.clock_check_interval_seconds
    health_gap = interval + min(config.capture_interval_seconds, config.max_frame_gap_seconds)
    return (0 <= proof['end_at']-proof['start_at'] <= interval
        and 0 <= proof['end_mono']-proof['start_mono'] <= interval
        and proof['start_at'] <= frame.captured_at <= proof['end_at'] <= now
        and proof['start_mono'] <= frame.captured_monotonic <= proof['end_mono'] <= mono
        and proof['health_start_at'] < proof['start_at']
        and proof['health_start_mono'] < proof['start_mono']
        and 0 < proof['end_at']-proof['health_start_at'] <= health_gap
        and interval <= proof['end_mono']-proof['health_start_mono'] <= health_gap
        and proof['ui_at'] <= proof['end_mono'] <= mono < proof['ui_until']
        and 0 < proof['ui_until']-proof['ui_at'] <= config.ui_check_interval_seconds
        and navigation.changed_monotonic <= proof['health_start_mono']
        and 0 <= now-proof['end_at'] <= config.max_live_age_seconds
        and 0 <= mono-proof['end_mono'] <= config.max_live_age_seconds)


class PendingWindow:
    def __init__(self, config):
        self.config = config
        self.root = config.data_dir / 'pending' / config.camera_id / uuid.uuid4().hex
        self.paths = []
        self.anchor = None
        self.resync_reason = None

    def reset(self, reason=None):
        # A cleanup is not evidence of recovery. Known uncertainty survives until
        # one complete progressing window has been discarded to re-prime trust.
        if reason is not None: self.resync_reason = reason
        # Delete only names created by this instance; never recurse/follow links.
        for row in self.paths:
            path = row[0]
            if not path.is_symlink() and path.is_file() and self.safe(): path.unlink()
        self.paths.clear()
        self.anchor = None
        if self.root.exists() and self.safe():
            try: self.root.rmdir()
            except OSError: pass

    def safe(self):
        base = self.config.data_dir.absolute()
        path = self.root.absolute()
        return path.is_relative_to(base) and not any(
            p.is_symlink() for p in (path, *path.parents) if p == base or p.is_relative_to(base))

    def add(self, png, clock, at, mono, sequence, session, generation):
        signature = (config_token(self.config), session, generation, current_clock_epoch())
        interval = self.config.clock_check_interval_seconds
        if self.anchor is not None:
            start_at, start_mono, old_clock, old_signature, last_at, last_mono = self.anchor
            if (signature != old_signature or not 0 < at-last_at <= self.config.max_frame_gap_seconds
                    or not 0 < mono-last_mono <= self.config.max_frame_gap_seconds
                    or at-start_at > interval+self.config.capture_interval_seconds
                    or mono-start_mono > interval+self.config.capture_interval_seconds
                    or (self.paths and (at-self.paths[0][2] > interval
                                        or mono-self.paths[0][3] > interval))):
                self.reset('capture_gap_or_identity_or_window_boundary')
        if self.anchor is None:
            self.anchor = (at, mono, clock, signature, at, mono)
            return [], None
        start_at, start_mono, old_clock, _, _, _ = self.anchor
        # Hard count plus disk quota bounds pending storage even under bad clocks.
        limit = min(82, math.ceil(interval/self.config.capture_interval_seconds)+1)
        if len(self.paths) >= limit or not self.safe():
            self.reset('pending_storage_unavailable'); raise ValueError('Pending window storage unavailable')
        path = self.root / (uuid.uuid4().hex + '.png')
        atomic_bytes(path, png)
        self.paths.append((path, sequence, at, mono))
        self.anchor = (start_at, start_mono, old_clock, signature, at, mono)
        if mono-start_mono < interval: return [], None
        if clock == old_clock:
            self.reset('clock_not_progressing')
            self.anchor = (at, mono, clock, signature, at, mono)
            return [], None
        if self.resync_reason is not None:
            # First progress after known failure establishes a NEW baseline. It
            # cannot certify images gathered while recovery was still uncertain.
            self.reset()
            self.resync_reason = None
            self.anchor = (at, mono, clock, signature, at, mono)
            return [], None
        proof = dict(session=session, epoch=signature[3], config=signature[0], generation=generation,
                     start_at=self.paths[0][2], end_at=at, start_mono=self.paths[0][3], end_mono=mono,
                     health_start_at=start_at, health_start_mono=start_mono,
                     ui_healthy=True, clock_progress=True)
        rows = list(self.paths)
        # The caller publishes while fenced, then resets. Never reuse old PNGs.
        return rows, proof
