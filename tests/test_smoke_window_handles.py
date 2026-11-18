"""Window recreation must be checked against the current owned native handle."""
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from smart_tapo import smoke_window


class SmokeWindowHandleTests(unittest.TestCase):
    def check(self,foreground):
        api=Mock()
        api.GetSystemMetrics.side_effect=lambda n:{76:0,78:1920,77:0,79:1080}[n]
        api.GetAncestor.side_effect=[101,202]
        api.GetWindowLongPtrW.return_value=0
        api.SetWindowLongPtrW.return_value=1
        api.GetForegroundWindow.return_value=foreground
        def rect(handle,pointer):
            box=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            box.left,box.top=(0,0) if handle==101 else (2184,1367)
            return True
        api.GetWindowRect.side_effect=rect
        window=Mock();window.winfo_id.return_value=99;window.winfo_ismapped.return_value=True
        with ExitStack() as stack:
            stack.enter_context(patch.object(smoke_window,'_policy',None))
            stack.enter_context(patch('smart_tapo.smoke_window.ctypes.WinDLL',return_value=api))
            for owner,names in ((tk.Wm,('wm_geometry','geometry','wm_deiconify','deiconify')),
                                (tk.Tk,('__init__',)),(tk.Toplevel,('__init__',))):
                for name in names:stack.enter_context(patch.object(owner,name,Mock()))
            policy=smoke_window.install()
            tk.Wm.deiconify(window)
            return policy,api

    def test_recreated_window_is_not_confused_with_reused_old_foreground_handle(self):
        # Old wrapper A has been replaced by owned wrapper B. A may now belong
        # to another foreground window; its rectangle is not the test's window.
        policy,api=self.check(101)
        self.assertEqual(policy['shown_windows'],[dict(left=2184,top=1367,mapped=True)])
        self.assertEqual(api.GetWindowRect.call_args.args[0],202)
        self.assertEqual(api.SetWindowLongPtrW.call_count,2)

    def test_current_owned_foreground_still_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,'offscreen/no-focus'):
            self.check(202)
