from types import SimpleNamespace
import unittest

from smart_tapo.history_image import validate_history_png


class HistoricalPngBoundsTests(unittest.TestCase):
    def test_portrait_and_landscape_do_not_depend_on_current_capture(self):
        for width,height in ((720,1280),(1920,1080),(100,100)):
            validate_history_png(SimpleNamespace(format='PNG',width=width,height=height))

    def test_pixel_axis_and_format_limits_remain_enforced_before_decode(self):
        for fmt,width,height in (('JPEG',720,1280),('PNG',8193,1),('PNG',5000,4000),('PNG',0,20)):
            with self.assertRaises(ValueError):
                validate_history_png(SimpleNamespace(format=fmt,width=width,height=height))
