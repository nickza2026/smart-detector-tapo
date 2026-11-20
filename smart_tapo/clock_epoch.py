"""Read-only kernel boot identity, not a hostname/account or persisted config.

Wall time can move backwards and monotonic counters restart on reboot. A frame
from another boot must never become current proof just because numbers align.
"""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import uuid


@lru_cache(maxsize=1)
def current_clock_epoch():
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        class BootEnvironment(ctypes.Structure):
            _fields_=[('identifier',ctypes.c_ubyte*16),('firmware',wintypes.ULONG),
                      ('flags',ctypes.c_ulonglong)]
        query=ctypes.WinDLL('ntdll').NtQuerySystemInformation
        query.argtypes=[wintypes.ULONG,ctypes.c_void_p,wintypes.ULONG,ctypes.POINTER(wintypes.ULONG)]
        query.restype=wintypes.LONG
        data=BootEnvironment();size=wintypes.ULONG()
        # SystemBootEnvironmentInformation: kernel boot GUID, shared by all
        # processes in this boot. No configuration, driver or clock mutation.
        status=query(90,ctypes.byref(data),ctypes.sizeof(data),ctypes.byref(size))
        identity=bytes(data.identifier)
        if status!=0 or size.value<16 or not any(identity):
            raise ValueError('Kernel boot identity unavailable')
    else:
        try:identity=uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()).bytes
        except (OSError,ValueError):raise ValueError('Kernel boot identity unavailable') from None
    return hashlib.sha256(identity).hexdigest()
