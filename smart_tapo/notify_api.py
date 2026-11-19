"""Bounded Notify API transport. Acceptance is not phone delivery or acknowledgement."""
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import math
import re
import time
from urllib import error, parse, request

DEFAULT_BASE_URL = "https://notify-alarm-api-7496986423345747739.olufy-0.nortezh.com"
MAX_RESPONSE_BYTES = 65536
_CODES = frozenset("provider_accepted provider_rejected provider_unregistered provider_authentication_failed expired_before_send provider_delivery_unknown partial_provider_outcome idempotency_key_required invalid_schema request_interrupted unauthorized not_found idempotency_conflict alert_id_conflict delivery_unknown_or_in_progress device_limit_reached body_too_large json_required invalid_message invalid_alert_id invalid_ttl invalid_importance payload_too_large rate_limited database_unavailable temporarily_unavailable fcm_not_configured ack_not_configured targeted_sender_not_configured device_registration_not_configured no_devices_registered delivery_state_unavailable network_unknown invalid_response cancelled retry_budget_exhausted".split())


@dataclass(frozen=True)
class NotifyTarget:
    base_url: str = field(repr=False)
    key: str = field(repr=False)
    history: object | None = field(default=None, repr=False, compare=False)
    channel: str = field(default='notify', compare=False)

    def __post_init__(self):
        try:
            url = parse.urlsplit(self.base_url)
            valid = (url.scheme == "https" and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and not any(ord(c) <= 32 or ord(c) == 127 for c in self.base_url))
            url.port
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("invalid Notify API base URL")
        if (not isinstance(self.key, str) or not self.key.strip() or len(self.key) > 4096
                or any(ord(c) < 32 or ord(c) > 126 for c in self.key)):
            raise ValueError("invalid Notify API key")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if self.channel not in ('notify', 'manual_test', 'health', 'stuck'):
            raise ValueError('invalid Notify history channel')


