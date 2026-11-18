"""Foreground must not consume the feature budget of fixed cabinet anchors."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from PIL import Image

from smart_tapo.config import Config,Rect
from smart_tapo.vision import Detector


class ForegroundTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=replace(Config(),expected_size=(1920,1080),video=Rect(0,0,1920,1080),
            roi=Rect(844,471,255,196),clock_roi=Rect(0,0,700,55),
            baseline_path=Path(self.temp.name)/'reference.png')
        rng=np.random.default_rng(4451)
        gray=rng.integers(0,256,(1080,1920),dtype=np.uint8)
        cv2.circle(gray,(972,569),480,110,-1)
        for x,y in ((782,569),(1162,569),(972,379),(972,759)):
            patch=rng.integers(90,151,(16,16),dtype=np.uint8)
            gray[y-24:y+24,x-24:x+24]=cv2.resize(patch,(48,48),interpolation=cv2.INTER_NEAREST)
        gray[471:667,844:1099]=70
        self.reference=Image.fromarray(gray).convert('RGB');self.reference.save(self.config.baseline_path)
        self.original=self.config.baseline_path.read_bytes()
        occupied=gray.copy()
        occupied[475:663,848:1095]=rng.integers(0,256,(188,247),dtype=np.uint8)
        self.occupied=Image.fromarray(occupied).convert('RGB')

    def test_textured_foreground_does_not_starve_static_context(self):
        detector=Detector(self.config)
        self.assertEqual(detector.observe_image(self.reference).label,'EMPTY')
        observation=detector.observe_image(self.occupied)
        self.assertEqual(observation.label,'PRESENT',observation.alignment_reason)
        self.assertEqual(len(observation.view_corners),8)
        self.assertGreaterEqual(observation.alignment_inliers,self.config.alignment_min_inliers)
        self.assertEqual(self.config.baseline_path.read_bytes(),self.original)

    def test_tracking_occupied_small_shift_keeps_reference_and_roi(self):
        moved=self.occupied.transform(self.occupied.size,Image.Transform.AFFINE,(1,0,-22,0,1,5),
            resample=Image.Resampling.BILINEAR,fillcolor='black')
        observation=Detector(self.config).observe_image(moved)
        self.assertEqual(observation.label,'PRESENT',observation.alignment_reason)
        expected=np.float32([[866,466],[1121,466],[1121,662],[866,662]])
        actual=np.asarray(observation.view_corners).reshape(4,2)
        self.assertLess(float(np.max(np.linalg.norm(actual-expected,axis=1))),3.)
        self.assertEqual(self.config.baseline_path.read_bytes(),self.original)

    def test_no_context_and_excessive_motion_remain_unknown(self):
        detector=Detector(self.config)
        self.assertEqual(detector.observe_image(Image.new('RGB',self.config.expected_size,'gray')).label,'UNKNOWN')
        moved=self.occupied.transform(self.occupied.size,Image.Transform.AFFINE,(1,0,-400,0,1,0),fillcolor='black')
        self.assertEqual(detector.observe_image(moved).label,'UNKNOWN')
        self.assertEqual(self.config.baseline_path.read_bytes(),self.original)

    def test_additional_proposal_is_bounded_and_excludes_fixed_slot(self):
        from smart_tapo.alignment import Aligner,ORB_FEATURES
        aligner=Aligner(self.config,self.reference)
        original=aligner.orb
        masks=[]
        class Recorder:
            def detectAndCompute(self,image,mask):
                masks.append(mask.copy())
                points,descriptors=original.detectAndCompute(image,mask)
                self_test.assertLessEqual(len(points),ORB_FEATURES)
                return points,descriptors
        self_test=self
        aligner.orb=Recorder()
        self.assertTrue(aligner.align(self.reference).ok)
        self.assertEqual(len(masks),1)
        masks.clear()
        result=aligner.align(self.occupied)
        self.assertTrue(result.ok,result.reason)
        self.assertEqual(result.proposal,'static_context')
        self.assertEqual(len(masks),2)
        x,y,w,h=aligner.roi
        self.assertFalse(np.any(masks[1][y:y+h,x:x+w]))
        self.assertEqual(self.config.baseline_path.read_bytes(),self.original)
