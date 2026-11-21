import threading
import unittest
from smart_tapo.capture.ui_lock import ui_lock


class UiLockTests(unittest.TestCase):
    def test_other_thread_cannot_dump_same_device_until_owned_lock_released(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def hold():
            try:
                with ui_lock('synthetic-ui-lock',1):
                    entered.set()
                    if not release.wait(3):raise RuntimeError('test barrier timed out')
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=hold);thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                with ui_lock('synthetic-ui-lock',.01):self.fail('overlapping UI ownership')
            with ui_lock('different-synthetic-device',.1):pass
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        with ui_lock('synthetic-ui-lock',1):pass
