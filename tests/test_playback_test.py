"""No hardware, SDK, device enumeration or network in these tests."""
from threading import Event
import unittest
from unittest.mock import patch
import numpy as np

from smart_tapo.playback_test import PlaybackTest, analyze_stream, validate_device
from tests.test_announcement import synthetic_phrase
from tests.test_announcement_stream import profile


def device():
    return dict(index=4, name='Explicit output [Loopback]', rate=8000, channels=1,
                endpoint_id='{11111111-2222-3333-4444-555555555555}')


def cooperative_child(final, updates, stop, *args):
    stop.wait(20)
    final.send(dict(kind='stopped', incomplete=True))
    final.close()


def stuck_child(final, updates, stop, *args):
    import time
    time.sleep(30)


class PlaybackTests(unittest.TestCase):
    def test_window_score_observer_does_not_change_matcher_decisions(self):
        from smart_tapo.offline_audio import _analyze_chunks
        x=np.concatenate((synthetic_phrase(),np.zeros(64000))).astype('float32')[:,None]
        def run(observer):
            return _analyze_chunks((x[i:i+4000] for i in range(0,len(x),4000)),
                8000,1,len(x),profile(),Event(),'synthetic',None,None,diagnostics=observer)
        baseline=run(None)
        windows=[]
        observed=run(windows.append)
        self.assertEqual(baseline,observed)
        self.assertTrue(windows)
        self.assertLessEqual(len(windows),int(len(x)/8000))
        self.assertTrue(all(row.reason!='accepted_test_candidate' for rows in windows for row in rows))

    def test_received_pcm_has_window_scores_even_without_accepted_phrase(self):
        stop=Event();updates=[]
        def chunks():
            for _ in range(8):yield np.zeros((2000,1),dtype='float32')
            stop.set()
        result=analyze_stream(chunks(),8000,1,profile(),stop,updates.append)
        self.assertTrue(result['received'])
        self.assertEqual(result['scores'],{})
        self.assertTrue(result['maxima'])
        self.assertEqual(result['rounds']['counts']['complete'],0)
        self.assertTrue(any(update['maxima'] for update in updates))

    def test_spawn_duplicate_stop_reap_and_no_restart(self):
        import time
        from types import SimpleNamespace
        controller=PlaybackTest(target=cooperative_child)
        controller.start(SimpleNamespace(),device(),consent=True)
        process=controller.process
        try:
            with self.assertRaises(ValueError): controller.start(None,device(),consent=True)
            self.assertIs(controller.process,process)
            controller.stop()
            deadline=time.monotonic()+5
            while controller.is_running and time.monotonic()<deadline:
                controller.poll(); time.sleep(.01)
            self.assertFalse(controller.is_running)
            self.assertEqual(controller.poll(),[])
        finally: controller.close()

    def test_close_force_reaps_stuck_owned_child(self):
        import time
        from types import SimpleNamespace
        controller=PlaybackTest(target=stuck_child)
        controller.start(SimpleNamespace(),device(),consent=True)
        start=time.monotonic(); controller.close()
        self.assertLess(time.monotonic()-start,2)
        self.assertFalse(controller.is_running)
        with self.assertRaises(ValueError): controller.start(None,device(),consent=True)

    def test_killed_child_with_broken_result_pipe_reports_exit(self):
        import multiprocessing
        from unittest.mock import Mock
        class Dead:
            def is_alive(self): return False
            def join(self,timeout=None): pass
            def close(self): pass
        # Exercise both Windows failure sites deterministically, independent of
        # whether this machine reports a closed native pipe as EOF or WinError109.
        for failed_method in ('poll','recv'):
            with self.subTest(failed_method=failed_method):
                ctx=multiprocessing.get_context('spawn')
                controller=PlaybackTest(target=stuck_child)
                receiver=Mock()
                receiver.poll.return_value=True
                getattr(receiver,failed_method).side_effect=BrokenPipeError(109,'synthetic closed pipe')
                controller.recv=receiver
                controller.updates=ctx.Queue();controller.process=Dead()
                updates=controller.updates
                controller.stop_at=None;controller.last_update=controller.clock()
                try:
                    self.assertEqual(controller.poll(),[dict(kind='error',code='capture_worker_exit',incomplete=True)])
                    self.assertFalse(controller.is_running)
                    self.assertEqual(controller.poll(),[])
                    receiver.close.assert_called_once()
                    self.assertTrue(updates._closed)
                finally:
                    updates.close();updates.cancel_join_thread()

    def test_native_closed_result_pipe_reports_exit_and_reaps(self):
        import multiprocessing
        from unittest.mock import Mock
        ctx=multiprocessing.get_context('spawn')
        controller=PlaybackTest(target=stuck_child)
        controller.recv,send=ctx.Pipe(duplex=False);send.close()
        controller.updates=ctx.Queue();controller.process=Mock()
        controller.process.is_alive.return_value=False
        controller.stop_at=None;controller.last_update=controller.clock()
        try:
            self.assertEqual(controller.poll(),[dict(kind='error',code='capture_worker_exit',incomplete=True)])
            self.assertFalse(controller.is_running)
        finally:
            controller.recv.close();controller.updates.close();controller.updates.cancel_join_thread()

    def test_native_job_identity_mismatch_never_opens_output(self):
        from types import SimpleNamespace
        from smart_tapo.playback_test import playback_job
        class Final:
            def send(self,result): self.result=result
            def close(self): pass
        class Updates:
            def cancel_join_thread(self): pass
        audio=unittest.mock.Mock()
        api=SimpleNamespace(PyAudio=lambda:audio)
        final=Final()
        with patch('smart_tapo.announcement_profile.load_announcement_profile',return_value=profile()), patch('smart_tapo.audio.loopback_devices',return_value=[dict(device(),endpoint_id='{aaaaaaaa-2222-3333-4444-555555555555}')]):
            playback_job(final,Updates(),Event(),device(),SimpleNamespace(camera_id='test'),'synthetic','',api)
        self.assertEqual(final.result['kind'],'error')
        self.assertEqual(final.result['code'],'device_identity_changed')
        audio.open.assert_not_called()
        audio.terminate.assert_called_once()

    def test_construct_poll_close_never_import_native(self):
        with patch('builtins.__import__', wraps=__import__) as imports:
            controller = PlaybackTest()
            self.assertEqual(controller.poll(), [])
            controller.stop(); controller.close()
            self.assertFalse(controller.is_running)
            self.assertFalse(any(call.args[0] == 'pyaudiowpatch' for call in imports.call_args_list))

    def test_explicit_consent_and_loopback_guid_required(self):
        controller = PlaybackTest()
        for kwargs in ({}, {'consent':True, 'other_running':True}):
            with self.assertRaises(ValueError): controller.start(None, device(), **kwargs)
        for bad in (None, {}, dict(device(), endpoint_id='default'), dict(device(), rate=0)):
            with self.assertRaises(ValueError): validate_device(bad)
        self.assertEqual(validate_device(device()), device())

    def test_unknown_then_genuine_silence_no_synthetic_end(self):
        stop = Event(); updates=[]
        def chunks():
            yield np.zeros((4000,1),dtype='float32')
            stop.set()
        state=analyze_stream(chunks(),8000,1,profile(),stop,updates.append)
        self.assertFalse(updates[0]['received']); self.assertIsNone(updates[0]['rms'])
        self.assertTrue(state['received']); self.assertEqual(state['rms'],0)
        self.assertTrue(state['incomplete']); self.assertEqual(state['rounds']['counts']['end'],0)

    def test_unexpected_eof_and_gap_fail_closed(self):
        for failure in (False,True):
            def chunks():
                yield np.zeros((4000,1),dtype='float32')
                if failure: raise ValueError('capture_discontinuity')
            updates=[]
            with self.assertRaises(ValueError):
                analyze_stream(chunks(),8000,1,profile(),Event(),updates.append)
            self.assertTrue(updates[-1]['incomplete'])
            self.assertEqual(updates[-1]['rounds']['counts']['complete'],0)

    def test_genuine_ncc_cross_twenty_seconds_and_minutes_state(self):
        rate=8000; phrase=synthetic_phrase()
        stop=Event(); updates=[]
        def chunks():
            for start in range(0,160*rate,rate//2):
                part=np.zeros(rate//2,dtype='float32')
                for offset, signal in ((18*rate,phrase),(145*rate,phrase[::-1])):
                    low=max(start,offset); high=min(start+len(part),offset+len(signal))
                    if high>low: part[low-start:high-start]=signal[low-offset:high-offset]
                yield part[:,None]
            stop.set()
        state=analyze_stream(chunks(),rate,1,profile(),stop,updates.append)
        self.assertEqual(state['rounds']['counts']['complete'],1)
        self.assertGreater(state['rounds']['records'][0]['duration'],120)
        self.assertEqual(set(state['scores']),{'paid_ready','end'})
        self.assertTrue(any(u['rounds']['pending'] and u['elapsed']<145 for u in updates))
        self.assertFalse(any('samples' in u or 'pcm' in u for u in updates))


if __name__ == '__main__': unittest.main()
