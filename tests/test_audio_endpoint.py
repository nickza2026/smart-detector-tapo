import unittest
from unittest.mock import patch, MagicMock

from smart_tapo import audio_endpoint as endpoint

GUID1 = '{11111111-1111-1111-1111-111111111111}'
GUID2 = '{22222222-2222-2222-2222-222222222222}'


class EndpointTests(unittest.TestCase):
    def mapped(self, rows):
        # The old registry has no FriendlyName PID14; metadata comes from MMDevice.
        registry = MagicMock()
        registry.QueryInfoKey.return_value = (0, 0, 0)
        with patch.object(endpoint.os, 'name', 'nt'), \
                patch.dict('sys.modules', winreg=registry), \
                patch.object(endpoint, '_read_render_endpoints', return_value=rows, create=True):
            return endpoint.render_endpoint_ids()

    def test_authoritative_friendly_name_not_raw_registry(self):
        self.assertEqual(self.mapped([('Custom output', '{0.0.0.00000000}.' + GUID1)]),
                         {'Custom output': GUID1})

    def test_duplicate_names_are_not_chosen(self):
        self.assertEqual(self.mapped([('Duplicate', '{0.0.0.00000000}.' + GUID1),
                                      ('Duplicate', '{0.0.0.00000000}.' + GUID2)]), {})

    def test_only_render_identity_is_accepted(self):
        self.assertEqual(self.mapped([('Capture', '{0.0.1.00000000}.' + GUID1),
                                      ('Output', '{0.0.0.00000000}.' + GUID2)]),
                         {'Output': GUID2})

    def test_duplicate_observations_deduplicated(self):
        row = ('Output', '{0.0.0.00000000}.' + GUID1.upper())
        self.assertEqual(self.mapped([row, row]), {'Output': GUID1})

    def test_ambiguous_loopbacks_and_stale_identity(self):
        devices = [dict(name='Output [Loopback]', index=i, endpoint_id='stale') for i in (1, 2)]
        self.assertIs(endpoint.attach_endpoint_ids(devices, {'Output': GUID1}), devices)
        self.assertTrue(all('endpoint_id' not in device for device in devices))
        endpoint.attach_endpoint_ids(devices[:1], {})
        self.assertNotIn('endpoint_id', devices[0])

    def test_metadata_failure_is_closed(self):
        with patch.object(endpoint.os, 'name', 'nt'), patch.object(
                endpoint, '_read_render_endpoints', side_effect=OSError('COM unavailable'), create=True):
            self.assertEqual(endpoint.render_endpoint_ids(), {})


class COMMetadataTests(unittest.TestCase):
    def run_backend(self, initialized=0, wrong_type=False, failure_slot=None):
        import ctypes as ct
        ole = MagicMock()
        ole.CoInitializeEx.return_value = initialized
        def create(*args):
            ct.cast(args[-1], ct.POINTER(ct.c_void_p))[0] = 10
            return 0
        ole.CoCreateInstance.side_effect = create
        self.calls = []
        # Keep synthetic strings alive while the backend reads their pointers.
        strings = [ct.create_unicode_buffer('{0.0.0.00000000}.' + GUID1),
                   ct.create_unicode_buffer('Synthetic output')]
        def call(pointer, slot, arguments, *values):
            handle = pointer.value
            self.calls.append((handle, slot, values))
            if (handle, slot) == failure_slot:
                return -2147467259
            if (handle, slot) == (10, 3):
                self.assertEqual(values[:2], (0, 1))  # eRender, ACTIVE only
                ct.cast(values[-1], ct.POINTER(ct.c_void_p))[0] = 20
            elif (handle, slot) == (20, 3):
                ct.cast(values[0], ct.POINTER(ct.c_uint32))[0] = 1
            elif (handle, slot) == (20, 4):
                ct.cast(values[-1], ct.POINTER(ct.c_void_p))[0] = 30
            elif (handle, slot) == (30, 5):
                ct.cast(values[0], ct.POINTER(ct.c_void_p))[0] = ct.addressof(strings[0])
            elif (handle, slot) == (30, 4):
                self.assertEqual(values[0], 0)  # STGM_READ
                ct.cast(values[-1], ct.POINTER(ct.c_void_p))[0] = 40
            elif (handle, slot) == (40, 5):
                key = ct.cast(values[0], ct.POINTER(endpoint._PROPERTYKEY)).contents
                self.assertEqual(key.pid, 14)
                variant = ct.cast(values[1], ct.POINTER(endpoint._PROPVARIANT)).contents
                variant.vt = 3 if wrong_type else 31
                variant.value.pointer = 1 if wrong_type else ct.addressof(strings[1])
            else:
                self.assertEqual(slot, 2)  # No Activate, defaults or other calls.
            return 0
        with patch.object(endpoint, '_call', side_effect=call):
            try:
                result = endpoint._read_render_endpoints(ole)
            except OSError:
                result = 'failed'
        return result, ole

    def test_active_render_metadata_and_cleanup(self):
        result, ole = self.run_backend()
        self.assertEqual(result, [('Synthetic output', '{0.0.0.00000000}.' + GUID1)])
        self.assertEqual([h for h, slot, _ in self.calls if slot == 2], [40, 30, 20, 10])
        ole.PropVariantClear.assert_called_once()
        ole.CoTaskMemFree.assert_called_once()
        ole.CoUninitialize.assert_called_once()

    def test_com_initialization_ownership(self):
        for result in (0, 1, -2147417850):
            with self.subTest(result=result):
                _, ole = self.run_backend(initialized=result)
                self.assertEqual(ole.CoUninitialize.call_count, int(result in (0, 1)))
        result, ole = self.run_backend(initialized=-2147467259)
        self.assertEqual(result, 'failed')
        ole.CoCreateInstance.assert_not_called()
        ole.CoUninitialize.assert_not_called()

    def test_wrong_variant_type_is_not_dereferenced(self):
        result, ole = self.run_backend(wrong_type=True)
        self.assertEqual(result, [])
        ole.PropVariantClear.assert_called_once()

    def test_failure_releases_acquired_interfaces(self):
        result, ole = self.run_backend(failure_slot=(40, 5))
        self.assertEqual(result, 'failed')
        self.assertEqual([h for h, slot, _ in self.calls if slot == 2], [40, 30, 20, 10])
        ole.PropVariantClear.assert_called_once()
        ole.CoTaskMemFree.assert_called_once()
        ole.CoUninitialize.assert_called_once()

    def test_propvariant_native_abi(self):
        import ctypes as ct
        self.assertEqual(ct.sizeof(endpoint._PROPVARIANT), 24 if ct.sizeof(ct.c_void_p) == 8 else 16)


if __name__ == '__main__':
    unittest.main()
