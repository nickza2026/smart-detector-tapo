"""Synthetic temporary inputs; native lifecycle test requires the offscreen runner."""
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
from smart_tapo.profile_setup import create_private_profile


def synthetic_wav(path, seed=19):
    samples = (np.random.default_rng(seed).normal(0, .15, 24000) * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        writer.writeframes(samples.tobytes())


class PrivateProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.sources = [self.root / 'a.wav', self.root / 'b.wav']
        for seed, path in enumerate(self.sources):
            synthetic_wav(path, seed)
        self.records = [dict(kind=kind, file=path, start=.1, end=2., threshold=.9)
                        for kind, path in zip(('paid_ready', 'end'), self.sources)]

    def create(self):
        return create_private_profile(self.data, 'camera_a', self.records)

    def test_explicit_temporal_constraint_survives_private_copy(self):
        for record in self.records:record['temporal_threshold']=.3
        path=self.create();profile=load_announcement_profile(path,'camera_a')
        self.assertEqual([t.temporal_threshold for t in profile.templates],[.3,.3])
        data=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual([t['temporal_threshold'] for t in data['templates']],[.3,.3])

    def test_invalid_temporal_constraint_never_creates_private_profile(self):
        for value in (True,None,0,-.1,1.01,float('nan'),'.3'):
            with self.subTest(value=value):
                self.records[0]['temporal_threshold']=value
                self.reject()

    def reject(self):
        with self.assertRaisesRegex(ValueError, '^invalid_private_profile$') as caught:
            self.create()
        self.assertIsNone(caught.exception.__cause__)
        profiles = self.data / 'announcement-profiles'
        if profiles.exists():
            self.assertEqual(list(profiles.iterdir()), [])

    def test_loader_success_hashes_sources_unchanged_and_exclusive(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.sources]
        manifest = self.create()
        result = load_announcement_profile(manifest, 'camera_a')
        self.assertEqual(result.camera_id, 'camera_a')
        self.assertEqual([t.label for t in result.templates], ['paid_ready', 'end'])
        self.assertEqual(manifest.parent.parent, self.data / 'announcement-profiles')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(set(data), {'schema_version', 'profile_id', 'camera_id', 'templates'})
        self.assertEqual({p.name for p in manifest.parent.iterdir()}, {'ready.wav', 'end.wav', 'profile.json'})
        for record, original in zip(data['templates'], self.sources):
            copied = manifest.parent / record['file']
            self.assertEqual(copied.read_bytes(), original.read_bytes())
            self.assertEqual(record['sha256'], hashlib.sha256(copied.read_bytes()).hexdigest())
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.sources])
        second = self.create()
        self.assertNotEqual(manifest.parent, second.parent)
        self.assertTrue(manifest.is_file())

    def test_invalid_bounds_thresholds_and_types(self):
        cases = [('start', True), ('end', False), ('threshold', True),
                 ('start', float('nan')), ('end', float('inf')), ('threshold', float('nan')),
                 ('start', -.1), ('end', .2), ('end', 10), ('end', 21),
                 ('threshold', .49), ('threshold', 1.01), ('start', '0')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                old = self.records[0][key]
                self.records[0][key] = value
                self.reject()
                self.records[0][key] = old

    def test_exact_pair_and_fields(self):
        self.records[1]['kind'] = 'paid_ready'
        self.reject()
        self.records[1]['kind'] = 'end'
        self.records[0]['sha256'] = 'not accepted'
        self.reject()

    def test_traversal_rejected_even_when_target_exists(self):
        self.records[0]['file'] = self.data / '..' / 'a.wav'
        self.reject()

    def test_hardlinked_source_rejected(self):
        linked = self.root / 'hard.wav'
        try:
            os.link(self.sources[0], linked)
        except OSError:
            self.skipTest('hardlinks unavailable')
        self.records[0]['file'] = linked
        self.reject()

    def test_symlink_source_and_destination_ancestry_rejected(self):
        linked = self.root / 'link.wav'
        try:
            linked.symlink_to(self.sources[0])
        except OSError:
            self.skipTest('symlinks unavailable')
        self.records[0]['file'] = linked
        self.reject()
        self.records[0]['file'] = self.sources[0]
        profiles = self.data / 'announcement-profiles'
        outside = self.root / 'outside'
        outside.mkdir()
        if profiles.exists():
            profiles.rmdir()
        profiles.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^invalid_private_profile$'):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])

    def test_oversized_and_non_pcm_inputs_rejected(self):
        oversized = self.root / 'large.wav'
        with oversized.open('wb') as output:
            output.truncate(64 * 1024 * 1024 + 1)
        self.records[0]['file'] = oversized
        self.reject()
        bad = self.root / 'bad.wav'
        with wave.open(str(bad), 'wb') as output:
            output.setparams((1, 1, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x00' * 16000)
        self.records[0]['file'] = bad
        self.reject()

    def test_low_space_does_not_create_profile(self):
        with patch('smart_tapo.profile_setup.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            self.reject()

    def test_loader_failure_cleans_only_owned_regular_files(self):
        profiles = self.data / 'announcement-profiles'
        profiles.mkdir()
        existing = profiles / 'existing'
        existing.mkdir()
        sentinel = existing / 'keep.txt'
        sentinel.write_bytes(b'keep')
        def fail(path, camera):
            raise ValueError('private path must not escape')
        with patch('smart_tapo.profile_setup.load_announcement_profile', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '^invalid_private_profile$'):
                self.create()
        self.assertEqual(list(profiles.iterdir()), [existing])
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_cleanup_never_removes_unowned_file(self):
        saved = []
        def fail(path, camera):
            foreign = path.parent / 'foreign.txt'
            foreign.write_bytes(b'do not remove')
            saved.append(foreign)
            raise ValueError
        with patch('smart_tapo.profile_setup.load_announcement_profile', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '^invalid_private_profile$'):
                self.create()
        self.assertEqual(saved[0].read_bytes(), b'do not remove')
        self.assertEqual(list(saved[0].parent.iterdir()), saved)

    def test_source_hash_change_fails_without_partial_success(self):
        from smart_tapo.profile_setup import _hash
        calls = 0
        def altered(path):
            nonlocal calls
            calls += 1
            value = _hash(path)
            return '0' * 64 if calls == 1 else value
        with patch('smart_tapo.profile_setup._hash', side_effect=altered):
            self.reject()

    def test_uuid_collision_does_not_delete_existing_data(self):
        profiles = self.data / 'announcement-profiles'
        profiles.mkdir()
        existing = profiles / ('private_' + 'a' * 32)
        existing.mkdir()
        sentinel = existing / 'keep'
        sentinel.write_bytes(b'keep')
        with patch('smart_tapo.profile_setup.uuid.uuid4', return_value=SimpleNamespace(hex='a' * 32)):
            with self.assertRaisesRegex(ValueError, '^invalid_private_profile$'):
                self.create()
        self.assertEqual(sentinel.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
class ProfileDialogLifecycleTests(unittest.TestCase):
    def test_destroyed_editor_variables_are_not_finalized_on_next_worker(self):
        import gc
        import threading
        import tkinter as tk
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from smart_tapo.config import Config
        from smart_tapo.profile_setup import open_profile_setup
        gc.collect()
        root=tk.Tk();root.withdraw()
        names=set();deferred=[];off_thread=[]
        original_init,original_del=tk.Variable.__init__,tk.Variable.__del__
        enabled=gc.isenabled();gc.disable()
        def initialize(variable,*args,**kwargs):
            original_init(variable,*args,**kwargs);names.add(variable._name)
        def finalize(variable):
            if (threading.current_thread() is not threading.main_thread()
                    and variable._name in names and variable._tk is not None):
                off_thread.append(variable._name);deferred.append(variable)
            else:original_del(variable)
        try:
            with patch.object(tk.Variable,'__init__',initialize),patch.object(tk.Variable,'__del__',finalize):
                dialog=open_profile_setup(root,Config(),lambda path:None)
                dialog.request_close()
                self.assertFalse(any(root.tk.getboolean(root.tk.call('info','exists',name)) for name in names),
                                 'Closed editor must release Tcl variables on the owner thread')
                with ThreadPoolExecutor(max_workers=1) as executor:executor.submit(gc.collect).result(timeout=5)
                self.assertEqual(off_thread,[],'Closed editor retained Tcl variables for background GC')
        finally:
            for variable in deferred:original_del(variable);variable._tk=None
            if enabled:gc.enable()
            root.destroy()

