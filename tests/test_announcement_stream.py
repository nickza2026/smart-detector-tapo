import unittest
from dataclasses import replace
import tempfile
from pathlib import Path
import numpy as np

from smart_tapo.announcement import AcousticResult,PhraseTemplate
from smart_tapo.announcement_profile import AnnouncementProfile
from smart_tapo.announcement_stream import AnnouncementStream,LiveLease,StreamError
from smart_tapo.capture.page_mode import PROFILE,PageMode
from tests.test_announcement import synthetic_phrase
from smart_tapo.refueling_store import RefuelingStore
from smart_tapo.continuous_listener import _commit

RUN='a'*32


def profile():
    y=synthetic_phrase()
    # Reversed synthetic tones share much temporal energy (.89 cross-score).
    # Strict synthetic-only thresholds make these fixtures discriminable;
    # deployment thresholds require independent recordings, not this value.
    return AnnouncementProfile('synthetic','cam1',(
        PhraseTemplate.from_recording('paid_ready',y,8000,.95,3.7,.95),
        PhraseTemplate.from_recording('end',y[::-1].copy(),8000,2.3,5.1,.95)), '1'*64)


def stream(**kwargs):
    return AnnouncementStream(profile(),rate=8000,channels=1,run_id=RUN,utc_origin=1000,monotonic_origin=100,**kwargs)


def lease(start,end):
    return LiveLease.from_page(PageMode('live','observed_live_layout',PROFILE),RUN,'cam1',start,end)


