"""Window recreation must be checked against the current owned native handle."""
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import os
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from smart_tapo import smoke_window


class SmokeWindowHandleTests(unittest.TestCase):
    def check(self,foreground,*,ci=False,offscreen=True):
        api=Mock()
        api.GetSystemMetrics.side_effect=lambda n:{76:0,78:1920,77:0,79:1080}[n]
        api.GetAncestor.side_effect=[101,202]
        api.GetWindowLongPtrW.return_value=0
        api.SetWindowLongPtrW.return_value=1
        api.GetForegroundWindow.return_value=foreground
        def rect(handle,pointer):
            box=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            box.left,box.top=(0,0) if handle==101 or not offscreen else (2184,1367)
            return True
        api.GetWindowRect.side_effect=rect
        window=Mock();window.winfo_id.return_value=99;window.winfo_ismapped.return_value=True
        environment={key:value for key,value in os.environ.items() if key!='GITHUB_ACTIONS'}
        if ci:environment['GITHUB_ACTIONS']='true'
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,environment,clear=True))
            stack.enter_context(patch.object(smoke_window,'_policy',None))
            stack.enter_context(patch('smart_tapo.smoke_window.ctypes.WinDLL',return_value=api))
            stack.enter_context(patch.object(smoke_window,'_install_activation_guard'))
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
        self.assertEqual(policy['shown_windows'],[dict(left=2184,top=1367,mapped=True,owned_foreground=False)])
        self.assertEqual(api.GetWindowRect.call_args.args[0],202)
        self.assertEqual(api.SetWindowLongPtrW.call_count,2)
        self.assertFalse(policy['ci_focus_allowed'])

    def test_current_owned_foreground_still_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError,'offscreen/no-focus'):
            self.check(202)

    def test_hosted_ci_cannot_bypass_foreground_or_offscreen_guard(self):
        with self.assertRaisesRegex(RuntimeError,'offscreen/no-focus'):
            self.check(202,ci=True)
        with self.assertRaisesRegex(RuntimeError,'offscreen/no-focus'):
            self.check(101,ci=True,offscreen=False)
        policy,_=self.check(101,ci=True)
        self.assertFalse(policy['ci_focus_allowed'])
        self.assertFalse(policy['shown_windows'][0]['owned_foreground'])

    def test_mapping_replacement_wrapper_cannot_activate_before_post_map_check(self):
        api,kernel=Mock(),Mock()
        api.GetSystemMetrics.side_effect=lambda n:{76:0,78:1920,77:0,79:1080}[n]
        api.GetAncestor.side_effect=[101,202]
        api.GetWindowLongPtrW.return_value=0
        api.SetWindowLongPtrW.return_value=1
        api.UnhookWindowsHookEx.return_value=True
        kernel.GetCurrentThreadId.return_value=456
        state={'foreground':999,'callback':None}
        def set_hook(kind,callback,instance,thread):
            state['callback']=callback
            return 123
        api.SetWindowsHookExW.side_effect=set_hook
        api.GetForegroundWindow.side_effect=lambda:state['foreground']
        def rect(handle,pointer):
            box=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            box.left,box.top=2184,1367
            return True
        api.GetWindowRect.side_effect=rect
        def map_replacement(window):
            # Native ordering seen on CI: a new wrapper requests activation
            # while mapping, BEFORE update_idletasks/post-map style(window).
            callback=state['callback']
            if callback is None or callback(5,202,0)==0:
                state['foreground']=202
        window=Mock();window.winfo_id.return_value=99;window.winfo_ismapped.return_value=True
        with ExitStack() as stack:
            stack.enter_context(patch.object(smoke_window,'_policy',None))
            stack.enter_context(patch.object(smoke_window,'_activation_guard',None,create=True))
            stack.enter_context(patch('atexit.register'))
            stack.enter_context(patch('smart_tapo.smoke_window.ctypes.WinDLL',
                side_effect=lambda name,**kwargs:api if name=='user32' else kernel))
            for owner,names in ((tk.Wm,('wm_geometry','geometry','wm_deiconify','deiconify')),
                                (tk.Tk,('__init__',)),(tk.Toplevel,('__init__',))):
                for name in names:
                    stack.enter_context(patch.object(owner,name,
                        Mock(side_effect=map_replacement) if name=='wm_deiconify' else Mock()))
            try:
                policy=smoke_window.install()
                tk.Wm.deiconify(window)
                self.assertEqual(state['foreground'],999)
                self.assertTrue(policy['shown_windows'][0]['mapped'])
            finally:
                guard=getattr(smoke_window,'_activation_guard',None)
                if guard is not None:guard.close()


