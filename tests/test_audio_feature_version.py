"""Versioned offline acoustic evidence; synthetic fixtures, no device/network access."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from smart_tapo.announcement import AnnouncementMatcher, PhraseTemplate, spectral_features
from smart_tapo.announcement_profile import load_announcement_profile
from smart_tapo.prepared_profile import needs_prepared_upgrade, prepare_profile
from smart_tapo.profile_setup import create_private_profile


def phrase():
    t = np.arange(48000) / 8000
    y = np.zeros_like(t)
    for i, frequency in enumerate((410, 730, 1150, 590, 1640, 870, 1280, 480)):
        start = 1 + i * .32
        selected = (t >= start) & (t < start + .25)
        u = t[selected] - start
        y[selected] = np.sin(np.pi * u / .25)**2 * (
            .2 * np.sin(2 * np.pi * frequency * u) + .08 * np.sin(2 * np.pi * frequency * 1.7 * u))
    return y


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.y = phrase() + np.random.default_rng(4831).normal(0, .002, 48000)

    def template(self, version=2):
        return PhraseTemplate.from_recording('end', self.y, 8000, .95, 3.7, .8, .3,
                                             feature_version=version)

    def test_default_is_exact_legacy_and_positional_calls_remain_valid(self):
        np.testing.assert_array_equal(spectral_features(self.y), spectral_features(self.y, 1))
        legacy = PhraseTemplate.from_recording('end', self.y, 8000, .95, 3.7, .8, .3)
        self.assertEqual(legacy.feature_version, 1)
        np.testing.assert_array_equal(legacy.features, self.template(1).features)
        self.assertEqual(PhraseTemplate('end', legacy.features, .8, .3).feature_version, 1)

    def test_context_regression_is_numerical_not_missing_api(self):
        # Same samples at the same frame alignment, but a different recording floor.
        context = np.random.default_rng(901).normal(0, .08, 64000)
        longer = np.r_[context, self.y]
        legacy_error = np.mean(np.abs(spectral_features(longer, 1)[:, 800:] - spectral_features(self.y, 1)))
        current_error = np.mean(np.abs(spectral_features(longer, 2)[:, 800:] - spectral_features(self.y, 2)))
        self.assertGreater(legacy_error, .01)
        self.assertLess(current_error, 1e-6)
        self.assertLess(current_error, legacy_error / 100)

    def test_template_and_both_analysis_paths_use_the_same_version(self):
        template = self.template()
        np.testing.assert_array_equal(template.features, spectral_features(self.y, 2)[:, 95:370])
        matcher = AnnouncementMatcher([template])
        with patch('smart_tapo.announcement.spectral_features', wraps=spectral_features) as features:
            self.assertTrue(matcher.analyze(self.y, 8000)[0].candidate)
            self.assertEqual(features.call_args.args[1], 2)
            self.assertEqual(len(matcher.analyze_candidates(self.y, 8000)), 1)
            self.assertEqual(features.call_args.args[1], 2)
        self.assertTrue(matcher.diagnostic_snapshot['analyzed'])

    def test_mixed_and_invalid_versions_fail_closed(self):
        for value in (True, False, None, 0, 3, 2., '2'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    spectral_features(self.y, value)
                with self.assertRaises(ValueError):
                    PhraseTemplate('end', self.template().features, .8, .3, value)
        with self.assertRaisesRegex(ValueError, 'Mixed'):
            AnnouncementMatcher([self.template(1), PhraseTemplate('ready', self.template(2).features, .8, .3, 2)])

    def test_v2_gain_silence_context_and_input_immutability(self):
        matcher = AnnouncementMatcher([self.template()])
        for gain in (1., .1):
            samples = np.pad(self.y * gain, (40000, 72000))
            before = samples.copy()
            results = matcher.analyze_candidates(samples, 8000)
            self.assertEqual(len(results), 1)
            self.assertAlmostEqual(results[0].onset_seconds, 5.95, places=2)
            self.assertGreater(results[0].temporal_score, .95)
            np.testing.assert_array_equal(samples, before)
        self.assertFalse(spectral_features(np.zeros(160000), 2).any())

    def test_v2_rejects_full_negative_signals_not_just_maxima(self):
        matcher = AnnouncementMatcher([self.template()])
        t = np.arange(48000) / 8000
        rng = np.random.default_rng(7741)
        impact = np.zeros(48000)
        impact[16000:16100] = np.hanning(100)
        # Reverse/permuted phrase, tone, broadband noise, impulse and silence.
        permuted = np.concatenate(np.array_split(self.y, 12)[::-1])
        for samples in (np.zeros(48000), .2 * np.sin(2*np.pi*610*t),
                        rng.normal(0, .08, 48000), impact, self.y[::-1], permuted):
            with self.subTest(energy=float(np.sum(samples*samples))):
                self.assertEqual(matcher.analyze_candidates(samples, 8000), ())
                self.assertFalse(matcher.analyze(samples, 8000)[0].candidate)
        for samples in (np.full(8000, np.nan), np.full(8000, np.inf), np.full(8000, 2.)):
            with self.assertRaises(ValueError):
                matcher.analyze_candidates(samples, 8000)

    def test_temporal_guard_remains_required_in_diagnostic_analysis(self):
        matcher = AnnouncementMatcher([self.template()])
        with patch('smart_tapo.announcement._temporal_correlation',
                   return_value=np.zeros(323)):
            self.assertFalse(matcher.analyze(self.y, 8000)[0].candidate)
            self.assertEqual(matcher.analyze_candidates(self.y, 8000), ())


class ProfileVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kit = self.root / 'prepared-profile'
        self.kit.mkdir()
        self.path = self.kit / 'profile.json'
        self.data_dir = self.root / 'data'
        entries = []
        for kind, name in (('paid_ready', 'ready.wav'), ('end', 'end.wav')):
            samples = phrase()
            if kind == 'paid_ready':
                samples = samples[::-1]
            with wave.open(str(self.kit / name), 'wb') as output:
                output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                output.writeframes((samples * 32767).astype('<i2').tobytes())
            entries.append(dict(kind=kind, file=name, sha256=hashlib.sha256(
                (self.kit/name).read_bytes()).hexdigest(), start=.95, end=3.7,
                threshold=.8, temporal_threshold=.3))
        self.data = dict(schema_version=1, profile_id='provided', camera_id='provided-recordings', templates=entries)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding='utf8')

    def load(self):
        return load_announcement_profile(self.path, 'provided-recordings')

    def prepare(self):
        return prepare_profile(self.data_dir, 'camera', self.path)

    def upgrade_needed(self, selected):
        return needs_prepared_upgrade(self.data_dir, 'camera', selected, self.path)

    def test_legacy_fingerprint_exact_original_canonical_formula(self):
        expected = hashlib.sha256(json.dumps(self.data, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True).encode('ascii') + b'\n' + '\n'.join(
                e['sha256'] for e in self.data['templates']).encode('ascii')).hexdigest()
        self.assertEqual(self.load().fingerprint, expected)
        self.assertEqual(self.load().feature_version, 1)

    def test_version_bound_fingerprint_and_loaded_templates(self):
        legacy = self.load()
        self.data['feature_version'] = 1
        self.save()
        explicit = self.load()
        self.data['feature_version'] = 2
        self.save()
        current = self.load()
        self.assertNotEqual(current.fingerprint, legacy.fingerprint)
        self.assertNotEqual(current.fingerprint, explicit.fingerprint)
        self.assertEqual(current.feature_version, 2)
        self.assertEqual([t.feature_version for t in current.templates], [2, 2])
        self.assertEqual(self.load().fingerprint, current.fingerprint)
        for template in current.templates:
            self.assertFalse(template.features.flags.writeable)
            with self.assertRaises(ValueError):
                template.features.flags.writeable = True

    def test_invalid_root_and_per_template_versions(self):
        for value in (True, False, None, 0, 3, 2., '2', {}):
            self.data['feature_version'] = value
            self.save()
            with self.assertRaisesRegex(ValueError, '^invalid_announcement_profile$'):
                self.load()
        self.data['feature_version'] = 2
        self.data['templates'][0]['feature_version'] = 1
        self.save()
        with self.assertRaises(ValueError):
            self.load()

    def test_private_import_v2_roundtrip_and_strict_argument(self):
        records = [{**{k:v for k,v in e.items() if k != 'sha256'}, 'file': self.kit/e['file']}
                   for e in self.data['templates']]
        for value in (True, None, 3, 2.):
            with self.assertRaisesRegex(ValueError, '^invalid_private_profile$'):
                create_private_profile(self.data_dir, 'camera', records, feature_version=value)
        installed = create_private_profile(self.data_dir, 'camera', records, feature_version=2)
        self.assertEqual(load_announcement_profile(installed, 'camera').feature_version, 2)
        self.assertEqual(json.loads(installed.read_text())['feature_version'], 2)
        self.assertFalse(self.upgrade_needed(installed))

    def test_managed_upgrade_is_readonly_new_copy_preserves_old_and_reuses_v2(self):
        old = self.prepare()
        old_files = {p.name:p.read_bytes() for p in old.parent.iterdir()}
        self.assertFalse(self.upgrade_needed(old))
        self.data['feature_version'] = 2
        self.data['profile_id'] = 'provided_v2'
        self.save()
        before = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertTrue(self.upgrade_needed(old))
        after = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)  # cancellation/preflight does not write
        new = self.prepare()
        self.assertNotEqual(old, new)
        self.assertEqual(old_files, {p.name:p.read_bytes() for p in old.parent.iterdir()})
        self.assertEqual(load_announcement_profile(new, 'camera').feature_version, 2)
        self.assertEqual(self.prepare(), new)
        self.assertFalse(self.upgrade_needed(old))
        self.assertFalse(self.upgrade_needed(new))

    def test_custom_changed_wrong_camera_or_malicious_pointer_not_upgradeable(self):
        old = self.prepare()
        self.data['feature_version'] = 2
        self.save()
        self.assertFalse(needs_prepared_upgrade(self.data_dir, 'other', old, self.path))
        self.assertFalse(self.upgrade_needed(self.root/'custom'/'profile.json'))
        content = json.loads(old.read_text())
        content['templates'][0]['threshold'] = .99
        old.write_text(json.dumps(content))
        self.assertFalse(self.upgrade_needed(old))
        pointer = old.parent.parent/'prepared-selection.json'
        receipt = json.loads(pointer.read_text())
        receipt['profile'] = '../custom/profile.json'
        pointer.write_text(json.dumps(receipt))
        self.assertFalse(self.upgrade_needed(old))
        self.assertFalse(self.upgrade_needed(None))

    def test_unpointed_private_v1_and_wrong_identity_never_upgrade(self):
        pointed = self.prepare()
        records = [{**{k:v for k,v in e.items() if k != 'sha256'}, 'file': self.kit/e['file']}
                   for e in self.data['templates']]
        custom = create_private_profile(self.data_dir, 'camera', records)
        self.data['feature_version'] = 2
        self.save()
        self.assertTrue(self.upgrade_needed(pointed))
        self.assertFalse(self.upgrade_needed(custom))
        content = json.loads(pointed.read_text())
        content['profile_id'] = 'custom'
        pointed.write_text(json.dumps(content))
        self.assertFalse(self.upgrade_needed(pointed))

    def test_symlink_ancestry_preflight_fails_closed(self):
        old = self.prepare()
        self.data['feature_version'] = 2
        self.save()
        alias = self.root/'alias'
        try:
            alias.symlink_to(self.data_dir, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.assertFalse(needs_prepared_upgrade(alias, 'camera',
            alias/'announcement-profiles'/old.parent.name/'profile.json', self.path))

    def test_hardlink_preflight_fails_closed(self):
        old = self.prepare()
        self.data['feature_version'] = 2
        self.save()
        os.link(old, self.root/'alias.json')
        self.assertFalse(self.upgrade_needed(old))

    def test_receipt_cannot_reuse_wrong_installed_feature_version(self):
        self.data['feature_version'] = 2
        self.save()
        old = self.prepare()
        content = json.loads(old.read_text())
        del content['feature_version']
        old.write_text(json.dumps(content))
        new = self.prepare()
        self.assertNotEqual(new, old)
        self.assertEqual(load_announcement_profile(new, 'camera').feature_version, 2)


if __name__ == '__main__':
    unittest.main()
