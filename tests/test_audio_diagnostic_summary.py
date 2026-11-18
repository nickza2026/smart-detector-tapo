import unittest
from smart_tapo.audio_diagnostic_summary import format_report,number
from smart_tapo.offline_audio import DiagnosticReport,DiagnosticRow


class DiagnosticSummaryTests(unittest.TestCase):
    def test_minutes_apart_test_intervals_are_explicit_not_money(self):
        from types import SimpleNamespace
        from smart_tapo.offline_test_rounds import TestRoundTracker
        tracker=TestRoundTracker()
        tracker.consume(SimpleNamespace(reason='accepted_test_candidate',label='paid_ready',onset=1))
        tracker.consume(SimpleNamespace(reason='accepted_test_candidate',label='end',onset=301))
        report=SimpleNamespace(duration=320,rate=8000,channels=1,rms=.1,peak=.5,
            finalized_until=310,lookahead=10,rows=(),test_rounds=tracker.snapshot(),metadata=lambda:{'mode':'TEST'})
        text=format_report(report)
        self.assertIn('300.0000',text)
        self.assertIn('ไม่ใช่รายการรับเงิน',text)
        self.assertIn('พบวลีครบ',text)

    def test_thai_summary_distinguishes_maximum_from_accepted_candidate(self):
        rows=(DiagnosticRow(10,'paid_ready',1,3,.98,(.9,.9,.9),.65,
              'maximum_passed_matcher_not_stream_decision','whole_file_maximum'),)
        report=DiagnosticReport('TEST_OFFLINE_NOT_LIVE','PCM16',10,8000,1,'a'*64,'b'*64,
                                .1,.8,1,8,rows)
        text=format_report(report)
        self.assertIn('คะแนนสูงสุดผ่าน matcher แต่ไม่ใช่ผลยอมรับจาก stream',text)
        self.assertIn('ไม่ใช่เปอร์เซ็นต์',text)
        self.assertIn('ไม่ใช่จำนวนลูกค้า',text)
        self.assertNotIn('ผ่านกติกาตรวจวลีในไฟล์',text)

    def test_unknown_or_nonfinite_scores_are_not_zero(self):
        self.assertEqual(number(None),'ไม่มีข้อมูล')
        self.assertEqual(number(float('nan')),'ไม่มีข้อมูล')
        self.assertEqual(number(0),'0.0000')
