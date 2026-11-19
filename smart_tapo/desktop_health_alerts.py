"""Desktop-owned, opt-in health delivery with explicit in-memory credentials.

No Tk, environment, configuration-file or device access. poll/close are called
by the owner; one owned worker serializes HTTP. All monitor access is protected
by a short lock, never held across sender calls. Call close on application exit;
shutdown does not wait for an already-open HTTP request (bounded by timeout).

Snapshots map vision/audio to expected: bool, healthy: bool, reason: safe code,
observed_at: monotonic seconds, generation: nonnegative int. Only expected=True
arms. Missing samples and samples older than SNAPSHOT_MAX_AGE become unknown;
repeated/out-of-order sample timestamps cannot freshen health. A new generation
is a manual scope change, not recovery. Settings/recipient changes start a new
grace period, not replay. A healthy static frame or silent PCM is still healthy.
"""
from concurrent.futures import ThreadPoolExecutor
import math
import re
import threading
import time
from uuid import uuid4

from .health_alerts import CHANNELS, HealthMonitor, format_notice, SAMPLE_MAX_AGE
from .notify_api import AlertMessage, NotifyTarget, send_alert

SNAPSHOT_MAX_AGE = SAMPLE_MAX_AGE
_TITLE = 'SmartTapo ระบบภาพ/เสียง'


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _credentials(value):
    """(Notify API key, base URL) as stripped text, or None."""
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    if not all(isinstance(part, str) and 1 <= len(part.strip()) <= 4096 for part in value):
        return None
    return tuple(part.strip() for part in value)


def _alert_id(notice):
    """Stable per notice so the monitor's retries replay instead of resending."""
    scope = getattr(notice, 'scope', '')
    if not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{32}', scope):
        scope = uuid4().hex
    return 'health-' + scope + '-' + str(int(notice.serial))


def send_health(key, base_url, notice, cancel, importance='normal', *, history=None):
    """POST one safe notice; return success/failed/ambiguous, never raw errors.

    success means Notify API provider acceptance, NOT phone delivery or Ack.
    Recovery is always normal (silent); faults use the configured importance.
    One POST only: HealthMonitor owns retry spacing with the same alert ID.
    cancel is an Event-like object; a request already opened cannot be withdrawn.
    """
    credentials = _credentials((key, base_url))
    if credentials is None or cancel.is_set():
        return 'failed'
    try:
        target = NotifyTarget(credentials[1], credentials[0], history=history, channel='health')
        alert_id = _alert_id(notice)
        alert = AlertMessage(alert_id, 'smarttapo:' + alert_id, _TITLE, format_notice(notice),
                             'normal' if notice.kind == 'recovered' else importance)
    except Exception:
        return 'failed'
    return send_alert(target, alert, cancel).state


