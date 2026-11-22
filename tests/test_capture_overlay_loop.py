"""Full capture-loop recovery with virtual time; no SDK/audio/real publications."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_tapo.capture import _capture, CaptureError
from smart_tapo.capture.live_verifier import LiveVerificationError
from tests import test_inactivity_recovery as fixtures


class CaptureOverlayLoopTests(unittest.TestCase):
    def test_recovery_phase_protocol_persists_only_fixed_kinds(self):
        import logging
        import queue
        from smart_tapo.desktop_controller import _SafeLog
        from smart_tapo.audit_log import AuditLog
        from smart_tapo.runtime import ProcessLock
        with tempfile.TemporaryDirectory() as directory,ProcessLock(Path(directory)/'capture.lock'):
            audit=AuditLog(Path(directory),'capture');messages=queue.Queue()
            try:
                handler=_SafeLog(messages,'capture',audit)
                for suffix in ('started','restored'):
                    handler.emit(logging.LogRecord('test',logging.INFO,'',1,'Live overlay recovery '+suffix,(),None))
                    self.assertEqual(messages.get_nowait()[1],'ui_recovery_'+suffix)
            finally:audit.close()
            text=audit.path.read_text(encoding='utf-8')
            self.assertIn('ui_recovery_started',text);self.assertIn('ui_recovery_restored',text)

    def test_initial_and_actually_expired_modal_restore_only_fresh_selected_live(self):
        for expired in (False,True):
            with self.subTest(expired=expired),tempfile.TemporaryDirectory() as directory:
                a=fixtures.InactivityTests().adapter(directory);now=[100.]
                with patch('smart_tapo.capture.time.monotonic',side_effect=lambda:now[0]),patch('threading.Event.wait',return_value=False):
                    if expired:a.read_page_mode();now[0]+=301
                    self.assertFalse(a._authorized())
                    a.xml=fixtures.modal()
                    self.assertEqual(a.ensure_live().mode,'live')
                    self.assertEqual(a.taps,1)
                    self.assertTrue(a._authorized())

    def exercise(self,reason='live_inactivity_confirmation',fail_repair=False,delayed=False):
        now=[100.];waits=[0];connects=[0];cycles=[0];established=[0];repairs=[0]
        with tempfile.TemporaryDirectory() as directory:
            a=fixtures.InactivityTests().adapter(directory)
            original=a.recover_live
            def recover(stop):
                repairs[0]+=1
                if fail_repair and cycles[0]:raise CaptureError('Controlled unavailable overlay')
                # Four independently bounded actual post-tap failures exceed
                # the old three-attempt budget, then a fresh observation wins.
                a.finish=not (delayed and cycles[0] and repairs[0]<=5)
                return original(stop)
            a.recover_live=recover
            class Stop:
                def is_set(self):return waits[0]>=150 or established[0]>=21
                def wait(self,delay):
                    waits[0]+=1;now[0]+=delay;return self.is_set()
            stop=Stop()
            def connect(stop):
                connects[0]+=1
                if connects[0]>1:now[0]+=11  # Controlled slow reconnect, not real timing.
            a.connect=connect
            self_test=self
            class Verifier:
                checks=0
                def establish(self,stamp):established[0]+=1
                def reset(self):self.checks=0
                def check(self,stamp):
                    self.checks+=1
                    if self.checks%8==0 and cycles[0]<20:
                        # Actually expire authorization; do not reseed it just
                        # before the modal (the prior test masked this defect).
                        now[0]+=300
                        self_test.assertFalse(a._authorized())
                        a.xml=(fixtures.modal() if reason=='live_inactivity_confirmation'
                               else fixtures.live().replace(b'Camera',b'Other'))
                        a.read_page_mode();cycles[0]+=1
                        raise LiveVerificationError(reason)
                    return True
            published=[];clock_reads=[0]
            def decode(*args):
                clock_reads[0]+=1
                return str(clock_reads[0]).encode()
            def publish(config,png,session,sequence,fresh,**kwargs):
                published.append((session,fresh,established[0]))
                return Path(directory)/'virtual.ready.json'
            with patch('smart_tapo.capture.time.monotonic',side_effect=lambda:now[0]), \
                    patch('smart_tapo.capture.decode_clock',side_effect=decode), \
                    patch('smart_tapo.capture.image_storage_bytes',return_value=0), \
                    patch('smart_tapo.capture.publish_frame',side_effect=publish):
                _capture(a.config,stop,False,a,lambda:now[0],Verifier())
            if not fail_repair and reason=='live_inactivity_confirmation':
                sessions={item[0] for item in published}
                self.assertGreaterEqual(len(sessions),20)
                for session in sessions:
                    samples=[item for item in published if item[0]==session]
                    self.assertFalse(samples[0][1],'First frame after navigation cannot be fresh')
                    self.assertTrue(any(item[1] for item in samples),'Require a subsequent changed clock')
            return connects[0],cycles[0],a.taps,repairs[0],established[0]

    def test_twenty_modal_cycles_repair_before_slow_unnecessary_transport_reconnect(self):
        connects,cycles,taps,repairs,established=self.exercise()
        self.assertEqual(connects,1,'Known overlay must not restart a healthy ADB transport')
        self.assertEqual(cycles,20)
        self.assertEqual(taps,20,'Each new valid cycle must rearm, not strand behind expired proof')
        self.assertEqual(established,21)

    def test_delayed_actual_failures_recover_after_budget_without_permanent_denial(self):
        connects,cycles,taps,repairs,established=self.exercise(delayed=True)
        self.assertEqual(cycles,20)
        self.assertEqual(established,21)
        self.assertEqual(taps,24)
        self.assertGreater(connects,1)

    def test_failed_inplace_repair_falls_back_without_tapping_or_claiming_live(self):
        connects,cycles,taps,repairs,established=self.exercise(fail_repair=True)
        self.assertGreater(connects,1)
        self.assertEqual(taps,0)
        self.assertEqual(established,1)

    def test_wrong_camera_does_not_use_inplace_overlay_repair(self):
        connects,cycles,taps,repairs,established=self.exercise(reason='camera_not_confirmed')
        self.assertGreater(connects,1)
        self.assertEqual(repairs,connects)
        self.assertEqual(taps,0)
        self.assertEqual(established,1)
