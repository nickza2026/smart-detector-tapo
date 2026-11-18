"""Synthetic offline loader tests, not classification-accuracy measurements."""
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

from smart_tapo.announcement_profile import load_announcement_profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profile.json'
        self.wav = self.path.parent / 'phrase.wav'
        samples = (np.random.default_rng(73).normal(0, .15, 24000) * 32767).astype('<i2')
        with wave.open(str(self.wav), 'wb') as writer:
            writer.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            writer.writeframes(samples.tobytes())
        digest = hashlib.sha256(self.wav.read_bytes()).hexdigest()
        self.data = dict(schema_version=1, profile_id='test_profile', camera_id='camera_a', templates=[
            dict(kind=kind, file='phrase.wav', sha256=digest, start=.1, end=2., threshold=.8)
            for kind in ('paid_ready', 'end')])
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def load(self):
        return load_announcement_profile(self.path, 'camera_a')

    def reject(self, camera='camera_a'):
        with self.assertRaisesRegex(ValueError, '^invalid_announcement_profile$') as caught:
            load_announcement_profile(self.path, camera)
        self.assertIsNone(caught.exception.__cause__)

    def test_success_frozen_no_writes(self):
        before = {p.name: p.read_bytes() for p in self.path.parent.iterdir()}
        result = self.load()
        self.assertEqual((result.id, result.camera_id), ('test_profile', 'camera_a'))
        self.assertEqual(tuple(t.label for t in result.templates), ('paid_ready', 'end'))
        self.assertEqual(len(result.fingerprint), 64)
        self.assertEqual(result.fingerprint, self.load().fingerprint)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.parent.iterdir()})
        with self.assertRaises(FrozenInstanceError):
            result.id = 'other'
        with self.assertRaises(FrozenInstanceError):
            result.templates[0].threshold = .9
        with self.assertRaises(ValueError):
            result.templates[0].features.flags.writeable = True
        self.assertFalse(np.shares_memory(result.templates[0].features, result.templates[1].features))
        self.data['templates'][0]['threshold'] = .9
        self.save()
        self.assertNotEqual(self.load().fingerprint, result.fingerprint)

    def test_optional_ready_alternate_is_immutable_and_fingerprint_bound(self):
        old=self.load()
        self.data['templates'][0]['alternates']=[dict(start=.3,end=2.2)]
        self.save();current=self.load()
        self.assertNotEqual(old.fingerprint,current.fingerprint)
        ready=current.templates[0]
        self.assertEqual(len(ready.alternate_views),1)
        matrix,offset=ready.alternate_views[0]
        self.assertAlmostEqual(offset,.2)
        self.assertEqual(matrix.shape,ready.features.shape)
        np.testing.assert_array_equal(old.templates[0].features,ready.features)
        with self.assertRaises(ValueError):matrix.flags.writeable=True

    def test_invalid_alternates_fail_closed(self):
        cases=[None,True,[],[None],[dict(start=True,end=2.2)],
            [dict(start=float('nan'),end=2.2)],[dict(start=.3,end=3.1)],
            [dict(start=.1,end=2.)],[dict(start=.3,end=2.1)],
            [dict(start=.3,end=2.2,threshold=.5)],
            [dict(start=.3,end=2.2),dict(start=.3,end=2.2)]]
        for value in cases:
            with self.subTest(value=value):
                self.data['templates'][0]['alternates']=value
                self.save();self.reject()
        del self.data['templates'][0]['alternates']
        self.data['templates'][1]['alternates']=[dict(start=.3,end=2.2)]
        self.save();self.reject()

    def test_invalid_root(self):
        for key, value in [('schema_version', True), ('schema_version', 2), ('schema_version', '1'),
                           ('profile_id', ''), ('profile_id', False), ('profile_id', '../private'),
                           ('camera_id', []), ('templates', {}), ('unknown', 'private')]:
            with self.subTest(key=key, value=value):
                original = self.data.copy()
                self.data[key] = value
                self.save()
                self.reject()
                self.data = original

    def test_temporal_evidence_is_explicit_and_fingerprint_bound(self):
        old=self.load()
        self.data['templates'][0]['temporal_threshold']=.3
        self.save();current=self.load()
        self.assertEqual(current.templates[0].temporal_threshold,.3)
        self.assertNotEqual(current.fingerprint,old.fingerprint)
        self.data['templates'][0]['temporal_threshold']=.4
        self.save();self.assertNotEqual(self.load().fingerprint,current.fingerprint)

    def test_invalid_temporal_evidence_fails_closed(self):
        for value in (True,False,None,0,-.1,1.01,float('nan'),float('inf'),'.3'):
            with self.subTest(value=value):
                self.data['templates'][0]['temporal_threshold']=value
                self.save();self.reject()

    def test_invalid_entries(self):
        cases = [('start', True), ('start', float('nan')), ('start', -1), ('start', '0'),
                 ('end', float('inf')), ('end', .2), ('end', 30), ('threshold', True),
                 ('threshold', float('nan')), ('threshold', .49), ('threshold', 1.01),
                 ('kind', 'other'), ('kind', []), ('sha256', '0' * 64), ('sha256', False),
                 ('file', '../phrase.wav'), ('file', '/phrase.wav'), ('file', 'C:/phrase.wav'),
                 ('file', 'sub/../phrase.wav'), ('file', 'sub\\phrase.wav'),
                 ('file', 'phrase.wav:stream'), ('file', False), ('unknown', 3)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                original = self.data['templates'][0].copy()
                self.data['templates'][0][key] = value
                self.save()
                self.reject()
                self.data['templates'][0] = original

    def test_camera_duplicate_missing(self):
        self.reject('camera_b')
        self.reject(True)
        self.data['templates'][1]['kind'] = 'paid_ready'
        self.save()
        self.reject()
        self.data['templates'].pop()
        self.save()
        self.reject()

    def test_invalid_json_and_size(self):
        for content in ('{"schema_version":1,"schema_version":1}', '[]', '{', ' ' * 32769):
            self.path.write_text(content)
            self.reject()

    def test_tampered_wav(self):
        with self.wav.open('ab') as source:
            source.write(b'tamper')
        self.reject()

    def test_unsupported_wav(self):
        self.wav.write_bytes(b'not a wav')
        for entry in self.data['templates']:
            entry['sha256'] = hashlib.sha256(self.wav.read_bytes()).hexdigest()
        self.save()
        self.reject()

    def test_hardlink(self):
        linked = self.path.parent / 'linked.wav'
        try:
            os.link(self.wav, linked)
        except OSError:
            self.skipTest('hardlinks unavailable')
        self.data['templates'][0]['file'] = linked.name
        self.save()
        self.reject()

    def test_symlink(self):
        linked = self.path.parent / 'linked.wav'
        try:
            linked.symlink_to(self.wav)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.data['templates'][0]['file'] = linked.name
        self.save()
        self.reject()

    def test_reparse_file_and_ancestor(self):
        original = Path.lstat
        for target in (self.wav, self.path.parent):
            def reparse(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with patch.object(Path, 'lstat', reparse):
                self.reject()


if __name__ == '__main__':
    unittest.main()
