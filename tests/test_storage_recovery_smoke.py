"""Keep the packaged GUI smoke aligned with the bounded 3 x 15s policy."""
from pathlib import Path
import tempfile
import unittest


class StorageRecoverySmokeTests(unittest.TestCase):
    def test_actual_smoke_checks_fifteen_second_retry_and_exhaustion(self):
        from smart_tapo.smoke_window import install
        install()
        import tkinter as tk
        from smart_tapo.storage_recovery_smoke import smoke_storage_recovery
        root = tk.Tk(); root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                result = smoke_storage_recovery(root, Path(temporary) / 'recovery')
            self.assertTrue(result['storage_retry_stop_fenced_gui'])
            self.assertTrue(result['storage_three_attempts_fifteen_seconds_gui'])
        finally:
            root.destroy()
