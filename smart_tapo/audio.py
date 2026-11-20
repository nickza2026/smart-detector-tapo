"""Explicit, process-isolated WASAPI mixed-output diagnostics (never detection)."""
from __future__ import annotations

import math
import multiprocessing
import os
from pathlib import Path
import queue
import stat
import struct
import time
import uuid
import wave
from datetime import datetime, timezone

MAX_CLIP = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 20


class ProbeError(Exception):
    pass


def pcm_levels(data: bytes) -> tuple[float | None, float | None, bool]:
    """Measure little-endian signed PCM16; zero has no finite dBFS value."""
    if len(data) % 2:
        raise ProbeError('invalid_pcm')
    samples = [v[0] for v in struct.iter_unpack('<h', data)]
    if not samples:
        return None, None, False
    peak = max(abs(v) for v in samples)
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    return (20 * math.log10(rms / 32768) if rms else None,
            20 * math.log10(peak / 32768) if peak else None,
            any(v in (-32768, 32767) for v in samples))


def checked_ancestry(directory: Path) -> Path:
    path = Path(os.path.abspath(directory))
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProbeError('unsafe_directory')
        if not stat.S_ISDIR(info.st_mode):
            raise ProbeError('unsafe_directory')
    return path


def prepare_sample_directory(data_directory: Path) -> Path:
    """Only on explicit record: create missing real directories, never links."""
    destination=Path(os.path.abspath(data_directory))/'audio-samples'
    missing=[]
    existing=destination
    while not existing.exists():
        if existing.is_symlink() or existing.is_junction() or len(missing)>=64:
            raise ProbeError('unsafe_directory')
        missing.append(existing)
        existing=existing.parent
    handles=lock_directory(existing,check_contents=False)
    try:
        for directory in reversed(missing):
            try:directory.mkdir()
            except FileExistsError:pass
            handles.extend(lock_directory(directory,check_contents=False))
        return checked_directory(destination)
    finally:
        unlock_directory(handles)


def checked_directory(directory: Path, *, reserve=MAX_CLIP, count_limit=MAX_FILES) -> Path:
    path=checked_ancestry(directory)
    total = 0
    for count, entry in enumerate(path.iterdir(), 1):
        if count >= count_limit:
            raise ProbeError('storage_limit')
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProbeError('unsafe_directory')
        if not stat.S_ISREG(info.st_mode):
            raise ProbeError('unsafe_directory')
        total += info.st_size
    if total + reserve > MAX_TOTAL:
        raise ProbeError('storage_limit')
    return path


def loopback_devices(audio, api) -> list[dict]:
    host = audio.get_host_api_info_by_type(api.paWASAPI)
    host_index = int(host['index'])
    devices = []
    for index in range(min(int(audio.get_device_count()), 1024)):
        item = audio.get_device_info_by_index(index)
        if not item.get('isLoopbackDevice') or item.get('hostApi') != host_index:
            continue
        name = str(item.get('name', ''))
        rate = int(item.get('defaultSampleRate', 0))
        channels = int(item.get('maxInputChannels', 0))
        if not name or len(name) > 160 or not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            continue
        devices.append(dict(index=index, name=name, rate=rate, channels=channels))
        if len(devices) >= 32:
            break
    if getattr(api, '__name__', '') == 'pyaudiowpatch':
        from .audio_endpoint import attach_endpoint_ids
        return attach_endpoint_ids(devices)
    return devices


def _progress(channel, event):
    try:
        channel.put_nowait(event)
    except queue.Full:
        pass


def list_job(final, progress, stop, api=None):
    _job(final, progress, stop, None, None, 0, api)


def capture_job(final, progress, stop, device, staging, duration, api=None):
    _job(final, progress, stop, device, staging, duration, api)


