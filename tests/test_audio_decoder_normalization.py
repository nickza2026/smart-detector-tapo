"""AAC reconstruction overshoot is normalized at the codec/PCM boundary."""
import io
import unittest

import av
import numpy as np

from smart_tapo.rtsp_audio import decoded_pcm


class DecoderNormalizationTests(unittest.TestCase):
    def encoded(self):
        output=io.BytesIO()
        with av.open(output,mode='w',format='mp4') as container:
            stream=container.add_stream('aac',rate=44100)
            stream.layout='mono';stream.bit_rate=64000
            t=np.arange(44100,dtype=np.float64)/44100
            audio=(.99*np.sign(np.sin(2*np.pi*440*t))).astype('float32').reshape(1,-1)
            frame=av.AudioFrame.from_ndarray(audio,format='fltp',layout='mono')
            frame.sample_rate=44100;frame.pts=0
            for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode(None):container.mux(packet)
        return output.getvalue()

    def test_real_aac_overshoot_yields_normalized_float_chunks(self):
        media=self.encoded()
        # Verify the fixture really exercises codec reconstruction beyond full
        # scale, rather than merely trusting its loud encoded source samples.
        with av.open(io.BytesIO(media),format='mov') as container:
            resampler=av.AudioResampler(format='flt',layout='mono',rate=16000)
            peaks=[float(np.max(np.abs(x.to_ndarray())))
                   for frame in container.decode(container.streams.audio[0])
                   for x in resampler.resample(frame)]
        self.assertGreater(max(peaks),1.001)
        with av.open(io.BytesIO(media),format='mov') as container:
            chunks=list(decoded_pcm(container,av))
        self.assertTrue(chunks)
        for samples,pts in chunks:
            self.assertEqual(samples.dtype,np.float32)
            self.assertEqual(samples.shape[1],1)
            self.assertTrue(np.isfinite(samples).all())
            self.assertGreaterEqual(float(samples.min()),-1.)
            self.assertLessEqual(float(samples.max()),1.)
            self.assertTrue(np.isfinite(pts))
        for (samples,pts),(_,next_pts) in zip(chunks,chunks[1:]):
            self.assertAlmostEqual(pts+len(samples)/16000,next_pts,places=6)
