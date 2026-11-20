"""Offline contract tests: every transport is fake; never open a live URL."""
import io
import json
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from smart_tapo.diagnostics import APP_VERSION

from smart_tapo.notify_api import (
    AlertMessage, NotifyTarget, NotifyResult, send_alert, deliver_alert, _NoRedirect,
)


class Response:
    def __init__(self, code, body, headers=None):
        self.code, self.headers = code, headers or {}
        self.stream = io.BytesIO(body)
        self.closed = False
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Event:
    def __init__(self, cancel_on_wait=False):
        self.cancelled = False
        self.cancel_on_wait = cancel_on_wait
        self.waits = []
        self.now = 0

    def is_set(self):
        return self.cancelled

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        self.cancelled = self.cancel_on_wait
        return self.cancelled


class NotifyTests(unittest.TestCase):
    def setUp(self):
        # Any accidental fallback transport fails before network activity.
        self.guard = patch("smart_tapo.notify_api.request.build_opener", side_effect=AssertionError("live transport forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.target = NotifyTarget("https://unit.invalid", "private-test-key")
        self.alert = AlertMessage("event_1", "unit:event:1", " ไทย ", "message", "normal")
        self.cancel = Event()

    def receipt(self, **updates):
        data = dict(alert_id="event_1", status="provider_accepted", importance="normal",
                    acknowledged=False, delivery_verified=False,
                    expires_at="2000-01-01T00:00:00.123456789Z")
        data.update(updates)
        return Response(202, json.dumps(data).encode())

    def send(self, response):
        return send_alert(self.target, self.alert, self.cancel, opener=Opener(response))

    def test_acceptance_both_importances_and_default_ttl(self):
        for importance in ("normal", "critical"):
            alert = AlertMessage("event_1", "unit:event:1", "t", "m", importance)
            opener = Opener(self.receipt(importance=importance, future_field="ignored"))
            self.assertEqual(send_alert(self.target, alert, self.cancel, opener=opener).state, "success")
            req, timeout = opener.requests[0]
            self.assertEqual(timeout, 10)
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.full_url, "https://unit.invalid/v1/alerts")
            self.assertEqual(json.loads(req.data)["ttl_seconds"], 300)
            self.assertEqual(req.get_header("Authorization"), "Bearer private-test-key")
            self.assertEqual(req.get_header("User-agent"), "SmartTapo/" + APP_VERSION.removeprefix('v'))
            self.assertEqual(req.get_header("Accept"), "application/json")

    def test_input_boundaries_and_no_implicit_importance(self):
        AlertMessage("a" * 100, "b" * 128, "😀" * 120, "ก" * 2000, "normal", 10800)
        with self.assertRaises(TypeError):
            AlertMessage("a", "b", "t", "m")
        invalid = [dict(alert_id="a/b"), dict(alert_id="a" * 101), dict(idempotency_key="bad\r\n"),
                   dict(idempotency_key="x" * 129), dict(title=" " ), dict(title="x" * 121),
                   dict(message=None), dict(message="x" * 2001), dict(importance=None),
                   dict(importance="urgent"), dict(ttl_seconds=True), dict(ttl_seconds=29),
                   dict(ttl_seconds=10801), dict(title="\ud800")]
        for update in invalid:
            args = dict(alert_id="a", idempotency_key="b", title="t", message="m", importance="normal")
            args.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                AlertMessage(**args)

    def test_target_rejects_unsafe_urls_and_keys(self):
        for url in ("http://unit.invalid", "https://u:p@unit.invalid", "https://unit.invalid/?q=x",
                    "https://unit.invalid/#secret", "https://unit.invalid\n", "https://unit.invalid:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                NotifyTarget(url, "key")
        for key in ("", " ", "k\n", "k\x7f", "x" * 4097, None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                NotifyTarget("https://unit.invalid", key)

    def test_receipt_strictness(self):
        for update in (dict(acknowledged=0), dict(acknowledged=True), dict(delivery_verified=True),
                       dict(delivery_verified=0), dict(alert_id="other"), dict(importance="critical"),
                       dict(status="delivered"), dict(expires_at="2026-02-30T00:00:00Z"),
                       dict(expires_at="2026-01-01T00:00:00+00:00"), dict(expires_at=None)):
            with self.subTest(update=update):
                self.assertEqual(self.send(self.receipt(**update)).state, "ambiguous")

    def test_http_status_is_retained_without_raw_provider_error(self):
        response = Response(401, b'{"error":{"code":"unauthorized","message":"private-test-key"}}')
        result = self.send(response)
        self.assertEqual(result.code, 'unauthorized')
        self.assertEqual(getattr(result, 'http_status', None), 401)
        self.assertNotIn('private-test-key', repr(result))

    def test_only_integer_http_status_metadata_is_allowed(self):
        for status in (True, False, '401', 401.0, 99, 600):
            with self.subTest(status=status), self.assertRaises(ValueError):
                NotifyResult('failed', 'unauthorized', http_status=status)
        self.assertIsNone(NotifyResult('ambiguous', 'network_unknown').http_status)

    def test_retry_exhaustion_retains_last_received_http_status(self):
        opener = Opener(Response(503, b'{"error":{"code":"database_unavailable"}}'),
                        Response(503, b'{"error":{"code":"database_unavailable"}}'))
        result = deliver_alert(self.target, self.alert, self.cancel, attempts=2,
            sender=lambda t,a,c: send_alert(t,a,c,opener=opener), clock=lambda: self.cancel.now)
        self.assertEqual(result.state, 'ambiguous')
        self.assertEqual(result.code, 'retry_budget_exhausted')
        self.assertEqual(result.http_status, 503)
        self.assertEqual(len(opener.requests), 2)

    def test_invalid_json_oversize_and_bounded_read(self):
        for body in (b"", b"[]", b"null", b"bad-private-test-key", b"{" * 65537):
            response = Response(202, body)
            result = self.send(response)
            self.assertEqual(result, NotifyResult("ambiguous", "invalid_response"))
            self.assertEqual(response.read_sizes, [65537])
            self.assertTrue(response.closed)

    def test_timeout_then_success_same_bytes_headers_and_ids(self):
        opener = Opener(TimeoutError("private-test-key"), self.receipt())
        def sender(t, a, c):
            return send_alert(t, a, c, opener=opener)
        result = deliver_alert(self.target, self.alert, self.cancel, sender=sender, clock=lambda: self.cancel.now)
        self.assertEqual(result.state, "success")
        self.assertEqual(self.cancel.waits, [2])
        first, second = [req for req, _ in opener.requests]
        self.assertIs(first.data, second.data)
        self.assertEqual(first.header_items(), second.header_items())
        self.assertEqual(json.loads(first.data)["title"], " ไทย ")
        self.assertEqual(first.get_header("Idempotency-key"), "unit:event:1")

    def test_429_wait_and_success(self):
        opener = Opener(Response(429, b"not json", {"Retry-After": "60"}), self.receipt())
        result = deliver_alert(self.target, self.alert, self.cancel,
                              sender=lambda t, a, c: send_alert(t, a, c, opener=opener), clock=lambda: self.cancel.now)
        self.assertEqual(result.state, "success")
        self.assertEqual(self.cancel.waits, [60])

    def test_cancel_during_rate_wait(self):
        event = Event(True)
        calls = []
        def sender(*args):
            calls.append(args)
            return NotifyResult("failed", "rate_limited", 60, True)
        result = deliver_alert(self.target, self.alert, event, sender=sender, clock=lambda: event.now)
        self.assertEqual(result.code, "cancelled")
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(len(calls), 1)

    def test_retry_after_exceeds_budget_no_early_post(self):
        calls = []
        def sender(*args):
            calls.append(args)
            return NotifyResult("failed", "rate_limited", 600, True)
        result = deliver_alert(self.target, self.alert, self.cancel, sender=sender)
        self.assertEqual(result.code, "retry_budget_exhausted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cancel.waits, [])

    def test_bad_retry_after_bounded_backoff(self):
        for value in ("garbage", "-1", "inf"):
            result = self.send(Response(503, b"bad", {"Retry-After": value}))
            self.assertTrue(result.retryable)
            self.assertIsNone(result.retry_after)
        result = deliver_alert(self.target, self.alert, self.cancel,
                              sender=lambda *args: NotifyResult("ambiguous", "network_unknown", retryable=True),
                              clock=lambda: self.cancel.now)
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(self.cancel.waits, [2, 4])

    def test_recorded_502_never_retries(self):
        for status, state in (("provider_failed", "failed"), ("delivery_unknown", "ambiguous")):
            body = dict(alert_id="event_1", importance="normal", delivery_verified=False,
                        status=status, error=dict(code="partial_provider_outcome"))
            opener = Opener(Response(502, json.dumps(body).encode()))
            result = deliver_alert(self.target, self.alert, self.cancel,
                                  sender=lambda t, a, c: send_alert(t, a, c, opener=opener))
            self.assertEqual(result.state, state)
            self.assertFalse(result.retryable)
            self.assertEqual(len(opener.requests), 1)

    def test_terminal_errors_and_conflicts(self):
        for status, code in ((400, "invalid_schema"), (401, "unauthorized"), (403, "unauthorized"),
                             (409, "idempotency_conflict"), (409, "alert_id_conflict"),
                             (409, "delivery_unknown_or_in_progress"), (413, "body_too_large"),
                             (415, "json_required"), (422, "payload_too_large")):
            opener = Opener(Response(status, json.dumps(dict(error=dict(code=code))).encode()))
            result = deliver_alert(self.target, self.alert, self.cancel,
                                  sender=lambda t, a, c: send_alert(t, a, c, opener=opener))
            self.assertEqual(result.code, code)
            self.assertFalse(result.retryable)
            self.assertEqual(len(opener.requests), 1)

    def test_redaction_and_unknown_errors(self):
        result = self.send(Response(401, b'{"error":{"code":"private-test-key"}}'))
        for value in (self.target, self.alert, result):
            self.assertNotIn("private-test-key", repr(value))
        self.assertEqual(result.code, "invalid_response")
        self.assertNotIn("message", repr(self.alert))

    def test_http_error_response_and_redirect_no_follow(self):
        exc = HTTPError("https://unit.invalid", 429, "private-test-key", {"Retry-After": "1"}, io.BytesIO(b"{}"))
        self.assertTrue(self.send(exc).retryable)
        opener = Opener(Response(302, b"", {"Location": "https://other.invalid"}))
        self.assertFalse(send_alert(self.target, self.alert, self.cancel, opener=opener).retryable)
        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_large_numeric_retry_after_never_resends_early(self):
        opener = Opener(Response(429, b"{}", {"Retry-After": "9" * 100}))
        result = deliver_alert(self.target, self.alert, self.cancel,
                              sender=lambda t, a, c: send_alert(t, a, c, opener=opener))
        self.assertEqual(result.code, "retry_budget_exhausted")
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.cancel.waits, [])

    def test_cancel_before_send_does_not_open(self):
        self.cancel.cancelled = True
        opener = Opener()
        self.assertEqual(send_alert(self.target, self.alert, self.cancel, opener=opener).code, "cancelled")
        self.assertEqual(opener.requests, [])

    def test_cancel_race_after_uncertain_attempt_stays_ambiguous(self):
        results = iter((NotifyResult("ambiguous", "network_unknown", retryable=True),
                        NotifyResult("failed", "cancelled")))
        result = deliver_alert(self.target, self.alert, self.cancel,
                              sender=lambda *args: next(results), clock=lambda: self.cancel.now)
        self.assertEqual(result.state, "ambiguous")
        self.assertEqual(result.code, "cancelled")

    def test_cancellation_after_open_cannot_undo_accepted_request(self):
        response = self.receipt()
        event = self.cancel
        class CancellingOpener:
            def open(self, req, timeout):
                event.cancelled = True
                return response
        result = send_alert(self.target, self.alert, event, opener=CancellingOpener())
        self.assertEqual(result.state, "success")

    def test_retry_argument_caps(self):
        for kwargs in (dict(attempts=True), dict(attempts=0), dict(attempts=4),
                       dict(max_elapsed=True), dict(max_elapsed=0), dict(max_elapsed=91), dict(max_elapsed=float("nan"))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                deliver_alert(self.target, self.alert, self.cancel, **kwargs)


if __name__ == "__main__":
    unittest.main()
