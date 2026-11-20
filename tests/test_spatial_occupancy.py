"""Partial occupancy regressions without private media or provider access."""
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from PIL import ImageDraw

from smart_tapo.config import Config
from smart_tapo.vision import Detector
from tests.test_alignment_regression import textured_reference


class SpatialOccupancyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='smarttapo-partial-regression-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = replace(Config(), baseline_path=root / 'empty.png', data_dir=root / 'data',
                              alignment_enabled=True)
        self.reference = textured_reference(self.config)
        self.reference.save(self.config.baseline_path)
        self.detector = Detector(self.config)

    def test_coherent_partial_object_detected_despite_small_whole_roi_average(self):
        current = self.reference.copy()
        x, y = self.config.roi.x + 35, self.config.roi.y + 40
        ImageDraw.Draw(current).rectangle((x, y, x + 29, y + 29), fill=(200, 200, 200))
        before = self.config.baseline_path.read_bytes()
        observation = self.detector.observe_image(current)
        self.assertLess(observation.score, self.config.difference_threshold)
        self.assertLess(observation.ratio, self.config.changed_pixel_ratio)
        self.assertEqual(observation.label, 'PRESENT', observation.alignment_reason)
        self.assertGreaterEqual(observation.local_score, self.config.difference_threshold)
        self.assertGreaterEqual(observation.local_ratio, self.config.changed_pixel_ratio)
        self.assertGreater(observation.component_area, 0)
        self.assertEqual(self.config.baseline_path.read_bytes(), before)

    def test_small_isolated_bright_patch_does_not_meet_coherent_support(self):
        current = self.reference.copy()
        x, y = self.config.roi.x + 10, self.config.roi.y + 10
        ImageDraw.Draw(current).rectangle((x, y, x + 9, y + 9), fill=(200, 200, 200))
        observation = self.detector.observe_image(current)
        self.assertEqual(observation.label, 'EMPTY', observation.alignment_reason)
        self.assertEqual(observation.component_area, 0)

    def test_empty_reference_stays_empty(self):
        observation = self.detector.observe_image(self.reference.copy())
        self.assertEqual(observation.label, 'EMPTY', observation.alignment_reason)

    def test_uniform_luminance_change_is_not_promoted_to_presence(self):
        current = self.reference.point(lambda value: min(255, value + 20))
        observation = self.detector.observe_image(current)
        self.assertNotEqual(observation.label, 'PRESENT')

    def test_local_rule_does_not_change_unaligned_legacy_detection(self):
        detector = Detector(replace(self.config, alignment_enabled=False))
        current = self.reference.copy()
        x, y = self.config.roi.x + 35, self.config.roi.y + 40
        ImageDraw.Draw(current).rectangle((x, y, x + 29, y + 29), fill=(200, 200, 200))
        observation = detector.observe_image(current)
        self.assertEqual(observation.label, 'EMPTY')
        self.assertEqual(observation.component_area, 0)


if __name__ == '__main__':
    unittest.main()
