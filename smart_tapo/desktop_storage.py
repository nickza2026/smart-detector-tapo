"""Desktop configuration and current-user Windows credential storage."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from smart_tapo.config import Config, load_config


class DesktopStorageError(RuntimeError):
    """Safe to display: never contains stored values or provider responses."""


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    if sys.platform != 'win32':
        raise DesktopStorageError('Credential storage requires Windows DPAPI.')

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    # UI_FORBIDDEN, deliberately NOT LOCAL_MACHINE: current user only.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise DesktopStorageError('Windows credential protection failed.')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def _atomic(path, content, *, create=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(name, path)  # Atomic, exclusive publication; never replace an existing file.
        else:
            os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


class DesktopStore:
    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            portable = Path(sys.executable).resolve().parent / 'config.local.json'
            if getattr(sys, 'frozen', False) and portable.is_file():
                config_path = portable
            elif sys.platform == 'win32':
                local = os.environ.get('LOCALAPPDATA')
                if not local or not Path(local).is_absolute():
                    raise DesktopStorageError('Windows LOCALAPPDATA is unavailable.')
                config_path = Path(local) / 'SmartTapo' / 'config.json'
            else:
                config_path = Path.home() / 'Library' / 'Application Support' / 'SmartTapo' / 'config.json'
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        self._credentials_path = self.root / 'pushover.dat'

    def load_or_create(self) -> Config:
        try:
            if not self.config_path.exists():
                config = Config(data_dir=self.root / 'data',
                                baseline_path=self.root / 'calibration' / 'empty.png').validate()
                self.root.mkdir(parents=True, exist_ok=True)
                # Exclusive creation never overwrites another instance's configuration.
                try:
                    _atomic(self.config_path, self._serialize(config).encode('utf-8'), create=True)
                except FileExistsError:
                    pass
            return load_config(self.config_path)
        except Exception:
            raise DesktopStorageError('Cannot load configuration; existing files were preserved. Check configuration fields and file permissions.') from None

    def _normalize(self, config):
        return replace(config, **{name: (self.root / Path(getattr(config, name))).resolve()
                                  for name in ('data_dir', 'baseline_path')}).validate()

    def _serialize(self, config):
        return json.dumps(asdict(self._normalize(config)), default=lambda value: str(value)
                          if isinstance(value, Path) else value, ensure_ascii=False, indent=2) + '\n'

    def save(self, config: Config):
        try:
            _atomic(self.config_path, self._serialize(config).encode('utf-8'))
        except Exception:
            raise DesktopStorageError('Cannot save configuration; check fields and file permissions.') from None

    def load_credentials(self):
        try:
            if not self._credentials_path.exists():
                return '', ''
            raw = json.loads(_dpapi(self._credentials_path.read_bytes(), decrypt=True))
            if (not isinstance(raw, dict) or set(raw) != {'token', 'user'}
                    or any(not isinstance(raw[k], str) for k in raw)):
                raise ValueError()
            return raw['token'], raw['user']
        except Exception:
            raise DesktopStorageError('Cannot read saved credentials; the encrypted file was preserved.') from None

    def save_credentials(self, token, user):
        if not isinstance(token, str) or not isinstance(user, str):
            raise DesktopStorageError('Credentials must be text.')
        try:
            encrypted = _dpapi(json.dumps(dict(token=token, user=user)).encode('utf-8'))
            _atomic(self._credentials_path, encrypted)
        except Exception:
            raise DesktopStorageError('Cannot save credentials; Windows DPAPI and writable storage are required.') from None

    def clear_credentials(self):
        try:
            self._credentials_path.unlink(missing_ok=True)
        except OSError:
            raise DesktopStorageError('Cannot remove saved credentials.') from None
