from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from smart_tapo.config import Config, Rect
from smart_tapo.desktop_calibration import is_ready, mark_ready


class DesktopCalibrationTests(unittest.TestCase):
    def test_binds_exact_reference_geometry_and_camera_but_not_alert_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            config=Config(baseline_path=root/'empty.png')
            config.baseline_path.write_bytes(b'synthetic-reference-bytes')
            self.assertFalse(is_ready(config,root))
            mark_ready(config,root)
            self.assertTrue(is_ready(config,root))
            self.assertTrue(is_ready(replace(config,stuck_consecutive_frames=12),root))
            for changed in (replace(config,roi=Rect(300,265,100,110)),
                            replace(config,camera_name='another'),
                            replace(config,adb_serial='other-device')):
                self.assertFalse(is_ready(changed,root))
            config.baseline_path.write_bytes(b'partially-replaced-reference')
            self.assertFalse(is_ready(config,root))


if __name__=='__main__':unittest.main()