class HealthAlertSupervisor:
    """One executor/future, at most one pending core notice per channel.

    sender(key, base_url, notice, cancel_event, importance) -> success|failed|ambiguous.
    Default sender is send_health. clock is the SAME monotonic source used for
    observed_at. poll accepts an attribute-based config (or a dict). status is
    a fixed redacted code, never provider text. is_running includes pending,
    queued or in-flight work, not merely armed monitoring.

    Worker claims immediately before sender invocation, not at submission time.
    A queued job therefore cannot send after health/stop/scope invalidation.
    Once claimed, health may cause an ordered recovery after success; manual
    stop/scope changes cancel and ignore that result instead. An already-open
    HTTP request is irrevocable. Sender MUST check cancel before opening HTTP.
    """
    def __init__(self, sender=send_health, clock=time.monotonic, *, history=None):
        if history is not None and sender is send_health:
            from functools import partial
            sender = partial(send_health, history=history)
        self._sender = sender
        self._clock = clock
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='health-alert')
        self._future = None
        self._job_cancel = None
        self._job_notice = None
        self._scope = None
        self._epoch = 0
        self._monitor = None
        self._channels = {}
        self._credentials = None
        self._importance = 'normal'
        self._closed = False
        self._last_now = float('-inf')
        self._status = 'disabled'
        self._pending = False
        self._notices = {}
        self._blocked_channels = set()

    @property
    def status(self):
        """Redacted state code; 'accepted' proves provider acceptance only."""
        with self._lock:
            return self._status

    @property
    def is_running(self):
        with self._lock:
            return self._pending or (self._future is not None and not self._future.done())

    @staticmethod
    def _get(config, name, default=None):
        return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)

    def _read_clock(self):
        try:
            return self._clock()
        except Exception:
            return None

    def _reset(self):
        self._epoch += 1
        if self._job_cancel is not None:
            self._job_cancel.set()
        # Keep the future slot until the worker drains it. Cancelling a Future
        # alone leaves its work item in ThreadPoolExecutor's queue; immediately
        # resubmitting on every scope change could accumulate cancelled items.
        self._monitor = None
        self._credentials = None
        self._channels = {}
        self._pending = False
        self._notices = {}
        self._blocked_channels = set()

    def _due(self, monitor, now):
        due = monitor.take_due(now)
        for notice in due:
            self._notices[notice.channel] = notice
        self._notices = {channel: notice for channel, notice in self._notices.items()
                         if monitor.is_current(notice)}
        self._pending = bool(self._notices)
        return [notice for notice in due if notice.channel not in self._blocked_channels]

    def _reap(self):
        if self._future is not None and self._future.done():
            # Worker handles all outcomes; never stringify an exception here.
            self._future = None
            self._job_cancel = None
            self._job_notice = None

    def poll(self, config, credentials, snapshots):
        """Consume a main-thread snapshot; never read Tk or credential stores.

        Invalid configuration/time fails closed. Both enable flags must be the
        actual bool True. Missing credentials do not arm or send. Keep polling
        normally; after close, polling only reaps the owned future.
        """
        with self._lock:
            self._reap()
            if self._closed:
                return
            now = self._read_clock()
            if not _finite(now):
                self._reset()
                self._scope = None
                self._status = 'invalid_clock'
                return
            if now < self._last_now:
                return
            self._last_now = now
            enabled = (self._get(config, 'notify_enabled', False) is True
                       and self._get(config, 'health_notify_enabled', True) is True)
            keys = _credentials(credentials)
            importance = self._get(config, 'notify_importance', 'normal')
            importance = importance if importance in ('normal', 'critical') else 'normal'
            delay = self._get(config, 'health_failure_seconds', 30)
            repeat = self._get(config, 'health_repeat_seconds', 900)
            try:
                # Validation uses the core contract, including bool rejection.
                candidate = HealthMonitor(delay, repeat)
            except ValueError:
                self._reset()
                self._scope = None
                self._status = 'invalid_settings'
                return
            scope = (enabled, keys, importance, delay, repeat,
                     self._get(config, 'camera_id'), self._get(config, 'data_dir'),
                     self._get(config, 'rtsp_url'))
            if scope != self._scope:
                self._reset()
                self._scope = scope
                if enabled and keys is not None:
                    self._monitor = candidate
                    self._credentials = keys
                    self._importance = importance
            if not enabled or keys is None:
                self._status = 'disabled' if not enabled else 'missing_credentials'
                return
            monitor = self._monitor
            if monitor is None:
                return
            snapshots = snapshots if isinstance(snapshots, dict) else {}
            audio = snapshots.get('audio')
            self._blocked_channels = ({'audio'} if isinstance(audio, dict)
                                      and audio.get('recovery_pending') is True else set())
            for channel in sorted(CHANNELS):
                sample = snapshots.get(channel, {})
                sample = sample if isinstance(sample, dict) else {}
                generation = sample.get('generation')
                expected = (sample.get('expected') is True and type(generation) is int
                            and generation >= 0)
                current = self._channels.get(channel)
                if not expected:
                    monitor.disarm(channel)
                    self._channels.pop(channel, None)
                    continue
                if current is None or current[0] != generation:
                    monitor.disarm(channel)
                    monitor.arm(channel, now)
                    current = (generation, float('-inf'))
                    self._channels[channel] = current
                observed = sample.get('observed_at')
                # A typed parent-side liveness/failure decision may refer to the
                # same last producer sample. It can invalidate, never freshen,
                # health. Retain its safe cause after the producer has stopped.
                bad = (sample.get('healthy') is False and (observed is None or
                       (_finite(observed) and current[1] <= observed <= now)))
                if bad:
                    monitor.observe(channel, False, sample.get('reason', 'unknown'), now)
                    if _finite(observed):self._channels[channel]=(generation,observed)
                elif not _finite(observed) or observed > now or now - observed > SNAPSHOT_MAX_AGE:
                    monitor.observe(channel, False, 'unknown', now)
                elif observed > current[1]:
                    healthy = sample.get('healthy')
                    monitor.observe(channel, healthy if type(healthy) is bool else False,
                                    sample.get('reason', 'unknown'), now)
                    self._channels[channel] = (generation, observed)
                # Equal/older fresh timestamps cannot overwrite a newer sample.
            if (self._job_notice is not None and not monitor.is_current(self._job_notice)
                    and self._job_cancel is not None):
                self._job_cancel.set()
            self._reap()
            due = self._due(monitor, now)
            if self._future is None and due:
                notice = due[0]
                cancel = threading.Event()
                epoch = self._epoch
                keys = self._credentials
                importance = self._importance
                self._job_cancel = cancel
                self._job_notice = notice
                self._status = 'queued'
                try:
                    self._future = self._executor.submit(
                        self._deliver, monitor, notice, keys, cancel, epoch, importance)
                except RuntimeError:
                    self._job_cancel = None
                    self._job_notice = None
                    self._status = 'unavailable'
            elif self._future is None and not due:
                self._status = 'monitoring' if self._channels else 'idle'

    def _deliver(self, monitor, notice, keys, cancel, epoch, importance='normal'):
        with self._lock:
            if (self._closed or epoch != self._epoch or cancel.is_set()
                    or notice.channel in self._blocked_channels
                    or monitor is not self._monitor or not monitor.mark_sending(notice)):
                return
            self._status = 'sending'
        try:
            outcome = self._sender(keys[0], keys[1], notice, cancel, importance)
            if outcome not in ('success', 'failed', 'ambiguous'):
                outcome = 'ambiguous'
        except Exception:
            outcome = 'ambiguous'
        with self._lock:
            if self._closed or epoch != self._epoch or cancel.is_set() or monitor is not self._monitor:
                return
            now = self._read_clock()
            if not _finite(now):
                self._reset()
                self._scope = None
                self._status = 'invalid_clock'
                return
            now = max(now, self._last_now)
            self._last_now = now
            if monitor.complete(notice, outcome, now):
                self._status = 'accepted' if outcome == 'success' else outcome
            self._due(monitor, now)

    def close(self):
        """Cancel queued work and stop future sends without waiting for HTTP.

        Idempotent. In-flight HTTP can finish, but cannot update a closed scope.
        poll may subsequently reap its future without submitting more work.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reset()
            if self._future is not None:
                self._future.cancel()
            self._scope = None
            self._status = 'closed'
        self._executor.shutdown(wait=False, cancel_futures=True)
