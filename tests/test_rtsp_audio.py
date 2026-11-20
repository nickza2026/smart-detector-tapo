"""Offline PCM/transport regression tests: never opens a network URL."""
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from types import SimpleNamespace
import queue
import numpy as np

from smart_tapo.rtsp_audio import PcmTimeline, AudioDiscontinuity, decoded_pcm, RtspContinuousListener
from smart_tapo.announcement_stream import AnnouncementStream, RtspAudioLease, StreamError
from tests.test_announcement_stream import profile, lease, RUN


class RtspAudioTests(unittest.TestCase):
    def source(self):
        return AnnouncementStream(profile(), rate=8000, channels=1, run_id=RUN,
            utc_origin=1000, monotonic_origin=100, source_fingerprint='f'*64)

    def proof(self):
        return RtspAudioLease(RUN, 'cam1', 99., 109., 'f'*64, 'b'*32)

    def test_silent_received_pcm_is_valid_not_missing_capture(self):
        source = self.source()
        batch = source.push(np.zeros((2000, 1), dtype='float32'), start_frame=0,
                            now=100.25, lease=self.proof())
        self.assertFalse(source.failed)
        self.assertEqual(batch.observations, ())
        self.assertIsNone(batch.finalized_until)

    def test_legacy_live_lease_cannot_authorize_rtsp(self):
        with self.assertRaisesRegex(StreamError, 'provenance'):
            self.source().push(np.zeros((2000, 1)), start_frame=0,
                               now=100.25, lease=lease(99,109))

    def test_wrong_uri_run_camera_and_expired_proofs_fail(self):
        for proof in (replace(self.proof(), source_fingerprint='e'*64),
                      replace(self.proof(), run_id='c'*32),
                      replace(self.proof(), camera_id='other'),
                      replace(self.proof(), issued_at=98., expires_at=99.)):
            with self.subTest(proof=proof):
                with self.assertRaises(StreamError):
                    self.source().push(np.zeros((2000, 1)), start_frame=0,
                                       now=100.25, lease=proof)

    def test_source_session_replay_cannot_bridge_epoch(self):
        source = self.source()
        source.push(np.zeros((2000,1)), start_frame=0, now=100.25, lease=self.proof())
        with self.assertRaises(StreamError):
            source.push(np.zeros((2000,1)), start_frame=2000, now=100.5,
                        lease=replace(self.proof(), session_id='c'*32))
        self.assertTrue(source.failed)
        self.assertEqual(source._size, 0)

    def test_pcm_sequence_gap_discards_not_end(self):
        source = self.source()
        with self.assertRaisesRegex(StreamError, 'sequence_gap'):
            source.push(np.zeros((2000,1)), start_frame=1, now=100.25, lease=self.proof())
        self.assertTrue(source.failed)
        source.discard()
        self.assertEqual(source._size, 0)

    def test_timeline_continues_with_real_silent_frames(self):
        timeline = PcmTimeline()
        self.assertAlmostEqual(timeline.accept(20., 1600, 100.1), 100.)
        self.assertAlmostEqual(timeline.accept(20.1, 1600, 100.2), 100.1)

    def test_native_millisecond_pts_steps_keep_sample_clock_and_rtsp_leases(self):
        timeline=PcmTimeline()
        source=AnnouncementStream(profile(),rate=8000,channels=1,run_id=RUN,
            utc_origin=1000,monotonic_origin=100,source_fingerprint='f'*64,
            matcher=Mock(analyze_candidates=Mock(return_value=())))
        for i in range(600):
            # Native A-law had 100ms PCM frames, with +1ms source-clock steps
            # every few seconds. These must not reset a valid phrase/window.
            pts=20+i*.1+(i//30)*.001
            now=100.1+i*.1
            begin=timeline.accept(pts,1600,now)
            self.assertAlmostEqual(begin,100+i*.1)
            proof=replace(self.proof(),issued_at=begin-.01,expires_at=now+1)
            source.push(np.zeros((800,1),dtype='float32'),start_frame=i*800,now=now,lease=proof)
        self.assertFalse(source.failed)
        self.assertEqual(source._total,480000)

    def test_negative_millisecond_jitter_is_not_sample_overlap(self):
        timeline=PcmTimeline()
        self.assertAlmostEqual(timeline.accept(20.,1600,100.1),100.)
        self.assertAlmostEqual(timeline.accept(20.099,1600,100.2),100.1)

    def test_relay_packet_rewind_keeps_epoch_and_sample_clock(self):
        timeline=PcmTimeline()
        for i in range(400):
            # Field relay: a -20 ms clock rewind about once a minute of 100 ms chunks.
            pts=20+i*.1-(i//300)*.02
            self.assertAlmostEqual(timeline.accept(pts,1600,100.1+i*.1),100+i*.1)
        self.assertAlmostEqual(timeline.origin+pts+.1,100.1+399*.1)

    def test_real_overlap_missing_audio_and_excess_jitter_still_fail(self):
        for pts in (20.03,20.12,20.2,20.102):
            with self.subTest(pts=pts):
                timeline=PcmTimeline();timeline.accept(20.,1600,100.1)
                with self.assertRaises(AudioDiscontinuity):timeline.accept(pts,1600,100.2)

    def test_tiny_pcm_chunks_keep_original_two_sample_gap_bound(self):
        timeline=PcmTimeline();timeline.accept(20.,1,100.)
        with self.assertRaises(AudioDiscontinuity):timeline.accept(20.+4/16000,1,100.001)

    def test_repeated_micro_steps_cannot_hide_unbounded_source_clock_drift(self):
        timeline=PcmTimeline()
        with self.assertRaises(AudioDiscontinuity):
            for i in range(2500):timeline.accept(20+i*.101,1600,100.1+i*.1)

    def test_missing_nan_duplicate_backward_gap_and_backlog_fail(self):
        for pts, received in ((None,100.2), (float('nan'),100.2),
                              (20.,100.2), (19.,100.2), (21.,100.2), (20.1,103.)):
            with self.subTest(pts=pts, received=received):
                timeline=PcmTimeline()
                timeline.accept(20.,1600,100.1)
                with self.assertRaises(AudioDiscontinuity):
                    timeline.accept(pts,1600,received)

    def test_no_audio_track_is_explicit_and_no_resampler_runs(self):
        container=Mock()
        container.streams.audio=[]
        av=Mock()
        with self.assertRaisesRegex(AudioDiscontinuity, 'no_audio_track'):
            next(decoded_pcm(container,av))
        av.AudioResampler.assert_not_called()

    def test_idle_stop_close_never_starts_process(self):
        context=Mock()
        listener=RtspContinuousListener(context=context)
        listener.stop()
        listener.close()
        self.assertFalse(listener.is_running)
        self.assertEqual(listener.poll(),[])
        context.Process.assert_not_called()

    def test_progress_backpressure_does_not_touch_pcm(self):
        from smart_tapo.rtsp_audio import _progress
        channel=Mock()
        channel.put_nowait.side_effect=queue.Full
        _progress(channel, {'kind':'listener_progress'})
        self.assertEqual(channel.put_nowait.call_count,2)
        channel.get_nowait.assert_called_once()

    def test_commit_observations_before_watermark_and_failure_never_ticks(self):
        from smart_tapo.rtsp_audio import commit_batch, AudioStorageFailure
        store=Mock()
        batch=SimpleNamespace(observations=(SimpleNamespace(source_key='key',announcement='ready'),),
                              finalized_until=123.)
        commit_batch(store,batch)
        self.assertEqual([call[0] for call in store.mock_calls],['accept','tick'])
        store.reset_mock()
        store.accept.side_effect=OSError('private storage detail')
        with self.assertRaises(AudioStorageFailure):
            commit_batch(store,batch)
        store.tick.assert_not_called()

    def test_native_hang_terminates_only_owned_process_and_reaps(self):
        now=[0.]
        listener=RtspContinuousListener(clock=lambda:now[0])
        process=Mock()
        process.is_alive.return_value=True
        listener._process=process
        listener._recv=Mock()
        listener._recv.poll.return_value=False
        listener._progress=Mock()
        listener._progress.get_nowait.side_effect=queue.Empty
        listener._stop=Mock()
        listener._result=listener._forced=listener._stop_at=listener._kill_at=None
        listener._heartbeat=0.
        now[0]=9.
        listener.poll()
        listener._stop.set.assert_called_once()
        now[0]=11.
        listener.poll()
        process.terminate.assert_called_once()
        now[0]=12.
        listener.poll()
        process.kill.assert_called_once()
        process.is_alive.return_value=False
        result=listener.poll()
        self.assertEqual(result,[{'kind':'error','code':'audio_timeout'}])
        self.assertFalse(listener.is_running)
        process.join.assert_called_once_with(timeout=0)
        process.close.assert_called_once()

    def test_worker_missing_dependency_never_opens_transport(self):
        from smart_tapo.rtsp_audio import rtsp_audio_job
        final,progress,stop=Mock(),Mock(),Mock()
        with patch.dict('sys.modules', {'av':None}):
            rtsp_audio_job(final,progress,stop,None,None,None,None)
        final.send.assert_called_once_with({'kind':'error','code':'decoder_unavailable',
            'observations_total':0,'ready_total':0,'end_total':0,'ambiguous_total':0})

    def test_reconnect_abandons_partial_epoch_without_synthetic_end(self):
        import threading
        from smart_tapo.rtsp_audio import rtsp_audio_job
        stop=threading.Event()
        final,progress,store,av=Mock(),Mock(),Mock(),Mock()
        calls=[]
        def pcm(container, api):
            calls.append(container)
            yield np.zeros((1600,1),dtype='float32'), 0.
            if len(calls)==1:
                raise AudioDiscontinuity('rtsp_disconnected')
            stop.set()
            yield np.zeros((1600,1),dtype='float32'), .1
        config=SimpleNamespace(camera_id='cam1',data_dir='unused-synthetic',rtsp_url='rtsp://offline.invalid/test')
        with patch.dict('sys.modules', {'av':av}), \
                patch('smart_tapo.rtsp_audio.load_announcement_profile',return_value=profile()), \
                patch('smart_tapo.rtsp_audio.RefuelingStore',return_value=store), \
                patch('smart_tapo.rtsp_audio.decoded_pcm',side_effect=pcm):
            rtsp_audio_job(final,progress,stop,config,'unused',profile().fingerprint,'f'*64)
        self.assertEqual(av.open.call_count,2)
        self.assertGreaterEqual(store.mark_audio_gap.call_count,3)
        store.accept.assert_not_called()
        store.tick.assert_not_called()
        final.send.assert_called_once_with({'kind':'finished','mode':'listener',
            'observations_total':0,'ready_total':0,'end_total':0,'ambiguous_total':0})
        for call in av.open.call_args_list:
            self.assertEqual(call.kwargs['options']['allowed_media_types'],'audio')
            self.assertEqual(call.kwargs['options']['rtsp_transport'],'tcp')

    def test_worker_no_audio_track_marks_gap_and_stops_not_fallback(self):
        import threading
        from smart_tapo.rtsp_audio import rtsp_audio_job
        final,progress,store,av=Mock(),Mock(),Mock(),Mock()
        av.open.return_value.streams.audio=[]
        config=SimpleNamespace(camera_id='cam1',data_dir='unused-synthetic',rtsp_url='rtsp://offline.invalid/test')
        with patch.dict('sys.modules', {'av':av}), \
                patch('smart_tapo.rtsp_audio.load_announcement_profile',return_value=profile()), \
                patch('smart_tapo.rtsp_audio.RefuelingStore',return_value=store):
            rtsp_audio_job(final,progress,threading.Event(),config,'unused',profile().fingerprint,'f'*64)
        final.send.assert_called_once_with({'kind':'error','code':'no_audio_track',
            'observations_total':0,'ready_total':0,'end_total':0,'ambiguous_total':0})
        self.assertEqual(av.open.call_count,1)
        store.accept.assert_not_called()
        store.tick.assert_not_called()
        self.assertEqual(store.mark_audio_gap.call_count,3)

    def test_worker_counters_are_typed_cumulative_and_retained_in_final(self):
        import threading
        from smart_tapo.rtsp_audio import rtsp_audio_job
        stop=threading.Event();final,progress,store,av,core=Mock(),Mock(),Mock(),Mock(),Mock()
        items=tuple(SimpleNamespace(source_key=f'synthetic-{i}',announcement=SimpleNamespace(kind=kind))
                    for i,kind in enumerate(('paid_ready','paid_ready','end')))
        core.push.return_value=SimpleNamespace(observations=items,ambiguous_candidates=1,finalized_until=None)
        def pcm(*args):
            yield np.zeros((1600,1),dtype='float32'),0.
            stop.set()
        config=SimpleNamespace(camera_id='cam1',data_dir='unused-synthetic',rtsp_url='rtsp://offline.invalid/test')
        with patch.dict('sys.modules',{'av':av}),patch('smart_tapo.rtsp_audio.load_announcement_profile',return_value=profile()), \
                patch('smart_tapo.rtsp_audio.RefuelingStore',return_value=store), \
                patch('smart_tapo.rtsp_audio.AnnouncementStream',return_value=core), \
                patch('smart_tapo.rtsp_audio.decoded_pcm',side_effect=pcm):
            rtsp_audio_job(final,progress,stop,config,'unused',profile().fingerprint,'f'*64)
        snapshot=progress.put_nowait.call_args.args[0]
        self.assertEqual((snapshot['observations_total'],snapshot['ready_total'],snapshot['end_total'],snapshot['ambiguous_total']),(3,2,1,1))
        final.send.assert_called_once_with(dict(kind='finished',mode='listener',observations_total=3,ready_total=2,end_total=1,ambiguous_total=1))
        self.assertEqual(store.accept.call_count,3)
        self.assertEqual(store.mark_audio_gap.call_count,2)

    def test_consent_required_before_profile_or_network(self):
        listener=RtspContinuousListener()
        with self.assertRaisesRegex(ValueError,'consent_required'):
            listener.start(None,None,'not-a-profile')
        self.assertFalse(listener.is_running)


if __name__ == '__main__':
    unittest.main()
