"""One bounded UI verification at a time, outside the screenshot path."""
from concurrent.futures import ThreadPoolExecutor
from smart_tapo.capture.page_mode import PageMode
import logging
import time
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class _CheckResult:
    value: object
    error: object
    detail: object

from smart_tapo.capture.ui_diagnostics import (SAFE_REASONS, UI_CHECK_REASONS,
                                             observation, valid_ui_check)
LOG = logging.getLogger(__name__)


# SAFE_REASONS remains re-exported for existing callers.


class LiveVerificationError(ValueError):
    def __init__(self, reason):
        self.reason = reason if isinstance(reason, str) and reason in SAFE_REASONS else 'unrecognized_reason'
        super().__init__('Live UI verification rejected: ' + self.reason)


class LiveVerifier:
    def __init__(self, verify, interval, diagnostics_getter=None):
        self.diagnostics_getter = diagnostics_getter
        self._request_started = None
        self._last_pending_log = float('-inf')
        self.verify = verify
        self.interval = interval
        self.executor = None
        self.pending = None
        self.verified_at = float('-inf')
        self.started_at = None

    def establish(self, now):
        self.verified_at = now

    def _verify(self):
        result = error = None
        try:
            result = self.verify()
        except Exception as exc:
            error = exc
        detail = None
        if self.diagnostics_getter is not None:
            try:
                detail = dict(self.diagnostics_getter())
            except Exception:
                pass
        return _CheckResult(result, error, detail)

    def _emit(self, state, reason, now, detail=None):
        data = observation()
        if detail is not None:
            data.update({key: detail[key] for key in data if key in detail})
        data.update(state=state, reason=reason)
        elapsed = time.monotonic() - self._request_started if self._request_started is not None else None
        data['check_seconds'] = float(min(86400, max(0, elapsed))) if elapsed is not None else None
        age = now - self.verified_at
        data['proof_age_seconds'] = float(min(86400, max(0, age))) if math.isfinite(age) else None
        if valid_ui_check(data):
            LOG.log(logging.INFO if state == 'valid' else logging.WARNING,
                    'Live UI check diagnostics', extra={'ui_check_context': data})

    def check(self, now):
        if self.pending is not None and self.pending.done():
            pending, self.pending = self.pending, None
            # Exceptions and negative results are deliberately not cached as trust.
            self.verified_at = float('-inf')
            try:
                outcome = pending.result()
                if isinstance(outcome, _CheckResult):
                    valid, error, detail = outcome.value, outcome.error, outcome.detail
                else:
                    valid, error, detail = outcome, None, None
            except Exception as exc:
                valid, error, detail = None, exc, None
            if error is not None:
                reason = getattr(error, 'reason', None)
                reason = reason if isinstance(reason, str) and reason in UI_CHECK_REASONS else 'verification_exception'
                self._emit('failed', reason, now, detail)
                if isinstance(error, LiveVerificationError):
                    raise error from None
                raise ValueError('Live UI verification failed (details withheld)') from None
            if isinstance(valid, PageMode):
                if valid.mode != 'live':
                    rejection = LiveVerificationError(valid.reason)
                    self._emit('rejected', rejection.reason, now, detail)
                    raise rejection
            elif valid is not True:
                # Legacy injected adapters may still return a strict boolean.
                self._emit('rejected', 'legacy_rejected', now, detail)
                raise ValueError('Live view lost; navigate to configured camera manually')
            # Conservatively age proof from request start, not result consumption.
            self.verified_at = self.started_at
            if now - self.verified_at < self.interval:
                self._emit('valid', 'none', now, detail)
            elif now - self._last_pending_log >= 5:
                self._emit('pending_expired', 'proof_expired', now, detail)
                self._last_pending_log = now
        if self.pending is None and now - self.verified_at >= self.interval / 2:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='live-ui')
            self.started_at = now
            self._request_started = time.monotonic()
            self.pending = self.executor.submit(self._verify)
        trusted = now - self.verified_at < self.interval
        if not trusted and now - self._last_pending_log >= 5:
            self._emit('pending_expired', 'proof_expired', now)
            self._last_pending_log = now
        return trusted

    def reset(self):
        if self.executor is not None:
            # ADB commands retain their configured subprocess timeouts. Join before
            # reconnecting so a previous session cannot verify a new connection.
            self.executor.shutdown(wait=True, cancel_futures=True)
        self.executor = self.pending = None
        self.verified_at = float('-inf')
        self._request_started = None
        self._last_pending_log = float('-inf')

    def close(self):
        self.reset()
