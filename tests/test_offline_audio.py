import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import wave

import numpy as np

from smart_tapo.offline_audio import analyze_pcm, diagnose_file, DiagnosticCancelled
from tests.test_announcement import synthetic_phrase
from tests.test_announcement_stream import profile, stream, lease


def wav(path,samples,rate=8000):
    with wave.open(str(path),'wb') as target:
        target.setnchannels(1);target.setsampwidth(2);target.setframerate(rate)
        target.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())


class OfflineAudioTests(unittest.TestCase):
    def test_subquarter_eof_does_not_finalize_extra_candidate(self):
        import math
        from smart_tapo.announcement import AcousticResult
        source=stream()
        horizon=source._horizon/8000
        last=math.ceil((horizon+1)*2)/2
        onset=math.floor((last-horizon)*100)/100+.05
        result=AcousticResult('paid_ready',True,.99,onset,(.9,.9,.9),'matched')
        class Fake:
            def analyze_candidates(self,*args,**kwargs):return (result,)
            def analyze(self,*args,**kwargs):return ()
        x=np.zeros((round((last+.1)*8000),1),dtype='float32')
        source=stream(matcher=Fake());observations=[]
        for start in range(0,len(x),4000):
            part=x[start:start+4000];begin=100+start/8000
            observations.extend(source.push(part,start_frame=start,now=begin+len(part)/8000,
                                            lease=lease(begin,begin+10)).observations)
        with patch('smart_tapo.offline_audio.AnnouncementMatcher',return_value=Fake()):
            report=analyze_pcm(x,8000,profile())
        self.assertEqual(observations,[])
        self.assertEqual(report.finalized_until,source._finalized/8000)
        self.assertFalse(any(row.reason=='accepted_test_candidate' for row in report.rows))

    def test_deterministic_and_stream_semantic_equivalence(self):
        x=np.concatenate((synthetic_phrase(),synthetic_phrase()[::-1],np.zeros(64000))).astype('float32')[:,None]
        report=analyze_pcm(x,8000,profile())
        self.assertEqual(report,analyze_pcm(x,8000,profile()))
        source=stream();expected=[]
        for start in range(0,len(x),4000):
            part=x[start:start+4000]
            begin=100+start/8000
            batch=source.push(part,start_frame=start,now=begin+len(part)/8000,lease=lease(begin,begin+10))
            expected.extend((o.announcement.kind,round(o.announcement.started_at-1000,4)) for o in batch.observations)
        actual=[(r.label,round(r.onset,4)) for r in report.rows if r.reason=='accepted_test_candidate']
        self.assertEqual(actual,expected)
        self.assertEqual([kind for kind,_ in actual],['paid_ready','end'])
        self.assertIn('N',report.mode)
        self.assertEqual(report.finalized_until,source._finalized/8000)
        self.assertLessEqual(len(report.rows),160)
        self.assertNotIn('samples',report.metadata())

    def test_short_phrase_never_flushed_as_stream_success(self):
        report=analyze_pcm(synthetic_phrase().astype('float32')[:,None],8000,profile())
        self.assertFalse(any(r.reason=='accepted_test_candidate' for r in report.rows))
        self.assertTrue(any(r.reason=='need_future_lookahead' for r in report.rows))
        self.assertEqual(report.finalized_until,0)

    def test_onset_guard_cross_kind_overlap_and_lookahead_boundary(self):
        from dataclasses import replace
        from smart_tapo.announcement import PhraseTemplate
        p=profile()
        y=synthetic_phrase()
        early=np.concatenate((y[6400:],np.zeros(64000))).astype('float32')[:,None]
        report=analyze_pcm(early,8000,p)
        self.assertFalse(any(r.reason=='accepted_test_candidate' for r in report.rows))
        self.assertTrue(any(r.reason=='onset_guard' for r in report.rows))
        same=replace(p,templates=(p.templates[0],PhraseTemplate('end',p.templates[0].features,.95)))
        x=np.concatenate((y,np.zeros(64000))).astype('float32')[:,None]
        report=analyze_pcm(x,8000,same)
        self.assertFalse(any(r.reason=='accepted_test_candidate' for r in report.rows))
        self.assertTrue(any(r.reason=='cross_kind_overlap' for r in report.rows))
        horizon=analyze_pcm(x,8000,p).lookahead
        before=analyze_pcm(x[:round((horizon+.90)*8000)],8000,p)
        after=analyze_pcm(x[:round((horizon+1.05)*8000)],8000,p)
        self.assertFalse(any(r.reason=='accepted_test_candidate' for r in before.rows))
        self.assertTrue(any(r.reason=='accepted_test_candidate' for r in after.rows))

    def test_truncated_phrase_silence_and_noise(self):
        rng=np.random.default_rng(42)
        for x in (np.zeros(80000),rng.normal(0,.1,80000),synthetic_phrase()[:8000]):
            report=analyze_pcm(x.astype('float32')[:,None],8000,profile())
            self.assertFalse(any(r.reason=='accepted_test_candidate' for r in report.rows))
            self.assertTrue(all(r.reason in ('below_full_pattern_threshold','insufficient_audio','anchor_requirement_failed')
                                for r in report.rows if r.source=='whole_file_maximum'))
        self.assertEqual(analyze_pcm(np.zeros((8000,1)),8000,profile()).rms,0)

    def test_minutes_row_cap_and_bounded_reads(self):
        from smart_tapo.announcement import AcousticResult
        class Fake:
            def analyze_candidates(self,x,rate,**kwargs):
                return (AcousticResult('paid_ready',True,.99,len(x)/rate-6,(.99,)*3,'matched'),)
            def analyze(self,*args,**kwargs):return ()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'minutes.wav';wav(path,np.zeros(240*8000))
            events=[];updates=[];sizes=[]
            original=wave.Wave_read.readframes
            def read(source,count):
                sizes.append(count)
                return original(source,count)
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=profile()),patch('smart_tapo.offline_audio.AnnouncementMatcher',return_value=Fake()),patch.object(wave.Wave_read,'readframes',read):
                report=diagnose_file(path,'unused','cam1',on_candidate=events.append,progress=updates.append)
            self.assertEqual(report.processed_frames,240*8000)
            self.assertLessEqual(max(sizes),4000)
            self.assertGreater(report.rows_truncated,0)
            self.assertLessEqual(len(report.rows),152)
            self.assertEqual(report.accepted_counts['paid_ready'],len(events))
            self.assertGreater(events[-1].onset,200)
            self.assertEqual([u['processed_frames'] for u in updates],sorted(u['processed_frames'] for u in updates))

    def test_cancel_during_hash_and_read(self):
        from smart_tapo.offline_audio import _hash
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cancel.wav';wav(path,np.zeros(80000))
            class Stop:
                calls=0
                def is_set(self):
                    self.calls+=1
                    return self.calls>=3
            with self.assertRaises(DiagnosticCancelled):_hash(path,Stop())
            stop=Event()
            original=wave.Wave_read.readframes
            def read(source,count):
                result=original(source,count);stop.set();return result
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=profile()),patch.object(wave.Wave_read,'readframes',read):
                with self.assertRaises(DiagnosticCancelled):diagnose_file(path,'unused','cam1',stop)

    def test_cancel_and_limits(self):
        stop=Event();stop.set()
        with self.assertRaises(DiagnosticCancelled):analyze_pcm(np.zeros((8000,1)),8000,profile(),stop=stop)
        for x in (np.zeros((160001,1)),np.full((8000,1),np.nan),np.zeros((8000,9))):
            with self.assertRaises(ValueError):analyze_pcm(x,8000,profile())

    def test_local_wav_and_fail_closed_without_device_or_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.wav'
            wav(path,np.concatenate((synthetic_phrase(),np.zeros(64000))))
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=profile()):
                report=diagnose_file(path,Path(directory)/'profile.json','cam1')
                self.assertEqual(report.decoder,'stdlib PCM16 WAV')
                self.assertEqual(len(report.file_fingerprint),64)
                path.write_bytes(b'not a WAV')
                with self.assertRaises((ValueError,EOFError,wave.Error)):diagnose_file(path,'unused','cam1')
            for invalid in ('https://example.com/a.wav','relative.wav',str(Path(directory)/'a.mp4')):
                with self.assertRaises(ValueError):diagnose_file(invalid,'unused','cam1')

    def test_long_wav_streams_across_rolling_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'long.wav'
            x=np.zeros(60*8000,dtype='float32')
            phrase=synthetic_phrase()
            x[18*8000:18*8000+len(phrase)]=phrase
            x[45*8000:45*8000+len(phrase)]=phrase[::-1]
            wav(path,x)
            from dataclasses import replace
            from smart_tapo.announcement import PhraseTemplate
            quantized=(phrase*32767).astype('<i2').astype('float32')/32768
            p=replace(profile(),templates=(
                PhraseTemplate.from_recording('paid_ready',quantized,8000,.95,3.7,.95),
                PhraseTemplate.from_recording('end',quantized[::-1].copy(),8000,2.3,5.1,.95)))
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=p):
                report=diagnose_file(path,'unused','cam1')
            accepted=[r for r in report.rows if r.reason=='accepted_test_candidate']
            self.assertEqual([r.label for r in accepted],['paid_ready','end'])
            self.assertGreater(accepted[1].onset,45)
            self.assertLessEqual(report.max_buffer_frames,20*8000)

    def test_oversize_header_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'long.wav';wav(path,np.zeros(8000))
            data=bytearray(path.read_bytes());data[40:44]=(2000000000).to_bytes(4,'little');path.write_bytes(data)
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=profile()),patch('smart_tapo.offline_audio.read_wav') as reader:
                with self.assertRaisesRegex(ValueError,'invalid_pcm16'):diagnose_file(path,'unused','cam1')
                reader.assert_not_called()

    def test_file_mutation_and_profile_mutation_fail_closed(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.wav';wav(path,np.zeros(8000))
            with patch('smart_tapo.offline_audio.load_announcement_profile',side_effect=[profile(),replace(profile(),fingerprint='2'*64)]):
                with self.assertRaisesRegex(ValueError,'profile_changed'):diagnose_file(path,'unused','cam1')
            with patch('smart_tapo.offline_audio.load_announcement_profile',return_value=profile()),patch('smart_tapo.offline_audio._hash',side_effect=['a','b']):
                with self.assertRaisesRegex(ValueError,'input_changed'):diagnose_file(path,'unused','cam1')
