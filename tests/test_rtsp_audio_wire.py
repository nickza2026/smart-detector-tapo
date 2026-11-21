"""Real PyAV decode over owned paced loopback, never a user RTSP source."""
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from tests.rtsp_av_fixture import SyntheticAV
from smart_tapo.rtsp_audio import decoded_pcm, PcmTimeline, RtspContinuousListener

import av


class AudioWireTests(unittest.TestCase):
    def test_real_paced_l16_normalized_pts_and_received_silence(self):
        for silent in (False, True):
            pcm = np.zeros(16000, dtype=np.float32) if silent else .2*np.sin(2*np.pi*440*np.arange(16000)/16000)
            with SyntheticAV(Image.new('RGB', (64, 64)), pcm) as source:
                start = time.monotonic()
                with av.open(source.url, options={'rtsp_transport': 'tcp', 'allowed_media_types': 'audio'}, timeout=(3., 2.)) as container:
                    timeline = PcmTimeline()
                    count = 0
                    peak = 0.
                    previous = None
                    for samples, pts in decoded_pcm(container, av):
                        now = time.monotonic()
                        timeline.accept(pts, len(samples), now)
                        self.assertEqual(samples.dtype, np.float32)
                        self.assertEqual(samples.shape[1], 1)
                        if previous is not None:
                            self.assertAlmostEqual(pts, previous, places=6)
                        previous = pts+len(samples)/16000
                        count += len(samples)
                        peak = max(peak, float(np.max(np.abs(samples))))
                        if count >= 32000:
                            break
                    self.assertGreaterEqual(time.monotonic()-start, 1.8)
                    self.assertEqual(peak == 0, silent)
                self.assertIn('PLAY', source.requests)
                self.assertEqual(source.errors, [])

    def test_video_only_is_rejected_without_audio_fallback(self):
        from smart_tapo.rtsp_audio import AudioDiscontinuity
        with SyntheticAV(Image.new('RGB', (64, 64)), np.zeros(16000), audio=False) as source:
            with av.open(source.url, options={'rtsp_transport': 'tcp', 'allowed_media_types': 'audio'}, timeout=(3., 2.)) as container:
                with self.assertRaisesRegex(AudioDiscontinuity, 'no_audio_track'):
                    next(decoded_pcm(container, av))
            self.assertEqual(source.errors, [])

    def test_real_disconnect_does_not_invent_pcm_or_end(self):
        with SyntheticAV(Image.new('RGB', (64, 64)), np.zeros(16000), disconnect_after=.6) as source:
            count = 0
            with av.open(source.url, options={'rtsp_transport': 'tcp', 'allowed_media_types': 'audio'}, timeout=(3., 2.)) as container:
                for samples, pts in decoded_pcm(container, av):
                    count += len(samples)
            self.assertGreater(count, 0)
            self.assertLess(count, 16000)
            self.assertEqual(source.errors, [])

    def test_real_offline_aac_and_g711_resample(self):
        import io
        for codec, format_name in (('aac', 'adts'), ('pcm_alaw', 'wav'), ('pcm_mulaw', 'wav')):
            with self.subTest(codec=codec):
                sink = io.BytesIO()
                rate = 48000 if codec == 'aac' else 8000
                with av.open(sink, mode='w', format=format_name) as output:
                    stream = output.add_stream(codec, rate=rate)
                    stream.layout = 'mono'
                    data = (.2*np.sin(2*np.pi*440*np.arange(rate)/rate)).astype(np.float32).reshape(1, -1)
                    frame = av.AudioFrame.from_ndarray(data, format='flt', layout='mono')
                    frame.sample_rate = rate
                    frame.pts = 0
                    from fractions import Fraction
                    frame.time_base = Fraction(1, rate)
                    for packet in stream.encode(frame):
                        output.mux(packet)
                    for packet in stream.encode(None):
                        output.mux(packet)
                sink.seek(0)
                with av.open(sink, format=format_name) as container:
                    frames = list(decoded_pcm(container, av))
                self.assertGreater(sum(len(samples) for samples, _ in frames), 15000)
                self.assertTrue(all(samples.dtype == np.float32 and samples.shape[1] == 1 for samples, _ in frames))
                self.assertGreater(max(float(np.max(np.abs(samples))) for samples, _ in frames), .1)
                for (samples, pts), (_, following) in zip(frames, frames[1:]):
                    self.assertAlmostEqual(following, pts+len(samples)/16000, places=6)

    def test_supervised_real_ready_repeat_end_and_stop(self):
        from smart_tapo.continuous_smoke import smoke_prepared_reference, _phrase, _end_phrase
        from smart_tapo.profile_setup import create_private_profile
        from smart_tapo.config import Config
        from smart_tapo.refueling_store import RefuelingStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(data_dir=root/'data', camera_id='wire-synthetic')
            reference = smoke_prepared_reference(root/'reference')
            import json
            manifest = json.loads(reference.read_text())
            templates = [dict(kind=item['kind'], file=reference.parent/item['file'], start=item['start'], end=item['end'], threshold=item['threshold']) for item in manifest['templates']]
            profile = create_private_profile(config.data_dir, config.camera_id, templates)
            pcm8 = np.concatenate((_end_phrase(), _phrase(), _phrase(), _end_phrase(), np.zeros(160000)))
            pcm = np.repeat(pcm8, 2)
            with SyntheticAV(Image.new('RGB', (64, 64)), pcm) as source:
                from dataclasses import replace
                config = replace(config, rtsp_url=source.url)
                runtime = RtspContinuousListener()
                events = []
                runtime.start(config, None, profile, consent=True)
                deadline = time.monotonic()+49
                try:
                    while runtime.is_running and time.monotonic() < deadline:
                        events.extend(runtime.poll())
                        if any(e.get('observations_total', 0) >= 4 for e in events):
                            break
                        time.sleep(.02)
                finally:
                    stopped = time.monotonic()
                    runtime.close()
                    while runtime.is_running and time.monotonic()-stopped < 5:
                        events.extend(runtime.poll())
                        time.sleep(.02)
                self.assertFalse(runtime.is_running)
                self.assertLess(time.monotonic()-stopped, 5)
                self.assertFalse([e for e in events if e.get('kind') == 'error'], events)
                self.assertGreaterEqual(max((e.get('frames', 0) for e in events), default=0), 16000*20)
                store = RefuelingStore(config.data_dir/'refueling', config.camera_id)
                try:
                    rows = store.list_sessions()
                    # Capture only after the owned live run is stopped: no extra
                    # child work/timing changes and no weakened completion oracle.
                    # Observation totals count submissions, including ignored END.
                    diagnostics = {
                        'events': [{k:e[k] for k in ('kind','code','observations_total',
                            'ready_total','end_total','frames','awaiting_boundary') if k in e}
                            for e in events if e.get('kind') != 'listener_progress' or
                            e.get('observations_total',0) >= 3][-32:],
                        'decisions': [dict(row) for row in store._db.execute(
                            'SELECT reason AS ignored_reason,kind,started_at,ended_at FROM observations ORDER BY rowid')],
                        'states': [{k:r.get(k) for k in ('state','ready_count','started_at',
                            'last_ready_at','ended_at','missing_end_at','interrupted_at')} for r in rows],
                    }
                    self.assertEqual(len(rows), 1, rows)
                    self.assertEqual(rows[0]['ready_count'], 2)
                    self.assertEqual(rows[0]['state'], 'completed', diagnostics)
                    self.assertIsNone(rows[0]['missing_end_at'], diagnostics)
                finally:
                    store.close()
                self.assertEqual(source.errors, [])


if __name__ == '__main__':
    unittest.main()
