"""Bounded, latest-only RTSP transport. PTS proves receiver continuity, NOT camera age.

A relay can serve a recording with perfectly plausible timestamps. No claim of
physical-camera liveness is made by this transport. Unknown timestamps fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import io
import json
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import pickle
import re
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image

from smart_tapo.clock_epoch import current_clock_epoch
from smart_tapo.frames import image_storage_bytes, publish_frame

LOG = logging.getLogger(__name__)
# Equal to passage.RETAIN_COUNT; passage imports cv2, which must stay out of this module.
MAX_PROOF_PASSAGES = 16


def validate_rtsp_url(value):
    """Initial relay delivery intentionally has no credential storage surface."""
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or '\\' in value or '%' in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in ('rtsp', 'rtsps') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '?' in value or '#' in value
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9.:-]+', parsed.hostname):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Enter a credential-free RTSP relay URL without query or fragment') from None
    return value


def rtsp_source_fingerprint(config):
    validate_rtsp_url(config.rtsp_url)
    identity = [config.rtsp_url, config.camera_id, list(config.expected_size)]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class RtspStatus:
    reason: str
    session_id: str | None = None
    decoded: int = 0
    dropped: int = 0


def _status(reason, session=None, decoded=0, dropped=0, callback=None):
    status = RtspStatus(reason, session, decoded, dropped)
    LOG.info('RTSP image status: %s', reason,
             extra={'rtsp_context': status.__dict__})
    if callback is not None:
        callback(status)


class StatusHeartbeat:
    """State changes immediately, unchanged status at most once per five seconds."""
    def __init__(self, callback=None):
        self.callback = callback
        self.reason = None
        self.emitted_at = float('-inf')

    def report(self, reason, now, session=None, decoded=0, dropped=0):
        if reason != self.reason or now-self.emitted_at >= 5:
            _status(reason, session, decoded, dropped, self.callback)
            self.reason, self.emitted_at = reason, now


class PtsContinuity:
    """Prime against monotonic receive time; reject duplicate/backward/burst PTS.

    Delayed but real-time recordings remain indistinguishable from a live relay.
    Priming is restarted after every discontinuity, including missing timestamps.
    Network jitter delays a frame and then delivers the backlog as a burst, so
    pacing is judged against the earliest arrival seen in the session: arriving
    more than ``late`` seconds behind it, or running ahead of source time by
    more than ``ahead`` seconds (buffered replay), still resets.
    """
    def __init__(self, max_gap=1.8, late=.75, ahead=.25):
        self.max_gap = max_gap
        self.late = late
        self.ahead = ahead
        self.reset()

    def reset(self):
        self.first = self.previous = None
        self.count = 0
        self.earliest = 0.

    def observe(self, pts, received):
        if (type(pts) not in (int, float) or not math.isfinite(pts) or pts < 0
                or type(received) not in (int, float) or not math.isfinite(received)):
            self.reset()
            return False
        if self.previous is not None:
            dp, dt = pts - self.previous[0], received - self.previous[1]
            # Multiple decoded frames can share a coarse receive-clock tick.
            # Source PTS must still advance; cumulative pacing/priming below
            # rejects buffered replay or a frozen receive clock as before.
            if not (0 < dp <= self.max_gap and 0 <= dt <= self.max_gap):
                self.reset()
        if self.first is None:
            self.first = (pts, received)
        self.previous = (pts, received)
        self.count += 1
        span, elapsed = pts-self.first[0], received-self.first[1]
        lag = elapsed - span
        self.earliest = min(self.earliest, lag)
        if self.earliest < -self.ahead or lag - self.earliest > self.late:
            self.reset()
            return False
        return self.count >= 3 and min(span, elapsed) >= 1.0


def compact_png(png):
    """Deflated copy for disk; the decoder's stored PNG is sized for RAM transport only."""
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        content = io.BytesIO()
        image.save(content, format='PNG', compress_level=1)
    return content.getvalue()