class SmokeActivationGuardTests(unittest.TestCase):
    def test_recreated_wrapper_activation_is_vetoed_before_it_can_get_focus(self):
        api,kernel=Mock(),Mock()
        api.SetWindowsHookExW.return_value=123
        api.CallNextHookEx.return_value=77
        api.UnhookWindowsHookEx.return_value=True
        kernel.GetCurrentThreadId.return_value=456
        guard=smoke_window._NoActivateGuard(api,kernel)
        try:
            install=api.SetWindowsHookExW.call_args.args
            self.assertEqual((install[0],install[2],install[3]),(5,None,456))
            for wrapper in (101,202):
                self.assertNotEqual(guard.callback(5,wrapper,0),0)
            self.assertEqual(guard.blocked,2)
            api.CallNextHookEx.assert_not_called()
            self.assertEqual(guard.callback(-1,202,0),77)
            self.assertEqual(guard.callback(3,202,0),77)
            self.assertEqual(api.CallNextHookEx.call_count,2)
        finally:guard.close()
        api.UnhookWindowsHookEx.assert_called_once_with(123)
        guard.close()
        self.assertEqual(api.UnhookWindowsHookEx.call_count,1)

    def test_install_failure_is_not_treated_as_protected(self):
        api,kernel=Mock(),Mock()
        api.SetWindowsHookExW.return_value=None
        with self.assertRaisesRegex(RuntimeError,'Cannot protect'):
            smoke_window._NoActivateGuard(api,kernel)

    def test_unhook_failure_is_not_silently_marked_closed(self):
        api,kernel=Mock(),Mock()
        api.SetWindowsHookExW.return_value=123
        api.UnhookWindowsHookEx.return_value=False
        guard=smoke_window._NoActivateGuard(api,kernel)
        with self.assertRaisesRegex(RuntimeError,'Cannot release'):
            guard.close()
        self.assertEqual(guard.handle,123)


@unittest.skipUnless(os.name=='nt','Windows native activation contract')
class ZNativeSmokeActivationTests(unittest.TestCase):
    def test_owned_offscreen_window_cannot_be_activated_on_smoke_thread(self):
        policy=smoke_window.install()
        api=ctypes.WinDLL('user32',use_last_error=True)
        api.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        api.GetAncestor.restype=wintypes.HWND
        api.GetForegroundWindow.restype=wintypes.HWND
        api.SetActiveWindow.argtypes=[wintypes.HWND]
        api.SetActiveWindow.restype=wintypes.HWND
        root=tk.Tk()
        try:
            root.geometry('240x100')
            root.deiconify();root.update_idletasks()
            handle=api.GetAncestor(root.winfo_id(),2)
            self.assertTrue(root.winfo_ismapped())
            self.assertNotEqual(api.GetForegroundWindow(),handle)
            before=smoke_window._activation_guard.blocked
            # The hook is already installed and verified before deliberately
            # requesting activation of this one owned offscreen test window.
            api.SetActiveWindow(handle)
            root.update_idletasks()
            self.assertGreater(smoke_window._activation_guard.blocked,before)
            self.assertNotEqual(api.GetForegroundWindow(),handle)
            self.assertFalse(policy['shown_windows'][-1]['owned_foreground'])
        finally:root.destroy()