def _job(final, progress, stop, device, staging, duration, api):
    audio = stream = writer = raw_file = None
    result = {'kind': 'error', 'code': 'audio_unavailable'}
    try:
        if api is None:
            import pyaudiowpatch as api
        audio = api.PyAudio()
        devices = loopback_devices(audio, api)
        if device is None:
            result = {'kind': 'devices', 'devices': devices}
        else:
            matched = next((d for d in devices if d['index'] == device['index'] and d['name'] == device['name']), None)
            if matched is None:
                raise ProbeError('device_changed')
            rate, channels = matched['rate'], matched['channels']
            target = int(duration * rate)
            if target * channels * 2 + 44 > MAX_CLIP:
                raise ProbeError('clip_limit')
            if staging:
                checked_directory(Path(staging).parent)
                raw_file = open(staging, 'xb')
                writer = wave.open(raw_file, 'wb')
                writer.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
            stream = audio.open(format=api.paInt16, channels=channels, rate=rate,
                                input=True, input_device_index=matched['index'], frames_per_buffer=1024)
            frames = 0
            silent = True
            while frames < target and not stop.is_set():
                count = min(1024, target - frames)
                data = stream.read(count, exception_on_overflow=True)
                if len(data) != count * channels * 2:
                    raise ProbeError('invalid_pcm')
                rms, peak, clipped = pcm_levels(data)
                silent = silent and peak is None
                if writer:
                    writer.writeframesraw(data)
                frames += count
                _progress(progress, dict(kind='level', rms_dbfs=rms, peak_dbfs=peak,
                                         clipped=clipped, elapsed=frames / rate))
            if not frames:
                raise ProbeError('empty_capture')
            result = dict(kind='finished', mode='record' if staging else 'monitor', path=None,
                          frames=frames, rate=rate, channels=channels, duration=frames / rate, silent=silent)
    except ProbeError as exc:
        result = {'kind': 'error', 'code': str(exc)}
    except Exception:
        result = {'kind': 'error', 'code': 'audio_unavailable'}
    finally:
        try:
            if writer:
                writer.close()
            if raw_file:
                raw_file.close()
            if stream:
                stream.close()
            if audio:
                audio.terminate()
        except Exception:
            result = {'kind': 'error', 'code': 'audio_unavailable'}
        progress.cancel_join_thread()
        try:
            final.send(result)
        finally:
            final.close()


def synthetic_fixture_job(final, progress, stop):
    """Offline spawn/IPC smoke fixture; never imports or opens audio."""
    final.send({'kind': 'devices', 'devices': []})
    final.close()


