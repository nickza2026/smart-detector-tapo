"""File-only TEST diagnostics. No leases, devices, stores or financial events.

Incrementally mirrors stream onset/lookahead/overlap rules in a 20-second ring.
Accepted-only candidate API cannot expose all rejected peaks; maxima are labelled
separately and never substituted for stream candidates. No EOF success flush.
"""
from dataclasses import dataclass, asdict
import math
import wave
from pathlib import Path
from threading import Event

import numpy as np

from .announcement import AnnouncementMatcher, HOP, RATE, read_wav
from .announcement_profile import _pinned_file, load_announcement_profile
import hashlib
import sys


def _hash(path, stop):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while True:
            check_cancel(stop)
            block = source.read(65536)
            if not block:
                break
            digest.update(block)
    check_cancel(stop)
    return digest.hexdigest()


class DiagnosticCancelled(ValueError):
    pass


def check_cancel(stop):
    if stop.is_set():
        raise DiagnosticCancelled('cancelled')


@dataclass(frozen=True)
class DiagnosticRow:
    window_end: float
    label: str
    onset: float | None
    end: float | None
    score: float | None
    anchors: tuple
    threshold: float
    reason: str
    source: str


@dataclass(frozen=True)
class DiagnosticReport:
    mode: str
    decoder: str
    duration: float
    rate: int
    channels: int
    profile_fingerprint: str
    file_fingerprint: str
    rms: float
    peak: float
    finalized_until: float
    lookahead: float
    rows: tuple[DiagnosticRow, ...]
    coverage: str = 'maxima plus accepted candidates only; rejected local peaks unobservable'

    accepted_counts: dict | None = None
    rows_truncated: int = 0
    processed_frames: int = 0
    max_buffer_frames: int = 0
    test_rounds: dict | None = None

    def metadata(self):
        return asdict(self)


