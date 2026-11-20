"""Frozen smoke's fake transport must drain new observations before completion."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from PIL import Image

from smart_tapo.config import Config
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore


@unittest.skipUnless(os.name == 'nt', 'Windows offscreen/DPAPI smoke contract')
class WebhookSmokeObservationTests(unittest.TestCase):
    def test_smoke_drains_preexisting_and_explicit_orphan_without_real_http(self):
        from smart_tapo.smoke_window import install
        install()
        import tkinter as tk
        from tkinter import ttk
        from smart_tapo.desktop_webhook import WebhookPanel
        from smart_tapo.webhook_smoke import smoke_webhook

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = tk.Tk()
            storage = DesktopStore(base/'config.json')
            config = Config(data_dir=base/'data', adb_serial='synthetic-device')
            config.data_dir.mkdir()
            storage.save(config)
            Image.new('RGB', (12, 12), 'blue').save(config.data_dir/'refueling-smoke-source.png')
            app = SimpleNamespace(config=config, store=storage, _active=lambda:False,
                                  audio_panel=SimpleNamespace(is_running=False))
            notebook = ttk.Notebook(root)
            notebook.pack(fill='both', expand=True)
            app.notebook = notebook

            def save(new_config):
                storage.save(new_config)
                app.config = new_config

            panel = WebhookPanel(notebook, lambda:app.config, save, storage)
            notebook.add(panel, text='Synthetic webhook')
            app.webhook_panel = panel
            store = RefuelingStore(config.data_dir/'refueling', config.camera_id)
            try:
                now = time.time()
                store.accept('synthetic-prior-orphan', ConfirmedAnnouncement('end', now-30, now-28))
            finally:
                store.close()
            try:
                report = smoke_webhook(root, app)
                self.assertTrue(report['webhook_fake_delivery_completed'])
                self.assertTrue(report['webhook_observation_delivery_completed'])
                self.assertFalse(report['webhook_real_http'])
            finally:
                panel.close()
                if panel.sender.future:
                    panel.sender.future.result(timeout=5)
                    panel.sender.poll()
                root.destroy()


if __name__ == '__main__':
    unittest.main()
