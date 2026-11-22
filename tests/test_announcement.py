"""Synthetic acoustic tests; no private sound/, devices, model download or API."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from smart_tapo.announcement import AnnouncementMatcher,PhraseTemplate,pcm8k,read_wav,spectral_features


def synthetic_phrase():
    rate=8000
    t=np.arange(rate*6)/rate
    y=np.zeros(len(t))
    for i,f in enumerate((410,730,1150,590,1640,870,1280,480)):
        start=1+i*.32
        selection=(t>=start)&(t<start+.25)
        u=t[selection]-start
        y[selection]=np.sin(np.pi*u/.25)**2*(.2*np.sin(2*np.pi*f*u)+.08*np.sin(2*np.pi*f*1.7*u))
    return y


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.y=synthetic_phrase()
        self.template=PhraseTemplate.from_recording('paid_ready',self.y,8000,.95,3.7,.8)
        self.matcher=AnnouncementMatcher([self.template])

    def test_accepted_alternate_beats_rejected_primary_without_new_label(self):
        rng=np.random.default_rng(912)
        primary=rng.normal(size=(32,100)).astype('float32')
        alternate=rng.normal(size=(32,100)).astype('float32')
        template=PhraseTemplate('paid_ready',primary,.6,.2,2,((alternate,-2.),))
        matcher=AnnouncementMatcher((template,))
        with patch('smart_tapo.announcement.spectral_features',return_value=alternate):
            selected=matcher.analyze(np.zeros(8000),8000)
            candidates=matcher.analyze_candidates(np.zeros(8000),8000)
        self.assertEqual(len(selected),1)
        self.assertTrue(selected[0].candidate)
        self.assertEqual(selected[0].reference_offset_seconds,-2.)
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0].label,'paid_ready')
        self.assertEqual(candidates[0].onset_seconds,0.)
        self.assertEqual(candidates[0].reference_offset_seconds,-2.)
        # Temporal evidence remains mandatory for the fallback too.
        with patch('smart_tapo.announcement.spectral_features',return_value=alternate), \
                patch('smart_tapo.announcement._temporal_correlation',return_value=np.array([0.])):
            self.assertFalse(matcher.analyze(np.zeros(8000),8000)[0].candidate)
            self.assertEqual(matcher.analyze_candidates(np.zeros(8000),8000),())

    def test_full_pattern_candidate_and_gain_invariance_not_probability(self):
        a=self.matcher.analyze(self.y,8000)[0]
        b=self.matcher.analyze(self.y*.1,8000)[0]
        self.assertTrue(a.candidate)
        self.assertTrue(b.candidate)
        self.assertAlmostEqual(a.score,b.score,places=5)
        self.assertAlmostEqual(a.onset_seconds,.95,places=2)
        self.assertEqual(a.reason,'acoustic_candidate')
        self.assertEqual(len(a.anchor_scores),3)

    def test_noisy_phrase_survives_digital_silent_context_and_gain(self):
        # A continuous recording floor must not become digital zero merely
        # because an offline/stream window includes silent context.
        rng=np.random.default_rng(4831)
        source=self.y+rng.normal(0,.002,len(self.y))
        template=PhraseTemplate.from_recording('paid_ready',source,8000,.95,3.7,.8)
        matcher=AnnouncementMatcher([template])
        expected=template.features.copy()
        for gain in (1.,.1):
            padded=np.pad(source*gain,(5*8000,9*8000))
            before=padded.copy()
            result=matcher.analyze(padded,8000)[0]
            self.assertTrue(result.candidate,result)
            self.assertGreater(result.score,.95)
            self.assertAlmostEqual(result.onset_seconds,5.95,places=2)
            hits=matcher.analyze_candidates(padded,8000)
            self.assertEqual(len(hits),1)
            np.testing.assert_array_equal(padded,before)
        # A louder isolated outlier must not exclude the low recording floor.
        outlier=np.pad(source*.1,(5*8000,9*8000))
        outlier[8000]=1.
        self.assertTrue(matcher.analyze(outlier,8000)[0].candidate)
        self.assertEqual(len(matcher.analyze_candidates(outlier,8000)),1)
        self.assertEqual(spectral_features(np.zeros(160000)).shape,(32,1997))
        self.assertFalse(spectral_features(np.zeros(160000)).any())
        for negative in (np.zeros(160000),rng.normal(0,.002,160000),
                         np.pad(np.hanning(100),(40000,119900))):
            self.assertFalse(matcher.analyze(negative,8000)[0].candidate)
            self.assertEqual(matcher.analyze_candidates(negative,8000),())
        np.testing.assert_array_equal(template.features,expected)
        self.assertFalse(template.features.flags.writeable)

    def test_silence_noise_and_single_impact_do_not_make_phrase(self):
        rng=np.random.default_rng(7781)
        for samples in (np.zeros_like(self.y),rng.normal(0,.1,len(self.y))):
            self.assertFalse(self.matcher.analyze(samples,8000)[0].candidate)
        impact=np.zeros_like(self.y)
        impact[16000:16100]=np.hanning(100)
        self.assertFalse(self.matcher.analyze(impact,8000)[0].candidate)

    def test_candidates_keep_two_complete_utterances(self):
        results=self.matcher.analyze_candidates(np.tile(self.y,2),8000)
        self.assertEqual(len(results),2)
        np.testing.assert_allclose([r.onset_seconds for r in results],[.95,6.95],atol=.01)
        self.assertTrue(all(r.candidate and r.reason=='acoustic_candidate' for r in results))

    def test_candidates_reject_nonphrases_and_short_or_discontinuous_input(self):
        impact=np.zeros_like(self.y);impact[16000:16100]=np.hanning(100)
        for samples in (np.zeros_like(self.y),np.random.default_rng(11).normal(0,.1,len(self.y)),
                        impact,self.y[:100],self.y[:3000]):
            self.assertEqual(self.matcher.analyze_candidates(samples,8000),())
        with patch('smart_tapo.announcement.pcm8k',side_effect=AssertionError):
            self.assertEqual(self.matcher.analyze_candidates(None,0,continuous=False),())
        with self.assertRaises(ValueError):self.matcher.analyze_candidates(self.y,8000,continuous=1)

    def test_candidates_suppress_overlapping_peaks_but_keep_back_to_back(self):
        width=self.template.features.shape[1]
        scores=np.zeros(width+1,dtype='float32')
        scores[0]=.95;scores[2]=.94;scores[width]=.93
        with patch('smart_tapo.announcement.spectral_features',return_value=np.tile(self.template.features,2)), \
             patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores[None,:]):
            results=self.matcher.analyze_candidates(self.y,8000)
        self.assertEqual([r.onset_seconds for r in results],[0.,width*.01])

    def test_candidates_fail_closed_on_peak_saturation(self):
        scores=np.tile([.9,0.],65).astype('float32')
        with patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores[None,:]):
            with self.assertRaisesRegex(ValueError,'peak'):
                self.matcher.analyze_candidates(self.y,8000)

    def test_candidates_bound_results_and_sort_tied_labels(self):
        short=self.template.features[:,:80]
        templates=[PhraseTemplate('label_'+str(i),short,.8) for i in reversed(range(8))]
        matcher=AnnouncementMatcher(templates)
        features=np.tile(short,10)
        scores=np.zeros(721,dtype='float32');scores[::80]=.95
        with patch('smart_tapo.announcement.spectral_features',return_value=features), \
             patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores[None,:]):
            with self.assertRaisesRegex(ValueError,'result saturation'):
                matcher.analyze_candidates(self.y,8000)
        scores=np.zeros(721,dtype='float32');scores[0:2]=.95
        with patch('smart_tapo.announcement.spectral_features',return_value=features), \
             patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores[None,:]):
            results=matcher.analyze_candidates(self.y,8000)
        self.assertEqual([r.label for r in results],sorted(t.label for t in templates))
        self.assertTrue(all(r.onset_seconds==0 for r in results))

    def test_candidates_require_anchors_and_preserve_input_guards(self):
        scores=np.zeros(50,dtype='float32');scores[0]=.99
        with patch('smart_tapo.announcement.spectral_features',return_value=np.zeros((32,400),dtype='float32')), \
             patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores[None,:]):
            self.assertEqual(self.matcher.analyze_candidates(self.y,8000),())
        for samples in (np.zeros(8000*21),np.full(8000,np.nan),np.full(8000,2.)):
            with self.assertRaises(ValueError):self.matcher.analyze_candidates(samples,8000)
        np.testing.assert_array_equal(self.template.features,self.template.features.copy())
        self.assertFalse(self.template.features.flags.writeable)

    def test_inverted_stereo_does_not_cancel_speech(self):
        stereo=np.stack([self.y,-self.y],axis=1)
        self.assertTrue(self.matcher.analyze(stereo,8000)[0].candidate)

    def test_discontinuity_cannot_be_stitched_into_announcement(self):
        with patch('smart_tapo.announcement.spectral_features',side_effect=AssertionError):
            result=self.matcher.analyze(self.y,8000,continuous=False)[0]
        self.assertFalse(result.candidate)
        self.assertEqual(result.reason,'discontinuous_input')

    def test_short_window_never_lets_opencv_swap_input_and_template(self):
        with patch('smart_tapo.announcement.cv2.matchTemplate',side_effect=AssertionError):
            result=self.matcher.analyze(self.y[:3000],8000)[0]
        self.assertFalse(result.candidate)
        self.assertEqual(result.reason,'insufficient_audio')

    def test_constant_template_and_bad_profile_are_rejected(self):
        for features in (np.zeros((32,100)),np.ones((32,100)),np.full((32,100),np.nan)):
            with self.assertRaises(ValueError):PhraseTemplate('end',features,.8)
        for label,threshold in [('private bad label',.8),('end',float('nan')),('end',True)]:
            with self.assertRaises(ValueError):PhraseTemplate(label,self.template.features,threshold)
        with self.assertRaises(ValueError):AnnouncementMatcher([self.template,self.template])

    def test_profile_owns_immutable_feature_copy(self):
        values=self.template.features.copy()
        template=PhraseTemplate('end',values,.8)
        expected=template.features.copy()
        values.fill(0)
        np.testing.assert_array_equal(template.features,expected)
        self.assertFalse(template.features.flags.writeable)

    def test_bounded_finite_inputs_and_resampling(self):
        for value,rate in [(np.ones(8000*21),8000),(np.array([np.nan]*800),8000),
                           (np.ones((3000,9)),8000),(np.ones(1000),0),(np.ones(1000),True),
                           (np.full(1000,1e300),8000)]:
            with self.assertRaises(ValueError):pcm8k(value,rate)
        t=np.arange(44100)/44100
        down=pcm8k(np.sin(t*2*np.pi*700),44100)
        self.assertEqual(len(down),8000)
        self.assertTrue(np.isfinite(down).all())
        with self.assertRaises(ValueError):spectral_features(np.ones(10))

    def test_wav_read_is_bounded_and_does_not_modify_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            file=Path(temporary)/'synthetic.wav'
            pcm=(self.y*32767).astype('<i2')
            with wave.open(str(file),'wb') as writer:
                writer.setparams((1,2,8000,0,'NONE','not compressed'))
                writer.writeframes(pcm.tobytes())
            before=file.read_bytes()
            samples,rate=read_wav(file)
            self.assertEqual(rate,8000)
            self.assertEqual(samples.shape,(48000,1))
            self.assertEqual(file.read_bytes(),before)
            self.assertTrue(self.matcher.analyze(samples,rate)[0].candidate)
