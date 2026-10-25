"""Exercise image -> file spool -> SQLite without ADB, a live camera or Pushover."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import ImageDraw

from .config import load_config
from .frames import publish_frame
from .vision import load_png
from .worker import Worker


def simulate(config, image_path):
    original_hash = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
    source = load_png(image_path, config.expected_size)
    present = source.copy()
    ImageDraw.Draw(present).rectangle(config.roi.box(), fill="white")
    encoded = {}
    for name, image in (("EMPTY", source), ("PRESENT", present)):
        content = BytesIO()
        image.save(content, format="PNG")
        encoded[name] = content.getvalue()
    with tempfile.TemporaryDirectory(prefix="smart-tapo-demo-") as directory:
        root = Path(directory)
        config = replace(config, data_dir=root / "data", baseline_path=root / "empty.png",
                         notify_enabled=False, stuck_consecutive_frames=10,
                         reminder_interval_seconds=60, clear_consecutive_frames=2)
        source.save(config.baseline_path)
        now = [1000.0]
        worker = Worker(config, clock=lambda: now[0])
        try:
            # Five occupied frames, clear at frame six: no alert. Then 70 occupied
            # frames: threshold at frame 17 and reminder at frame 77. Finally clear.
            labels = ["PRESENT"] * 5 + ["EMPTY"] * 2 + ["PRESENT"] * 70 + ["EMPTY"] * 2
            for sequence, label in enumerate(labels, 1):
                now[0] += 1
                publish_frame(config, encoded[label], "simulation", sequence, True,
                              captured_at=now[0], captured_monotonic=now[0])
                worker.scan()
            events = [dict(row) for row in worker.db.execute('SELECT * FROM events ORDER BY started')]
            notifications = [dict(row) for row in worker.db.execute('SELECT * FROM notifications ORDER BY created')]
            assert [event['kind'] for event in events] == ['normal_visual', 'stuck']
            assert len(notifications) == 2 and all(row['status'] == 'shadow' for row in notifications)
            source_modified = hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != original_hash
            assert not source_modified
            return {'simulation_only': True, 'source_modified': source_modified, 'synthetic_frames': len(labels),
                    'normal_events': 1, 'stuck_events': 1, 'shadow_notifications': len(notifications),
                    'notification_sequences': [int(row['created'] - 1000) for row in notifications],
                    'real_notifications': 0,
                    'note': 'Synthetic white ROI patch, not a banknote dataset or freshness test.'}
        finally:
            worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    print(json.dumps(simulate(load_config(args.config), args.image), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
