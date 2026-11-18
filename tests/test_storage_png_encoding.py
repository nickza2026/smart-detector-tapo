"""Reference compression must not understate actual capture PNG encoding."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image,ImageDraw
from smart_tapo.config import Config
from smart_tapo.storage_budget import estimate_storage


class PngEncodingBudgetTests(unittest.TestCase):
    def test_first_setup_uses_capture_encoder_size_not_smaller_saved_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            image=Image.new('RGB',(320,240),'#8192a3');draw=ImageDraw.Draw(image)
            for y in range(0,240,12):
                for x in range(0,320,12):draw.rectangle((x,y,x+5,y+5),fill=((x*7)%255,(y*3)%255,(x+y)%255))
            baseline=root/'reference.png';image.save(baseline,compress_level=9)
            pixels=cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)
            ok,encoded=cv2.imencode('.png',pixels);self.assertTrue(ok)
            self.assertGreater(len(encoded),baseline.stat().st_size)
            config=replace(Config(),expected_size=image.size,baseline_path=baseline,
                data_dir=root/'data',capture_interval_seconds=.2)
            budget=estimate_storage(config)
            self.assertGreaterEqual(budget.png_bytes,len(encoded),
                'Pillow reference compression differs from actual OpenCV capture')

    def test_reference_measurement_is_cached_across_repeated_previews(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);baseline=root/'reference.png'
            Image.new('RGB',(64,48),'gray').save(baseline)
            config=replace(Config(),expected_size=(64,48),baseline_path=baseline,data_dir=root/'data')
            with patch('cv2.imencode',wraps=cv2.imencode) as encode:
                estimate_storage(config);estimate_storage(config)
                self.assertEqual(encode.call_count,1)
                Image.new('RGB',(64,48),'white').save(baseline)
                estimate_storage(config)
                self.assertEqual(encode.call_count,2)