@dataclass(frozen=True)
class AlertMessage:
    alert_id: str
    idempotency_key: str = field(repr=False)
    title: str = field(repr=False)
    message: str = field(repr=False)
    importance: str
    ttl_seconds: int = 300
    _body: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        for value, pattern in ((self.alert_id, r"[A-Za-z0-9_-]{1,100}"),
                               (self.idempotency_key, r"[A-Za-z0-9_.:-]{1,128}")):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("invalid alert identifier")
        for value, limit in ((self.title, 120), (self.message, 2000)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("invalid alert text")
        if self.importance not in ("normal", "critical"):
            raise ValueError("invalid alert importance")
        if type(self.ttl_seconds) is not int or not 30 <= self.ttl_seconds <= 10800:
            raise ValueError("invalid alert TTL")
        payload = {name: getattr(self, name) for name in
                   ("alert_id", "title", "message", "importance", "ttl_seconds")}
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("invalid alert encoding") from None
        if len(body) > 16384:
            raise ValueError("alert body too large")
        object.__setattr__(self, "_body", body)


@dataclass(frozen=True)
class NotifyResult:
    state: str
    code: str
    retry_after: float | None = None
    retryable: bool = False
    # Diagnostic metadata, not a different delivery outcome.
    http_status: int | None = field(default=None, compare=False)
    history_saved: bool | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.state not in ("success", "failed", "ambiguous") or self.code not in _CODES:
            raise ValueError("invalid notification result")
        if (self.retry_after is not None and (isinstance(self.retry_after, bool)
                or not isinstance(self.retry_after, (int, float))
                or not math.isfinite(self.retry_after) or self.retry_after < 0)):
            raise ValueError("invalid retry delay")
        if type(self.retryable) is not bool:
            raise ValueError("invalid retry flag")
        if self.http_status is not None and (type(self.http_status) is not int
                                            or not 100 <= self.http_status <= 599):
            raise ValueError("invalid HTTP status")
        if self.history_saved is not None and type(self.history_saved) is not bool:
            raise ValueError('invalid history status')


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _expiry_valid(value):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def _response(status, headers, raw, alert):
    delay = None
    value = headers.get("Retry-After", "") if headers else ""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        digits = value.strip().lstrip("0") or "0"
        # Oversized valid decimal delays prohibit retry inside our 90-second cap.
        # Do not discard them and accidentally resend after a short backoff.
        delay = int(digits) if len(digits) <= 100 else 10 ** 100
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_RESPONSE_BYTES else None
    except (ValueError, UnicodeError, RecursionError):
        data = None
    if not isinstance(data, dict):
        data = {}
    candidate = data.get("error", {})
    code = candidate.get("code") if isinstance(candidate, dict) else None
    code = code if isinstance(code, str) and code in _CODES else "invalid_response"
    if status == 202:
        valid = (data.get("status") == "provider_accepted" and data.get("alert_id") == alert.alert_id
                 and data.get("importance") == alert.importance and data.get("acknowledged") is False
                 and data.get("delivery_verified") is False and _expiry_valid(data.get("expires_at")))
        return NotifyResult("success", "provider_accepted") if valid else NotifyResult("ambiguous", "invalid_response")
    if status == 502:
        valid = (data.get("alert_id") == alert.alert_id and data.get("importance") == alert.importance
                 and data.get("delivery_verified") is False)
        if (valid and data.get("status") in ("provider_failed", "delivery_unknown")
                and code in ("provider_rejected", "provider_unregistered", "provider_authentication_failed",
                             "expired_before_send", "provider_delivery_unknown", "partial_provider_outcome")):
            return NotifyResult("failed" if data["status"] == "provider_failed" else "ambiguous", code)
        return NotifyResult("ambiguous", "invalid_response")
    if status == 429:
        return NotifyResult("failed", code, delay, True)
    if status == 503:
        return NotifyResult("ambiguous", code, delay, True)
    if status == 409 and code == "delivery_unknown_or_in_progress":
        return NotifyResult("ambiguous", code)
    if status in (400, 401, 403, 404, 409, 413, 415, 422) or 300 <= status < 400:
        return NotifyResult("failed", code)
    return NotifyResult("ambiguous", "invalid_response")


def send_alert(target, alert, cancel, *, opener=None):
    """One POST only; injected opener must implement open(request, timeout=10)."""
    if cancel.is_set():
        return NotifyResult("failed", "cancelled")
    req = request.Request(target.base_url + "/v1/alerts", data=alert._body, method="POST",
                          headers={"Authorization": "Bearer " + target.key,
                                   "Idempotency-Key": alert.idempotency_key,
                                   "Content-Type": "application/json; charset=utf-8"})
    if opener is None:
        opener = request.build_opener(_NoRedirect())
    if cancel.is_set():
        return NotifyResult("failed", "cancelled")
    started = time.monotonic()
    identifier = None
    if target.history is not None:
        try:
            identifier = target.history.begin(target, alert, target.channel)
        except Exception:
            pass  # Diagnostic failure must never suppress or retry an alarm.
    status, raw, headers = None, None, None
    result = NotifyResult('ambiguous', 'network_unknown', retryable=True)
    try:
        if cancel.is_set():
            result = NotifyResult('failed', 'cancelled')
        else:
            try:
                response = opener.open(req, timeout=10)
            except error.HTTPError as exc:
                response = exc
            try:
                status = response.code
                headers = response.headers
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                result = replace(_response(status, headers, raw, alert), http_status=status)
            finally:
                response.close()
    except Exception:
        # Never expose exception strings, URLs, headers or response bodies.
        result = NotifyResult("ambiguous", "network_unknown", retryable=True)
    finally:
        if target.history is not None:
            saved = False
            if identifier is not None:
                try:
                    target.history.finish(identifier, result,
                        status if type(status) is int and 100 <= status <= 599 else None,
                        raw, headers, max(0, int((time.monotonic()-started)*1000)), target.key)
                    saved = True
                except Exception:
                    pass
            result = replace(result, history_saved=saved)
    return result


def deliver_alert(target, alert, cancel, *, sender=send_alert, clock=time.monotonic,
                  attempts=3, max_elapsed=90):
    """Retry the immutable event, never create a new event or poll acknowledgement."""
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("invalid attempt budget")
    if (isinstance(max_elapsed, bool) or not isinstance(max_elapsed, (int, float))
            or not math.isfinite(max_elapsed) or not 1 <= max_elapsed <= 90):
        raise ValueError("invalid elapsed budget")
    start = clock()
    sent = False
    for index in range(attempts):
        if cancel.is_set():
            return NotifyResult("ambiguous" if sent else "failed", "cancelled")
        # A full single-attempt timeout must fit once retrying.
        if index and clock() - start + 10 > max_elapsed:
            return NotifyResult("ambiguous" if sent else "failed", "retry_budget_exhausted",
                                http_status=result.http_status, history_saved=result.history_saved)
        result = sender(target, alert, cancel)
        if result.code == "cancelled" and sent:
            return NotifyResult("ambiguous", "cancelled")
        sent = sent or result.code != "cancelled"
        if not result.retryable:
            return result
        if index + 1 == attempts:
            break
        delay = max(2 ** (index + 1), result.retry_after or 0)
        if clock() - start + delay + 10 > max_elapsed:
            break
        if cancel.wait(delay):
            return NotifyResult("ambiguous" if sent else "failed", "cancelled")
    return NotifyResult("ambiguous" if sent else "failed", "retry_budget_exhausted",
                        http_status=result.http_status, history_saved=result.history_saved)
