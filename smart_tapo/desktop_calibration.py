"""Bind GUI calibration confirmation to geometry/device and the exact saved image."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .frames import atomic_bytes


def signature(config):
    result={
        'camera_id': config.camera_id,
        'expected_size': list(config.expected_size), 'video': asdict(config.video),
        'roi': asdict(config.roi), 'baseline_path': str(config.baseline_path.resolve()),
        'sha256': hashlib.sha256(config.baseline_path.read_bytes()).hexdigest(),
    }
    if getattr(config,'capture_backend','adb')=='rtsp':
        from .capture.rtsp import rtsp_source_fingerprint
        fingerprint=rtsp_source_fingerprint(config)
        if config.rtsp_source_binding!=fingerprint:raise ValueError('RTSP calibration source not bound')
        result.update(capture_backend='rtsp',source_fingerprint=fingerprint)
    else:
        result.update(camera_name=config.camera_name,adb_serial=config.adb_serial,
                      tapo_package=config.tapo_package)
    return result


def mark_ready(config, root):
    atomic_bytes(Path(root) / 'calibration-state.json',
                 json.dumps(signature(config), ensure_ascii=False).encode('utf-8'))


def is_ready(config, root):
    try:
        saved = json.loads((Path(root) / 'calibration-state.json').read_text(encoding='utf-8'))
        return saved == signature(config)
    except (OSError, ValueError, TypeError):
        return False
