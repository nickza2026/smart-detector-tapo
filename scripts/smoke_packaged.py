"""Verify packaged worker imports and image processing without cameras or secrets."""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageDraw

from smart_tapo.config import load_config
from smart_tapo.frames import publish_frame
from tests.test_alignment_regression import textured_reference


def run(command):
    # Even a developer machine with credentials configured cannot send through
    # this smoke test: remove credentials AND force shadow mode below.
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PUSHOVER_TOKEN', 'PUSHOVER_USER')}
    result = subprocess.run(command, env=environment, capture_output=True,
                            text=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError('Packaged command failed:\n' + result.stdout + '\n' + result.stderr)


def smoke(capture_command, worker_command):
    run([*capture_command, '--help'])
    run([*worker_command, '--help'])
    with tempfile.TemporaryDirectory(prefix='smart-tapo-package-smoke-') as directory:
        root = Path(directory)
        config_path = root / 'config.json'
        config_path.write_text(json.dumps({
            'notify_enabled': False,
            'alignment_enabled': True,
            'stuck_consecutive_frames': 1,
            # Allow for cold one-file extraction, without changing production config.
            'max_live_age_seconds': 120,
        }), encoding='utf-8')
        config = load_config(config_path)
        reference = textured_reference(config)
        source = root / 'empty-source.png'
        reference.save(source)
        run([*worker_command, '--config', str(config_path), '--calibrate', str(source), '--confirm-empty'])
        occupied = reference.copy()
        x, y, width, height = config.roi.x, config.roi.y, config.roi.width, config.roi.height
        ImageDraw.Draw(occupied).rectangle((x+10, y+15, x+width-10, y+height-15), fill='white')
        # Exercise registration as well as Pillow/NumPy/OpenCV DLL collection.
        video = occupied.crop(config.video.box())
        video = video.transform(video.size, Image.Transform.AFFINE, (1, 0, -42, 0, 1, 15),
                                resample=Image.Resampling.BILINEAR, fillcolor='black')
        occupied.paste(video, (config.video.x, config.video.y))
        encoded = io.BytesIO()
        occupied.save(encoded, format='PNG')
        publish_frame(config, encoded.getvalue(), 'package-smoke', 1, True)
        run([*worker_command, '--config', str(config_path), '--once'])
        with closing(sqlite3.connect(str(config.data_dir / 'worker.sqlite3'))) as database:
            events = database.execute("SELECT count(*) FROM events WHERE kind='stuck'").fetchone()[0]
            statuses = database.execute('SELECT status FROM notifications').fetchall()
            if events != 1 or statuses != [('shadow',)]:
                raise RuntimeError('Packaged registration/occupancy did not create the expected shadow event')
            evidence = database.execute("SELECT path FROM evidence WHERE role='threshold'").fetchone()
            if evidence is None:
                raise RuntimeError('Packaged worker did not save threshold evidence')
            with Image.open(evidence[0]) as image:
                if image.size != (width, height):
                    raise RuntimeError('Packaged evidence has the wrong ROI dimensions')
    return {'stuck_events': events, 'shadow_notifications': len(statuses), 'real_notifications': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Run Windows executables on Windows; no cross-platform execution is implied')
    dist = args.dist.resolve()
    executable = dist / 'SmartTapo.exe'
    if not executable.is_file():
        parser.error('Missing packaged desktop executable: SmartTapo.exe')
    with tempfile.TemporaryDirectory(prefix='smart-tapo-gui-report-') as directory:
        report = Path(directory) / 'report.json'
        result = subprocess.run([str(executable), '--smoke-report', str(report)],
                                capture_output=True, text=True, timeout=150, check=False)
        if result.returncode or not report.is_file():
            raise RuntimeError('Packaged GUI failed or did not produce its smoke report')
        data = json.loads(report.read_text(encoding='utf-8'))
        for key in ('gui_created', 'auto_config_created', 'worker_stopped',
                    'shadow_notification', 'calibration_ready', 'windows_dpapi_roundtrip',
                    'capture_process_started'):
            if data.get(key) is not True:
                raise RuntimeError('Packaged GUI did not verify: ' + key)
        if data.get('real_notifications') != 0:
            raise RuntimeError('GUI smoke must not use live notifications')
        print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()
