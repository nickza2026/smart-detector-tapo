import threading
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from smart_tapo import desktop_notify_test as mod
from smart_tapo.notify_api import NotifyResult


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from smart_tapo import smoke_window
        smoke_window.install()  # Must precede Tk: no focus or visible windows.
        cls.root = tk.Tk()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.credentials = ['secret-key', 'https://notify.invalid']
        self.sender = Mock(return_value=NotifyResult('success', 'provider_accepted'))
        self.confirm = Mock(return_value=True)
        self.panel = mod.NotifyTestPanel(self.root, lambda: tuple(self.credentials),
                                         sender=self.sender, confirm=self.confirm)

    def drain(self):
        deadline = time.monotonic() + 3
        while self.panel.is_running and time.monotonic() < deadline:
            self.root.update()
            self.panel.poll()
            time.sleep(.001)
        self.assertFalse(self.panel.is_running)

    def tearDown(self):
        self.panel.close()
        self.drain()
        self.panel.destroy()

    def test_parent_unlock_does_not_enable_inflight_button(self):
        pending = Future()
        self.panel._future = pending
        self.panel.button.state(['!disabled'])
        self.panel.poll()
        self.assertTrue(self.panel.button.instate(['disabled']))
        pending.set_result(NotifyResult('success', 'provider_accepted'))
        self.panel.poll()
        self.assertFalse(self.panel.is_running)

    def test_no_automatic_send_missing_or_invalid_target_and_cancel(self):
        self.panel.poll()
        self.sender.assert_not_called()
        for credentials in (['', 'https://notify.invalid'], ['secret-key', 'http://notify.invalid']):
            self.credentials[:] = credentials
            self.panel.start()
        self.confirm.assert_not_called()
        self.credentials[:] = ['secret-key', 'https://notify.invalid']
        self.confirm.return_value = False
        self.panel.button.invoke()
        self.sender.assert_not_called()
        self.assertFalse(self.panel.is_running)

    def test_reentrant_confirmation_snapshots_single_normal_send(self):
        def confirm():
            self.panel.start()
            self.credentials[:] = ['changed-key', 'https://changed.invalid']
            return True
        self.panel.confirm = confirm
        self.panel.button.invoke()
        self.panel.start()
        self.drain()
        self.sender.assert_called_once()
        target, alert, cancel = self.sender.call_args.args
        self.assertEqual((target.key, target.base_url), ('secret-key', 'https://notify.invalid'))
        self.assertEqual(alert.importance, 'normal')
        self.assertIn('TEST', alert.title)
        self.assertFalse(cancel.is_set())
        self.assertEqual(self.panel.status.get(), mod.RESULTS['success'])

    def test_inflight_close_drains_no_retry_no_raw_error(self):
        entered, release = threading.Event(), threading.Event()
        def sender(*_):
            entered.set()
            release.wait(2)
            raise RuntimeError('secret-key')
        self.panel.sender = sender
        self.panel.start()
        self.assertTrue(entered.wait(2))
        self.panel.close()
        self.assertTrue(self.panel.is_running)
        self.panel.start()
        self.assertNotIn('secret', self.panel.status.get())
        release.set()
        self.drain()
        self.assertEqual(self.panel.status.get(), mod.RESULTS['failed'])
        self.assertIn('disabled', self.panel.button.state())

    def test_queued_close_cancels_before_send(self):
        future = Future()
        with patch.object(self.panel._executor, 'submit', return_value=future):
            self.panel.start()
            self.panel.close()
        self.assertTrue(future.cancelled())
        self.assertFalse(self.panel.is_running)
        self.sender.assert_not_called()

    def test_fake_smoke_route(self):
        with patch('smart_tapo.notify_api.request.build_opener', side_effect=AssertionError('HTTP forbidden')):
            flags = mod.smoke_notify_test(self.root, self.panel)
        self.assertTrue(all(flags.values()), flags)
        self.sender.assert_not_called()
        self.assertIs(self.panel.sender, self.sender)
        self.assertEqual(self.credentials, ['secret-key', 'https://notify.invalid'])


if __name__ == '__main__':
    unittest.main()
