"""Bounded offline acoustic-template matching, NOT payment/session decisions.

No audio devices, network, database, or model downloads are used here. Candidate
scores are correlations, never probabilities. A caller must provide separately
verified phrase templates and continuity/provenance before using any result.
The streaming owner must still gate source/continuity and arbitrate different
kinds of announcements before calling a store.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import wave

import cv2
import numpy as np

RATE=8000
HOP=80
WINDOW=256
MAX_SECONDS=20


def pcm8k(samples,rate):
    """Anti-aliased conversion of bounded mono or frames-by-channels PCM floats."""
    if type(rate) is not int or not 8000<=rate<=192000:
        raise ValueError('Unsupported sample rate')
    x=np.asarray(samples)
    if x.ndim not in (1,2) or not len(x) or len(x)>rate*MAX_SECONDS:
        raise ValueError('Audio window is empty or exceeds bounded duration')
    if x.ndim==2 and not 1<=x.shape[1]<=8:
        raise ValueError('Unsupported channel count')
    if not np.issubdtype(x.dtype,np.number) or np.iscomplexobj(x) or not np.isfinite(x).all():
        raise ValueError('Audio must be finite real PCM')
    if x.min()<-1.001 or x.max()>1.001:
        raise ValueError('Audio must be normalized PCM')
    if x.ndim==2:
        # Avoid cancelling an inverted stereo channel: use the strongest channel.
        energy=np.einsum('ij,ij->j',x,x,dtype='float64')
        x=x[:,int(np.argmax(energy))]
    x=x.astype('float64')
    if rate==RATE:return x
    positions=np.arange(-128,129)
    cutoff=3600/rate
    kernel=2*cutoff*np.sinc(2*cutoff*positions)*np.hanning(257)
    kernel/=kernel.sum()
    if len(x)<len(kernel):
        raise ValueError('Audio is too short to resample')
    filtered=np.convolve(x,kernel,mode='same')
    return np.interp(np.arange(round(len(x)*RATE/rate))*rate/RATE,np.arange(len(x)),filtered)


def read_wav(path):
    """Read PCM16 samples only; no decoder execution, playback, or path writes."""
    path=Path(path)
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size>64*1024*1024:
        raise ValueError('Unsafe or oversized audio input')
    with wave.open(str(path),'rb') as reader:
        rate,channels,frames=reader.getframerate(),reader.getnchannels(),reader.getnframes()
        if (reader.getsampwidth()!=2 or reader.getcomptype()!='NONE' or not 1<=channels<=8
                or not 8000<=rate<=192000 or not 0<frames<=rate*MAX_SECONDS):
            raise ValueError('Unsupported or oversized PCM WAV')
        raw=reader.readframes(frames)
    if len(raw)!=frames*channels*2:raise ValueError('Truncated PCM WAV')
    return np.frombuffer(raw,dtype='<i2').reshape(-1,channels).astype('float32')/32768,rate


def _feature_version(value):
    if type(value) is not int or value not in (1, 2):
        raise ValueError('Unsupported acoustic feature version')
    return value


def spectral_features(samples, feature_version=1):
    """Bounded log-mel shape: legacy band floor (1) or frame-local contrast (2).

    The default remains exactly legacy. Templates and analyzed windows must use
    the same explicit version; clipping and per-frame normalization are shared.
    """
    _feature_version(feature_version)
    y=np.asarray(samples,dtype='float64')
    if y.ndim!=1 or not WINDOW<=len(y)<=RATE*MAX_SECONDS or not np.isfinite(y).all():
        raise ValueError('Invalid bounded mono audio window')
    if y.min()<-1.001 or y.max()>1.001:raise ValueError('Audio must be normalized PCM')
    # Make the log floor relative to this window, not to recording volume.
    # Otherwise quiet/zero intervals change shape when the same voice is softer.
    peak=max(abs(float(y.min())),abs(float(y.max())))
    if peak>1e-9:y=y/peak
    frames=np.lib.stride_tricks.sliding_window_view(y,WINDOW)[::HOP]
    power=np.abs(np.fft.rfft(frames*np.hanning(WINDOW),n=512))**2
    edges=700*(10**(np.linspace(2595*np.log10(1+150/700),2595*np.log10(1+3500/700),34)/2595)-1)
    frequency=np.fft.rfftfreq(512,1/RATE)
    filters=np.array([np.maximum(0,np.minimum((frequency-a)/(b-a),(c-frequency)/(c-b)))
                      for a,b,c in zip(edges,edges[1:],edges[2:])])
    energy=power@filters.T
    log=np.log(np.maximum(energy,1e-12))
    # Digital-silent context is not a recording's background floor. Including
    # it in the quantile clips every active band to 9, erasing phrase shape.
    # Select frames by relative total power (60 dB below the strongest frame),
    # preserving gain invariance and excluding silence before estimating floor.
    total=energy.sum(axis=1)
    nonsilent=total>max(float(total.max())*1e-6,1e-12)
    if not nonsilent.any():
        return np.zeros((32,len(frames)),dtype='float32')
    if feature_version == 1:
        clean=np.clip(log-np.quantile(log[nonsilent],.2,axis=0),0,9)
    else:
        # Frame-local spectral contrast: surrounding audio cannot move a band's floor.
        clean=np.clip(log-np.mean(log,axis=1,keepdims=True),0,9)
    clean/=np.maximum(np.linalg.norm(clean,axis=1,keepdims=True),1e-6)
    return np.ascontiguousarray(clean.T,dtype='float32')


@dataclass(frozen=True)
class PhraseTemplate:
    label:str
    features:np.ndarray
    threshold:float
    temporal_threshold:float|None=None
    feature_version:int=1
    alternate_views:tuple=()

    def __post_init__(self):
        _feature_version(self.feature_version)
        if not isinstance(self.label,str) or not re.fullmatch('[a-z][a-z0-9_]{0,47}',self.label):
            raise ValueError('Invalid acoustic label')
        if type(self.threshold) not in (float,int) or not math.isfinite(self.threshold) or not .5<=self.threshold<=1:
            raise ValueError('Invalid correlation threshold')
        if self.temporal_threshold is not None and (type(self.temporal_threshold) not in (float,int)
                or not math.isfinite(self.temporal_threshold) or not 0<self.temporal_threshold<=1):
            raise ValueError('Invalid temporal correlation threshold')
        features=np.array(self.features,dtype='float32',copy=True,order='C')
        if (features.ndim!=2 or features.shape[0]!=32 or not 80<=features.shape[1]<=1000
                or not np.isfinite(features).all()):
            raise ValueError('Invalid phrase feature dimensions')
        # Constant/single-tone templates can produce misleading perfect NCC.
        if float(np.mean(np.std(features,axis=1)))<.005:
            raise ValueError('Template lacks sustained temporal phrase structure')
        features=np.frombuffer(features.tobytes(),dtype='float32').reshape(features.shape)
        object.__setattr__(self,'features',features)
        if type(self.alternate_views) is not tuple or len(self.alternate_views)>1:
            raise ValueError('Invalid alternate views')
        views=[]
        for matrix,offset in self.alternate_views:
            if (self.label!='paid_ready' or type(offset) not in (int,float)
                    or not math.isfinite(offset) or offset==0 or abs(offset)>MAX_SECONDS):
                raise ValueError('Invalid alternate offset')
            validated=PhraseTemplate(self.label,matrix,self.threshold,self.temporal_threshold,self.feature_version)
            if validated.features.shape!=features.shape or features.shape[1]*HOP/RATE+abs(offset)>9.5:
                raise ValueError('Invalid alternate width')
            views.append((validated.features,float(offset)))
        object.__setattr__(self,'alternate_views',tuple(views))

    @classmethod
    def from_recording(cls,label,samples,rate,start,end,threshold,temporal_threshold=None,*,feature_version=1,alternate_spans=()):
        y=pcm8k(samples,rate)
        if (type(start) not in (int,float) or type(end) not in (int,float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0<=start<end<=len(y)/RATE or not .8<=end-start<=10):
            raise ValueError('Invalid manually verified phrase bounds')
        if type(alternate_spans) not in (tuple,list) or len(alternate_spans)>1:
            raise ValueError('Invalid alternate spans')
        for span in alternate_spans:
            if (type(span) not in (tuple,list) or len(span)!=2
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in span)
                    or not 0<=span[0]<span[1]<=len(y)/RATE
                    or not .8<=span[1]-span[0]<=9.5
                    or not math.isclose(span[1]-span[0],end-start,abs_tol=1e-9)
                    or span[0]==start):
                raise ValueError('Invalid alternate bounds')
        features=spectral_features(y,feature_version)
        primary=features[:,round(start*100):round(end*100)]
        # End-of-recording windows lack the final FFT context. Preserve the
        # legacy primary bytes and use that exact width for every same-span view.
        views=tuple((features[:,round(a*100):round(a*100)+primary.shape[1]],a-start)
                    for a,b in alternate_spans)
        return cls(label,primary,threshold,temporal_threshold,feature_version,views)


@dataclass(frozen=True)
class AcousticResult:
    label:str
    candidate:bool
    score:float|None
    onset_seconds:float|None
    anchor_scores:tuple[float,...]
    reason:str
    temporal_score:float|None=None
    reference_offset_seconds:float=0.


def _temporal_correlation(features,template):
    """NCC after removing EACH band's local mean: color alone cannot pass.

    Prefix sums bound work/memory to the existing <=20s feature window.
    No resampling, dynamic time warping, or changed phrase ordering occurs.
    """
    f=features.astype('float64');t=template.astype('float64');width=t.shape[1]
    t-=t.mean(axis=1,keepdims=True)
    sums=np.pad(np.cumsum(f,axis=1),((0,0),(1,0)))
    squares=np.pad(np.cumsum(f*f,axis=1),((0,0),(1,0)))
    sums=sums[:,width:]-sums[:,:-width]
    squares=squares[:,width:]-squares[:,:-width]
    power=squares.sum(axis=0)
    variance=np.maximum((squares-sums*sums/width).sum(axis=0),0)
    numerator=cv2.matchTemplate(features,np.ascontiguousarray(t,dtype='float32'),cv2.TM_CCORR).ravel()
    denominator=np.sqrt(variance*np.sum(t*t));scores=np.zeros_like(variance)
    valid=(variance>np.maximum(power*1e-10,1e-12))&(denominator>1e-12)
    np.divide(numerator,denominator,out=scores,where=valid)
    return np.clip(scores,-1,1)


class AnnouncementMatcher:
    """Analyze a bounded window; cannot open/close a refueling transaction."""
    def __init__(self,templates):
        self.templates=tuple(templates)
        self._diagnostic = None
        if not 1<=len(self.templates)<=8 or not all(isinstance(t,PhraseTemplate) for t in self.templates):
            raise ValueError('Provide one to eight validated templates')
        if len({t.label for t in self.templates})!=len(self.templates):raise ValueError('Duplicate acoustic labels')
        versions={t.feature_version for t in self.templates}
        if len(versions)!=1:raise ValueError('Mixed acoustic feature versions')
        self.feature_version=versions.pop()

    def _views(self):
        for template in self.templates:
            yield template,0.
            for features,offset in template.alternate_views:
                yield PhraseTemplate(template.label,features,template.threshold,
                    template.temporal_threshold,template.feature_version),offset

    @property
    def diagnostic_snapshot(self):
        if self._diagnostic is None:
            return {'analyzed': False}
        return dict(self._diagnostic, labels={k: dict(v) for k, v in self._diagnostic['labels'].items()})

    def analyze_candidates(self,samples,rate,*,continuous=True) -> tuple[AcousticResult,...]:
        """Accepted full phrases, ordered by onset then label (not probabilities).

        Source spans have duration template.features.shape[1]*HOP/RATE.
        Flat local maxima use their earliest frame; ties prefer earlier onsets.
        Saturation raises rather than returning an incomplete window for a caller
        to advance past. Work is bounded to 64 peaks/template and 64 results.
        """
        self._diagnostic = None
        if type(continuous) is not bool:raise ValueError('Continuity must be explicit bool')
        if not continuous:return ()
        y=pcm8k(samples,rate)
        if len(y)<WINDOW:return ()
        features=spectral_features(y,self.feature_version)
        result=[]
        labels = {}
        peak_counts={}
        for template,reference_offset in self._views():
            width=template.features.shape[1]
            if features.shape[1]<width:continue
            scores=cv2.matchTemplate(features,template.features,cv2.TM_CCOEFF_NORMED).ravel()
            if not np.isfinite(scores).all():raise ValueError('Ambiguous nonfinite correlation')
            temporal=(_temporal_correlation(features,template.features)
                      if template.temporal_threshold is not None else None)
            if temporal is not None and not np.isfinite(temporal).all():raise ValueError('Nonfinite temporal correlation')
            best = int(np.argmax(scores))
            diagnostic = dict(raw_score=float(scores[best]), threshold=float(template.threshold),
                temporal_score=float(temporal[best]) if temporal is not None else None,
                temporal_threshold=template.temporal_threshold, temporal_rejected=0, anchor_rejected=0)
            diagnostic['reference_offset_seconds']=reference_offset
            diagnostic_key=template.label if reference_offset==0 else template.label+'@'+str(reference_offset)
            labels[diagnostic_key] = diagnostic
            # Collapse constant runs before testing neighbors, including endpoints.
            starts=np.r_[0,np.flatnonzero(scores[1:]!=scores[:-1])+1]
            ends=np.r_[starts[1:]-1,len(scores)-1]
            peaks=[]
            for start,end in zip(starts,ends):
                value=float(scores[start])
                if (value>=template.threshold
                        and (start==0 or value>scores[start-1])
                        and (end==len(scores)-1 or value>scores[end+1])):
                    if temporal is not None and temporal[start]<template.temporal_threshold:
                        diagnostic['temporal_rejected'] += 1
                        continue
                    peaks.append(int(start))
                    peak_counts[template.label]=peak_counts.get(template.label,0)+1
                    if peak_counts[template.label]>64:raise ValueError('Acoustic peak saturation')
            accepted=[]
            for i in sorted(peaks,key=lambda i:(-float(scores[i]),i)):
                candidate=features[:,i:i+width]
                anchors=[]
                for part in np.array_split(np.arange(width),3):
                    a=template.features[:,part].ravel().astype('float64')
                    b=candidate[:,part].ravel().astype('float64')
                    a-=a.mean();b-=b.mean()
                    denominator=np.linalg.norm(a)*np.linalg.norm(b)
                    anchors.append(float(a@b/denominator) if denominator>1e-12 else 0.)
                if sum(a>=.55 for a in anchors)<2:
                    diagnostic['anchor_rejected'] += 1
                    continue
                # At most 64x64 checks, never an unbounded all-pairs scan.
                if any(2*abs(i-j)<=width for j in accepted):continue
                accepted.append(i)
                result.append(AcousticResult(template.label,True,float(np.clip(scores[i],-1,1)),
                                            i*HOP/RATE,tuple(anchors),'acoustic_candidate',
                                            float(temporal[i]) if temporal is not None else None,reference_offset))
                if len(result)>64:raise ValueError('Acoustic result saturation')
            diagnostic['accepted_count']=len(accepted)
        self._diagnostic = dict(analyzed=True, labels=labels, candidate_count=len(result),
            rejected_count=sum(d['temporal_rejected']+d['anchor_rejected'] for d in labels.values()))
        return tuple(sorted(result,key=lambda r:(r.onset_seconds,r.label)))

    def analyze(self,samples,rate,*,continuous=True):
        if type(continuous) is not bool:raise ValueError('Continuity must be explicit bool')
        if not continuous:
            return tuple(AcousticResult(t.label,False,None,None,(),'discontinuous_input') for t in self.templates)
        y=pcm8k(samples,rate)
        if len(y)<WINDOW:
            return tuple(AcousticResult(t.label,False,None,None,(),'insufficient_audio') for t in self.templates)
        features=spectral_features(y,self.feature_version)
        result=[]
        for template,reference_offset in self._views():
            width=template.features.shape[1]
            if features.shape[1]<width:
                result.append(AcousticResult(template.label,False,None,None,(),'insufficient_audio'))
                continue
            # OpenCV can swap short inputs/templates, so the size guard is required.
            scores=cv2.matchTemplate(features,template.features,cv2.TM_CCOEFF_NORMED).ravel()
            i=int(np.argmax(scores))
            candidate=features[:,i:i+width]
            anchors=[]
            for part in np.array_split(np.arange(width),3):
                a=template.features[:,part].ravel().astype('float64');b=candidate[:,part].ravel().astype('float64')
                a-=a.mean();b-=b.mean()
                denominator=np.linalg.norm(a)*np.linalg.norm(b)
                anchors.append(float(a@b/denominator) if denominator>1e-12 else 0.)
            value=float(np.clip(scores[i],-1,1))
            temporal_score=(float(_temporal_correlation(features,template.features)[i])
                            if template.temporal_threshold is not None else None)
            accepted=(value>=template.threshold and sum(a>=.55 for a in anchors)>=2
                      and (temporal_score is None or temporal_score>=template.temporal_threshold))
            result.append(AcousticResult(template.label,bool(accepted),value,i*HOP/RATE,tuple(anchors),
                                        'acoustic_candidate' if accepted else 'no_match',temporal_score,reference_offset))
        return tuple(max((r for r in result if r.label==t.label),
                         key=lambda r:(r.candidate,r.score if r.score is not None else -2))
                     for t in self.templates)
