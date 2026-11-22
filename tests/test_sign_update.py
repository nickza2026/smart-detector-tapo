"""Operator signing tests use exclusively disposable keys and files."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sign_update', Path(__file__).parents[1] / 'scripts/sign_update.py')
signer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(signer)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.private = self.root / 'test.dpapi'
        self.public = self.root / 'public.bin'

    def generate(self):
        signer.main(['keygen', '--windows-account', '--private', str(self.private), '--public', str(self.public)])

    @unittest.skipUnless(signer.sys.platform == 'win32', 'real current-user DPAPI')
    def test_windows_roundtrip_no_prompt_no_plaintext(self):
        with patch.object(signer.getpass, 'getpass', side_effect=AssertionError('unexpected prompt')):
            self.generate()
            key = signer.load_key(self.private, windows_account=True)
            raw = key.private_bytes_raw()
            self.assertNotIn(raw, self.private.read_bytes())
            self.assertNotIn(b'PRIVATE KEY', self.private.read_bytes())
            exe = self.root / 'SmartTapo-V45.exe'
            exe.write_bytes(b'synthetic test only')
            output = self.root / 'manifest.json'
            signer.main(['sign', '--windows-account', '--private', str(self.private), '--exe', str(exe), '--version', '45', '--output', str(output)])
            obj = json.loads(output.read_text())
            signature = base64.b64decode(obj.pop('signature'))
            key.public_key().verify(signature, json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii'))
            self.assertEqual({p.name for p in self.root.iterdir()}, {'test.dpapi', 'public.bin', exe.name, output.name})

    @unittest.skipUnless(signer.sys.platform == 'win32', 'real current-user DPAPI')
    def test_tamper_wrong_mode_and_existing(self):
        self.generate()
        original = self.private.read_bytes()
        with self.assertRaises(signer.SigningError):
            signer.load_key(self.private, windows_account=False, password=b'test')
        self.private.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(signer.SigningError):
            signer.load_key(self.private, windows_account=True)
        with self.assertRaises(signer.SigningError):
            self.generate()
        self.assertEqual(self.public.stat().st_size, 32)

    def test_partial_publish_rollback_and_preserve_existing(self):
        original = signer._write_new
        def failing(path, content, created):
            if path == self.public:
                raise OSError('test failure')
            return original(path, content, created)
        with patch.object(signer, '_write_new', side_effect=failing):
            with self.assertRaises(signer.SigningError):
                signer.generate_keys(self.private, self.public, windows_account=False, password=b'fixture-only')
        self.assertFalse(self.private.exists())
        self.assertFalse(self.public.exists())
        self.public.write_bytes(b'preserve')
        with self.assertRaises(signer.SigningError):
            signer.generate_keys(self.private, self.public, windows_account=False, password=b'fixture-only')
        self.assertEqual(self.public.read_bytes(), b'preserve')
        self.assertFalse(self.private.exists())

    def test_non_windows_fail_closed(self):
        with patch.object(signer.sys, 'platform', 'linux'):
            with self.assertRaises(signer.SigningError):
                signer.generate_keys(self.private, self.public, windows_account=True)
        self.assertFalse(self.private.exists())

    def test_repository_private_rejected(self):
        with self.assertRaises(signer.SigningError):
            signer.generate_keys(Path(__file__).parents[1] / 'test-private-never-created', self.public, windows_account=False, password=b'test')

    @unittest.skipUnless(signer.sys.platform == 'win32', 'real current-user DPAPI')
    def test_payload_size_context_and_public_mismatch(self):
        self.generate()
        key = signer.load_key(self.private, windows_account=True)
        good = signer.CONTEXT + key.private_bytes_raw() + key.public_key().public_bytes_raw()
        for payload in (good[:-1], good + b'x', b'X' + good[1:], good[:-32] + b'x' * 32):
            self.private.write_bytes(signer.HEADER + signer._dpapi(payload))
            with self.assertRaises(signer.SigningError):
                signer.load_key(self.private, windows_account=True)

    def test_partial_write_cleanup(self):
        original = signer.os.fsync
        with patch.object(signer.os, 'fsync', side_effect=OSError('fixture write failure')):
            with self.assertRaises(signer.SigningError):
                signer.generate_keys(self.private, self.public, password=b'test-only')
        self.assertFalse(self.private.exists())
        self.assertFalse(self.public.exists())

    def test_hardlink_rejected(self):
        self.public.write_bytes(b'existing')
        alias = self.root / 'alias'
        signer.os.link(self.public, alias)
        with self.assertRaises(signer.SigningError):
            signer.check_path(alias)

    def test_password_pem_roundtrip(self):
        signer.generate_keys(self.private, self.public, windows_account=False, password=b'fixture-only')
        key = signer.load_key(self.private, windows_account=False, password=b'fixture-only')
        self.assertEqual(key.public_key().public_bytes_raw(), self.public.read_bytes())
        with self.assertRaises(signer.SigningError):
            signer.load_key(self.private, windows_account=True)


if __name__ == '__main__':
    unittest.main()