def _latest_put(output, row):
    """Queue has a single slot; never wait for vision or preserve old frames."""
    try:
        output.put_nowait(row)
        return False
    except queue.Full:
        try:
            output.get_nowait()
        except queue.Empty:
            pass
        try:
            output.put_nowait(row)
        except queue.Full:
            pass
        return True


class EncodeCadence:
    """Only PNG work is throttled; decoding and PTS observation remain continuous."""
    def __init__(self, capture_interval):
        self.interval = min(.25, capture_interval / 2)
        self.next_encode = float('-inf')

    def due(self, received):
        if received < self.next_encode:
            return False
        self.next_encode = received + self.interval
        return True


def _decoder_main(config, output, halt):
    # Native FFmpeg errors can contain the input URI. Redirect only in this owned
    # decoder process, before importing/opening cv2; never change parent logging.
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 2)
    os.close(null)
    import cv2
    from smart_tapo.passage import PassageTracker
    output.cancel_join_thread()
    decoded = dropped = 0
    while not halt.is_set():
        decoder = None
        session = uuid.uuid4().hex
        try:
            decoder = cv2.VideoCapture(config.rtsp_url, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(config.rtsp_open_timeout_seconds*1000),
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(config.rtsp_read_timeout_seconds*1000)])
            if not decoder.isOpened():
                raise RuntimeError('open_failed')
            tracker = PtsContinuity(config.max_frame_gap_seconds)
            passages = None
            if config.passage_tracking_enabled:
                try:
                    passages = PassageTracker(config.roi, config.expected_size)
                except ValueError:
                    pass
            recent = ()
            was_fresh = False
            cadence = EncodeCadence(config.capture_interval_seconds)
            while not halt.is_set():
                ok, pixels = decoder.read()
                received, wall = time.monotonic(), time.time()
                if not ok or pixels is None:
                    break
                decoded += 1
                # OpenCV 5 FFmpeg PTS is in FPS timebase. POS_MSEC is used only
                # on builds lacking PTS, and must pass the identical clock guard.
                fps = decoder.get(cv2.CAP_PROP_FPS)
                if hasattr(cv2, 'CAP_PROP_PTS') and math.isfinite(fps) and fps > 0:
                    pts = decoder.get(cv2.CAP_PROP_PTS) / fps
                else:
                    pts = decoder.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                fresh = tracker.observe(pts, received)
                if was_fresh and not fresh:
                    # A timestamp fault is a new proof session even if the same
                    # socket recovers quickly enough to fit the worker gap TTL.
                    session = uuid.uuid4().hex
                was_fresh = fresh
                height, width = pixels.shape[:2]
                if max(width, height) > 8192 or width*height > 16_777_216:
                    raise RuntimeError('geometry_limit')
                if passages is not None and (width, height) == config.expected_size:
                    try:
                        recent = tuple(p.record() for p in passages.update(pixels, wall))
                    except Exception:
                        # Passage verdicts are advisory; video proof must keep flowing.
                        passages, recent = None, ()
                if not cadence.due(received):
                    continue
                # A reference may adopt geometry; production checks it before publish.
                # Unfiltered stored PNG is lossless and ~4x cheaper to encode and decode.
                ok, encoded = cv2.imencode('.png', pixels, [cv2.IMWRITE_PNG_COMPRESSION, 0,
                                                            cv2.IMWRITE_PNG_FILTER, cv2.IMWRITE_PNG_FILTER_NONE])
                if not ok:
                    continue
                row = (session, decoded, received, wall, pts, fresh,
                       width, height, encoded.tobytes(), dropped, recent)
                dropped += int(_latest_put(output, row))
        except Exception:
            # Never export an exception string, URL, provider output or native body.
            _latest_put(output, ('error',))
        finally:
            if decoder is not None:
                decoder.release()
        if halt.wait(config.rtsp_reconnect_seconds):
            break


