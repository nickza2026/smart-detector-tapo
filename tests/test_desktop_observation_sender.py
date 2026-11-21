"""The actual consent/cancellation wrapper must forward incomplete observations."""
import threading
import unittest
from unittest.mock import patch

from smart_tapo.desktop_webhook import _attempt
from smart_tapo.refueling_webhook import DeliveryResult, WebhookTarget


class DesktopObservationSenderTests(unittest.TestCase):
    def target(self):
        return WebhookTarget('https://example.invalid', 'synthetic-key-only', 'cabinet-1')

    def test_actual_worker_wrapper_forwards_observation(self):
        target = self.target()
        calls = []

        class Client:
            def __init__(self, destination, media):
                self.target = destination

            def send_observation(self, payload, event_id):
                calls.append((payload, event_id))
                return DeliveryResult('delivered')

        def dispatch(store, client):
            return client.send_observation(b'fixture', 'observation:1')

        with patch('smart_tapo.desktop_webhook.RefuelingStore') as store, patch(
                'smart_tapo.desktop_webhook.dispatch_one', side_effect=dispatch):
            result = _attempt('unused', target, threading.Event(), Client)
            self.assertEqual(result.status, 'delivered')
            self.assertEqual(calls, [(b'fixture', 'observation:1')])
            store.return_value.close.assert_called_once()

    def test_stop_between_requests_prevents_observation_http(self):
        target, stop = self.target(), threading.Event()
        calls = []

        class Client:
            def __init__(self, destination, media):
                self.target = destination

            def send_observation(self, *args):
                calls.append(args)
                return DeliveryResult('delivered')

        def dispatch(store, client):
            stop.set()
            return client.send_observation(b'fixture', 'observation:1')

        with patch('smart_tapo.desktop_webhook.RefuelingStore'), patch(
                'smart_tapo.desktop_webhook.dispatch_one', side_effect=dispatch):
            result = _attempt('unused', target, stop, Client)
            self.assertEqual((result.status, result.error_code), ('retry', 'transport_unconfirmed'))
            self.assertEqual(calls, [])

    def test_receiver_auth_failure_wins_over_later_queue_failure(self):
        target = self.target()

        class Client:
            def __init__(self, destination, media):
                self.target = destination

            def send_observation(self, *args):
                return DeliveryResult('failed', 'receiver_auth_rejected')

        def dispatch(store, client):
            client.send_observation(b'fixture', 'observation:1')
            raise OSError('synthetic queue failure after receiver rejection')

        with patch('smart_tapo.desktop_webhook.RefuelingStore'), patch(
                'smart_tapo.desktop_webhook.dispatch_one', side_effect=dispatch):
            result = _attempt('unused', target, threading.Event(), Client)
            self.assertEqual(result.error_code, 'receiver_auth_rejected')


if __name__ == '__main__':
    unittest.main()
