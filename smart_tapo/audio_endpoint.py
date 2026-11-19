"""Read-only MMDevice render metadata; never activate audio or change routing."""
from __future__ import annotations

import ctypes as ct
import os
import re
import uuid
from collections import Counter


class _GUID(ct.Structure):
    _fields_ = [('data', ct.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


class _PROPERTYKEY(ct.Structure):
    _fields_ = [('fmtid', _GUID), ('pid', ct.c_uint32)]


class _VALUE(ct.Union):
    # PROPVARIANT's counted-array member is pointer aligned and determines size.
    _fields_ = [('pointer', ct.c_void_p),
                ('array', type('_ARRAY', (ct.Structure,), {
                    '_fields_': [('count', ct.c_uint32), ('pointer', ct.c_void_p)]}))]


class _PROPVARIANT(ct.Structure):
    _fields_ = [('vt', ct.c_uint16), ('reserved', ct.c_uint16 * 3), ('value', _VALUE)]


def _check(result):
    if ct.c_int32(result).value < 0:
        raise OSError('MMDevice metadata HRESULT 0x%08x' % (result & 0xffffffff))


def _call(pointer, slot, arguments, *values):
    table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    method = ct.WINFUNCTYPE(ct.c_int32, ct.c_void_p, *arguments)(table[slot])
    return method(pointer, *values)


def _release(pointer):
    if pointer.value:
        _call(pointer, 2, ())


def _read_render_endpoints(ole32=None):
    """Enumerate ACTIVE eRender IMMDevices and their authoritative FriendlyName.

    Only EnumAudioEndpoints, GetId and the read-only property store are used.
    The optional ole32 argument is a test seam, not an alternate identity source.
    """
    if ole32 is None:
        ole32 = ct.WinDLL('ole32')
        ole32.CoInitializeEx.argtypes = [ct.c_void_p, ct.c_uint32]
        ole32.CoInitializeEx.restype = ct.c_int32
        ole32.CoCreateInstance.argtypes = [ct.POINTER(_GUID), ct.c_void_p, ct.c_uint32,
                                         ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)]
        ole32.CoCreateInstance.restype = ct.c_int32
        ole32.PropVariantClear.argtypes = [ct.POINTER(_PROPVARIANT)]
        ole32.PropVariantClear.restype = ct.c_int32
        ole32.CoTaskMemFree.argtypes = [ct.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
    initialized = ct.c_int32(ole32.CoInitializeEx(None, 2)).value
    # RPC_E_CHANGED_MODE: COM already belongs to the caller's other apartment.
    if initialized != -2147417850:
        _check(initialized)
    owned = initialized in (0, 1)
    enumerator, collection = ct.c_void_p(), ct.c_void_p()
    rows = []
    try:
        _check(ole32.CoCreateInstance(
            ct.byref(_GUID('bcde0395-e52f-467c-8e3d-c4579291692e')), None, 1,
            ct.byref(_GUID('a95664d2-9614-4f35-a746-de8db63617e6')), ct.byref(enumerator)))
        _check(_call(enumerator, 3, (ct.c_int32, ct.c_uint32, ct.POINTER(ct.c_void_p)),
                     0, 1, ct.byref(collection)))
        count = ct.c_uint32()
        _check(_call(collection, 3, (ct.POINTER(ct.c_uint32),), ct.byref(count)))
        for index in range(count.value):
            device, store, identifier = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
            value = _PROPVARIANT()
            try:
                _check(_call(collection, 4, (ct.c_uint32, ct.POINTER(ct.c_void_p)),
                             index, ct.byref(device)))
                _check(_call(device, 5, (ct.POINTER(ct.c_void_p),), ct.byref(identifier)))
                endpoint_id = ct.wstring_at(identifier.value) if identifier.value else ''
                _check(_call(device, 4, (ct.c_uint32, ct.POINTER(ct.c_void_p)),
                             0, ct.byref(store)))
                key = _PROPERTYKEY(_GUID('a45c254e-df1c-4efd-8020-67d146a850e0'), 14)
                _check(_call(store, 5, (ct.POINTER(_PROPERTYKEY), ct.POINTER(_PROPVARIANT)),
                             ct.byref(key), ct.byref(value)))
                # Never interpret another variant type as a pointer/string.
                if value.vt == 31 and value.value.pointer:
                    rows.append((ct.wstring_at(value.value.pointer), endpoint_id))
            finally:
                ole32.PropVariantClear(ct.byref(value))
                if identifier.value:
                    ole32.CoTaskMemFree(identifier)
                _release(store)
                _release(device)
        return rows
    finally:
        _release(collection)
        _release(enumerator)
        if owned:
            ole32.CoUninitialize()


_RENDER_ID = re.compile(r'^\{0\.0\.0\.00000000\}\.(\{[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\})$', re.I)


def render_endpoint_ids():
    if os.name != 'nt':
        return {}
    try:
        rows = _read_render_endpoints()
    except (OSError, AttributeError, ValueError):
        return {}
    names = {}
    identifiers = {}
    for name, endpoint_id in rows:
        match = _RENDER_ID.fullmatch(endpoint_id) if isinstance(endpoint_id, str) else None
        if not isinstance(name, str) or not name or not match:
            continue
        identifier = match.group(1).lower()
        names.setdefault(name, set()).add(identifier)
        identifiers.setdefault(identifier, set()).add(name)
    return {name: next(iter(values)) for name, values in names.items()
            if len(values) == 1 and len(identifiers[next(iter(values))]) == 1}


def attach_endpoint_ids(devices, endpoints=None):
    endpoints = render_endpoint_ids() if endpoints is None else endpoints
    suffix = ' [Loopback]'
    names = [device['name'][:-len(suffix)] if device['name'].endswith(suffix)
             else device['name'] for device in devices]
    counts = Counter(names)
    identity_counts = Counter(endpoints.values())
    for device, name in zip(devices, names):
        device.pop('endpoint_id', None)
        identifier = endpoints.get(name)
        if identifier and counts[name] == 1 and identity_counts[identifier] == 1:
            device['endpoint_id'] = identifier
    return devices