class RtspReader:
    """One owned decoder, bounded reap and watchdog; never restarts other pipelines.

    Native timeout support is not trusted as the only escape hatch. A decoded
    row or sanitized error is progress; an alive but silent process is not.
    Every watchdog restart discards its queue/event and primes a new session.
    """
    def __init__(self, config, *, context=None, clock=None):
        validate_rtsp_url(config.rtsp_url)
        self.config = config
        self.context = context or mp.get_context('spawn')
        self.clock = clock or time.monotonic
        self.watchdog_seconds = config.rtsp_open_timeout_seconds + config.rtsp_read_timeout_seconds + 2
        self.output = self.halt = self.process = None
        self.closed = True
        self.last_progress = 0.
        self.restart_at = None

    def _spawn(self):
        self.output = self.context.Queue(maxsize=1)
        self.halt = self.context.Event()
        self.process = self.context.Process(target=_decoder_main,
                                           args=(self.config, self.output, self.halt), daemon=True)
        try:
            self.process.start()
        except Exception:
            self.output.close()
            self.output.cancel_join_thread()
            self.process.close()
            self.output = self.halt = self.process = None
            raise RuntimeError('RTSP decoder unavailable') from None
        self.last_progress = self.clock()
        self.restart_at = None

    def _reap(self):
        if self.process is None:
            return
        self.halt.set()
        self.process.join(timeout=.25)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        if self.process.is_alive():
            # Never spawn alongside an unreaped decoder, even on OS failure.
            raise RuntimeError('RTSP decoder could not stop')
        self.output.close()
        self.output.cancel_join_thread()
        self.process.close()
        self.output = self.halt = self.process = None

    def __enter__(self):
        if not self.closed:
            raise RuntimeError('RTSP reader already active')
        self.closed = False
        try:
            self._spawn()
        except Exception:
            self.closed = True
            raise
        return self

    def get(self, timeout=.1):
        if self.closed:
            return None
        now = self.clock()
        if self.process is None:
            if self.restart_at is not None and now >= self.restart_at:
                self._spawn()
            else:
                # Bounded idle wait avoids spinning while honoring reconnect backoff.
                time.sleep(max(0., min(timeout, .1)))
            return None
        if (not self.process.is_alive()
                or now-self.last_progress > self.watchdog_seconds):
            self._reap()
            self.restart_at = self.clock()+self.config.rtsp_reconnect_seconds
            return ('error',)
        try:
            row = self.output.get(timeout=timeout)
        except queue.Empty:
            return None
        except (EOFError, OSError, pickle.UnpicklingError) as error:
            from smart_tapo.audit_log import safe_runtime_failure
            detail = safe_runtime_failure(error, 'decoder_ipc', recoverable=True)
            if not detail['recoverable']:
                LOG.warning('Safe runtime failure', extra={'runtime_failure': detail})
                raise
            # Only the owned decoder IPC boundary is recoverable here. Reap
            # before scheduling any replacement, then honor normal backoff.
            self._reap()
            self.restart_at = self.clock() + self.config.rtsp_reconnect_seconds
            LOG.warning('Safe runtime failure', extra={'runtime_failure': detail})
            return ('error',)
        self.last_progress = self.clock()
        return row

    def __exit__(self, *_):
        self.closed = True
        self.restart_at = None
        self._reap()


def _proof_key(config, create=False):
    root = Path(config.data_dir)
    path = root / '.rtsp-proof-key'
    if root.is_symlink() or path.is_symlink():
        raise ValueError('Unsafe RTSP proof key path')
    if create:
        root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
    with path.open('rb') as stream:
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError('RTSP proof key unavailable')
    return key


def sign_rtsp_proof(config, png, session, sequence, received, wall, pts, passages=()):
    """Local process provenance, not authentication of the relay/camera."""
    proof = dict(version=1, source=rtsp_source_fingerprint(config), session=session,
                 sequence=sequence, received=received, captured_at=wall, pts=pts,
                 width=config.expected_size[0], height=config.expected_size[1],
                 clock_epoch=current_clock_epoch(), image_sha256=hashlib.sha256(png).hexdigest(),
                 passages=[dict(p) for p in passages])
    proof['signature'] = hmac.new(_proof_key(config, create=True),
                                 _proof_bytes(proof), hashlib.sha256).hexdigest()
    return proof


