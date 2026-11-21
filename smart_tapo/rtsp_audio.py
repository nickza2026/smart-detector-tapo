"""Explicit RTSP audio receiver. No ADB, desktop audio, or physical-Live claim.

Native decode and analysis share one owned, supervised child. No PCM queue can
silently drop samples. A stalled decoder is terminated by poll(); callers must
continue polling after stop/close. Transport errors abandon the entire epoch.
"""
from dataclasses import replace
import hashlib
import logging
import math
import multiprocessing
from pathlib import Path
import queue
import time
import uuid

from .announcement_profile import load_announcement_profile
from .announcement_stream import AnnouncementStream, RtspAudioLease
from .refueling_store import RefuelingStore
from .storage_diagnostics import storage_failure_event

LOG = logging.getLogger(__name__)

RATE = 16000
# Native RTSP A-law clocks can step by one millisecond while payload sample
# counts stay continuous. Cap jitter at 1% of the preceding chunk, with the
# original two-sample floor for tiny chunks. This is never silence
# insertion, sample dropping, missing-PTS synthesis or a larger-gap bridge.
PTS_JITTER_SECONDS = .001
# The field relay periodically rewinds its audio clock by one 20 ms packet while
# sample counts stay continuous (measured ~1/min). A rewind is overlap, never
# missing audio, so it is absorbed by rebasing the source clock; a forward step
# beyond the jitter above still ends the epoch.
PTS_REWIND_SECONDS = .06


class AudioDiscontinuity(ValueError):
    pass


class AudioStorageFailure(RuntimeError):
    def __init__(self, error=None, stage='storage_commit'):
        self.event = storage_failure_event(error, stage)
        super().__init__(self.event['code'])


