"""Serialize our Windows UiAutomation clients for one selected emulator.

Ephemeral session-local kernel mutex, not a file/global setting or device lock.
It never blocks a human interacting with MEmu. Non-Windows has process-local
thread protection only; Windows loopback is the supported desktop target.
"""
from contextlib import contextmanager
import hashlib
import os
import threading

_guard=threading.Lock()
_locks={}


@contextmanager
def ui_lock(serial,timeout):
    key=hashlib.sha256(serial.encode()).hexdigest()
    if os.name!='nt':
        with _guard:lock=_locks.setdefault(key,threading.RLock())
        if not lock.acquire(timeout=timeout):raise ValueError('ui_dump_busy')
        try:yield
        finally:lock.release()
        return
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateMutexW;create.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR];create.restype=wintypes.HANDLE
    wait=kernel.WaitForSingleObject;wait.argtypes=[wintypes.HANDLE,wintypes.DWORD];wait.restype=wintypes.DWORD
    release=kernel.ReleaseMutex;release.argtypes=[wintypes.HANDLE];release.restype=wintypes.BOOL
    close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
    handle=create(None,False,'Local\\SmartTapoUI-'+key)
    if not handle:raise ValueError('ui_dump_lock_unavailable')
    acquired=False
    try:
        status=wait(handle,max(0,min(5000,int(timeout*1000))))
        if status not in (0,0x80):raise ValueError('ui_dump_busy')
        acquired=True
        yield
    finally:
        if acquired:release(handle)
        close(handle)
