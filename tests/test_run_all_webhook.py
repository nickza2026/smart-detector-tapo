import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock

from smart_tapo.config import Config
from smart_tapo.desktop_webhook import WebhookPanel, PreparedStartError
from tests.test_run_all import Var


class PreparedWebhookTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(refueling_api_enabled=True, refueling_api_url='https://example.invalid')
        self.panel = object.__new__(WebhookPanel)
        self.panel.get_config = lambda: self.config
        self.panel.enabled = Var(True)
        self.panel.url = Var(self.config.refueling_api_url)
        self.panel.key = Var()
        self.panel.consent = Var(False)
        self.panel.scope = None
        self.panel.recovery = None
        self.panel.storage = NS(load_refueling_key=Mock(return_value='synthetic-key-only'))
        self.panel.label = Mock()
        self.panel.sender = NS(scope=None, busy=False, closed=False, stop_event=threading.Event(),
                               start=Mock(), stop=Mock(), status='synthetic')

    def test_saved_key_loaded_once_no_checkbox_needed(self):
        plan = self.panel.prepare_start(consent=True)
        self.panel.start_prepared(plan)
        self.panel.storage.load_refueling_key.assert_called_once_with(self.config.refueling_api_url, self.config.camera_id)
        self.panel.sender.start.assert_called_once()
        self.assertNotIn('synthetic-key-only', repr(plan))
        self.assertNotIn('example.invalid', repr(plan))

    def test_matching_active_sender_not_rebound_or_key_loaded(self):
        from pathlib import Path
        from smart_tapo.refueling_webhook import WebhookTarget
        target = WebhookTarget(self.config.refueling_api_url, 'synthetic-key-only', self.config.camera_id)
        directory = Path(self.config.data_dir).absolute()
        self.panel.scope = (directory, self.config.camera_id, self.config.refueling_api_url)
        self.panel.sender.scope = (directory / 'refueling', target)
        plan = self.panel.prepare_start(consent=True)
        self.assertFalse(self.panel.start_prepared(plan))
        self.panel.storage.load_refueling_key.assert_not_called()
        self.panel.sender.start.assert_not_called()

    def test_busy_without_active_scope_blocks_key_read(self):
        self.panel.sender.busy = True
        with self.assertRaises(PreparedStartError):
            self.panel.prepare_start(consent=True)
        self.panel.storage.load_refueling_key.assert_not_called()

    def test_unsaved_fields_permissions_and_no_consent_reject(self):
        for variable, bad in ((self.panel.enabled, False), (self.panel.url, 'https://changed.invalid'),
                              (self.panel.key, 'secret-unsaved')):
            old = variable.get()
            variable.set(bad)
            with self.assertRaises(PreparedStartError):
                self.panel.prepare_start(consent=True)
            variable.set(old)
        with self.assertRaises(PreparedStartError):
            self.panel.prepare_start()
        self.config = replace(self.config, refueling_api_enabled=False)
        with self.assertRaises(PreparedStartError):
            self.panel.prepare_start(consent=True)
        self.panel.storage.load_refueling_key.assert_not_called()

    def test_key_error_redacted(self):
        self.panel.storage.load_refueling_key.side_effect = ValueError('secret-url-key')
        with self.assertRaises(PreparedStartError) as caught:
            self.panel.prepare_start(consent=True)
        self.assertNotIn('secret-url-key', str(caught.exception))

    def test_config_and_stop_change_cancel_prepared(self):
        plan = self.panel.prepare_start(consent=True)
        self.panel.sender.stop_event.set()
        with self.assertRaises(PreparedStartError):
            self.panel.start_prepared(plan)
        self.panel.sender.start.assert_not_called()

    def test_previously_stopped_sender_can_restart(self):
        self.panel.sender.stop_event.set()
        plan = self.panel.prepare_start(consent=True)
        self.assertTrue(self.panel.start_prepared(plan))

    def test_partial_start_failure_clears_scope(self):
        plan = self.panel.prepare_start(consent=True)
        self.panel.sender.start.side_effect = RuntimeError('secret-key')
        with self.assertRaises(PreparedStartError):
            self.panel.start_prepared(plan)
        self.assertIsNone(self.panel.scope)
        self.panel.sender.stop.assert_called_once()
