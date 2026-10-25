"""Bind GUI calibration confirmation to geometry/device and the exact saved image."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .frames import atomic_bytes


def signature(config):
    return {
        'camera_id': config.camera_id, 'camera_name': config.camera_name,
        'adb_serial': config.adb_serial, 'tapo_package': config.tapo_package,
        'expected_size': list(config.expected_size), 'video': asdict(config.video),
        'roi': asdict(config.roi), 'baseline_path': str(config.baseline_path.resolve()),
        'sha256': hashlib.sha256(config.baseline_path.read_bytes()).hexdigest(),
    }


def mark_ready(config, root):
    atomic_bytes(Path(root) / 'calibration-state.json',
                 json.dumps(signature(config), ensure_ascii=False).encode('utf-8'))


def is_ready(config, root):
    try:
        saved = json.loads((Path(root) / 'calibration-state.json').read_text(encoding='utf-8'))
        return saved == signature(config)
    except (OSError, ValueError, TypeError):
        return False
