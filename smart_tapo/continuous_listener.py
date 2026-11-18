"""Explicit mixed-loopback listener. Page checks are NOT audio isolation proof.

Profile acceptance is not an accuracy guarantee. No SDK runs in the audio child.
Call poll regularly, including after close, until is_running becomes false.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import math
import multiprocessing
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from .announcement_profile import load_announcement_profile
from .announcement_stream import AnnouncementStream, LiveLease, StreamError
from .capture.page_mode import PageMode,PROFILE
from .capture.coordination import navigation_snapshot,navigation_lock
from .capture.ui_lock import ui_lock
from .audio import ProbeError, _progress, loopback_devices
from .refueling_store import RefuelingStore

STREAM_CODES=frozenset(('invalid_stream_configuration','invalid_stream_profile','profile_too_long_for_stream',
    'stream_already_failed','audio_sequence_gap','invalid_audio_chunk','live_provenance_lost',
    'audio_clock_discontinuity','acoustic_analysis_failed','acoustic_result_limit',
    'invalid_acoustic_result','invalid_acoustic_span','live_navigation_changed'))
MODE_REASONS=frozenset(('unsupported_package','invalid_hierarchy','sensitive_page',
    'incomplete_playback_layout','playback_or_conflicting_controls','unrecognized_foreground',
    'camera_not_confirmed','incomplete_live_layout','foreground_changed_or_unknown','live_refresh_required',
    'live_inactivity_confirmation'))


class CheckFailure(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def _timing(**values):
    return {key:round(float(value),3) for key,value in values.items()
            if type(value) in (int,float) and math.isfinite(value) and abs(value)<=86400}


class AudioModeReader:
    """One default adapter per explicit listener start, never a global proof cache."""
    def __init__(self):self.adb=None

    def clear(self):
        if self.adb is not None:self.adb._clear_authorization()

    def __call__(self,config,stop=None):
        from .capture import Adb
        bounded=replace(config,adb_timeout_seconds=min(5,config.adb_timeout_seconds))
        if self.adb is None or self.adb.config!=bounded:self.adb=Adb(bounded)
        try:return _read_mode(config,stop=stop,adb=self.adb)
        finally:
            if stop is not None and stop.is_set():self.adb._clear_authorization()


def _read_mode(config,stop=None,adb=None):
    from .capture import Adb,CaptureError
    adb=adb or Adb(replace(config,adb_timeout_seconds=min(5,config.adb_timeout_seconds)))
    try:
        if stop is not None and stop.is_set():
            adb._clear_authorization()
            raise CheckFailure('stopped')
        mode=adb.read_page_mode()
        if (mode.reason in ('live_refresh_required','live_inactivity_confirmation','navigation_in_progress') or
                (mode.mode=='live' and navigation_snapshot(config).changed_monotonic>time.monotonic())):
            if stop is not None and stop.is_set():raise CheckFailure('stopped')
            mode=adb.recover_live(stop=stop)
        return replace(mode,navigation_generation=navigation_snapshot(config).generation)
    except CaptureError:
        # Reconnect only the already selected loopback transport. This neither
        # launches/navigates Tapo nor treats a connect reply as Live proof.
        match=re.fullmatch(r'(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})',config.adb_serial)
        if match and 1<=int(match.group(1))<=65535:
            try:adb.run('connect',config.adb_serial,selected=False)
            except CaptureError:pass
        raise


def _prepare(config, path, reader, initial, expected_fingerprint=None, stop=None):
    fingerprint=None
    if initial:
        try:profile=load_announcement_profile(path,config.camera_id)
        except Exception:raise CheckFailure('profile_invalid') from None
        if max(t.features.shape[1]/100 for t in profile.templates)>9.5:raise CheckFailure('unsupported_profile_span')
        fingerprint=profile.fingerprint
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise CheckFailure('profile_changed')
    try:
        if stop is not None and stop.is_set():raise CheckFailure('stopped')
        with ui_lock(config.adb_serial,min(5,config.adb_timeout_seconds)):
            before=navigation_snapshot(config)
            mode=reader(config,stop=stop) if reader is _read_mode or isinstance(reader,AudioModeReader) else reader(config)
            after=navigation_snapshot(config)
            if after.recovering or (before.generation!=after.generation and
                    (not isinstance(mode,PageMode) or mode.navigation_generation!=after.generation)):
                raise CheckFailure('live_navigation_changed')
            if isinstance(mode,PageMode):mode=replace(mode,navigation_generation=after.generation)
    except CheckFailure as error:
        error.fingerprint=fingerprint
        raise
    except Exception:
        error=CheckFailure('live_check_unavailable')
        error.fingerprint=fingerprint
        raise error from None
    return fingerprint,mode


def _valid_lease(lease, run_id, camera_id, now):
    return (isinstance(lease, LiveLease) and lease.run_id == run_id
            and lease.camera_id == camera_id and lease.issued_at <= now < lease.expires_at)


def _navigation_valid(lease,config):
    state=navigation_snapshot(config)
    return not state.recovering and (lease.navigation_generation or 'initial')==state.generation


def _commit(store, batch):
    for item in batch.observations:
        store.accept(item.source_key, item.announcement)
    if batch.finalized_until is not None:
        store.tick(batch.finalized_until)


def listener_job(final, progress, stop, leases, config, device, profile_path,
                 fingerprint, run_id, api=None):
    """Child owns all hardware and persistence; optional api is an offline test seam."""
    audio = stream = store = None
    origin=None;frames=0;count=0;last_read=last_work=peak_work=0.
    result = {'kind': 'error', 'code': 'listener_failed'}
    code = 'lease_invalid'
    try:
        lease = leases.get(timeout=1)
        now = time.monotonic()
        if not _valid_lease(lease, run_id, config.camera_id, now) or stop.is_set():
            raise ProbeError()
        code='live_navigation_changed'
        if not _navigation_valid(lease,config):raise ProbeError()
        navigation_generation=lease.navigation_generation or 'initial'
        code = 'profile_invalid'
        profile = load_announcement_profile(profile_path, config.camera_id)
        if profile.fingerprint != fingerprint:
            raise ProbeError()
        if not _valid_lease(lease, run_id, config.camera_id, time.monotonic()) or stop.is_set():
            code = 'lease_invalid'
            raise ProbeError()
        code = 'audio_unavailable'
        if api is None:
            import pyaudiowpatch as api
        audio = api.PyAudio()
        available = loopback_devices(audio, api)
        if device.get('endpoint_id'):
            from .startup import device_identity, matching_device
            try:
                device = matching_device(device_identity(device), available)
            except ValueError:
                code = 'device_changed'
                raise ProbeError() from None
        elif device not in available:
            code = 'device_changed'
            raise ProbeError()
        if stop.is_set() or not _valid_lease(lease, run_id, config.camera_id, time.monotonic()):
            code = 'lease_invalid'
            raise ProbeError()
        code = 'storage_error'
        store = RefuelingStore(Path(config.data_dir) / 'refueling', config.camera_id,
                               source_root=config.data_dir)
        store.mark_audio_gap()
        if stop.is_set() or not _valid_lease(lease, run_id, config.camera_id, time.monotonic()):
            code = 'lease_invalid'
            raise ProbeError()
        rate, channels = device['rate'], device['channels']
        code = 'audio_unavailable'
        stream = audio.open(format=api.paInt16, channels=channels, rate=rate,
                            input=True, input_device_index=device['index'], frames_per_buffer=1024)
        origin = time.monotonic()
        core = AnnouncementStream(profile, rate=rate, channels=channels, run_id=run_id,
                                  utc_origin=datetime.now(timezone.utc).timestamp(), monotonic_origin=origin)
        frames = 0
        segment_id=run_id
        last_poll=origin
        empty_seen=False
        epoch_active=True
        observations_total=ambiguous_total=0
        cleanup_at = origin + 30
        progress_at=origin
        last_tick=None
        pending = None
        import numpy as np
        while not stop.is_set():
            code = 'lease_invalid'
            now = time.monotonic()
            # Queue is control, never a source of refreshed timestamps.
            try:
                fresh = leases.get_nowait()
                if isinstance(fresh,LiveLease) and (fresh.navigation_generation or 'initial')!=navigation_generation:
                    code='live_navigation_changed'
                    raise ProbeError()
                if (not _valid_lease(fresh, run_id, config.camera_id, now)
                        or fresh.issued_at <= lease.issued_at
                        or pending is not None):
                    raise ProbeError()
                pending = fresh
            except queue.Empty:
                pass
            if now-last_poll>2:
                raise StreamError('audio_clock_discontinuity')
            last_poll=now
            begin = origin + frames / rate
            if pending is not None and (not epoch_active or pending.issued_at <= begin):
                lease, pending = pending, None
            if not _valid_lease(lease, run_id, config.camera_id, now):
                raise ProbeError()
            code='live_navigation_changed'
            if not _navigation_valid(lease,config):raise ProbeError()
            # Retention still runs while a loopback endpoint supplies no data.
            if now>=cleanup_at:
                code='storage_error'
                store.cleanup()
                cleanup_at=now+30
            code = 'audio_availability_failed'
            available=stream.get_read_available()
            if type(available) is not int or available<0:
                raise ProbeError()
            if available==0:
                empty_seen=True
                if epoch_active and now-begin>2:
                    code='storage_error'
                    store.mark_audio_gap()
                    core.discard()
                    epoch_active=False
                    if pending is not None:
                        lease,pending=pending,None
                if now>=progress_at:
                    _progress(progress,dict(kind='listener_progress',heartbeat=now,frames=frames,
                        observations=0,observations_total=observations_total,ambiguous_candidates=0,
                        ambiguous_total=ambiguous_total,waiting_audio=True,
                        awaiting_boundary=store._meta('awaiting_end_after_gap') is True))
                    progress_at=now+.25
                stop.wait(.02)
                continue
            count = min(1024, rate // 2, available)
            # Rebase only a continuously observed empty-buffer interval, never backlog.
            if empty_seen and now-begin>2:
                if epoch_active:
                    code='storage_error'
                    store.mark_audio_gap()
                    core.discard()
                    epoch_active=False
            if not epoch_active:
                if pending is not None:lease,pending=pending,None
                origin=now-count/rate
                if lease.issued_at>origin:
                    raise StreamError('live_provenance_lost')
                frames=0
                segment_id=uuid.uuid4().hex
                core=AnnouncementStream(profile,rate=rate,channels=channels,run_id=segment_id,
                    utc_origin=datetime.now(timezone.utc).timestamp()-count/rate,monotonic_origin=origin)
                epoch_active=True
                last_tick=None
            empty_seen=False
            code = 'audio_unavailable'
            read_at=time.monotonic()
            raw = stream.read(count, exception_on_overflow=True)
            last_read=time.monotonic()-read_at
            code='audio_short_read'
            if len(raw) != count * channels * 2:
                raise ProbeError()
            samples = np.frombuffer(raw, dtype='<i2').astype('float32').reshape(-1, channels) / 32768
            code = 'stream_fault'
            work_at=time.monotonic()
            try:batch = core.push(samples, start_frame=frames, now=work_at,
                                 lease=replace(lease,run_id=segment_id))
            finally:
                last_work=time.monotonic()-work_at
                peak_work=max(peak_work,last_work)
            frames += count
            code = 'lease_invalid'
            if stop.is_set():
                break
            if not _valid_lease(lease, run_id, config.camera_id, time.monotonic()):
                raise ProbeError()
            code = 'storage_error'
            if (not batch.observations and batch.finalized_until is not None and last_tick is not None
                    and batch.finalized_until-last_tick<1):
                batch=replace(batch,finalized_until=None)
            # No navigation may begin between the epoch check and persistence.
            # Analysis stays outside the lock; a changed epoch discards it.
            code='live_navigation_changed'
            with navigation_lock(config.adb_serial,min(5,config.adb_timeout_seconds)):
                if not _navigation_valid(lease,config):raise ProbeError()
                if not _valid_lease(lease,run_id,config.camera_id,time.monotonic()):
                    code='lease_invalid'
                    raise ProbeError()
                code='storage_error'
                _commit(store, batch)
            observations_total+=len(batch.observations)
            ambiguous_total+=batch.ambiguous_candidates
            if batch.finalized_until is not None:last_tick=batch.finalized_until
            now = time.monotonic()
            if now >= cleanup_at:
                store.cleanup()
                cleanup_at = now + 30
            if now>=progress_at:
                _progress(progress, dict(kind='listener_progress', heartbeat=now,
                                        frames=frames, observations=len(batch.observations),observations_total=observations_total,
                                        ambiguous_candidates=batch.ambiguous_candidates,ambiguous_total=ambiguous_total,
                                        waiting_audio=False,awaiting_boundary=store._meta('awaiting_end_after_gap') is True))
                progress_at=now+.25
        result = {'kind': 'finished', 'mode': 'listener'}
    except StreamError as error:
        reason=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else None
        result={'kind':'error','code':reason if reason in STREAM_CODES else 'stream_fault'}
        if origin is not None:
            now=time.monotonic()
            result['diagnostics']=_timing(lag_seconds=now-origin-(frames+count)/rate,
                read_seconds=last_read,work_seconds=last_work,max_work_seconds=peak_work,
                audio_seconds=(frames+count)/rate,wall_seconds=now-origin,rate=rate,channels=channels)
    except Exception:
        result = {'kind': 'error', 'code': code}
    finally:
        if store is not None:
            try:store.mark_audio_gap()
            except Exception:result={'kind':'error','code':'storage_error'}
        for resource, method in ((store, 'close'), (stream, 'close'), (audio, 'terminate')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    if result.get('code')!='storage_error':
                        result = {'kind': 'error', 'code': 'close_failed'}
        progress.cancel_join_thread()
        try:
            final.send(result)
        finally:
            final.close()


class ContinuousListener:
    def __init__(self, *, context=None, clock=time.monotonic, mode_reader=None):
        self._context, self._clock = context, clock
        self._injected_reader = mode_reader
        self._reader = mode_reader or AudioModeReader()
        self._executor = self._future = self._process = None
        self._closing = self._cancelled = False
        self._events = []
        self._fingerprint=None
        self._check_stop=threading.Event()

    @property
    def profile_fingerprint(self):
        return self._fingerprint

    @property
    def is_running(self):
        return self._future is not None or self._process is not None

    def start(self, config, device, profile_path, *, consent=False, expected_fingerprint=None):
        if self._closing or self.is_running:
            raise ValueError('busy_or_closing')
        if consent is not True:
            raise ValueError('consent_required')
        if (not isinstance(device, dict) or set(device) not in ({'index', 'name', 'rate', 'channels'},
                {'index', 'name', 'rate', 'channels', 'endpoint_id'})
                or ('endpoint_id' in device and (not isinstance(device['endpoint_id'], str)
                    or not re.fullmatch(r'\{[0-9a-fA-F-]{36}\}', device['endpoint_id'])))
                or type(device['index']) is not int or device['index'] < 0
                or not isinstance(device['name'], str) or not 0 < len(device['name']) <= 160
                or type(device['rate']) is not int or not 8000 <= device['rate'] <= 192000
                or type(device['channels']) is not int or not 1 <= device['channels'] <= 8):
            raise ValueError('invalid_device')
        if device['rate']%100 or device['rate']*device['channels']*4*20>16*1024*1024:
            raise ValueError('unsupported_stream_format')
        if expected_fingerprint is not None and (not isinstance(expected_fingerprint,str)
                or re.fullmatch('[0-9a-f]{64}',expected_fingerprint) is None):
            raise ValueError('invalid_profile_fingerprint')
        self._expected_fingerprint=expected_fingerprint
        self._fingerprint=None
        config.validate()
        self._config = replace(config, data_dir=Path(config.data_dir).absolute())
        self._device = dict(device)
        self._path = str(Path(profile_path).absolute())
        self._reader = self._injected_reader or AudioModeReader()
        self._run = uuid.uuid4().hex
        self._cancelled = False
        self._check_stop=threading.Event()
        self._forced = self._result = self._stop_at = self._kill_at = None
        self._forced_diagnostics=None
        self._lease = None
        self._submit(True)

    def _submit(self, initial):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='listener-check')
        self._requested = self._clock()
        self._initial = initial
        self._future = self._executor.submit(_prepare, self._config, self._path, self._reader, initial, self._expected_fingerprint,self._check_stop)

    def _spawn(self, fingerprint, lease):
        ctx = self._context or multiprocessing.get_context('spawn')
        owned=[];send=None
        try:
            self._recv, send = ctx.Pipe(duplex=False);owned.extend((self._recv,send))
            self._progress = ctx.Queue(maxsize=8);owned.append(self._progress)
            self._leases = ctx.Queue(maxsize=2);owned.append(self._leases)
            self._stop = ctx.Event()
            self._leases.put_nowait(lease)
            process = ctx.Process(target=listener_job, args=(send, self._progress, self._stop,
                                  self._leases, self._config, self._device, self._path,
                                  fingerprint, self._run), daemon=True)
            owned.append(process)
            process.start()
        except Exception:
            for resource in reversed(owned):
                try:resource.close()
                except Exception:pass
            raise
        finally:
            if send is not None:send.close()
        self._process = process
        self._heartbeat = self._clock()

    def stop(self):
        self._cancelled = True
        self._check_stop.set()
        if isinstance(self._reader,AudioModeReader):self._reader.clear()
        if self._process is not None and self._stop_at is None:
            self._stop.set()
            self._stop_at = self._clock()

    def close(self):
        self._closing = True
        self.stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _fail(self, code,diagnostics=None):
        self._forced = code
        self._forced_diagnostics=diagnostics
        self.stop()
        if self._process is None:
            event={'kind':'error','code':code}
            if diagnostics:event['diagnostics']=diagnostics
            self._events.append(event)

    def _receive(self):
        try:
            if self._result is None and self._recv.poll():
                self._result = self._recv.recv()
        except (EOFError, OSError):
            pass

    def poll(self) -> list[dict]:
        now = self._clock()
        if self._future is not None and self._future.done():
            future, self._future = self._future, None
            if not self._cancelled:
                stage='initial_check_failed'
                try:
                    fingerprint, mode = future.result()
                    if self._initial and type(fingerprint) is str and re.fullmatch('[0-9a-f]{64}',fingerprint):
                        self._fingerprint=fingerprint
                    if not isinstance(mode,PageMode):raise CheckFailure('live_proof_invalid')
                    if mode.mode!='live':
                        reason='live_playback_detected' if mode.mode=='playback' else (
                            'live_'+mode.reason if mode.reason in MODE_REASONS else 'live_mode_unconfirmed')
                        raise CheckFailure(reason)
                    if mode.profile_id!=PROFILE:raise CheckFailure('live_proof_invalid')
                    stage='live_proof_invalid'
                    lease = LiveLease.from_page(mode, self._run, self._config.camera_id,
                                                self._requested, self._requested + 10)
                    if not _valid_lease(lease, self._run, self._config.camera_id, now):
                        raise CheckFailure('initial_check_expired' if self._initial else 'live_check_expired')
                    if self._initial:
                        self._fingerprint=(fingerprint if isinstance(fingerprint,str)
                            and re.fullmatch('[0-9a-f]{64}',fingerprint) else None)
                        stage='listener_start_failed'
                        self._spawn(fingerprint, lease)
                    else:
                        stage='live_control_backlog'
                        self._leases.put_nowait(lease)
                    self._lease = lease
                except CheckFailure as error:
                    fingerprint=getattr(error,'fingerprint',None)
                    if self._initial and type(fingerprint) is str and re.fullmatch('[0-9a-f]{64}',fingerprint):
                        self._fingerprint=fingerprint
                    self._fail(error.code,_timing(check_seconds=now-self._requested))
                except Exception:
                    self._fail(stage,_timing(check_seconds=now-self._requested))
            else:
                # Consume exceptions too; stale SDK work remains owned until done.
                try:
                    future.result()
                except Exception:
                    pass
        events, self._events = self._events, []
        if self._process is None:
            return events
        for _ in range(8):
            try:
                event = self._progress.get_nowait()
            except queue.Empty:
                break
            stamp = event.get('heartbeat')
            if type(stamp) in (int, float) and math.isfinite(stamp) and self._heartbeat <= stamp <= now:
                self._heartbeat = stamp
            events.append(event)
        self._receive()
        if not self._cancelled:
            if self._lease is None or now >= self._lease.expires_at:
                self._fail('lease_expired')
            elif now - self._heartbeat >= 5:
                self._fail('audio_timeout')
            elif self._future is None and now - self._requested >= 5:
                self._submit(False)
        if self._stop_at is not None and now - self._stop_at >= 2 and self._kill_at is None:
            if self._process.is_alive():
                self._forced = self._forced or 'stopped'
                self._process.terminate()
                self._kill_at = now
        if self._kill_at is not None and now - self._kill_at >= 1 and self._process.is_alive():
            self._process.kill()
        if self._process.is_alive():
            return events
        self._process.join(timeout=0)
        self._receive()  # terminal may arrive between poll and reap
        result = self._result or {'kind': 'error', 'code': 'worker_failed'}
        if self._forced or self._process.exitcode != 0:
            result = {'kind': 'error', 'code': self._forced or 'worker_failed'}
            if self._forced_diagnostics:result['diagnostics']=self._forced_diagnostics
        for resource in (self._recv, self._progress, self._leases, self._process):
            resource.close()
        self._process = None
        self._cancelled = True
        events.append(result)
        return events
