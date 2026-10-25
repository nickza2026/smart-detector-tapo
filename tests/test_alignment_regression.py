from __future__ import annotations

import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from smart_tapo.config import Config
from smart_tapo.vision import Detector


def textured_reference(config):
    image = Image.new('RGB', config.expected_size, 'gray')
    draw = ImageDraw.Draw(image)
    randomizer = random.Random(45156)
    for _ in range(900):
        x = randomizer.randrange(15, config.expected_size[0] - 15)
        y = randomizer.randrange(config.video.y + 30, config.video.y + config.video.height - 45)
        shade = randomizer.choice([10, 40, 190, 240])
        draw.rectangle((x, y, x + 8, y + 8), fill=(shade, shade, shade))
    draw.rectangle(config.roi.box(), fill=(70, 70, 70))
    return image


class AlignmentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(Config(), baseline_path=Path(self.temp.name) / 'empty.png')
        self.reference = textured_reference(self.config)
        self.reference.save(self.config.baseline_path)
        self.detector = Detector(self.config)

    def transform(self, source, coefficients):
        result = source.copy()
        video = source.crop(self.config.video.box())
        video = video.transform(video.size, Image.Transform.AFFINE, coefficients,
                                resample=Image.Resampling.BILINEAR, fillcolor='black')
        result.paste(video, (self.config.video.x,self.config.video.y))
        return result

    def test_empty_scene_translation_is_not_occupancy(self):
        shifted = self.transform(self.reference,(1,0,-42,0,1,15))
        observation = self.detector.observe_image(shifted)
        self.assertEqual(observation.label, 'EMPTY',
                         'Moving the camera must not turn an empty scene into PRESENT')
        self.assertEqual(len(observation.view_corners),8)

    def test_object_survives_registration_and_reference_is_immutable(self):
        original = self.config.baseline_path.read_bytes()
        occupied = self.reference.copy()
        x,y,w,h = self.config.roi.x,self.config.roi.y,self.config.roi.width,self.config.roi.height
        ImageDraw.Draw(occupied).rectangle((x+10,y+15,x+w-10,y+h-15),fill='white')
        observation = self.detector.observe_image(self.transform(occupied,(1,0,-42,0,1,15)))
        self.assertEqual(observation.label,'PRESENT')
        self.assertGreater(observation.score,self.config.difference_threshold)
        self.assertEqual(self.config.baseline_path.read_bytes(),original)
        self.assertEqual(self.detector.observe_image(self.reference).label,'EMPTY')

    def test_rotation_is_registered(self):
        moved = self.reference.copy()
        video = self.reference.crop(self.config.video.box()).rotate(4,resample=Image.Resampling.BILINEAR)
        moved.paste(video,(self.config.video.x,self.config.video.y))
        self.assertEqual(self.detector.observe_image(moved).label,'EMPTY')

    def test_featureless_occluded_and_out_of_bounds_are_unknown(self):
        blank = Image.new('RGB',self.config.expected_size,'gray')
        self.assertEqual(self.detector.observe_image(blank).label,'UNKNOWN')
        self.assertEqual(self.detector.observe_image(self.transform(self.reference,(1,0,-300,0,1,0))).label,'UNKNOWN')
        blank.save(self.config.baseline_path)
        no_anchors = Detector(self.config)
        self.assertEqual(no_anchors.observe_image(blank).label,'UNKNOWN')

    def test_ui_clock_logo_and_player_overlays_are_not_anchors(self):
        current = self.reference.copy()
        draw = ImageDraw.Draw(current)
        draw.rectangle((0,0,719,self.config.video.y-1),fill='black')
        draw.rectangle((0,self.config.video.y,719,self.config.video.y+24),fill='white')
        bottom = self.config.video.y+self.config.video.height
        draw.rectangle((0,bottom-70,719,1279),fill='white')
        observation = self.detector.observe_image(current)
        self.assertEqual(observation.label,'EMPTY')

    def test_configuration_rejects_unsafe_alignment_values(self):
        for changes in ({'alignment_enabled':'yes'},{'alignment_min_inliers':3},
                        {'alignment_min_inlier_ratio':0},{'alignment_spatial_tolerance_pixels':9},
                        {'alignment_reset_pixels':float('nan')}):
            with self.assertRaises(ValueError):
                replace(self.config,**changes).validate()


if __name__ == '__main__':
    unittest.main()
