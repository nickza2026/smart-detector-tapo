"""Bounded in-RAM streaming decisions, never devices/files/network by itself.

Live leases are a finite policy based on observed UI checks, not a guarantee
against an undetected between-check switch or other apps on mixed loopback.
The owner commits observations in order BEFORE advancing finalized_until.
"""
from dataclasses import dataclass
import math
import re

import numpy as np

from .announcement import AnnouncementMatcher,HOP,RATE,MAX_SECONDS
from .announcement_profile import AnnouncementProfile
from .capture.page_mode import PROFILE,PageMode
from .refueling import ConfirmedAnnouncement


class StreamError(ValueError):
    pass


def _number(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


@dataclass(frozen=True)
class LiveLease:
    run_id:str
    camera_id:str
    issued_at:float
    expires_at:float
    page_profile_id:str
    navigation_generation:str|None=None

    def __post_init__(self):
        if (not isinstance(self.run_id,str) or not re.fullmatch('[0-9a-f]{32}',self.run_id)
                or not isinstance(self.camera_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.camera_id)
                or not _number(self.issued_at) or not _number(self.expires_at)
                or not 0<self.expires_at-self.issued_at<=10 or self.page_profile_id!=PROFILE
                or (self.navigation_generation is not None and self.navigation_generation!='initial'
                    and (not isinstance(self.navigation_generation,str)
                         or re.fullmatch('[0-9a-f]{32}',self.navigation_generation) is None))):
            raise StreamError('invalid_live_lease')

    @classmethod
    def from_page(cls,page,run_id,camera_id,issued_at,expires_at):
        if not isinstance(page,PageMode) or page.mode!='live' or page.profile_id!=PROFILE:
            raise StreamError('live_page_not_confirmed')
        return cls(run_id,camera_id,issued_at,expires_at,page.profile_id,page.navigation_generation)


@dataclass(frozen=True)
class RtspAudioLease:
    """Receiver continuity only; does not attest that a camera is physically Live."""
    run_id:str
    camera_id:str
    issued_at:float
    expires_at:float
    source_fingerprint:str
    session_id:str

    def __post_init__(self):
        if (not isinstance(self.run_id,str) or re.fullmatch('[0-9a-f]{32}',self.run_id) is None
                or not isinstance(self.session_id,str) or re.fullmatch('[0-9a-f]{32}',self.session_id) is None
                or not isinstance(self.source_fingerprint,str)
                or re.fullmatch('[0-9a-f]{64}',self.source_fingerprint) is None
                or not isinstance(self.camera_id,str) or re.fullmatch('[A-Za-z0-9_-]{1,64}',self.camera_id) is None
                or not _number(self.issued_at) or not _number(self.expires_at)
                or not 0<self.expires_at-self.issued_at<=10):
            raise StreamError('invalid_rtsp_lease')


@dataclass(frozen=True)
class StreamObservation:
    source_key:str
    announcement:ConfirmedAnnouncement


@dataclass(frozen=True)
class StreamBatch:
    observations:tuple[StreamObservation,...]=()
    finalized_until:float|None=None
    ambiguous_candidates:int=0


class AnnouncementStream:
    """Frozen run/format/profile, reliable ordered chunks; any gap is terminal.

    <=20s raw ring /16MiB; common 44.1/48kHz stereo is supported. Analysis at
    most4Hz. Two maximum-phrase lengths of lookahead allow cross-kind overlap
    arbitration BEFORE publishing/ticking; this is detection latency, not a
    fabricated end time. No partial phrase is flushed as success at stop.
    """
    def __init__(self,profile,*,rate,channels,run_id,utc_origin,monotonic_origin,matcher=None,source_fingerprint=None):
        if (not isinstance(profile,AnnouncementProfile) or type(rate) is not int or not 8000<=rate<=192000
                or rate%100 or type(channels) is not int or not 1<=channels<=8
                or rate*channels*4*MAX_SECONDS>16*1024*1024
                or not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id)
                or not _number(utc_origin) or utc_origin>253402300700 or not _number(monotonic_origin)):
            raise StreamError('invalid_stream_configuration')
        kinds={t.label for t in profile.templates}
        if kinds!={'paid_ready','end'} or len(profile.templates)!=2:raise StreamError('invalid_stream_profile')
        durations={t.label:t.features.shape[1]*HOP/RATE for t in profile.templates}
        self._offsets={t.label:{0.,*(offset for _,offset in t.alternate_views)} for t in profile.templates}
        longest=max(durations[t.label]+max(self._offsets[t.label])-min(self._offsets[t.label])
                    for t in profile.templates)
        if longest>9.5:raise StreamError('profile_too_long_for_stream')
        self.profile=profile;self.rate=rate;self.channels=channels;self.run_id=run_id
        self.utc_origin=float(utc_origin);self.monotonic_origin=float(monotonic_origin)
        self.matcher=matcher if matcher is not None else AnnouncementMatcher(profile.templates)
        self._durations={key:round(value*rate) for key,value in durations.items()}
        self._horizon=math.ceil((longest*2+.4)*rate)
        self._capacity=rate*MAX_SECONDS
        self._buffer=np.empty((self._capacity,channels),dtype='float32')
        self._total=self._write=self._size=self._last_analysis=self._finalized=0
        self._last_emitted={};self.failed=False
        self._pending=[]
        self._last_observation_count = 0
        if source_fingerprint is not None and (not isinstance(source_fingerprint,str)
                or re.fullmatch('[0-9a-f]{64}',source_fingerprint) is None):
            raise StreamError('invalid_stream_configuration')
        self.source_fingerprint=source_fingerprint
        self._navigation_generation=None

    @property
    def diagnostic_snapshot(self):
        """Last analyzed window only; never infer speech, customers, or money."""
        result = dict(match_state='failed' if self.failed else 'warming_up', pending_count=len(self._pending))
        diagnostic = getattr(self.matcher, 'diagnostic_snapshot', None)
        if type(diagnostic) is dict and diagnostic.get('analyzed') is True and not self.failed:
            result.update(candidate_count=diagnostic['candidate_count'], rejected_count=diagnostic['rejected_count'])
            result['match_state'] = ('confirmed' if self._last_observation_count else
                'pending' if self._pending else 'filtered' if diagnostic['rejected_count'] else 'no_candidate')
            views=[v for k,v in diagnostic['labels'].items() if k=='paid_ready' or k.startswith('paid_ready@')]
            ready=max(views,key=lambda v:(v.get('accepted_count',0)>0,v.get('raw_score',-2)),default={})
            result.update({f'ready_{k}': v for k, v in ready.items()
                           if k in ('raw_score', 'threshold', 'temporal_score', 'temporal_threshold',
                                    'reference_offset_seconds')})
        return result

    def discard(self):
        """Wipe an abandoned epoch without publishing partial decisions."""
        self._buffer.fill(0)
        self._size=0
        self._pending.clear()
        self.failed=True

    def _fault(self,code):
        self.failed=True;self._buffer.fill(0);self._size=0;self._pending.clear()
        raise StreamError(code)

    def push(self,samples,*,start_frame,now,lease):
        if self.failed:raise StreamError('stream_already_failed')
        if type(start_frame) is not int or start_frame!=self._total:self._fault('audio_sequence_gap')
        try:x=np.asarray(samples)
        except (ValueError,TypeError,OverflowError):self._fault('invalid_audio_chunk')
        if (x.ndim!=2 or x.shape[1]!=self.channels or not 0<len(x)<=self.rate//2
                or not np.issubdtype(x.dtype,np.number) or np.iscomplexobj(x)
                or not np.isfinite(x).all() or x.min()<-1.001 or x.max()>1.001):
            self._fault('invalid_audio_chunk')
        begin=self.monotonic_origin+start_frame/self.rate
        end=begin+len(x)/self.rate
        rtsp=self.source_fingerprint is not None
        valid_source=(isinstance(lease,RtspAudioLease) and lease.source_fingerprint==self.source_fingerprint
                      if rtsp else isinstance(lease,LiveLease))
        if (not _number(now) or not valid_source or lease.run_id!=self.run_id
                or lease.camera_id!=self.profile.camera_id or lease.issued_at>begin
                or lease.expires_at<end or now>lease.expires_at):
            self._fault('live_provenance_lost')
        generation=lease.session_id if rtsp else lease.navigation_generation or 'initial'
        if self._navigation_generation is None:self._navigation_generation=generation
        elif self._navigation_generation!=generation:self._fault('live_navigation_changed')
        if abs(now-end)>2:self._fault('audio_clock_discontinuity')
        count=len(x);first=min(count,self._capacity-self._write)
        self._buffer[self._write:self._write+first]=x[:first]
        if first<count:self._buffer[:count-first]=x[first:]
        self._write=(self._write+count)%self._capacity
        self._size=min(self._capacity,self._size+count);self._total+=count
        if self._total-self._last_analysis<self.rate//4 or self._total<=self._horizon:return StreamBatch()
        self._last_analysis=self._total
        hop=self.rate//100
        window_start=self._total-self._size
        skip=(-window_start)%hop;window_start+=skip
        if self._size<self._capacity:window=self._buffer[:self._size]
        else:window=np.concatenate((self._buffer[self._write:],self._buffer[:self._write]))
        window=window[skip:]
        finalized=((self._total-self._horizon)//hop)*hop
        if finalized<=self._finalized:return StreamBatch()
        try:results=self.matcher.analyze_candidates(window,self.rate,continuous=True)
        except Exception:self._fault('acoustic_analysis_failed')
        if not isinstance(results,tuple) or len(results)>64:self._fault('acoustic_result_limit')
        for result in results:
            if (result.label not in self._durations or result.candidate is not True
                    or not _number(result.onset_seconds)):
                self._fault('invalid_acoustic_result')
            offset=getattr(result,'reference_offset_seconds',0.)
            if (type(offset) not in (int,float) or not math.isfinite(offset)
                    or offset not in self._offsets[result.label]):
                self._fault('invalid_acoustic_reference_offset')
            start=window_start+round(result.onset_seconds*self.rate)
            end_frame=start+self._durations[result.label]
            if start<window_start or end_frame>self._total:self._fault('invalid_acoustic_span')
            if start<=self._finalized or start<window_start+round(.2*self.rate):continue
            # Accepted acoustic evidence can disappear on the next analysis:
            # its background normalization depends on the rolling context.
            # Retain it until the existing lookahead watermark, never emit early.
            identity_start=start-round(offset*self.rate)
            identity_end=identity_start+self._durations[result.label]
            match=next((item for item in self._pending if item['kind']==result.label
                and min(identity_end,item['identity_end'])-max(identity_start,item['identity_start'])>=
                .5*self._durations[result.label]),None)
            if match is None:
                match=dict(start=start,end=end_frame,kind=result.label,ambiguous=False,
                           identity_start=identity_start,identity_end=identity_end)
                self._pending.append(match)
                if len(self._pending)>128:self._fault('acoustic_pending_limit')
            elif self._offsets[result.label]!={0.}:
                # Identity stays anchored to the first accepted view: repeated
                # window jitter must not chain unrelated phrases into one group.
                if min(end_frame,match['end'])<max(start,match['start']):
                    self._fault('disjoint_acoustic_evidence')
                match['start']=min(start,match['start'])
                match['end']=max(end_frame,match['end'])
                if match['end']-match['start']>self._capacity:self._fault('invalid_acoustic_span')
            for other in self._pending:
                if other['kind']!=result.label and min(match['end'],other['end'])>max(match['start'],other['start']):
                    match['ambiguous']=other['ambiguous']=True
        output=[];ambiguous=0
        for item in sorted(self._pending,key=lambda item:(item['start'],item['end'],item['kind'])):
            start,end_frame,kind=item['start'],item['end'],item['kind']
            if not self._finalized<start<=finalized:continue
            # A conflict stays uncertain even after the earlier span finalizes.
            if item['ambiguous']:
                ambiguous+=1;continue
            previous=self._last_emitted.get(kind)
            identity_start,identity_end=item['identity_start'],item['identity_end']
            if previous and min(identity_end,previous[1])-max(identity_start,previous[0])>=.5*self._durations[kind]:continue
            self._last_emitted[kind]=(identity_start,identity_end)
            announcement=ConfirmedAnnouncement(kind,self.utc_origin+start/self.rate,self.utc_origin+end_frame/self.rate)
            output.append(StreamObservation(f'{self.run_id}:{kind}:{start}',announcement))
        self._pending=[item for item in self._pending if item['start']>finalized]
        self._finalized=finalized
        self._last_observation_count = len(output)
        return StreamBatch(tuple(output),self.utc_origin+finalized/self.rate,ambiguous)
