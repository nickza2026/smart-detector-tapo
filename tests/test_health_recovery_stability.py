"""A single returning PCM sample must not split one outage into new incidents."""
import unittest
from smart_tapo.health_alerts import HealthMonitor


class RecoveryStabilityTests(unittest.TestCase):
    def fault(self):
        monitor=HealthMonitor(delay=30,repeat=900)
        monitor.arm('audio',0)
        monitor.observe('audio',False,'audio_no_pcm',0)
        notice=monitor.take_due(30)[0]
        self.assertTrue(monitor.mark_sending(notice))
        self.assertTrue(monitor.complete(notice,'success',30))
        return monitor

    def test_one_pcm_then_stale_does_not_send_recovery_or_new_fault(self):
        monitor=self.fault()
        monitor.observe('audio',True,'ok',120)
        self.assertEqual(monitor.take_due(120),[])
        monitor.observe('audio',False,'audio_no_pcm',129)
        self.assertEqual(monitor.take_due(159),[])
        notice=monitor.take_due(930)[0]
        self.assertEqual(notice.kind,'reminder')
        self.assertEqual(notice.incident,1)

    def test_continuous_progress_confirms_recovery_once_then_real_new_fault(self):
        monitor=self.fault()
        for now in range(120,150):
            monitor.observe('audio',True,'ok',now)
            self.assertEqual(monitor.take_due(now),[])
        monitor.observe('audio',True,'ok',150)
        recovery=monitor.take_due(150)[0]
        self.assertEqual(recovery.kind,'recovered')
        self.assertTrue(monitor.mark_sending(recovery))
        self.assertTrue(monitor.complete(recovery,'success',150))
        self.assertEqual(monitor.take_due(151),[])
        monitor.observe('audio',False,'audio_no_pcm',160)
        self.assertEqual(monitor.take_due(189),[])
        self.assertEqual(monitor.take_due(190)[0].kind,'fault')

    def test_time_without_new_samples_never_proves_recovery(self):
        monitor=self.fault()
        monitor.observe('audio',True,'ok',120)
        self.assertEqual(monitor.take_due(150),[])
        monitor.observe('audio',True,'ok',155)
        self.assertEqual(monitor.take_due(155),[])
