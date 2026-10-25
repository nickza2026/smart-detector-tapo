import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from smart_tapo.config import Config
from smart_tapo.desktop_storage import DesktopStore, DesktopStorageError


class DesktopStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DesktopStore(Path(self.temp.name) / 'settings' / 'config.json')

    def test_create_roundtrip_and_absolute_paths(self):
        config = self.store.load_or_create()
        self.assertFalse(config.notify_enabled)
        self.assertEqual(config.data_dir, self.store.root / 'data')
        self.store.save(replace(config, camera_id='other', data_dir=Path('relative')))
        loaded = self.store.load_or_create()
        self.assertEqual(loaded.camera_id, 'other')
        self.assertEqual(loaded.data_dir, self.store.root / 'relative')
        self.assertEqual(loaded.roi, config.roi)
        self.assertNotIn('token', json.loads(self.store.config_path.read_text()))

    def test_invalid_preserved_and_safe_error(self):
        self.store.root.mkdir()
        content = '{"secret-token": "do-not-echo"}'
        self.store.config_path.write_text(content)
        with self.assertRaises(DesktopStorageError) as caught:
            self.store.load_or_create()
        self.assertNotIn('do-not-echo', str(caught.exception))
        self.assertEqual(self.store.config_path.read_text(), content)

    def test_invalid_save_preserves_config(self):
        config = self.store.load_or_create()
        original = self.store.config_path.read_bytes()
        with self.assertRaises(DesktopStorageError):
            self.store.save(replace(config, poll_interval_seconds=-1))
        self.assertEqual(self.store.config_path.read_bytes(), original)

    def test_encrypted_credentials_separate_and_explicit_clear(self):
        self.assertEqual(self.store.load_credentials(), ('', ''))
        def cipher(value, decrypt=False):
            return bytes(b ^ 0xA5 for b in value)
        with patch('smart_tapo.desktop_storage._dpapi', cipher):
            self.store.save_credentials('private-token', 'private-user')
            self.assertNotIn(b'private-token', self.store._credentials_path.read_bytes())
            self.assertEqual(self.store.load_credentials(), ('private-token', 'private-user'))
        original = self.store._credentials_path.read_bytes()
        with patch('smart_tapo.desktop_storage._dpapi', side_effect=ValueError('private-token')):
            with self.assertRaises(DesktopStorageError) as caught:
                self.store.load_credentials()
        self.assertNotIn('private-token', str(caught.exception))
        self.assertEqual(self.store._credentials_path.read_bytes(), original)
        self.store.clear_credentials()
        self.assertEqual(self.store.load_credentials(), ('', ''))

    @unittest.skipIf(sys.platform == 'win32', 'Non-Windows contract')
    def test_no_plaintext_fallback(self):
        with self.assertRaises(DesktopStorageError):
            self.store.save_credentials('private-token', 'private-user')
        self.assertFalse(self.store._credentials_path.exists())

    def test_existing_frozen_portable_config(self):
        executable = Path(self.temp.name) / 'SmartTapo.exe'
        portable = executable.parent / 'config.local.json'
        portable.write_text('{}')
        with patch('sys.executable', str(executable)), patch('sys.frozen', True, create=True):
            self.assertEqual(DesktopStore().config_path, portable.resolve())