def _storage_call(stage, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        raise AudioStorageFailure(error, stage) from None


def commit_batch(store, batch, *, on_committed=None):
    try:
        for item in batch.observations:
            store.accept(item.source_key, item.announcement)
            if on_committed is not None:
                on_committed(item)
        if batch.finalized_until is not None:
            store.tick(batch.finalized_until)
    except Exception as error:
        raise AudioStorageFailure(error) from None


class PcmTimeline:
    """Validate decoded media time AND receiver cadence, including silent PCM."""
    def __init__(self):
        self.next_pts = self.origin = self.last_received = self.last_pts = None
        self.sample_origin = None
        self.samples = 0
        self.last_count = 0

    def accept(self, pts, count, received):
        if (type(count) is not int or not 0 < count <= RATE // 2
                or pts is None or not math.isfinite(float(pts))
                or not math.isfinite(received)):
            raise AudioDiscontinuity('audio_timestamp_invalid')
        pts = float(pts)
        if self.next_pts is None:
            self.origin = received - count / RATE - pts
            self.sample_origin = received - count / RATE
        else:
            step = pts - self.next_pts
            jitter = max(2/RATE,min(PTS_JITTER_SECONDS,self.last_count/RATE*.01)) + 1e-9
            if (pts <= self.last_pts or not -PTS_REWIND_SECONDS - 1e-9 <= step <= jitter
                    or received - self.last_received > 2):
                raise AudioDiscontinuity('audio_timestamp_gap')
            if step < -jitter:
                self.origin -= step
        source_end = self.origin + pts + count / RATE
        begin = self.sample_origin + self.samples / RATE
        sample_end = self.sample_origin + (self.samples + count) / RATE
        # Validate both clocks: a succession of tiny timestamp steps must not
        # accumulate into unbounded drift/backlog or move the stream's leases.
        if (abs(received - source_end) > 2 or abs(received - sample_end) > 2
                or abs(source_end - sample_end) > 2):
            raise AudioDiscontinuity('audio_backlog')
        self.last_pts = pts
        self.next_pts = pts + count / RATE
        self.last_received = received
        self.last_count = count
        self.samples += count
        return begin


def decoded_pcm(container, av):
    """Audio-only normalized float32 mono. Missing timestamps are not invented."""
    tracks = list(container.streams.audio)
    if not tracks:
        raise AudioDiscontinuity('no_audio_track')
    # Lossy codecs can reconstruct float PCM slightly beyond nominal full
    # scale. Convert at the codec boundary through canonical PCM16 instead of
    # handing invalid normalized floats to the strictly gated stream owner.
    # This preserves its range/finite checks; no matcher threshold is relaxed.
    resampler = av.AudioResampler(format='s16', layout='mono', rate=RATE)
    for frame in container.decode(tracks[0]):
        for output in resampler.resample(frame):
            if output.pts is None or output.time_base is None:
                raise AudioDiscontinuity('audio_timestamp_invalid')
            samples = output.to_ndarray().reshape(-1, 1).astype('float32') / 32768
            pts = float(output.pts * output.time_base)
            # Split a genuine decoded frame, preserving its media timestamps.
            for start in range(0, len(samples), RATE // 2):
                yield samples[start:start + RATE // 2], pts + start / RATE


def _progress(channel, event):
    try:
        channel.put_nowait(event)
    except queue.Full:
        # Telemetry only; PCM and observations never use this queue. Retain
        # recent producer timestamps/cumulative totals when the UI falls behind.
        # A multiprocessing feeder may report Full before an item is readable;
        # races are best-effort, bounded and never block the audio producer.
        try:
            channel.get_nowait()
        except queue.Empty:
            return
        try:
            channel.put_nowait(event)
        except queue.Full:
            pass


def rtsp_audio_job(final, progress, stop, config, path, fingerprint, source):
    store = core = container = None
    result = {'kind': 'finished', 'mode': 'listener'}
    observations = ready = ended = ambiguous = 0
    def committed(item):
        nonlocal observations, ready, ended
        observations += 1
        ready += item.announcement.kind == 'paid_ready'
        ended += item.announcement.kind == 'end'
    code = 'decoder_unavailable'
    try:
        import av
        code = 'profile_invalid'
        profile = load_announcement_profile(path, config.camera_id)
        if profile.fingerprint != fingerprint:
            raise AudioDiscontinuity('profile_changed')
        code = 'storage_error'
        # SQLite must report BUSY before the supervisor's eight-second watchdog.
        store = _storage_call('storage_open', RefuelingStore,
            Path(config.data_dir) / 'refueling', config.camera_id,
            source_root=config.data_dir, busy_timeout_seconds=1.)
        _storage_call('storage_gap', store.mark_audio_gap)
        while not stop.is_set():
            core = None
            code = 'rtsp_disconnected'
            try:
                container = av.open(config.rtsp_url, mode='r',
                    options={'rtsp_transport': 'tcp', 'allowed_media_types': 'audio'},
                    timeout=(3.0, 2.0))
                timeline = PcmTimeline()
                frames = 0
                session = uuid.uuid4().hex
                cleanup_at = time.monotonic() + 30
                for samples, pts in decoded_pcm(container, av):
                    if stop.is_set():
                        break
                    now = time.monotonic()
                    begin = timeline.accept(pts, len(samples), now)
                    if core is None:
                        core = AnnouncementStream(profile, rate=RATE, channels=1,
                            run_id=session, utc_origin=time.time() - (now - begin),
                            monotonic_origin=begin, source_fingerprint=source)
                    lease = RtspAudioLease(session, config.camera_id,
                        max(0., begin - .01), now + 1, source, session)
                    batch = core.push(samples, start_frame=frames, now=now, lease=lease)
                    frames += len(samples)
                    if stop.is_set():
                        break
                    # Never advance finalized time before accepting ordered observations.
                    commit_batch(store, batch, on_committed=committed)
                    ambiguous += batch.ambiguous_candidates
                    if now >= cleanup_at:
                        _storage_call('storage_cleanup', store.cleanup)
                        cleanup_at = now + 30
                    import numpy as np
                    health = getattr(core, 'diagnostic_snapshot', {})
                    health = health if type(health) is dict else {}
                    _progress(progress, dict(kind='listener_progress', heartbeat=now,
                        frames=frames, pcm_session=session, pcm_started_at=begin,
                        observations=len(batch.observations),
                        audio_frames=frames, audio_rms=float(np.sqrt(np.mean(samples.astype('float64')**2))),
                        stage='persisted', **health,
                        observations_total=observations, ambiguous_total=ambiguous,
                        ready_total=ready,end_total=ended,
                        ambiguous_candidates=batch.ambiguous_candidates, waiting_audio=False,
                        awaiting_boundary=store._meta('awaiting_end_after_gap') is True))
                if not stop.is_set():
                    raise AudioDiscontinuity('rtsp_disconnected')
            except Exception as error:
                if isinstance(error, AudioStorageFailure):
                    code = 'storage_error'
                    raise
                if core is not None:
                    core.discard()
                _storage_call('storage_gap', store.mark_audio_gap)
                reason = str(error) if isinstance(error, AudioDiscontinuity) else 'rtsp_disconnected'
                if reason == 'no_audio_track':
                    raise
                _progress(progress, dict(kind='listener_status', code=reason,
                                         heartbeat=time.monotonic(), waiting_audio=True,
                                         observations_total=observations,ready_total=ready,end_total=ended,
                                         ambiguous_total=ambiguous))
                stop.wait(.5)
            finally:
                if container is not None:
                    container.close()
                    container = None
    except Exception as error:
        safe = {'no_audio_track', 'profile_changed'}
        result = {'kind': 'error', 'code': str(error) if isinstance(error, AudioDiscontinuity)
                  and str(error) in safe else code}
        if isinstance(error, AudioStorageFailure):
            result.update(error.event)
            LOG.warning('Audio storage failure: %s (%s)', error.event['code'], error.event['stage'])
    finally:
        if core is not None:
            core.discard()
        if store is not None:
            try:
                _storage_call('storage_gap', store.mark_audio_gap)
            except AudioStorageFailure as error:
                if result.get('kind') != 'error':
                    result = dict(kind='error', **error.event)
            finally:
                try:
                    _storage_call('storage_close', store.close)
                except AudioStorageFailure as error:
                    if result.get('kind') != 'error':
                        result = dict(kind='error', **error.event)
        result.update(observations_total=observations,ready_total=ready,end_total=ended,
                      ambiguous_total=ambiguous)
        progress.cancel_join_thread()
        try:
            final.send(result)
        finally:
            final.close()


class RtspContinuousListener:
    def __init__(self, *, context=None, clock=time.monotonic):
        self._context, self._clock = context, clock
        self._process = None
        self._closing = False
        self._fingerprint = None
        self.status = 'idle'

    @property
    def profile_fingerprint(self):
        return self._fingerprint

    @property
    def is_running(self):
        return self._process is not None

    def start(self, config, device, profile_path, *, consent=False, expected_fingerprint=None):
        if self._closing or self.is_running:
            raise ValueError('busy_or_closing')
        if consent is not True:
            raise ValueError('consent_required')
        config.validate()
        frozen = replace(config, data_dir=Path(config.data_dir).absolute())
        if not isinstance(frozen.rtsp_url, str) or not frozen.rtsp_url.startswith(('rtsp://', 'rtsps://')):
            raise ValueError('rtsp_url_required')
        profile = load_announcement_profile(profile_path, config.camera_id)
        self._fingerprint = profile.fingerprint
        if expected_fingerprint is not None and expected_fingerprint != self._fingerprint:
            raise ValueError('profile_changed')
        self.source_fingerprint = hashlib.sha256(frozen.rtsp_url.encode('utf-8')).hexdigest()
        ctx = self._context or multiprocessing.get_context('spawn')
        self._recv, send = ctx.Pipe(duplex=False)
        self._progress = ctx.Queue(maxsize=8)
        self._stop = ctx.Event()
        self._result = self._forced = self._stop_at = self._kill_at = None
        process = ctx.Process(target=rtsp_audio_job, args=(send, self._progress,
            self._stop, frozen, str(Path(profile_path).absolute()), self._fingerprint,
            self.source_fingerprint), daemon=True)
        try:
            process.start()
        except Exception:
            self._recv.close()
            self._progress.close()
            raise
        finally:
            send.close()
        self._process = process
        self._heartbeat = self._clock()
        self.status = 'connecting'

    def stop(self):
        if self._process is not None and self._stop_at is None:
            self._stop.set()
            self._stop_at = self._clock()
            self.status = 'stopping'

    def close(self):
        self._closing = True
        self.stop()

    def _receive(self):
        try:
            if self._result is None and self._recv.poll():
                self._result = self._recv.recv()
        except (EOFError, OSError):
            pass

    def poll(self):
        events = []
        if self._process is None:
            return events
        now = self._clock()
        for _ in range(8):
            try:
                event = self._progress.get_nowait()
            except queue.Empty:
                break
            stamp = event.get('heartbeat')
            if type(stamp) in (int, float) and self._heartbeat <= stamp <= now:
                self._heartbeat = stamp
            self.status = event.get('code', 'receiving')
            events.append(event)
        self._receive()
        if self._stop_at is None and now - self._heartbeat > 8:
            self._forced = 'audio_timeout'
            self.stop()
        if self._stop_at is not None and now - self._stop_at >= 2 and self._kill_at is None:
            if self._process.is_alive():
                self._process.terminate()
                self._kill_at = now
        if self._kill_at is not None and now - self._kill_at >= 1 and self._process.is_alive():
            self._process.kill()
        if self._process.is_alive():
            return events
        self._process.join(timeout=0)
        self._receive()
        result = self._result or {'kind': 'error', 'code': 'worker_failed'}
        totals={key:result[key] for key in ('observations_total','ready_total','end_total','ambiguous_total')
                if type(result.get(key)) is int and result[key]>=0}
        if self._forced:
            result = {'kind': 'error', 'code': self._forced}
        elif self._stop_at is not None and result.get('kind') != 'error':
            result = {'kind': 'finished', 'mode': 'listener'}
        result.update(totals)
        self.status = result.get('code', 'stopped')
        for resource in (self._recv, self._progress, self._process):
            resource.close()
        self._process = None
        events.append(result)
        return events
