from __future__ import annotations

import io
import unittest
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image

from smart_tapo.vision import Observation
from tests import test_worker


class AlignmentWorkerTests(unittest.TestCase):
    setUp = test_worker.WorkerTests.setUp
    tearDown = test_worker.WorkerTests.tearDown
    make_worker = test_worker.WorkerTests.make_worker
    frame = test_worker.WorkerTests.frame
    notifications = test_worker.WorkerTests.notifications

    def install_detector(self):
        self.offset = 0.0
        self.worker.config = replace(self.config,alignment_reset_pixels=0.5)
        self.worker.detector = SimpleNamespace(observe=lambda _: Observation(self.label,
            view_corners=(self.offset,0,self.offset+2,0,self.offset+2,2,self.offset,2)))

    def test_camera_shift_starts_a_new_ten_frame_proof(self):
        self.install_detector()
        for i in range(1,6): self.frame(i)
        self.assertEqual(self.worker.count,5)
        self.offset = 1
        self.frame(6)
        self.assertEqual(self.worker.count,1)
        self.assertEqual(len(self.notifications()),0)
        for i in range(7,15): self.frame(i)
        self.assertEqual(self.worker.count,9)
        self.assertEqual(len(self.notifications()),0)
        self.frame(15)
        self.assertEqual(len(self.notifications()),1)

    def test_small_registration_jitter_does_not_reset_but_cumulative_drift_does(self):
        self.install_detector()
        for i in range(1,6): self.frame(i)
        for i in range(6,10):
            self.offset=(i-5)*0.1
            self.frame(i)
        self.assertEqual(self.worker.count,9)
        self.offset=0.7
        self.frame(10)
        self.assertEqual(self.worker.count,1)
        self.assertEqual(len(self.notifications()),0)

    def test_low_confidence_alignment_breaks_the_count(self):
        self.install_detector()
        for i in range(1,6): self.frame(i)
        self.frame(6,'UNKNOWN')
        self.assertEqual(self.worker.count,0)
        self.assertEqual(self.worker.live_count,0)
        self.assertIsNone(self.worker.view_anchor)

    def test_evidence_uses_aligned_crop_not_the_old_fixed_crop(self):
        encoded = io.BytesIO()
        Image.new('RGB',(2,2),'white').save(encoded,format='PNG')
        self.worker.detector=SimpleNamespace(observe=lambda _: Observation('PRESENT',roi_png=encoded.getvalue()))
        self.frame(1)
        evidence=self.worker.db.execute('SELECT path FROM evidence').fetchone()[0]
        with Image.open(evidence) as image:
            self.assertEqual(image.convert('RGB').getpixel((0,0)),(255,255,255))


if __name__=='__main__':
    unittest.main()
