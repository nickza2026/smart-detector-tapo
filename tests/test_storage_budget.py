import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from smart_tapo.config import Config
from smart_tapo.storage_budget import estimate_storage, apply_storage_budget, MAX_SAMPLES


class StorageBudgetTests(unittest.TestCase):
    def test_event_budget_does_not_reserve_existing_images_twice(self):
        from smart_tapo.storage_budget import apply_event_storage_budget
        events=self.config.data_dir/'events';events.mkdir(parents=True)
        with (events/'already-stored.png').open('wb') as file:file.truncate(4_000_000)
        config=replace(self.config,max_image_storage_mb=4.096)
        with patch('smart_tapo.storage_budget.shutil.disk_usage',return_value=SimpleNamespace(free=1_001_000_000)):
            actual,_=apply_event_storage_budget(config)
        self.assertEqual(actual,config)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = replace(Config(), data_dir=self.root/'data', baseline_path=self.root/'ref.png')

    def test_cadences_retention_cleanup_headroom_and_changed_size(self):
        for size in (100, 200):
            self.config.baseline_path.write_bytes(b'x'*size)
            for cadence in (1, .5, .2):
                for minutes in (5, 10):
                    cfg = replace(self.config, capture_interval_seconds=cadence, rolling_frames_minutes=minutes)
                    budget = estimate_storage(cfg)
                    frames = math.ceil((minutes*60+30)/cadence)+1
                    self.assertEqual(budget.frames, frames)
                    self.assertEqual(budget.png_bytes, size)
                    self.assertEqual(budget.recommended_mb, math.ceil(frames*size*1.25/1_000_000))

    def test_unknown_and_invalid_numbers(self):
        self.assertIsNone(estimate_storage(self.config).recommended_mb)
        result, _ = apply_storage_budget(self.config)
        self.assertEqual(result, self.config)
        for value in (0, float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                estimate_storage(replace(self.config, capture_interval_seconds=value))

    def test_recent_samples_bounded_and_protected_usage_added(self):
        frames = self.config.data_dir/'frames'/'camera'; frames.mkdir(parents=True)
        for i in range(80):
            path = frames/f'{i}.png'; path.write_bytes(b'x'*(i+1))
            import os
            os.utime(path, ns=(i+1, i+1))
        events = self.config.data_dir/'events'; events.mkdir()
        (events/'e.png').write_bytes(b'x'*1_000_000)
        result = estimate_storage(self.config)
        self.assertEqual(result.png_bytes, 80)
        self.assertEqual(result.existing_bytes, sum(range(1,81))+1_000_000)
        self.assertEqual(result.recommended_mb, math.ceil((result.frames*80*1.25+1_000_000)/1_000_000))
        self.assertEqual(MAX_SAMPLES,64)

    def test_raise_only_and_disk_additional_not_double_counted(self):
        self.config.baseline_path.write_bytes(b'x'*100)
        cfg = replace(self.config, max_image_storage_mb=4096)
        with patch('smart_tapo.storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=10_000_000_000)):
            result, _ = apply_storage_budget(cfg)
        self.assertEqual(result, cfg)
        with patch('smart_tapo.storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=1_000_000_000)):
            with self.assertRaisesRegex(ValueError, 'พื้นที่ดิสก์ไม่พอ'): apply_storage_budget(cfg)

    def test_existing_image_usage_is_deducted_from_required_disk_allocation(self):
        self.config.baseline_path.write_bytes(b'x'*100)
        frames = self.config.data_dir/'frames'; frames.mkdir(parents=True)
        with (frames/'already-allocated.tmp').open('wb') as file:
            file.truncate(4_000_000)
        cfg = replace(self.config,max_image_storage_mb=4.096)
        with patch('smart_tapo.storage_budget.shutil.disk_usage',return_value=SimpleNamespace(free=1_001_000_000)):
            result, budget = apply_storage_budget(cfg)
        self.assertEqual(result,cfg)
        self.assertEqual(budget.existing_bytes,4_000_000)

    def test_symlink_reference_not_followed(self):
        target = self.root/'target.png'; target.write_bytes(b'x'*100)
        try: self.config.baseline_path.symlink_to(target)
        except OSError: self.skipTest('symlink privilege unavailable')
        self.assertIsNone(estimate_storage(self.config).png_bytes)
