from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from smart_tapo.config import Config, Rect, load_config
from smart_tapo.frames import publish_frame, read_frame
from smart_tapo.runtime import ProcessLock
from smart_tapo.vision import Detector


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # These fixtures exercise the legacy occupancy rule, not registration;
        # featureless gray scenes cannot supply trustworthy alignment anchors.
        self.config = replace(Config(), data_dir=self.root / "data", baseline_path=self.root / "empty.png",
                              alignment_enabled=False)
        self.image = Image.new("RGB", self.config.expected_size, "gray")
        self.image.save(self.config.baseline_path)

    def test_configuration_and_validation(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"stuck_consecutive_frames": 3}), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config.stuck_consecutive_frames, 3)
        self.assertEqual(config.data_dir, (self.root / "data").resolve())
        for invalid in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError):
                replace(config, stuck_consecutive_frames=invalid).validate()
        with self.assertRaises(ValueError):
            replace(config, camera_id="../escape").validate()
        with self.assertRaises(ValueError):
            replace(config, roi=Rect(700, 0, 100, 100)).validate()

    def test_ui_change_is_ignored(self):
        detector = Detector(self.config)
        changed = self.image.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 719, 120), fill="black")
        self.assertEqual(detector.observe_image(changed).label, "EMPTY")
        ImageDraw.Draw(changed).rectangle(self.config.roi.box(), fill="white")
        self.assertEqual(detector.observe_image(changed).label, "PRESENT")

    def test_invalid_png_is_unknown(self):
        path = self.root / "invalid.png"
        path.write_bytes(b"\x00" * 100)
        self.assertEqual(Detector(self.config).observe(path).label, "UNKNOWN")

    def test_publish_and_path_safety(self):
        data = BytesIO()
        self.image.save(data, format="PNG")
        marker = publish_frame(self.config, data.getvalue(), "session1", 1, True)
        frame, path = read_frame(marker, self.config)
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(path.read_bytes(), data.getvalue())
        raw = json.loads(marker.read_text())
        raw["image_name"] = "../../outside.png"
        marker.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            read_frame(marker, self.config)

    def test_lock_refuses_second_owner_and_releases(self):
        path = self.root / "worker.lock"
        with ProcessLock(path):
            with self.assertRaises(RuntimeError):
                with ProcessLock(path):
                    pass
        with ProcessLock(path):
            pass


if __name__ == "__main__":
    unittest.main()
