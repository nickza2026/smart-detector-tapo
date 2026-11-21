"""RAM pressure must be visible, bounded-rate and never a successful --once."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_rtsp import config_for,png
from smart_tapo.capture.rtsp import capture_rtsp,RtspStatus
from smart_tapo.desktop_controller import _rtsp_hint


class FullChannel:
    generation=1
    def snapshot(self):return self.generation,0.
    def fence(self):self.generation+=1
    def publish(self,envelope):return False


class RamStatusTests(unittest.TestCase):
    def run_capture(self,*,once=False):
        with tempfile.TemporaryDirectory() as directory:
            config=config_for(directory);stop=threading.Event();clock=[1000.];reasons=[]
            class Reader:
                calls=0
                def __init__(self,config):pass
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def get(self):
                    self.calls+=1;clock[0]+=.25
                    if self.calls>3:stop.set();return None
                    return ('status',self.calls,clock[0],1_000_000.+clock[0],float(self.calls),True,8,8,png(),0)
            with patch('smart_tapo.capture.rtsp.time.monotonic',side_effect=lambda:clock[0]):
                result=capture_rtsp(config,stop,once=once,reader_factory=Reader,channel=FullChannel(),
                    status_callback=lambda status:reasons.append(status.reason))
            self.assertFalse(list(Path(directory).rglob('*.png')))
            return result,reasons

    def test_overflow_is_rendered_by_desktop(self):
        self.assertIsNotNone(_rtsp_hint(RtspStatus('overflow','status',3,0).__dict__))

    def test_persistent_overflow_does_not_alternate_healthy_and_flood_audit(self):
        _,reasons=self.run_capture()
        self.assertNotIn('healthy',reasons)
        self.assertEqual(reasons.count('overflow'),1)

    def test_once_fails_when_no_frame_was_published(self):
        result,_=self.run_capture(once=True)
        self.assertEqual(result,1)
