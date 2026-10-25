from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image
from smart_tapo.config import Config
from smart_tapo.simulate import simulate


class IntegrationTests(unittest.TestCase):
    def test_images_spool_history_reset_and_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'original.png'
            Image.new('RGB', (720, 1280), 'gray').save(path)
            original = path.read_bytes()
            # Geometry is covered separately with textured registration fixtures.
            report = simulate(replace(Config(), alignment_enabled=False), path)
            self.assertEqual(report['normal_events'], 1)
            self.assertEqual(report['notification_sequences'], [17, 77])
            self.assertEqual(report['real_notifications'], 0)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