def _proof_bytes(proof):
    return json.dumps({k: v for k, v in proof.items() if k != 'signature'},
                      sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def valid_rtsp_proof(frame, config, image, *, now=None, monotonic=None):
    try:
        proof = frame.source_proof
        fields = {'version', 'source', 'session', 'sequence', 'received', 'captured_at',
                  'pts', 'width', 'height', 'clock_epoch', 'image_sha256', 'signature'}
        if not isinstance(proof, dict) or set(proof) - {'passages'} != fields:
            return False
        if not isinstance(proof.get('passages', []), list) or len(proof.get('passages', [])) > MAX_PROOF_PASSAGES:
            return False
        if config.rtsp_source_binding != rtsp_source_fingerprint(config):
            return False
        if (type(proof['version']) is not int or proof['version'] != 1
                or proof['source'] != config.rtsp_source_binding
                or proof['session'] != frame.session_id or proof['sequence'] != frame.sequence
                or type(proof['sequence']) is not int
                or proof['received'] != frame.captured_monotonic
                or proof['captured_at'] != frame.captured_at
                or (proof['width'], proof['height']) != config.expected_size
                or proof['clock_epoch'] != current_clock_epoch()
                or frame.clock_epoch != current_clock_epoch()):
            return False
        for name in ('received', 'captured_at', 'pts'):
            if type(proof[name]) not in (int, float) or not math.isfinite(proof[name]) or proof[name] < 0:
                return False
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        age = min(config.rtsp_max_receive_age_seconds, config.max_live_age_seconds)
        if not (0 <= now-frame.captured_at <= age and 0 <= monotonic-proof['received'] <= age):
            return False
        signature = hmac.new(_proof_key(config), _proof_bytes(proof), hashlib.sha256).hexdigest()
        if not isinstance(proof['signature'], str) or not hmac.compare_digest(signature, proof['signature']):
            return False
        if type(image) is bytes:
            digest = hashlib.sha256(image).hexdigest()
        else:
            with Path(image).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return digest == proof['image_sha256']
    except (ValueError, OSError, TypeError, AttributeError, OverflowError):
        return False


def read_rtsp_reference(config, stop, *, reader_factory=RtspReader):
    """Return an independently decoded PIL image, without old expected geometry.

    A reference is calibration material, not live alert proof. Deadline includes
    reconnects; no user URL is connected until this explicitly invoked API runs.
    """
    if stop.is_set():
        raise ValueError('RTSP reference unavailable or stopped')
    deadline = time.monotonic()+config.rtsp_open_timeout_seconds+config.rtsp_read_timeout_seconds+2
    with reader_factory(config) as reader:
        while not stop.is_set() and time.monotonic() < deadline:
            row = reader.get()
            if row is None or len(row) not in (10, 11):
                continue
            with Image.open(io.BytesIO(row[8])) as image:
                image.load()
                return image.convert('RGB')
    raise ValueError('RTSP reference unavailable or stopped')


def _passage_loss(reason, since):
    LOG.warning('Passage verdicts may be lost', extra={'passage_loss': dict(reason=reason, since=float(since))})


class PassageDelivery:
    """Warn when a full-rate verdict leaves the decoder without riding a published frame."""
    def __init__(self):
        self.pending, self.delivered = {}, set()

    def observe(self, passages):
        current = {p['id']: p['started'] for p in passages if isinstance(p, dict) and 'id' in p and 'started' in p}
        lost = [started for identity, started in self.pending.items() if identity not in current]
        self.delivered &= current.keys()
        self.pending = {identity: started for identity, started in current.items() if identity not in self.delivered}
        if lost:
            _passage_loss('expired', min(lost))

    def published(self, passages):
        for passage in passages:
            if isinstance(passage, dict) and 'id' in passage:
                self.delivered.add(passage['id'])
                self.pending.pop(passage['id'], None)

    def reset(self):
        if self.pending:
            _passage_loss('reconnect', min(self.pending.values()))
        self.pending, self.delivered = {}, set()


def capture_rtsp(config, stop, once=False, *, status_callback=None, reader_factory=RtspReader, channel=None):
    config.validate()
    if config.rtsp_source_binding != rtsp_source_fingerprint(config):
        _status('calibration_required', callback=status_callback)
        return 1
    if stop.is_set():
        return 0
    previous_session, sequence = None, 0
    next_sample = 0.
    heartbeat = StatusHeartbeat(status_callback)
    last_row_at = time.monotonic()
    last_session, last_decoded, last_dropped = None, 0, 0
    delivery = PassageDelivery()
    heartbeat.report('starting', last_row_at)
    # --once is bounded even when a relay never produces an eligible frame.
    deadline = time.monotonic()+config.rtsp_open_timeout_seconds+config.rtsp_read_timeout_seconds+5 if once else math.inf
    with reader_factory(config) as reader:
        while not stop.is_set() and time.monotonic() < deadline:
            row = reader.get()
            now = time.monotonic()
            if row is None:
                if now-last_row_at >= 5:
                    heartbeat.report('waiting', now, last_session, last_decoded, last_dropped)
                continue
            last_row_at = now
            if len(row) not in (10, 11):
                if channel is not None:
                    channel.invalidate()
                    channel.fence()
                delivery.reset()
                heartbeat.report('reconnecting', now, last_session, last_decoded, last_dropped)
                continue
            session, decoded, received, wall, pts, fresh, width, height, png, dropped = row[:10]
            passages = row[10] if len(row) == 11 else ()
            delivery.observe(passages)
            last_session, last_decoded, last_dropped = session, decoded, dropped
            reason = ('geometry_changed' if (width, height) != config.expected_size else
                      'stale' if not 0 <= now-received <= config.rtsp_max_receive_age_seconds else
                      'timestamp_unconfirmed' if not fresh else 'healthy')
            if channel is None or reason!='healthy':
                heartbeat.report(reason, now, session, decoded, dropped)
            elif now<next_sample and heartbeat.reason!='overflow':
                heartbeat.report(reason, now, session, decoded, dropped)
            if channel is not None and reason != 'healthy':
                # Even a brief invalid geometry/freshness interval must not be
                # hidden by sampling cadence or joined to the preceding proof.
                channel.fence()
            if now < next_sample:
                continue
            next_sample = now+config.capture_interval_seconds
            if session != previous_session:
                previous_session, sequence = session, 0
            if reason != 'healthy':
                continue
            if channel is None:
                png = compact_png(png)
            if channel is None and image_storage_bytes(config)+len(png) > config.max_image_storage_mb*1_000_000:
                _status('storage_limit', session, decoded, dropped, status_callback)
                return 1
            sequence += 1
            generation = None
            if channel is not None:
                generation, fresh_after = channel.snapshot()
                if received <= fresh_after:
                    continue
            try:
                proof = sign_rtsp_proof(config, png, session, sequence, received, wall, pts, passages)
                published=True
                if channel is not None:
                    from smart_tapo.ram_frames import MemoryFrame
                    envelope = MemoryFrame.create(generation, config, png, session, sequence, wall, received, proof)
                    published=channel.publish(envelope)
                    if not published:
                        channel.fence()
                        heartbeat.report('overflow', now, session, decoded, dropped)
                    else:
                        heartbeat.report('healthy', now, session, decoded, dropped)
                else:
                    publish_frame(config, png, session, sequence, True, captured_at=wall,
                                  captured_monotonic=received, source_proof=proof)
                if published:
                    delivery.published(passages)
            except Exception as error:
                from smart_tapo.audit_log import safe_runtime_failure
                LOG.warning('Safe runtime failure', extra={'runtime_failure': safe_runtime_failure(error, 'frame_publish')})
                raise
            if once:
                return 0 if published else 1
    if stop.is_set():
        heartbeat.report('stopped', time.monotonic(), last_session, last_decoded, last_dropped)
        return 0
    return 1
