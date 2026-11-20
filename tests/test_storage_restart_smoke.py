"""Regressions for the owned normal-main restart fixture, not field behavior."""
import unittest
from unittest.mock import patch

from smart_tapo.storage_restart_smoke import _unexpected_main_error, smoke_storage_restart


class StorageRestartSmokeTests(unittest.TestCase):
    def test_error_dialog_retains_original_startup_cause(self):
        original=ValueError('synthetic constructor failure')
        with self.assertRaisesRegex(RuntimeError,'ValueError: synthetic constructor failure') as caught:
            try:raise original
            except ValueError:_unexpected_main_error('redacted production message')
        self.assertIs(caught.exception.__cause__,original)

    def test_nested_normal_main_never_checks_external_releases(self):
        from smart_tapo.desktop_updates import UpdatesPanel
        from smart_tapo import updates
        attempts=[]
        def external_check(*args):
            attempts.append(True)
            return None
        def immediate_periodic(panel):
            panel.periodic_enabled=True
            panel._periodic_check()
            # Deterministically complete the worker: no timing-dependent sleep.
            if panel.thread is not None:panel.thread.join(timeout=2)
        with patch.object(updates.ReleaseClient,'check',side_effect=external_check), \
             patch.object(UpdatesPanel,'start_periodic',immediate_periodic):
            result=smoke_storage_restart()
        self.assertEqual(attempts,[], 'nested smoke escaped to release service')
        self.assertTrue(result['storage_save_main_exit_gui'])
        self.assertTrue(result['storage_saved_root_reopened_gui'])
        self.assertTrue(result['storage_restart_managers_reaped_gui'])