def analyze_pcm(samples, rate, profile, *, stop=None, file_fingerprint='synthetic',
                on_candidate=None, progress=None):
    """Deterministic half-second chunks, with real EOF (no padding). Max 160 rows."""
    stop = stop if stop is not None else Event()
    check_cancel(stop)
    x = np.asarray(samples)
    if (type(rate) is not int or not 8000 <= rate <= 192000 or rate % 100
            or x.ndim != 2 or not 1 <= x.shape[1] <= 8
            or not 0 < len(x) <= rate * 20 or x.nbytes > 16 * 1024 * 1024
            or not np.isfinite(x).all() or x.min() < -1.001 or x.max() > 1.001):
        raise ValueError('unsupported_bounded_pcm')
    return _analyze_chunks((x[i:i+rate//2] for i in range(0,len(x),rate//2)),
                           rate, x.shape[1], len(x), profile, stop,
                           file_fingerprint, on_candidate, progress)


def _analyze_chunks(chunks, rate, channels, total_frames, profile, stop,
                    file_fingerprint, on_candidate, progress, diagnostics=None):
    durations = {t.label: round(t.features.shape[1]*HOP/RATE*rate) for t in profile.templates}
    if set(durations) != {'paid_ready', 'end'} or max(durations.values()) > 9.5*rate:
        raise ValueError('unsupported_stream_profile')
    matcher = AnnouncementMatcher(profile.templates)
    thresholds = {t.label: t.threshold for t in profile.templates}
    horizon = math.ceil((max(durations.values())/rate*2+.4)*rate)
    hop = rate//100
    finalized = 0
    last_analysis = 0
    previous = {}
    rows = []
    accepted_counts = dict.fromkeys(durations, 0)
    truncated = 0
    total = 0
    energy = 0.0
    peak = 0.0
    max_buffer = 0
    last_progress = 0
    last_diagnostic = 0
    x = np.empty((0,channels),dtype='float32')
    def retain(row):
        nonlocal truncated
        if len(rows) >= 150:
            rows.pop(0)
            truncated += 1
        rows.append(row)
    for part in chunks:
        check_cancel(stop)
        total += len(part)
        energy += float(np.sum(part.astype('float64')**2))
        peak = max(peak,float(np.max(np.abs(part))))
        x = np.concatenate((x,part))[-20*rate:]
        max_buffer = max(max_buffer,len(x))
        window_start = total-len(x)
        skip = (-window_start)%hop
        window_start += skip
        window = x[skip:]
        if diagnostics is not None and total-last_diagnostic >= rate:
            diagnostics(tuple(_window_maxima(matcher,window,rate,window_start,total,
                                            finalized,durations,thresholds,stop)))
            last_diagnostic=total
        if progress is not None and total-last_progress >= rate//2:
            progress({'processed_frames':total,'total_frames':total_frames,'rate':rate})
            last_progress = total
        if total-last_analysis < rate//4 or total <= horizon:
            continue
        last_analysis = total
        boundary = ((total-horizon)//hop)*hop
        if boundary <= finalized:
            continue
        results = matcher.analyze_candidates(window, rate, continuous=True)
        check_cancel(stop)
        candidates = [(window_start+round(r.onset_seconds*rate), window_start+round(r.onset_seconds*rate)+durations[r.label], r)
                      for r in results]
        for start, end, result in sorted(candidates, key=lambda item:(item[0],item[2].label)):
            if start <= finalized:
                continue
            if start < window_start+round(.2*rate):
                reason = 'onset_guard'
            elif start > boundary:
                reason = 'need_future_lookahead'
            elif any(other.label != result.label and min(end,b)>max(start,a) for a,b,other in candidates):
                reason = 'cross_kind_overlap'
            elif result.label in previous and min(end,previous[result.label][1])-max(start,previous[result.label][0]) >= .5*min(end-start,previous[result.label][1]-previous[result.label][0]):
                reason = 'duplicate_suppression'
            else:
                reason = 'accepted_test_candidate'
                previous[result.label] = (start,end)
            row = DiagnosticRow(total/rate,result.label,start/rate,end/rate,result.score,
                                result.anchor_scores,thresholds[result.label],reason,'accepted_candidates_api')
            # Repeated lookahead rows add no information; keep only latest state.
            rows = [old for old in rows if not (old.label == row.label and old.onset == row.onset
                    and old.reason == 'need_future_lookahead')]
            retain(row)
            if reason == 'accepted_test_candidate':
                accepted_counts[result.label] += 1
                if on_candidate is not None:
                    on_candidate(row)
                check_cancel(stop)
        finalized = boundary
    # Final maxima explain threshold/anchor failures but are NOT an exhaustive
    # rejection trace or a replacement for analyze_candidates.
    check_cancel(stop)
    window_start = total-len(x)
    skip = (-window_start)%hop
    window_start += skip
    rows.extend(_window_maxima(matcher,x[skip:],rate,window_start,total,finalized,durations,thresholds,stop))
    check_cancel(stop)
    return DiagnosticReport('TEST_OFFLINE_NOT_LIVE','stdlib PCM16 WAV',total/rate,rate,channels,
                            profile.fingerprint,file_fingerprint,math.sqrt(energy/(total*channels)),
                            peak,finalized/rate,horizon/rate,tuple(rows),
                            'final bounded window maxima plus accepted candidates only; rejected local peaks unobservable',
                            accepted_counts,truncated,total,max_buffer)


def _window_maxima(matcher,window,rate,window_start,total,finalized,durations,thresholds,stop):
    for result in matcher.analyze(window,rate):
        check_cancel(stop)
        if result.score is None:
            reason = 'insufficient_audio'
        elif result.score < thresholds[result.label]:
            reason = 'below_full_pattern_threshold'
        elif sum(a >= .55 for a in result.anchor_scores) < 2:
            reason = 'anchor_requirement_failed'
        elif result.onset_seconds < .2:
            reason = 'onset_guard'
        elif window_start/rate+result.onset_seconds > finalized/rate:
            reason = 'need_future_lookahead'
        else:
            reason = 'maximum_passed_matcher_not_stream_decision'
        onset = None if result.onset_seconds is None else window_start/rate+result.onset_seconds
        yield DiagnosticRow(total/rate,result.label,onset,
                    None if onset is None else onset+durations[result.label]/rate,
                    result.score,result.anchor_scores,thresholds[result.label],reason,
                    'whole_file_maximum' if total <= 20*rate else 'bounded_window_maximum')


def diagnose_file(path, profile_path, camera_id, stop=None, *, progress=None, on_candidate=None):
    """Explicit local WAV only. Files pinned while reading; changes fail closed."""
    stop = stop if stop is not None else Event()
    check_cancel(stop)
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\','//')) or path.suffix.lower() != '.wav':
        raise ValueError('local_pcm16_wav_only_no_mp4_decoder')
    profile = load_announcement_profile(profile_path,camera_id)
    check_cancel(stop)
    from .audio import checked_ancestry
    checked_ancestry(path.parent)
    with _pinned_file(path,sys.maxsize):
        digest = _hash(path,stop)
        with wave.open(str(path),'rb') as source:
            rate,channels,total = source.getframerate(),source.getnchannels(),source.getnframes()
            if (source.getsampwidth()!=2 or source.getcomptype()!='NONE'
                    or not 8000<=rate<=192000 or rate%100 or not 1<=channels<=8 or total<=0
                    or total*channels*2 > path.stat().st_size):
                raise ValueError('invalid_pcm16_wav_header')
            def chunks():
                processed = 0
                while processed < total:
                    check_cancel(stop)
                    count = min(rate//2,total-processed)
                    raw = source.readframes(count)
                    check_cancel(stop)
                    if len(raw)!=count*channels*2:
                        raise ValueError('truncated_pcm16_wav')
                    processed += count
                    yield np.frombuffer(raw,dtype='<i2').reshape(-1,channels).astype('float32')/32768
            report = _analyze_chunks(chunks(),rate,channels,total,profile,stop,digest,on_candidate,progress)
        if _hash(path,stop) != digest:
            raise ValueError('input_changed')
    check_cancel(stop)
    if load_announcement_profile(profile_path,camera_id).fingerprint != profile.fingerprint:
        raise ValueError('profile_changed')
    with _pinned_file(path,sys.maxsize):
        if _hash(path,stop) != digest:
            raise ValueError('input_changed')
    check_cancel(stop)
    return report
