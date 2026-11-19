"""Known shipped FV2 reference upgrades are explicit; custom selections stay intact."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from smart_tapo.announcement_profile import load_announcement_profile
from smart_tapo.prepared_profile import (BUNDLED_MANIFEST, default_prepared_manifest,
    needs_prepared_upgrade, prepare_profile)


class ReadyReferenceUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kit = self.root / 'prepared-profile'
        self.kit.mkdir()
        for name in ('ready.wav', 'end.wav'):
            shutil.copyfile(BUNDLED_MANIFEST.parent / name, self.kit / name)
        data = json.loads(BUNDLED_MANIFEST.read_text(encoding='utf-8'))
        ready = next(entry for entry in data['templates'] if entry['kind'] == 'paid_ready')
        ready['start'], ready['end'] = 2.0, 6.0
        ready.pop('alternates',None)
        self.old = self.kit / 'profile.json'
        self.old.write_text(json.dumps(data), encoding='utf-8')
        self.data = self.root / 'data'

    def test_bundled_uses_later_voice_span_not_lower_threshold_or_new_recording(self):
        data = json.loads(BUNDLED_MANIFEST.read_text(encoding='utf-8'))
        ready = next(entry for entry in data['templates'] if entry['kind'] == 'paid_ready')
        self.assertEqual((ready['start'], ready['end'], ready['threshold'], ready['temporal_threshold']),
                         (4.0, 8.0, .6, .2))
        self.assertEqual(ready['sha256'], 'd10177ae6d0e5d7609ef99bac0612181bc047dec98e590f1521213c439f28f77')

    def test_managed_old_fv2_preflight_is_read_only_then_explicit_copy(self):
        self.assertEqual(load_announcement_profile(self.old, 'provided-recordings').fingerprint,
                         '44b6c450642e9410206ccc76f45edc15d08416e89460160bcb08ebc98894ff99')
        selected = prepare_profile(self.data, 'cam1', self.old)
        before = selected.read_bytes()
        pointer = selected.parent.parent / 'prepared-selection.json'
        pointer_before = pointer.read_bytes()
        self.assertTrue(needs_prepared_upgrade(self.data, 'cam1', selected, BUNDLED_MANIFEST))
        self.assertEqual(pointer.read_bytes(), pointer_before)
        self.assertEqual(selected.read_bytes(), before)
        current = prepare_profile(self.data, 'cam1', BUNDLED_MANIFEST)
        self.assertNotEqual(current, selected)
        self.assertEqual(selected.read_bytes(), before)
        self.assertFalse(needs_prepared_upgrade(self.data, 'cam1', current, BUNDLED_MANIFEST))

    def test_v53_single_voice_managed_reference_upgrades_and_cache_is_idempotent(self):
        data=json.loads(self.old.read_text(encoding='utf-8'))
        ready=next(e for e in data['templates'] if e['kind']=='paid_ready')
        ready['start'],ready['end']=4.,8.
        self.old.write_text(json.dumps(data),encoding='utf-8')
        self.assertEqual(load_announcement_profile(self.old,'provided-recordings').fingerprint,
            '5543e07cfe9ff78e2223ce41367752f5a9f0c0325d439ba7b8dc1ca60ef3665c')
        self.assertEqual(default_prepared_manifest(self.root),BUNDLED_MANIFEST)
        selected=prepare_profile(self.data,'cam1',self.old)
        self.assertTrue(needs_prepared_upgrade(self.data,'cam1',selected,BUNDLED_MANIFEST))
        current=prepare_profile(self.data,'cam1',BUNDLED_MANIFEST)
        self.assertEqual(current,prepare_profile(self.data,'cam1',BUNDLED_MANIFEST))
        self.assertFalse(needs_prepared_upgrade(self.data,'cam1',current,BUNDLED_MANIFEST))
        data=json.loads(current.read_text(encoding='utf-8'))
        ready=next(e for e in data['templates'] if e['kind']=='paid_ready')
        ready['alternates']=[dict(start=3.,end=7.)]
        current.write_text(json.dumps(data),encoding='utf-8')
        self.assertFalse(needs_prepared_upgrade(self.data,'cam1',current,BUNDLED_MANIFEST))

    def test_customized_old_selection_is_not_automatically_upgraded(self):
        selected = prepare_profile(self.data, 'cam1', self.old)
        data = json.loads(selected.read_text(encoding='utf-8'))
        data['templates'][0]['threshold'] = .91
        selected.write_text(json.dumps(data), encoding='utf-8')
        self.assertFalse(needs_prepared_upgrade(self.data, 'cam1', selected, BUNDLED_MANIFEST))

    def test_legacy_v1_reference_still_has_explicit_upgrade_path(self):
        data = json.loads(self.old.read_text(encoding='utf-8'))
        data.pop('feature_version')
        self.old.write_text(json.dumps(data), encoding='utf-8')
        selected = prepare_profile(self.data, 'cam1', self.old)
        self.assertEqual(load_announcement_profile(selected, 'cam1').feature_version, 1)
        self.assertTrue(needs_prepared_upgrade(self.data, 'cam1', selected, BUNDLED_MANIFEST))

    def test_known_fv1_sidecars_and_explicit_version_have_consent_path(self):
        original=json.loads(self.old.read_text(encoding='utf-8'))
        for start,end in ((2.,6.),(4.,8.)):
            for explicit in (False,True):
                with self.subTest(start=start,explicit=explicit):
                    data=json.loads(json.dumps(original))
                    ready=next(e for e in data['templates'] if e['kind']=='paid_ready')
                    ready['start'],ready['end']=start,end
                    if explicit:data['feature_version']=1
                    else:data.pop('feature_version')
                    self.old.write_text(json.dumps(data),encoding='utf-8')
                    self.assertEqual(default_prepared_manifest(self.root),BUNDLED_MANIFEST)
                    selected=prepare_profile(self.data,'cam1',self.old)
                    self.assertTrue(needs_prepared_upgrade(self.data,'cam1',selected,BUNDLED_MANIFEST))
                    pointer=selected.parent.parent/'prepared-selection.json'
                    receipt=json.loads(pointer.read_text(encoding='utf-8'))
                    receipt['reference_fingerprint']='f'*64
                    pointer.write_text(json.dumps(receipt),encoding='utf-8')
                    self.assertFalse(needs_prepared_upgrade(self.data,'cam1',selected,BUNDLED_MANIFEST))

    def test_unknown_reference_pointer_does_not_qualify_as_known_old_fv2(self):
        selected = prepare_profile(self.data, 'cam1', self.old)
        pointer = selected.parent.parent / 'prepared-selection.json'
        data = json.loads(pointer.read_text(encoding='utf-8'))
        data['reference_fingerprint'] = 'f' * 64
        pointer.write_text(json.dumps(data), encoding='utf-8')
        self.assertFalse(needs_prepared_upgrade(self.data, 'cam1', selected, BUNDLED_MANIFEST))

    def test_known_old_sidecar_does_not_shadow_new_bundled_voice_reference(self):
        before = self.old.read_bytes()
        self.assertEqual(default_prepared_manifest(self.root), BUNDLED_MANIFEST)
        self.assertEqual(self.old.read_bytes(), before)

    def test_custom_or_corrupt_sidecar_is_not_bypassed(self):
        data = json.loads(self.old.read_text(encoding='utf-8'))
        data['templates'][0]['threshold'] = .91
        self.old.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(default_prepared_manifest(self.root), self.old)
        self.old.write_text('{}', encoding='utf-8')
        self.assertEqual(default_prepared_manifest(self.root), self.old)