class AudioProbe:
    def __init__(self, *, context=None, clock=time.monotonic):
        self._context = context
        self._clock = clock
        self._process = None
        self._closing = False
        self._staging = None
        self._directory_handles = []

    @property
    def is_running(self):
        return self._process is not None

    def start_list(self):
        self._start(list_job, (), 10)

    def start_capture(self, device: dict, directory: Path | None = None, duration: float = 10):
        self._available()
        if (not isinstance(device, dict) or type(device.get('index')) is not int
                or device['index'] < 0 or not isinstance(device.get('name'), str)
                or not 0 < len(device['name']) <= 160):
            raise ValueError('invalid_device')
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 15:
            raise ValueError('invalid_duration')
        staging = None
        if directory is not None:
            try:
                self._directory_handles = lock_directory(Path(directory))
                path = checked_directory(Path(directory))
            except (OSError, ProbeError):
                unlock_directory(self._directory_handles)
                self._directory_handles = []
                raise ValueError('unsafe_or_full_directory') from None
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            staging = path / f'audio-{stamp}-{uuid.uuid4().hex}.partial'
        try:
            self._start(capture_job, (dict(device), str(staging) if staging else None, duration), min(15, duration + 3))
        except Exception:
            unlock_directory(self._directory_handles)
            self._directory_handles = []
            raise
        self._staging = staging

    def _available(self):
        if self._closing or self.is_running:
            raise ValueError('busy_or_closing')

    def _start(self, target, args, timeout):
        self._available()
        ctx = self._context or multiprocessing.get_context('spawn')
        self._recv, send = ctx.Pipe(duplex=False)
        self._progress = ctx.Queue(maxsize=8)
        self._stop = ctx.Event()
        self._result = None
        self._forced = None
        self._stop_at = None
        self._kill_at = None
        self._deadline = self._clock() + timeout
        process = ctx.Process(target=target, args=(send, self._progress, self._stop, *args), daemon=True)
        try:
            process.start()
        except Exception:
            self._recv.close()
            self._progress.close()
            raise ValueError('start_failed') from None
        finally:
            send.close()
        self._process = process

    def stop(self):
        if self.is_running and self._stop_at is None:
            self._stop.set()
            self._stop_at = self._clock()

    def close(self):
        self._closing = True
        self.stop()

    def poll(self) -> list[dict]:
        if not self.is_running:
            return []
        events = []
        for _ in range(8):
            try:
                events.append(self._progress.get_nowait())
            except queue.Empty:
                break
        try:
            if self._result is None and self._recv.poll():
                self._result = self._recv.recv()
        except (EOFError, OSError):
            pass
        now = self._clock()
        if now >= self._deadline and self._stop_at is None:
            self._forced = 'timeout'
            self.stop()
        if self._stop_at is not None and now - self._stop_at >= 2 and self._kill_at is None:
            self._forced = self._forced or 'stopped'
            self._process.terminate()
            self._kill_at = now
        if self._kill_at is not None and now - self._kill_at >= 1 and self._process.is_alive():
            self._process.kill()
        if self._process.is_alive():
            return events
        self._process.join(timeout=0)
        # The child can send/exit between the first poll and is_alive().
        # Re-read after reap before declaring that a terminal result is absent.
        try:
            if self._result is None and self._recv.poll():
                self._result=self._recv.recv()
        except (EOFError,OSError):
            pass
        result = self._result or {'kind': 'error', 'code': 'worker_failed'}
        if self._forced or self._process.exitcode != 0:
            result = {'kind': 'error', 'code': self._forced or 'worker_failed'}
        if self._staging:
            try:
                if result['kind'] == 'finished':
                    # The child is reaped: only now can a complete WAV become visible.
                    checked_directory_for_finalize(self._staging)
                    destination = self._staging.with_suffix('.wav')
                    os.link(self._staging, destination)  # atomic, fails rather than overwrites
                    result['path'] = str(destination)
            except (OSError, ProbeError):
                result = {'kind': 'error', 'code': 'storage_error'}
            finally:
                try:
                    self._staging.unlink(missing_ok=True)
                except OSError:
                    result = {'kind': 'error', 'code': 'storage_error'}
        self._staging = None
        unlock_directory(self._directory_handles)
        self._directory_handles = []
        self._recv.close()
        self._progress.close()
        self._process.close()
        self._process = None
        events.append(result)
        return events


def lock_directory(directory, *, check_contents=True):
    """On Windows pin ancestry against rename/reparse replacement until reap."""
    if check_contents:
        checked_directory(directory)
    else:
        checked_ancestry(directory)
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handles = []
    path = Path(os.path.abspath(directory))
    try:
        for part in reversed((path, *path.parents)):
            handle = create(str(part), 0, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ProbeError('unsafe_directory')
            handles.append(handle)
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ProbeError('unsafe_directory')
        return handles
    except Exception:
        unlock_directory(handles)
        raise


def unlock_directory(handles):
    if handles:
        import ctypes
        from ctypes import wintypes
        close = ctypes.WinDLL('kernel32', use_last_error=True).CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        for handle in handles:
            close(handle)


def checked_directory_for_finalize(staging):
    # Recheck ancestry and quotas without reserving a second clip's capacity.
    checked_directory(staging.parent, reserve=0, count_limit=MAX_FILES + 1)
    for part in (staging, *staging.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProbeError('unsafe_directory')
    if staging.stat().st_size > MAX_CLIP:
        raise ProbeError('clip_limit')
