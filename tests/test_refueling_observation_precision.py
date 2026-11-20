"""Observation instants retain precision instead of comparing rounded floats."""
import json
import unittest

from smart_tapo.refueling_contract import validate_observation


class ObservationPrecisionTests(unittest.TestCase):
    def body(self, start, occurred=None, end=None):
        return json.dumps({
            'schema_version': 1,
            'event_id': '11111111111141118111111111111111:1',
            'event_type': 'refueling.observation',
            'occurred_at': occurred or start,
            'observation': {
                'id': '11111111111141118111111111111111', 'camera_id': 'cabinet-1',
                'kind': 'end', 'reason': 'completion_without_start',
                'observed_started_at': start,
                'observed_ended_at': end or '2026-11-19T18:48:02Z',
                'audio_group_id': None, 'amount_baht': None, 'evidence': [],
            },
        }).encode()

    def test_different_nanosecond_instants_are_not_equal(self):
        with self.assertRaises(ValueError):
            validate_observation(self.body('2026-11-19T18:48:00.0000002Z',
                                           occurred='2026-11-19T18:48:00.0000001Z'))

    def test_end_one_nanosecond_before_start_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_observation(self.body('2026-11-19T18:48:00.0000002Z',
                                           end='2026-11-19T18:48:00.0000001Z'))

    def test_subnanosecond_difference_is_not_rounded_away(self):
        with self.assertRaises(ValueError):
            validate_observation(self.body('2026-11-19T18:48:00.0000000002Z',
                                           occurred='2026-11-19T18:48:00.0000000001Z'))

    def test_same_instant_with_offset_and_fraction_width_is_accepted(self):
        validate_observation(self.body('2026-11-19T18:48:00.0000001Z',
                                       occurred='2026-11-20T01:48:00.000000100+07:00'))

    def test_long_fraction_keeps_exact_equality(self):
        stamp = '2026-11-19T18:48:00.123456789123456789Z'
        validate_observation(self.body(stamp))


if __name__ == '__main__':
    unittest.main()
