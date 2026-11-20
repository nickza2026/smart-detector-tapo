"""Opt-in current-user login startup. Importing this module has no side effects."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time

MARKER = "' SmartTapo owned login entry v1\r\n"
ENTRY = 'SmartTapo-login.vbs'


def checked_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked/reparse startup paths are not allowed')
    return path


def startup_folder():
    if os.name != 'nt':
        raise ValueError('Windows login startup requires Windows')
    # SHGetFolderPathW: current-user CSIDL_STARTUP, not a machine-wide directory.
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 7, None, 0, buffer):
        raise ValueError('Cannot locate current-user Startup folder')
    return checked_path(buffer.value)


def scope_marker(config):
    if config is None:
        raise ValueError('Explicit config owner is required to manage Windows startup')
    identity = os.path.normcase(str(checked_path(config)))
    return "' Config owner " + hashlib.sha256(identity.encode('utf-8')).hexdigest() + '\r\n'


def entry_content(executable, config):
    executable, config = checked_path(executable), checked_path(config)
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ValueError('Choose the installed SmartTapo EXE, not Python or a temporary bundle')
    if executable.name.lower().startswith(('python', 'wscript', 'cscript')) or any(
            part.lower().startswith(('_mei', '.venv')) for part in executable.parts):
        raise ValueError('Startup requires a permanent installed SmartTapo EXE')
    if any(c in str(executable) + str(config) for c in '\r\n"%'):
        raise ValueError('Invalid startup path')
    command = subprocess.list2cmdline([str(executable), '--config', str(config), '--windows-login'])
    literal = command.replace('"', '""')
    target = str(executable).replace('"', '""')
    return (MARKER + scope_marker(config) + 'Set fs = CreateObject("Scripting.FileSystemObject")\r\n'
            + f'If Not fs.FileExists("{target}") Then\r\n'
            + ' MsgBox "SmartTapo EXE moved or missing. Open its new location and enable Windows startup again."\r\n'
            + ' WScript.Quit 1\r\nEnd If\r\n'
            + f'CreateObject("WScript.Shell").Run "{literal}", 1, False\r\n')


def manage_entry(enabled, executable=None, config=None, *, folder=None):
    folder = checked_path(folder if folder is not None else startup_folder())
    if not folder.is_dir():
        raise ValueError('Startup folder does not exist')
    path = checked_path(folder / ENTRY)
    ownership = (MARKER + scope_marker(config)).replace('\r\n', '\n')
    if path.exists():
        if path.stat().st_nlink != 1:
            raise ValueError('Hardlinked Startup entry is not owned safely')
        if not path.is_file() or not path.read_text(encoding='utf-16').startswith(ownership):
            raise ValueError('Foreign Startup entry exists; remove/rename it yourself')
    if not enabled:
        if path.exists():
            path.unlink()
        return
    content = entry_content(executable, config)
    # Exclusive creation protects foreign files; owned updates are reversible.
    if path.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-16', newline='', dir=folder,
                                             prefix='.SmartTapo-login-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            checked_path(path)
            if path.stat().st_nlink != 1 or not path.read_text(encoding='utf-16').startswith(ownership):
                raise ValueError('Startup entry ownership changed')
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    else:
        with path.open('x', encoding='utf-16', newline='') as stream:
            stream.write(content)


def profile_digest(path):
    path = checked_path(path)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Missing or oversized saved profile')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def device_identity(device):
    # PortAudio indices are session-local. A unique WASAPI name AND format are
    # required, never a default device or a same-index replacement.
    if not isinstance(device.get('endpoint_id'), str) or not device['endpoint_id']:
        raise ValueError('Stable Windows endpoint identity unavailable; refresh/select the saved output again')
    return {key: device[key] for key in ('endpoint_id', 'name', 'rate', 'channels')}


def matching_device(binding, devices):
    matches = [device for device in devices if device.get('endpoint_id') == binding.get('endpoint_id')
               and device_identity(device) == binding]
    if len(matches) != 1:
        raise ValueError('Saved output endpoint missing or ambiguous; confirm the device again')
    return dict(matches[0])


def video_source_binding(config):
    """Non-secret transport identity, not proof of physical camera liveness."""
    if getattr(config, 'capture_backend', 'adb') != 'rtsp':
        return None
    from .capture.rtsp import rtsp_source_fingerprint, validate_rtsp_url
    uri = validate_rtsp_url(config.rtsp_url)
    return dict(schema='rtsp-startup-v1', capture_backend='rtsp',
                uri_sha256=hashlib.sha256(uri.encode('utf-8')).hexdigest(),
                source_fingerprint=rtsp_source_fingerprint(config),
                geometry=list(config.expected_size))


def bind(config, profile='', device=None):
    source = video_source_binding(config)
    from .storage_location import location_identity
    result = dict(camera_id=config.camera_id, data_root=location_identity(config.data_dir))
    if source is not None:
        result.update(source)
        result['schema'] = 'rtsp-av-startup-v2'
        result['audio_source'] = 'rtsp' if config.windows_startup_audio else None
        if config.windows_startup_audio:
            from .announcement_profile import load_announcement_profile
            fingerprint = load_announcement_profile(profile, config.camera_id).fingerprint
            result.update(profile=str(Path(profile).absolute()), digest=profile_digest(profile), fingerprint=fingerprint)
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    if source is None or device is not None:
        result.update(camera_name=config.camera_name, adb_serial=config.adb_serial,
                      tapo_package=config.tapo_package)
        if not config.adb_serial or not config.camera_name:
            raise ValueError('Save one explicit ADB device and camera first')
    if device is not None:
        from .announcement_profile import load_announcement_profile
        fingerprint = load_announcement_profile(profile, config.camera_id).fingerprint
        result.update(profile=str(Path(profile).absolute()), digest=profile_digest(profile),
                      fingerprint=fingerprint, device=device_identity(device))
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def validate_binding(config):
    saved = json.loads(config.windows_startup_binding)
    from .storage_location import location_identity
    if saved.get('data_root') != location_identity(config.data_dir):
        raise ValueError('Data storage changed; confirm startup consent again')
    source = video_source_binding(config)
    if source is not None:
        expected = dict(source, schema='rtsp-av-startup-v2', camera_id=config.camera_id,
                        audio_source='rtsp' if config.windows_startup_audio else None)
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError('RTSP source/consent changed; bind startup again')
        if any(key in saved for key in ('device', 'android_identity', 'adb_serial')):
            raise ValueError('Legacy audio consent is not RTSP consent')
        if config.windows_startup_audio:
            from .announcement_profile import load_announcement_profile
            if (profile_digest(saved['profile']) != saved['digest'] or
                    load_announcement_profile(saved['profile'], config.camera_id).fingerprint != saved['fingerprint']):
                raise ValueError('Saved RTSP audio profile changed; bind startup again')
        return saved
    if 'schema' in saved or 'capture_backend' in saved:
        raise ValueError('Saved video source changed; confirm startup binding again')
    keys = ('camera_id',)
    if source is None or config.windows_startup_audio:
        keys += ('camera_name', 'adb_serial', 'tapo_package')
    for key in keys:
        if saved.get(key) != getattr(config, key):
            raise ValueError('Saved camera/device changed; confirm startup binding again')
    if config.windows_startup_audio:
        if not config.adb_serial or not config.camera_name:
            raise ValueError('Save one explicit ADB device and camera first')
        if profile_digest(saved['profile']) != saved['digest']:
            raise ValueError('Saved audio profile changed; confirm startup binding again')
        if source is not None:
            device_identity(saved['device'])
    return saved


class StartupCoordinator:
    """One bounded attempt at a time, asynchronous and cancellable; injected work is testable."""
    def __init__(self, attempt, *, clock=time.monotonic, retry_seconds=15):
        self.attempt, self.clock, self.retry_seconds = attempt, clock, retry_seconds
        self.stop_event = threading.Event()
        self.thread = None
        self.enabled = False
        self.next_retry = 0
        self.result = None
        self.status = 'off'
        self._lock = threading.Lock()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            raise ValueError('Startup attempt is still stopping')
        with self._lock:
            self.stop_event.clear()
            self.result = None
            self.enabled = True
            self.next_retry = 0

    def stop(self):
        with self._lock:
            self.enabled = False
            self.stop_event.set()
            self.result = None
            self.status = 'stopped'

    @property
    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def poll(self):
        if not self.enabled or self.is_running:
            return None
        if self.result is not None:
            result, self.result = self.result, None
            self.enabled = False
            return result
        if self.clock() < self.next_retry:
            return None
        self.next_retry = self.clock() + self.retry_seconds
        self.status = 'waiting'
        def run():
            try:
                result = self.attempt(self.stop_event)
                with self._lock:
                    if not self.stop_event.is_set():
                        self.result = result
                        self.status = 'ready'
            except Exception as error:
                with self._lock:
                    if not self.stop_event.is_set():
                        self.status = str(error)[:200]
            finally:
                self.next_retry = self.clock() + self.retry_seconds
        self.thread = threading.Thread(target=run, name='windows-login-startup', daemon=True)
        self.thread.start()
        return None
