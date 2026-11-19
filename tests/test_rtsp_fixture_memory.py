"""Synthetic server packetization must not duplicate its entire PCM recording."""
import unittest
import numpy as np
from PIL import Image
from tests.rtsp_av_fixture import SyntheticAV

class FixtureMemoryTests(unittest.TestCase):
    def test_packetization_survives_bounded_allocation_with_large_pcm(self):
        class BoundedBytes(bytes):
            def __mul__(self,count):
                if len(self)*count>1280:raise MemoryError('Full-wave allocation forbidden')
                return super().__mul__(count)
        source=SyntheticAV(Image.new('RGB',(320,240),'gray'),np.zeros(100000))
        source.pcm=BoundedBytes(b'\x12\x34'*100000)
        packets=[]
        class Connection:
            def settimeout(self,value):pass
            def recv(self,size):
                return (b'SETUP rtsp://127.0.0.1/test/trackID=1 RTSP/1.0\r\nCSeq: 1\r\n'
                    b'Transport: RTP/AVP/TCP;interleaved=0-1\r\n\r\n'
                    b'PLAY rtsp://127.0.0.1/test RTSP/1.0\r\nCSeq: 2\r\n\r\n')
            def sendall(self,data):
                if data.startswith(b'$'):packets.append(data);source.stop.set()
            def close(self):pass
        try:source.serve(Connection())
        finally:source.sock.close()
        self.assertEqual(source.errors,[])
        self.assertEqual(len(packets),1)
        self.assertEqual(packets[0][16:],b'\x12\x34'*320)

if __name__=='__main__':unittest.main()
