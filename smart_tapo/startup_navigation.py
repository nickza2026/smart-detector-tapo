"""Explicit login-start action; read_page_mode remains strictly read-only."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
import subprocess
import time

from .capture import Adb, camera_center, nodes
from .capture.coordination import navigation_lock, navigation_scope, navigation_snapshot
from .capture.page_mode import foreground_activity
from .capture.ui_lock import ui_lock
from .runtime import hidden_console_options


def android_identity(adb, stop):
    values = []
    for command in (('shell', 'getprop', 'ro.serialno'), ('shell', 'settings', 'get', 'secure', 'android_id')):
        if stop.is_set():
            raise ValueError('Startup stopped')
        value = adb.run(*command).decode('ascii', errors='strict').strip()
        if not re.fullmatch('[A-Za-z0-9._:-]{4,128}', value) or value.lower() in ('null', 'unknown'):
            raise ValueError('Android instance identity unavailable; confirm the selected instance manually')
        values.append(value)
    if stop.is_set():
        raise ValueError('Startup stopped')
    return values


class LoginNavigation:
    def __init__(self, config, *, adb=None, running=None):
        self.config = config
        self.running = running or self._memu_running
        self.video_only = (getattr(config, 'capture_backend', 'adb') == 'rtsp')
        self.adb = None if self.video_only else (adb or Adb(config, launcher=self._launch))

    def _memu_running(self):
        # Fail closed when process enumeration is unavailable. Never kill or
        # attach to a different MEmu instance. This only avoids duplicate launch.
        if os.name != 'nt':
            raise ValueError('Windows MEmu startup requires Windows')
        name = Path(self.config.memu_executable).name
        result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq ' + name, '/FO', 'CSV', '/NH'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                                check=False, **hidden_console_options())
        if result.returncode:
            raise ValueError('Cannot check existing MEmu; open it manually')
        return ('"' + name.lower() + '"') in result.stdout.decode(errors='replace').lower()

    def _launch(self, argv, **kwargs):
        if self.running():
            return None
        path = Path(self.config.memu_executable)
        if not path.is_file():
            raise ValueError('Saved MEmu EXE missing; select its installed path')
        return subprocess.Popen(argv, **kwargs, **hidden_console_options())

    def __call__(self, stop):
        if stop.is_set():
            raise ValueError('Startup stopped')
        if self.video_only:
            from .startup import validate_binding
            validate_binding(self.config)
            return True  # No network open until the explicitly consented capture start.
        # A previous bounded attempt may have seen a missing/crashed launcher.
        # Re-check process existence on each retry; the launcher never spawns
        # another copy while that configured executable is already running.
        self.adb.started = False
        self.adb.connect(stop)  # exact configured serial only, bounded boot wait
        if stop.is_set():
            raise ValueError('Startup stopped')
        expected = json.loads(self.config.windows_startup_binding).get('android_identity')
        if expected is None or android_identity(self.adb, stop) != expected:
            raise ValueError('Selected Android instance changed; confirm startup binding again (no port fallback)')
        mode = self.adb.read_page_mode()
        # Preserve existing selected Live. Only launch the configured package
        # when no recognized Tapo Live page is already in front.
        if mode.reason == 'unrecognized_foreground':
            if stop.is_set():
                raise ValueError('Startup stopped')
            self.adb.run('shell', 'monkey', '-p', self.config.tapo_package,
                         '-c', 'android.intent.category.LAUNCHER', '1')
            if stop.wait(.5):
                raise ValueError('Startup stopped')
            mode = self.adb.read_page_mode()
        if mode.mode != 'live' and mode.reason not in ('live_refresh_required', 'live_inactivity_confirmation'):
            self._select_saved_camera(stop)
        if stop.is_set():
            raise ValueError('Startup stopped')
        mode = self.adb.ensure_live(stop)
        if stop.is_set() or mode.mode != 'live':
            raise ValueError('Selected camera Live is pending; retrying without listening')
        return True

    def _select_saved_camera(self, stop):
        config, adb = self.config, self.adb
        with ui_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)), \
                navigation_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)):
            started = time.monotonic()
            state = navigation_snapshot(config)
            before = foreground_activity(adb.run('shell', 'dumpsys', 'activity', 'activities'), config.tapo_package)
            xml = adb.ui()
            after = foreground_activity(adb.run('shell', 'dumpsys', 'activity', 'activities'), config.tapo_package)
            # This specific home activity is the only startup selector scope.
            # Unknown versions, login, Playback and wrong-camera Live never tap.
            allowed = {config.tapo_package + '/.view.main.MainActivity',
                       config.tapo_package + '/' + config.tapo_package + '.view.main.MainActivity'}
            tree = nodes(xml)
            target = [node for node in tree if config.camera_name in
                      (node.get('text', '').strip(), node.get('content-desc', '').strip())]
            home = [node for node in tree if node.get('package') == config.tapo_package
                    and node.get('visible-to-user') != 'false' and any(node.get(key, '').strip() in
                    {'Home', 'หน้าหลัก', 'บ้าน', 'อุปกรณ์', 'Devices'} for key in ('text', 'content-desc'))]
            size = config.expected_size
            if getattr(config, 'capture_backend', 'adb') == 'rtsp':
                # Android touch bounds belong to the audio UI, not RTSP pixels.
                output = adb.run('shell', 'wm', 'size').decode('ascii', errors='strict')
                sizes = re.findall(r'(?:Physical|Override) size: (\d+)x(\d+)', output)
                if not sizes:
                    raise ValueError('Android display size unavailable; open saved camera manually')
                size = tuple(map(int, sizes[-1]))
                if not all(0 < value <= 16384 for value in size):
                    raise ValueError('Android display size invalid')
            point = camera_center(xml, config.camera_name, size)
            if (len(target) != 1 or target[0].get('package') != config.tapo_package
                    or target[0].get('visible-to-user') == 'false' or target[0].get('enabled') == 'false'
                    or not home):
                point = None
            if (before not in allowed or before != after or point is None
                    or state != navigation_snapshot(config) or state.recovering
                    or not 0 <= time.monotonic() - started < min(10, config.ui_check_interval_seconds)):
                raise ValueError('Open Tapo Home / login manually; saved camera not safely selectable')
            if stop.is_set():
                raise ValueError('Startup stopped')
            with navigation_scope(config):
                if stop.is_set():
                    raise ValueError('Startup stopped')
                adb.run('shell', 'input', 'tap', str(point[0]), str(point[1]))
        if stop.wait(.5):
            raise ValueError('Startup stopped')
