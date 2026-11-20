"""Synthetic-only tests: never inspect shipped recordings or hardware."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from smart_tapo.announcement_profile import load_announcement_profile
from smart_tapo.prepared_profile import BUNDLED_MANIFEST, default_prepared_manifest, prepare_profile


class BundledProfileTests(unittest.TestCase):
    def test_bundled_views_match_same_file_with_one_window_feature_pass(self):
        from smart_tapo.announcement import AnnouncementMatcher,read_wav,spectral_features
        profile=load_announcement_profile(BUNDLED_MANIFEST,'provided-recordings')
        ready=next(t for t in profile.templates if t.label=='paid_ready')
        self.assertEqual(len(ready.alternate_views),1)
        self.assertEqual(ready.alternate_views[0][1],-2.)
        samples,rate=read_wav(BUNDLED_MANIFEST.parent/'ready.wav')
        matcher=AnnouncementMatcher(profile.templates)
        with patch('smart_tapo.announcement.spectral_features',wraps=spectral_features) as features:
            results=matcher.analyze_candidates(samples,rate)
            self.assertEqual(features.call_count,1)
        self.assertEqual({r.reference_offset_seconds for r in results if r.label=='paid_ready'},{0.,-2.})
        with patch('smart_tapo.announcement.spectral_features',wraps=spectral_features) as features:
            selected=matcher.analyze(samples,rate)
            self.assertEqual(features.call_count,1)
        self.assertEqual(len(selected),2)
        self.assertTrue(next(r for r in selected if r.label=='paid_ready').candidate)
        self.assertEqual(len(matcher.templates),2)
        self.assertIn('paid_ready@-2.0',matcher.diagnostic_snapshot['labels'])
        end_samples,end_rate=read_wav(BUNDLED_MANIFEST.parent/'end.wav')
        end_results=matcher.analyze_candidates(end_samples,end_rate)
        self.assertTrue(any(r.label=='end' and r.reference_offset_seconds==0. for r in end_results))


    def test_bundled_field_kit_imports_through_real_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = prepare_profile(directory, 'cabinet-1', BUNDLED_MANIFEST)
            profile = load_announcement_profile(installed, 'cabinet-1')
            self.assertEqual({t.label for t in profile.templates}, {'paid_ready', 'end'})
            settings = {t.label: (t.threshold, t.temporal_threshold) for t in profile.templates}
            self.assertEqual(settings, {'paid_ready': (.6, .2), 'end': (.55, .3)})

    def test_kit_beside_application_overrides_bundled(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(default_prepared_manifest(directory), BUNDLED_MANIFEST)
            beside = Path(directory) / 'prepared-profile' / 'profile.json'
            beside.parent.mkdir()
            beside.write_text('{}', encoding='utf-8')
            self.assertEqual(default_prepared_manifest(directory), beside)


class PreparedProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kit = self.root / 'prepared-profile'
        self.kit.mkdir()
        self.data = self.root / 'data'
        self.manifest = self.kit / 'profile.json'
        entries = []
        for kind, name, frequency in [('paid_ready', 'ready.wav', 370), ('end', 'end.wav', 610)]:
            t = np.arange(32000) / 16000
            samples = (10000 * np.sin(2 * np.pi * frequency * t) * (0.6 + 0.4 * np.sin(13 * t))).astype('<i2')
            with wave.open(str(self.kit / name), 'wb') as output:
                output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                output.writeframes(samples.tobytes())
            entries.append(dict(kind=kind, file=name, sha256=hashlib.sha256(
                (self.kit / name).read_bytes()).hexdigest(), start=0.1, end=1.4, threshold=.85))
        self.source = dict(schema_version=1, profile_id='provided', camera_id='provided-recordings', templates=entries)
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.source), encoding='utf-8')

    def prepare(self, camera='configured-camera'):
        return prepare_profile(self.data, camera, self.manifest)

    def fails(self, call=None):
        with self.assertRaisesRegex(ValueError, '^prepared_profile_unavailable$'):
            (call or self.prepare)()

    def test_bind_unchanged_and_reuse(self):
        original = {p.name: p.read_bytes() for p in self.kit.iterdir()}
        result = self.prepare()
        self.assertEqual(load_announcement_profile(result, 'configured-camera').camera_id, 'configured-camera')
        self.assertEqual(self.prepare(), result)
        self.assertEqual(len(list(result.parent.parent.glob('private_*'))), 1)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.kit.iterdir()})

    def test_temporal_kit_is_preserved_and_never_reuses_legacy_cache(self):
        old=self.prepare()
        for entry in self.source['templates']:entry['temporal_threshold']=.3
        self.save();new=self.prepare()
        self.assertNotEqual(new,old)
        self.assertTrue(old.exists())
        self.assertEqual([t.temporal_threshold for t in load_announcement_profile(new,'configured-camera').templates],[.3,.3])
        self.assertEqual(self.prepare(),new)

    def test_new_camera_and_valid_changed_kit(self):
        first = self.prepare()
        second = self.prepare('other-camera')
        self.assertNotEqual(first, second)
        self.source['templates'][0]['threshold'] = .9
        self.save()
        self.assertNotEqual(second, self.prepare('other-camera'))
        self.assertTrue(first.exists())

    def test_bad_current_kit_never_uses_cache(self):
        self.prepare()
        self.manifest.write_text('{}')
        self.fails()
        self.save()
        (self.kit / 'ready.wav').write_bytes(b'corrupt')
        self.fails()

    def test_missing_current_kit_never_uses_cache(self):
        self.prepare()
        self.manifest.unlink()
        self.fails()

    def test_canonical_paths_and_invalid_camera(self):
        self.fails(lambda: self.prepare('../camera'))
        self.source['templates'][0]['file'] = '../ready.wav'
        self.save()
        self.fails()

    def test_hardlinked_source(self):
        os.link(self.kit / 'ready.wav', self.root / 'alias.wav')
        self.fails()

    def test_unsafe_receipt_and_pointer(self):
        result = self.prepare()
        receipt = result.parent.parent / 'prepared-selection.json'
        os.link(receipt, self.root / 'receipt-alias')
        self.fails()
        (self.root / 'receipt-alias').unlink()
        data = json.loads(receipt.read_text())
        data['profile'] = '../elsewhere/profile.json'
        receipt.write_text(json.dumps(data))
        self.fails()

    def test_symlink_ancestry(self):
        parent = self.root / 'alias'
        parent.mkdir()
        alias = parent / 'prepared-profile'
        try:
            alias.symlink_to(self.kit, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.fails(lambda: prepare_profile(self.data, 'camera', alias / 'profile.json'))

    def test_installed_settings_must_match_exactly(self):
        first = self.prepare()
        content = json.loads(first.read_text())
        content['templates'][0]['threshold'] = .99
        first.write_text(json.dumps(content))
        self.assertNotEqual(first, self.prepare())

    def test_installed_hardlink_fails_closed(self):
        first = self.prepare()
        os.link(first.parent / 'ready.wav', self.root / 'installed-alias.wav')
        self.fails()

    def test_missing_cache_is_rebuilt(self):
        first = self.prepare()
        first.unlink()
        second = self.prepare()
        self.assertNotEqual(first, second)

    def test_failed_publication_is_failure_and_retains_prior(self):
        first = self.prepare()
        pointer = first.parent.parent / 'prepared-selection.json'
        before = pointer.read_bytes()
        with patch('smart_tapo.prepared_profile.os.replace', side_effect=OSError('private details')):
            self.fails(lambda: self.prepare('other-camera'))
        self.assertEqual(pointer.read_bytes(), before)
        self.assertFalse(list(pointer.parent.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