class AnnouncementStreamTests(unittest.TestCase):
    def test_alternate_views_merge_identity_without_backdating_evidence(self):
        base=profile()
        ready=base.templates[0]
        # Attach metadata without requiring the new constructor: RED exercises
        # the existing stream's actual duplicate publication, not a missing API.
        object.__setattr__(ready,'alternate_views',((ready.features,-2.),))
        class Views:
            calls=0
            def analyze_candidates(self,*args,**kwargs):
                self.calls+=1
                if len(args[0])<64000 or getattr(self,'emitted',False):return ()
                self.emitted=True
                first=AcousticResult('paid_ready',True,.95,3.,(.95,)*3,'acoustic_candidate')
                object.__setattr__(first,'reference_offset_seconds',-2.)
                second=AcousticResult('paid_ready',True,.95,5.13,(.95,)*3,'acoustic_candidate')
                return (first,second)
        source=AnnouncementStream(base,rate=8000,channels=1,run_id=RUN,
            utc_origin=1000,monotonic_origin=100,matcher=Views())
        observations=[]
        for start in range(0,144000,2000):
            begin=100+start/8000
            batch=source.push(np.zeros((2000,1),dtype='float32'),start_frame=start,
                now=begin+.25,lease=lease(begin,begin+10))
            observations.extend(batch.observations)
        self.assertEqual(len(observations),1)
        self.assertAlmostEqual(observations[0].announcement.started_at,1003.)
        self.assertAlmostEqual(observations[0].announcement.ended_at,1005.13+2.75)

    def test_alternate_group_orders_repeat_and_sticky_conflict(self):
        for order in ((-2.,0.),(0.,-2.),(-2.,)):
            for conflict in (False,True):
                with self.subTest(order=order,conflict=conflict):
                    base=profile();ready=base.templates[0]
                    ready=replace(ready,alternate_views=((ready.features,-2.),))
                    base=replace(base,templates=(ready,base.templates[1]))
                    class Views:
                        calls=0
                        def analyze_candidates(self,*args,**kwargs):
                            self.calls+=1
                            # First analysis occurs after 9.9s. Deliver views
                            # across windows in either order, then a true repeat.
                            if self.calls<=len(order):
                                offset=order[self.calls-1]
                                onset=3. if offset else 5.13
                                return (AcousticResult('paid_ready',True,.95,onset,
                                    (.95,)*3,'acoustic_candidate',None,offset),)
                            if conflict and self.calls==len(order)+1:
                                return (AcousticResult('end',True,.95,3.1,(.95,)*3,'acoustic_candidate'),)
                            if self.calls==len(order)+2:
                                return (AcousticResult('paid_ready',True,.95,7.5,(.95,)*3,
                                    'acoustic_candidate',None,-2.),)
                            return ()
                    source=AnnouncementStream(base,rate=8000,channels=1,run_id=RUN,
                        utc_origin=1000,monotonic_origin=100,matcher=Views())
                    observations=[];ambiguous=0
                    for start in range(0,160000,2000):
                        begin=100+start/8000
                        batch=source.push(np.zeros((2000,1),dtype='float32'),start_frame=start,
                            now=begin+.25,lease=lease(begin,begin+10))
                        observations.extend(batch.observations);ambiguous+=batch.ambiguous_candidates
                    self.assertEqual(len(observations),1 if conflict else 2)
                    self.assertEqual(observations[-1].announcement.started_at,1007.5)
                    if conflict:self.assertGreaterEqual(ambiguous,2)
                    else:self.assertEqual(observations[0].announcement.started_at,1003.)

    def test_late_alternate_identity_cannot_publish_second_event(self):
        base=profile();ready=base.templates[0]
        base=replace(base,templates=(replace(ready,alternate_views=((ready.features,-2.),)),base.templates[1]))
        class Late:
            first=False
            second=False
            def analyze_candidates(self,samples,*args,**kwargs):
                if not self.first:
                    self.first=True
                    return (AcousticResult('paid_ready',True,.95,3.,(.95,)*3,'acoustic_candidate',None,-2.),)
                if len(samples)>=112000 and not self.second:
                    self.second=True
                    return (AcousticResult('paid_ready',True,.95,5.13,(.95,)*3,'acoustic_candidate'),)
                return ()
        source=AnnouncementStream(base,rate=8000,channels=1,run_id=RUN,
            utc_origin=1000,monotonic_origin=100,matcher=Late())
        observations=[]
        for start in range(0,144000,2000):
            begin=100+start/8000
            batch=source.push(np.zeros((2000,1)),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
            observations.extend(batch.observations)
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0].announcement.started_at,1003.)

    def test_alternate_pending_is_discarded_on_stop_or_gap(self):
        for action in ('stop','gap'):
            base=profile();ready=base.templates[0]
            base=replace(base,templates=(replace(ready,alternate_views=((ready.features,-2.),)),base.templates[1]))
            class View:
                calls=0
                def analyze_candidates(self,*args,**kwargs):
                    self.calls+=1
                    return (AcousticResult('paid_ready',True,.95,3.,(.95,)*3,
                        'acoustic_candidate',None,-2.),) if self.calls==1 else ()
            source=AnnouncementStream(base,rate=8000,channels=1,run_id=RUN,
                utc_origin=1000,monotonic_origin=100,matcher=View())
            for start in range(0,88000,2000):
                begin=100+start/8000
                batch=source.push(np.zeros((2000,1)),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
                self.assertEqual(batch.observations,())
            self.assertEqual(len(source._pending),1)
            if action=='stop':source.discard()
            else:
                with self.assertRaisesRegex(StreamError,'audio_sequence_gap'):
                    source.push(np.zeros((2000,1)),start_frame=88001,now=111.25,lease=lease(111,121))
            self.assertEqual(source._pending,[])
            self.assertTrue(source.failed)

    def test_unavailable_reference_offset_fails_closed(self):
        class Invalid:
            def analyze_candidates(self,*args,**kwargs):
                return (AcousticResult('paid_ready',True,.95,3.,(.95,)*3,'acoustic_candidate',None,-2.),)
        source=stream(matcher=Invalid())
        with self.assertRaisesRegex(StreamError,'invalid_acoustic_reference_offset'):
            for start in range(0,64000,2000):
                begin=100+start/8000
                source.push(np.zeros((2000,1)),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
        self.assertTrue(source.failed)

    def test_accepted_candidate_survives_context_change_until_watermark(self):
        class Transient:
            calls=0
            def analyze_candidates(self,*args,**kwargs):
                self.calls+=1
                return (AcousticResult('paid_ready',True,.95,3.,(.95,.95,.95),'acoustic_candidate'),) if self.calls==1 else ()
        source=stream(matcher=Transient());observations=[]
        for start in range(0,96000,2000):
            begin=100+start/8000
            batch=source.push(np.zeros((2000,1),dtype='float32'),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
            if source._finalized<24000:self.assertEqual(batch.observations,())
            observations.extend(batch.observations)
        self.assertEqual([x.announcement.kind for x in observations],['paid_ready'])
        self.assertAlmostEqual(observations[0].announcement.started_at,1003.)

    def test_later_overlapping_kind_remains_ambiguous_after_earlier_finalizes(self):
        class Changing:
            calls=0
            def analyze_candidates(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1:return (AcousticResult('paid_ready',True,.95,3.,(.95,)*3,'acoustic_candidate'),)
                if self.calls==2:return (AcousticResult('end',True,.95,3.5,(.95,)*3,'acoustic_candidate'),)
                return ()
        source=stream(matcher=Changing());observations=[];ambiguous=0
        for start in range(0,96000,2000):
            begin=100+start/8000
            batch=source.push(np.zeros((2000,1),dtype='float32'),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
            observations.extend(batch.observations);ambiguous+=batch.ambiguous_candidates
        self.assertEqual(observations,[])
        self.assertGreaterEqual(ambiguous,2)

    def test_pending_evidence_is_erased_on_discard_or_sequence_failure(self):
        class Transient:
            calls=0
            def analyze_candidates(self,*args,**kwargs):
                self.calls+=1
                return (AcousticResult('paid_ready',True,.95,3.,(.95,)*3,'acoustic_candidate'),) if self.calls==1 else ()
        for action in ('discard','gap'):
            with self.subTest(action=action):
                source=stream(matcher=Transient())
                for start in range(0,64000,2000):
                    begin=100+start/8000
                    batch=source.push(np.zeros((2000,1),dtype='float32'),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
                    self.assertEqual(batch.observations,())
                self.assertEqual(len(source._pending),1)
                if action=='discard':source.discard()
                else:
                    with self.assertRaisesRegex(StreamError,'audio_sequence_gap'):
                        source.push(np.zeros((2000,1)),start_frame=64001,now=108.25,lease=lease(108,118))
                self.assertEqual(source._pending,[])
                self.assertTrue(source.failed)

    def test_changed_navigation_lease_discards_buffer_instead_of_bridging(self):
        source=stream()
        source.push(np.zeros((2000,1),dtype='float32'),start_frame=0,now=100.25,
                    lease=replace(lease(99,109),navigation_generation='a'*32))
        with self.assertRaisesRegex(StreamError,'live_navigation_changed'):
            source.push(np.zeros((2000,1),dtype='float32'),start_frame=2000,now=100.5,
                        lease=replace(lease(100,110),navigation_generation='b'*32))
        self.assertTrue(source.failed);self.assertEqual(source._size,0)
        self.assertFalse(source._buffer.any())

    def test_discard_wipes_partial_phrase_and_never_flushes(self):
        source=stream()
        samples=synthetic_phrase()[:2000,None].astype('float32')
        batch=source.push(samples,start_frame=0,now=100.25,lease=lease(99,109))
        self.assertEqual(batch.observations,())
        self.assertIsNone(batch.finalized_until)
        source.discard()
        self.assertEqual(source._size,0)
        self.assertFalse(source._buffer.any())
        source.discard() # idempotent, still no publication API
        with self.assertRaisesRegex(StreamError,'stream_already_failed'):
            source.push(samples,start_frame=2000,now=100.5,lease=lease(99,109))

    def test_two_ready_utterances_then_thanks_across_chunks_and_ring_wrap(self):
        y=synthetic_phrase();samples=np.concatenate((y,y,y[::-1],np.zeros(80000))).astype('float32')
        source=stream();observations=[];watermarks=[]
        self.assertEqual([hit.label for hit in source.matcher.analyze_candidates(y,8000)],['paid_ready'])
        self.assertEqual([hit.label for hit in source.matcher.analyze_candidates(y[::-1].copy(),8000)],['end'])
        with tempfile.TemporaryDirectory() as directory:
            store=RefuelingStore(Path(directory),'cam1',clock=lambda:1100)
            try:
                for start in range(0,len(samples),2000):
                    part=samples[start:start+2000,None]
                    begin=100+start/8000;now=begin+len(part)/8000
                    batch=source.push(part,start_frame=start,now=now,lease=lease(begin,begin+10))
                    observations.extend(batch.observations)
                    _commit(store,batch)
                    if batch.finalized_until is not None:
                        self.assertLess(batch.finalized_until,1000+start/8000)
                        watermarks.append(batch.finalized_until)
                rows=store.list_sessions()
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['ready_count'],2)
                self.assertEqual(rows[0]['state'],'completed')
                self.assertIsNone(rows[0]['missing_end_at'])
            finally:store.close()
        self.assertEqual([x.announcement.kind for x in observations],['paid_ready','paid_ready','end'])
        self.assertAlmostEqual(observations[0].announcement.started_at,1000.95,delta=.08)
        self.assertAlmostEqual(observations[1].announcement.started_at,1006.95,delta=.08)
        self.assertEqual(len({x.source_key for x in observations}),3)
        self.assertEqual(watermarks,sorted(watermarks))
        self.assertLessEqual(source._buffer.nbytes,16*1024*1024)

    def test_missing_expired_future_or_wrong_scope_lease_is_terminal(self):
        cases=(None,LiveLease(RUN,'cam1',90,100,PROFILE),LiveLease(RUN,'cam1',100.1,110,PROFILE),
               LiveLease('b'*32,'cam1',100,110,PROFILE),LiveLease(RUN,'other',100,110,PROFILE))
        for item in cases:
            source=stream()
            with self.assertRaises(StreamError):source.push(np.zeros((2000,1)),start_frame=0,now=100.25,lease=item)
            self.assertTrue(source.failed)
            with self.assertRaises(StreamError):source.push(np.zeros((2000,1)),start_frame=0,now=100.25,lease=lease(100,110))
        with self.assertRaises(StreamError):LiveLease.from_page(PageMode('playback','observed_playback_layout',PROFILE),RUN,'cam1',100,110)

    def test_sequence_gap_and_clock_backlog_never_advance_watermark(self):
        for start,now in ((1,100.25),(0,104)):
            source=stream()
            with self.assertRaises(StreamError):source.push(np.zeros((2000,1)),start_frame=start,now=now,lease=lease(100,110))
            self.assertEqual(source._finalized,0)
            self.assertTrue(source.failed)

    def test_cross_kind_overlap_is_ambiguous_not_a_payment_or_end(self):
        class Ambiguous:
            def analyze_candidates(self,*args,**kwargs):
                return tuple(AcousticResult(kind,True,.99,.5,(.99,.99,.99),'acoustic_candidate') for kind in ('paid_ready','end'))
        source=stream(matcher=Ambiguous());ambiguous=0
        for start in range(0,64000,2000):
            begin=100+start/8000
            result=source.push(np.zeros((2000,1)),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
            self.assertFalse(result.observations);ambiguous+=result.ambiguous_candidates
        self.assertGreater(ambiguous,0)

    def test_partial_phrase_stop_is_not_flushed_and_analysis_error_is_terminal(self):
        source=stream();self.assertIsNone(source.push(np.zeros((2000,1)),start_frame=0,now=100.25,lease=lease(100,110)).finalized_until)
        class Saturated:
            def analyze_candidates(self,*args,**kwargs):raise ValueError('candidate_limit')
        source=stream(matcher=Saturated())
        with self.assertRaisesRegex(StreamError,'acoustic_analysis_failed'):
            for start in range(0,64000,2000):
                begin=100+start/8000
                source.push(np.zeros((2000,1)),start_frame=start,now=begin+.25,lease=lease(begin,begin+10))
        self.assertEqual(source._finalized,0)

    def test_oversized_native_format_and_invalid_chunks_are_rejected(self):
        with self.assertRaises(StreamError):AnnouncementStream(profile(),rate=192000,channels=8,run_id=RUN,utc_origin=1000,monotonic_origin=100)
        for data in (np.full((2000,1),np.nan),np.ones((2000,2)),np.full((2000,1),2.),np.zeros((5000,1))):
            source=stream()
            with self.assertRaises(StreamError):source.push(data,start_frame=0,now=100.25,lease=lease(100,110))
            self.assertTrue(source.failed)
