import unittest
from types import SimpleNamespace
from smart_tapo.offline_test_rounds import TestRoundTracker


def event(label,onset,reason='accepted_test_candidate'):
    return SimpleNamespace(label=label,onset=onset,reason=reason)


class TestPhraseIntervals(unittest.TestCase):
    def test_minutes_apart_are_complete_without_twenty_second_expiry(self):
        tracker=TestRoundTracker()
        tracker.consume(event('paid_ready',2))
        tracker.consume(event('end',302))
        result=tracker.snapshot()
        self.assertEqual(result['counts']['complete'],1)
        self.assertEqual(result['records'][0]['duration'],300)
        self.assertIsNone(result['pending'])

    def test_eof_does_not_invent_end_and_orphan_end_does_not_invent_start(self):
        tracker=TestRoundTracker()
        tracker.consume(event('end',10))
        tracker.consume(event('paid_ready',20))
        result=tracker.snapshot()
        self.assertEqual(result['records'][0]['status'],'end_without_start')
        self.assertEqual(result['pending']['status'],'start_without_end')
        self.assertIsNone(result['pending']['end'])
        self.assertEqual(result['counts']['complete'],0)

    def test_row_limit_does_not_discard_pending_start_or_stop_late_detection(self):
        tracker=TestRoundTracker(limit=1)
        for start in (1,100,1000):
            tracker.consume(event('paid_ready',start))
            tracker.consume(event('end',start+50))
        result=tracker.snapshot()
        self.assertEqual(result['counts']['complete'],3)
        self.assertEqual(len(result['records']),1)
        self.assertEqual(result['omitted_records'],2)
        self.assertTrue(result['records_truncated'])

    def test_repeated_ready_is_ambiguous_not_an_invented_transaction(self):
        tracker=TestRoundTracker()
        tracker.consume(event('paid_ready',1))
        tracker.consume(event('paid_ready',100))
        tracker.consume(event('end',300))
        result=tracker.snapshot()
        self.assertEqual(result['counts']['complete'],0)
        self.assertEqual(result['records'][0]['status'],'ambiguous')
        self.assertIsNone(result['records'][0]['duration'])

    def test_rejected_maximum_is_not_a_start(self):
        tracker=TestRoundTracker()
        tracker.consume(event('paid_ready',1,'maximum_passed_matcher_not_stream_decision'))
        tracker.consume(event('end',30))
        self.assertEqual(tracker.snapshot()['counts']['ready'],0)
        self.assertEqual(tracker.snapshot()['counts']['complete'],0)
