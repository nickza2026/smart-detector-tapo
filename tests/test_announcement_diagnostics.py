"""Diagnostic-only observations must not change acoustic decisions."""
import unittest
from unittest.mock import patch
from dataclasses import replace
import numpy as np
from smart_tapo.announcement import AnnouncementMatcher
from tests.test_announcement_stream import profile, stream, lease


class AnnouncementDiagnosticsTests(unittest.TestCase):
    def test_no_analysis_is_unknown_not_measured_rejection(self):
        source = stream()
        self.assertEqual(source.diagnostic_snapshot['match_state'], 'warming_up')
        self.assertNotIn('rejected_count', source.diagnostic_snapshot)

    def test_temporal_rejection_is_reported_without_changing_output(self):
        template = replace(profile().templates[0], temporal_threshold=.3)
        matcher = AnnouncementMatcher((template,))
        features = np.ones((32, template.features.shape[1]+1), dtype='float32')
        with patch('smart_tapo.announcement.spectral_features', return_value=features), \
                patch('smart_tapo.announcement.cv2.matchTemplate', return_value=np.array([[.99, .1]], dtype='float32')), \
                patch('smart_tapo.announcement._temporal_correlation', return_value=np.array([.1, .1])):
            self.assertEqual(matcher.analyze_candidates(np.zeros(8000), 8000), ())
        snapshot = matcher.diagnostic_snapshot
        self.assertEqual(snapshot['rejected_count'], 1)
        self.assertEqual(snapshot['candidate_count'], 0)
        self.assertAlmostEqual(snapshot['labels']['paid_ready']['raw_score'], .99, places=5)
        self.assertEqual(snapshot['labels']['paid_ready']['temporal_rejected'], 1)
        snapshot['labels']['paid_ready']['raw_score'] = -1
        self.assertGreater(matcher.diagnostic_snapshot['labels']['paid_ready']['raw_score'], .9)


if __name__ == '__main__':
    unittest.main()
