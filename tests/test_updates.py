"""Updater security regression tests; no network, device, config or user DB."""
import base64
import hashlib
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from smart_tapo import updates as u


class UpdateTests(unittest.TestCase):
    def manifest(self, **changes):
        obj=dict(schema=1,repository=u.REPOSITORY,version=45,filename='SmartTapo-V45.exe',size=3,sha256=hashlib.sha256(b'abc').hexdigest(),signature=base64.b64encode(b'x'*64).decode())
        obj.update(changes)
        return json.dumps(obj).encode()

    def test_missing_key_fails_closed(self):
        with self.assertRaises(u.UpdateError):u.verify_manifest(self.manifest(),44,None)

    def test_strict_schema(self):
        for changes in (dict(version=True),dict(version=44),dict(repository='other/repo'),dict(size=-1),dict(filename='../evil.exe'),dict(signature=''),dict(extra=1)):
            with self.subTest(changes=changes),self.assertRaises(u.UpdateError):u.parse_manifest(self.manifest(**changes),44)
        with self.assertRaises(u.UpdateError):u.parse_manifest(b'{"schema":1,"schema":1}',44)

    def test_url_boundary(self):
        for url in ('http://github.com/a','https://evil.test/a','https://github.com.evil.test/a','https://user:password@github.com/a','https://github.com:444/a','https://github.com/other/repo/releases/download/V45/a.exe'):
            with self.subTest(url=url),self.assertRaises(u.UpdateError):u.validate_url(url)
        u.validate_url('https://github.com/'+u.REPOSITORY+'/releases/download/V45/a.exe')
        u.validate_url('https://release-assets.githubusercontent.com/github-production-release-asset/a')

    def test_download_stale_and_cancel(self):
        m=u.parse_manifest(self.manifest(),44)
        with tempfile.TemporaryDirectory() as d:
            dest=Path(d)/'download.exe'
            dest.write_bytes(b'abc')
            stop=threading.Event();stop.set()
            with self.assertRaises(u.UpdateError):u.download(m,'https://github.com/'+u.REPOSITORY+'/releases/download/V45/SmartTapo-V45.exe',dest,stop,fetch=lambda *a,**k:b'abc')
            self.assertFalse(dest.exists())
            with self.assertRaises(u.UpdateError):u.download(m,'https://github.com/'+u.REPOSITORY+'/releases/download/V45/SmartTapo-V45.exe',dest,threading.Event(),fetch=lambda *a,**k:b'bad')
            self.assertFalse(dest.exists())

    def test_unknown_business_state_blocks(self):
        self.assertFalse(u.install_allowed(None))
        self.assertFalse(u.install_allowed(dict(workers=False)))
        state={k:False for k in u.BUSINESS_FLAGS}
        self.assertTrue(u.install_allowed(state))
        for k in state:
            state[k]=True
            self.assertFalse(u.install_allowed(state))
            state[k]=False

    def test_signed_manifest_and_tamper(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
        except ImportError:self.skipTest('Requires isolated requirements-updater environment')
        # Ephemeral test-only key: never a production trust root.
        key=Ed25519PrivateKey.generate()
        public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        obj=json.loads(self.manifest())
        payload=u.parse_manifest(self.manifest(),44).payload
        obj['signature']=base64.b64encode(key.sign(payload)).decode()
        self.assertEqual(u.verify_manifest(json.dumps(obj).encode(),44,public).version,45)
        obj['size']=4
        with self.assertRaises(u.UpdateError):u.verify_manifest(json.dumps(obj).encode(),44,public)
        with self.assertRaises(u.UpdateError):u.verify_manifest(self.manifest(),44,public)

    def test_mock_release(self):
        release=dict(draft=False,prerelease=False,tag_name='V45',assets=[dict(name=name,browser_download_url='https://github.com/'+u.REPOSITORY+'/releases/download/V45/'+name) for name in ('SmartTapo-update.json','SmartTapo-V45.exe')])
        calls=[]
        def fetch(url,*a,**k):
            calls.append(url)
            return json.dumps(release).encode() if url==u.API_URL else self.manifest()
        client=u.ReleaseClient(fetch=fetch)
        with patch.object(u,'verify_manifest',side_effect=u.UpdateError('No approved pinned signing key; installation disabled')):
            with self.assertRaisesRegex(u.UpdateError,'pinned signing key'):client.check(44)
        self.assertEqual(len(calls),2)

    def test_rate_limit(self):
        client=u.ReleaseClient(clock=lambda:100)
        client.next_check=200
        with self.assertRaises(u.UpdateError):client.check(44)

if __name__=='__main__':unittest.main()
