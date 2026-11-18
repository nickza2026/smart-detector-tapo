import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_tapo.desktop_storage import DesktopStore, DesktopStorageError


class NotifyCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = DesktopStore(self.root / 'config.json')
        self.base = 'https://notify.invalid'

    @staticmethod
    def fake_dpapi(data, *, decrypt=False):
        if decrypt:
            if not data.startswith(b'protected:'):
                raise ValueError('synthetic bad envelope')
            return data[len(b'protected:'):]
        return b'protected:' + data

    def test_missing_notify_never_reads_or_reinterprets_legacy_credentials(self):
        old = self.root / 'pushover.dat'
        old.write_bytes(b'synthetic-old-provider-do-not-use')
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=AssertionError('must not decrypt legacy')):
            self.assertEqual(self.store.load_notify_key(self.base, 'cam'), '')
        self.assertEqual(old.read_bytes(), b'synthetic-old-provider-do-not-use')

    def test_target_bound_roundtrip_keeps_legacy_and_config_untouched(self):
        old = self.root / 'pushover.dat'; old.write_bytes(b'legacy-fixture')
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=self.fake_dpapi):
            self.store.save_notify_key('synthetic-new-key', self.base, 'cam')
            self.assertEqual(self.store.load_notify_key(self.base, 'cam'), 'synthetic-new-key')
            for base, camera in ((self.base, 'other'), ('https://other.invalid', 'cam')):
                with self.assertRaises(DesktopStorageError) as error:
                    self.store.load_notify_key(base, camera)
                self.assertNotIn('synthetic-new-key', str(error.exception))
            self.assertTrue((self.root / 'notify-api.dat').exists())
            self.assertFalse(self.store.config_path.exists())
            self.store.clear_notify_key()
            self.assertEqual(self.store.load_notify_key(self.base, 'cam'), '')
        self.assertEqual(old.read_bytes(), b'legacy-fixture')

    def test_invalid_target_or_camera_does_not_overwrite_existing_notify_key(self):
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=self.fake_dpapi):
            self.store.save_notify_key('synthetic-new-key', self.base, 'cam')
            before = (self.root / 'notify-api.dat').read_bytes()
            for base, camera in (('http://notify.invalid', 'cam'),
                                 ('https://user:pass@notify.invalid', 'cam'),
                                 (self.base, '../cam')):
                with self.assertRaises(DesktopStorageError):
                    self.store.save_notify_key('replacement', base, camera)
                self.assertEqual((self.root / 'notify-api.dat').read_bytes(), before)

    def test_corrupt_envelope_is_preserved_and_error_redacted(self):
        path = self.root / 'notify-api.dat'
        raw = b'protected:' + json.dumps({'key': 'synthetic-secret'}).encode()
        path.write_bytes(raw)
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=self.fake_dpapi):
            with self.assertRaises(DesktopStorageError) as error:
                self.store.load_notify_key(self.base, 'cam')
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.assertEqual(path.read_bytes(), raw)

    def test_no_plaintext_write_when_dpapi_fails(self):
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=RuntimeError('private-error')):
            with self.assertRaises(DesktopStorageError) as error:
                self.store.save_notify_key('synthetic-secret', self.base, 'cam')
        self.assertNotIn('private-error', str(error.exception))
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.assertFalse((self.root / 'notify-api.dat').exists())


if __name__ == '__main__':
    unittest.main()
