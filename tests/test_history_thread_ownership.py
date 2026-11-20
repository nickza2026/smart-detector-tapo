"""Background history work must never own the GUI or its Tk interpreter."""
from __future__ import annotations

import gc
from dataclasses import replace
import queue
import threading
import unittest
from unittest.mock import Mock, patch
import weakref

from smart_tapo.config import Config
from smart_tapo.desktop_history import HistoryPanel


class Owner:
    def __init__(self):
        self.config = Config()

    def get_config(self):
        return self.config


class Panel:
    pass


class HistoryThreadOwnershipTests(unittest.TestCase):
    def test_blocked_reader_does_not_keep_panel_alive_after_close(self):
        panel = Panel()
        panel.generation = 3
        panel.pending = False
        panel.results = queue.Queue(maxsize=2)
        results = panel.results
        reference = weakref.ref(panel)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def work():
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError('Synthetic job release timed out')
                return 'synthetic-result'
            finally:
                finished.set()

        try:
            HistoryPanel._job(panel, 'events', work)
            self.assertTrue(entered.wait(2))
            panel = None
            gc.collect()
            self.assertIsNone(reference(), 'A background closure still owns the GUI panel')
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(results.get(timeout=2), ('events', 3, 'synthetic-result', False))

    def test_web_provider_holds_only_config_state_not_gui_owner(self):
        owner = Owner()
        config = owner.config
        reference = weakref.ref(owner)
        panel = Panel()
        panel.port_var = Mock(get=Mock(return_value='8080'))
        panel.server = None
        panel.save_port = Mock()
        panel.config_provider = owner.get_config
        panel._web_config = [config]
        for name in ('web_status', 'start_web_button', 'port_entry', 'stop_web_button', 'open_web_button'):
            setattr(panel, name, Mock())
        server = Mock(port=8080)
        with patch('smart_tapo.history_web.HistoryWebServer', return_value=server) as factory:
            self.assertTrue(HistoryPanel.start_web(panel))
            provider = factory.call_args.args[0]
        # Runtime settings remain live, but are published on the GUI thread.
        owner.config = replace(config, deposit_history_hours=48)
        panel.closed = False
        panel.retention = Mock()
        panel.scope = (owner.config.data_dir, owner.config.camera_id, 48)
        panel.results = queue.Queue()
        panel.pending = True
        HistoryPanel.poll(panel)
        config = owner.config
        self.assertIs(provider(), config)
        panel = owner = None
        gc.collect()
        self.assertIsNone(reference(), 'The HTTP-thread callback still owns the GUI owner')
        self.assertIs(provider(), config)


if __name__ == '__main__':
    unittest.main()
