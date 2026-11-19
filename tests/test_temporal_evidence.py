"""Temporal-order proof is opt-in; spectral color alone is not a phrase."""
import unittest
from unittest.mock import patch
import numpy as np
from smart_tapo.announcement import AnnouncementMatcher,PhraseTemplate

class TemporalEvidenceTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(4944)
        self.features=(np.linspace(.1,1.,32)[:,None]+rng.normal(0,.04,(32,240))).astype('float32')
        self.template=PhraseTemplate('paid_ready',self.features,.6)
        object.__setattr__(self.template,'temporal_threshold',.3)
        self.matcher=AnnouncementMatcher([self.template])

    def analyze(self,features):
        with patch('smart_tapo.announcement.spectral_features',return_value=np.ascontiguousarray(features)):
            return self.matcher.analyze_candidates(np.zeros(24000),8000)

    def test_same_spectral_color_with_wrong_temporal_order_is_rejected(self):
        shuffled=self.features[:,np.random.default_rng(435).permutation(240)]
        self.assertEqual(self.analyze(shuffled),())

    def test_constant_color_has_no_temporal_evidence(self):
        constant=np.repeat(self.features.mean(axis=1,keepdims=True),240,axis=1)
        self.assertEqual(self.analyze(constant),())

    def test_ordered_pattern_remains_a_candidate(self):
        results=self.analyze(self.features)
        self.assertEqual([x.label for x in results],['paid_ready'])

    def test_many_color_only_peaks_do_not_fault_before_temporal_rejection(self):
        features=(self.features.mean(axis=1,keepdims=True)+np.random.default_rng(4945).normal(0,.04,(32,2000))).astype('float32')
        self.assertEqual(self.analyze(features),())

    def test_temporally_eligible_peak_saturation_still_fails_closed(self):
        scores=np.tile(np.array([.9,0.],dtype='float32'),100)[None,:]
        with patch('smart_tapo.announcement.spectral_features',return_value=self.features), \
                patch('smart_tapo.announcement._temporal_correlation',return_value=np.ones(200)), \
                patch('smart_tapo.announcement.cv2.matchTemplate',return_value=scores), \
                self.assertRaisesRegex(ValueError,'peak saturation'):
            self.matcher.analyze_candidates(np.zeros(24000),8000)

if __name__=='__main__':unittest.main()
