"""Safe emulator capture; clock-pixel changes are NOT OCR or proof of live video."""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from PIL import Image

from smart_tapo.frames import image_storage_bytes, publish_frame
from smart_tapo.runtime import hidden_console_options

LOG = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    pass


class Adb:
    """argv-only ADB adapter; never expose subprocess output containing UI secrets."""

    def __init__(self, config, runner=subprocess.run, launcher=subprocess.Popen):
        if not config.adb_serial or config.adb_serial.startswith('-'):
            raise ValueError('Configure an explicit adb_serial')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', config.tapo_package):
            raise ValueError('tapo_package must be an Android package identifier')
        self.config, self.runner, self.launcher = config, runner, launcher
        self.started = False

    def run(self, *args, selected=True):
        argv = [self.config.adb_path]
        if selected:
            argv += ['-s', self.config.adb_serial]
        argv += list(args)
        try:
            result = self.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=self.config.adb_timeout_seconds, check=False,
                                 **hidden_console_options())
        except (OSError, subprocess.TimeoutExpired):
            raise CaptureError('ADB unavailable or timed out; check configured path and emulator') from None
        if result.returncode:
            raise CaptureError('ADB command failed; check device connection (output withheld)')
        return result.stdout

    def connect(self, stop):
        serial = self.config.adb_serial
        if ':' in serial:
            if not re.fullmatch(r'(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}', serial):
                raise CaptureError('TCP adb_serial must use configured loopback host:port')
            if not 1 <= int(serial.rsplit(':', 1)[1]) <= 65535:
                raise CaptureError('Invalid ADB TCP port')
            try:
                self.run('connect', serial, selected=False)
            except CaptureError:
                pass  # A stopped emulator may not yet expose its configured port.
        try:
            available = self.run('get-state').strip() == b'device'
        except CaptureError:
            available = False
        if not available and self.config.memu_executable and not self.started:
            if os.name != 'nt':
                raise CaptureError('Automatic MEmu startup is supported only on Windows')
            self.started = True
            try:
                self.launcher([self.config.memu_executable], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
            except OSError:
                raise CaptureError('Cannot start configured MEmu executable') from None
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                if ':' in serial:
                    self.run('connect', serial, selected=False)
                if self.run('shell', 'getprop', 'sys.boot_completed').strip() == b'1':
                    return
            except CaptureError:
                pass
            stop.wait(min(1.0, max(0, deadline - time.monotonic())))
        raise CaptureError('Emulator boot timed out or capture stopped')

    def ui(self):
        self.run('shell', 'uiautomator', 'dump', '/sdcard/smart-tapo-window.xml')
        return self.run('shell', 'cat', '/sdcard/smart-tapo-window.xml')

    def ensure_live(self):
        self.run('shell', 'monkey', '-p', self.config.tapo_package,
                 '-c', 'android.intent.category.LAUNCHER', '1')
        xml = self.ui()
        if live_ui(xml, self.config.camera_name):
            return
        point = camera_center(xml, self.config.camera_name, self.config.expected_size)
        if point is not None:
            self.run('shell', 'input', 'tap', str(point[0]), str(point[1]))
            if live_ui(self.ui(), self.config.camera_name):
                return
        raise CaptureError('Live view not verified. Log in manually and open the configured camera live view; check camera_name and controls')


def nodes(xml):
    try:
        return list(ET.fromstring(xml).iter('node'))
    except ET.ParseError:
        raise CaptureError('Invalid UI hierarchy; navigate to live view manually') from None


def labels(xml):
    return {n.get(k, '').strip() for n in nodes(xml) for k in ('text', 'content-desc')}


def live_ui(xml, camera_name):
    text = labels(xml)
    return camera_name in text and bool(text & {
        'Playback & Download', 'Pan & Tilt', 'การเล่นและดาวน์โหลด',
        'เล่นย้อนหลังและดาวน์โหลด', 'การดูย้อนหลังและดาวน์โหลด', 'แพนและเอียง'})


def camera_center(xml, camera_name, size):
    tree = nodes(xml)
    text = labels(xml)
    if not text.intersection({'Home', 'หน้าหลัก', 'บ้าน', 'อุปกรณ์', 'Devices'}):
        return None
    # Never tap through a login/password screen, even if it contains the camera name.
    if any(n.get('password') == 'true' for n in tree):
        return None
    matches = [n for n in tree if camera_name in
               (n.get('text', '').strip(), n.get('content-desc', '').strip())]
    if len(matches) != 1:
        return None
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', matches[0].get('bounds', ''))
    if not match:
        return None
    x, y, right, bottom = map(int, match.groups())
    if not (0 <= x < right <= size[0] and 0 <= y < bottom <= size[1]):
        return None
    return ((x + right) // 2, (y + bottom) // 2)


def decode_clock(png, config):
    """Pixel-difference freshness heuristic, NOT OCR or proof of live video."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != 'PNG' or image.size != config.expected_size:
                raise CaptureError('Screenshot must be PNG with configured dimensions')
            image.load()
            return image.convert('RGB').crop(config.clock_roi.box()).tobytes()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise CaptureError('Screenshot PNG decode failed') from None


def capture(config, stop, once=False, adb=None, monotonic=time.monotonic):
    config.validate()
    adb = adb or Adb(config)
    session = uuid.uuid4().hex
    sequence, previous, connected = 0, None, False
    last_ui_check = float('-inf')
    target = monotonic()
    while not stop.is_set():
        if stop.wait(max(0, target - monotonic())):
            break
        now = monotonic()
        missed = max(0, int((now - target) // config.capture_interval_seconds))
        sequence += missed + 1
        target += (missed + 1) * config.capture_interval_seconds
        try:
            if not connected:
                adb.connect(stop)
                adb.ensure_live()
                connected = True
                last_ui_check = monotonic()
            # UIAutomator can itself take seconds; do not run it on every 1 Hz sample.
            # Reverify periodically; clock-region checks still run on every image.
            if monotonic() - last_ui_check >= config.ui_check_interval_seconds:
                if not live_ui(adb.ui(), config.camera_name):
                    raise CaptureError('Live view lost; navigate to configured camera manually')
                last_ui_check = monotonic()
            png = adb.run('exec-out', 'screencap', '-p')
            captured_at, captured_monotonic = time.time(), monotonic()
            clock = decode_clock(png, config)
            fresh = previous is not None and clock != previous
            previous = clock
            if image_storage_bytes(config) + len(png) > config.max_image_storage_mb * 1_000_000:
                LOG.warning('Image quota full; publishing paused, pending frames retained')
            else:
                publish_frame(config, png, session, sequence, fresh,
                              captured_at=captured_at, captured_monotonic=captured_monotonic)
        except (CaptureError, OSError, ValueError) as error:
            LOG.warning('Capture unavailable: %s', error)
            connected, previous = False, None
            if once:
                return 1
            stop.wait(min(1.0, config.capture_interval_seconds))
        if once:
            return 0
    return 0
