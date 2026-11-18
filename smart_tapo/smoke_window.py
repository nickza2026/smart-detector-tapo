"""Windows native GUI smoke policy. Installed ONLY by explicit smoke mode."""
import atexit
import ctypes
from ctypes import wintypes
import os
import re
import tkinter as tk

_policy=None
_activation_guard=None


class _NoActivateGuard:
    """Veto activation on this smoke thread, including a newly-created wrapper.

    WS_EX_NOACTIVATE belongs to an HWND, not a Tk object. Tk can replace that
    HWND during mapping before the post-map style check gets a chance to run.
    A thread-local CBT hook vetoes HCBT_ACTIVATE before Windows grants focus;
    it neither changes the foreground window nor hooks another process/thread.
    """
    def __init__(self, api, kernel):
        self.api=api
        self.handle=None
        self.blocked=0
        callback_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,ctypes.c_int,
                                        wintypes.WPARAM,wintypes.LPARAM)
        api.SetWindowsHookExW.argtypes=[ctypes.c_int,callback_type,wintypes.HINSTANCE,wintypes.DWORD]
        api.SetWindowsHookExW.restype=wintypes.HANDLE
        api.CallNextHookEx.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.WPARAM,wintypes.LPARAM]
        api.CallNextHookEx.restype=ctypes.c_ssize_t
        api.UnhookWindowsHookEx.argtypes=[wintypes.HANDLE]
        api.UnhookWindowsHookEx.restype=wintypes.BOOL
        kernel.GetCurrentThreadId.argtypes=[]
        kernel.GetCurrentThreadId.restype=wintypes.DWORD
        def callback(code,handle,detail):
            if code==5:  # HCBT_ACTIVATE; nonzero prevents activation.
                self.blocked+=1
                return 1
            return api.CallNextHookEx(self.handle,code,handle,detail)
        self.callback=callback_type(callback)  # Keep the native callback alive.
        self.handle=api.SetWindowsHookExW(5,self.callback,None,kernel.GetCurrentThreadId())  # WH_CBT
        if not self.handle:
            raise RuntimeError('Cannot protect native smoke window activation')

    def close(self):
        if self.handle is not None:
            if not self.api.UnhookWindowsHookEx(self.handle):
                raise RuntimeError('Cannot release native smoke activation hook')
            self.handle=None


def _install_activation_guard(api):
    global _activation_guard
    if _activation_guard is None:
        _activation_guard=_NoActivateGuard(api,ctypes.WinDLL('kernel32',use_last_error=True))
        atexit.register(_activation_guard.close)
    return _activation_guard


def install():
    global _policy
    if _policy is not None:return _policy
    if os.name!='nt':raise RuntimeError('Native smoke requires the Windows offscreen policy')
    api=ctypes.WinDLL('user32',use_last_error=True)
    _install_activation_guard(api)  # Before constructing/mapping any Tk window.
    api.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT];api.GetAncestor.restype=wintypes.HWND
    api.GetWindowLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int];api.GetWindowLongPtrW.restype=ctypes.c_ssize_t
    api.SetWindowLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t];api.SetWindowLongPtrW.restype=ctypes.c_ssize_t
    api.GetForegroundWindow.restype=wintypes.HWND
    api.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)];api.GetWindowRect.restype=wintypes.BOOL
    right=api.GetSystemMetrics(76)+api.GetSystemMetrics(78)
    bottom=api.GetSystemMetrics(77)+api.GetSystemMetrics(79)
    x,y=max(0,right)+256,max(0,bottom)+256
    geometry=tk.Wm.wm_geometry;show=tk.Wm.wm_deiconify
    tk_init,top_init=tk.Tk.__init__,tk.Toplevel.__init__
    seen=[]
    def style(window):
        handle=api.GetAncestor(window.winfo_id(),2)
        if not handle:raise RuntimeError('Missing native smoke window')
        before=api.GetWindowLongPtrW(handle,-20)
        ctypes.set_last_error(0)
        result=api.SetWindowLongPtrW(handle,-20,before|0x08000000|0x80)
        if not result and ctypes.get_last_error():raise ctypes.WinError(ctypes.get_last_error())
        return handle
    def move(window,value=None):
        if value is None:return geometry(window)
        size=re.match(r'^(\d+x\d+)',value)
        return geometry(window,f'{size.group(1) if size else ""}+{x}+{y}')
    def initialize(original,window,*args,**kwargs):
        original(window,*args,**kwargs);window.withdraw();style(window);geometry(window,f'+{x}+{y}')
    def deiconify(window):
        handle=style(window);geometry(window,f'+{x}+{y}');result=show(window)
        window.update_idletasks()
        # Tk may replace its native wrapper while mapping/painting. Do not
        # inspect an old HWND (which may already belong to another window).
        previous=handle
        handle=style(window)
        rect=wintypes.RECT()
        if not api.GetWindowRect(handle,ctypes.byref(rect)):raise ctypes.WinError(ctypes.get_last_error())
        foreground=api.GetForegroundWindow()==handle
        if rect.left<right or rect.top<bottom or foreground:
            # Safe synthetic-window diagnostics only; no titles/PIDs/handles.
            detail={'left':rect.left,'top':rect.top,'virtual_right':right,
                    'virtual_bottom':bottom,'owned_foreground':foreground,
                    'wrapper_changed':previous!=handle}
            raise RuntimeError('Native smoke escaped offscreen/no-focus policy: '+str(detail))
        seen.append({'left':rect.left,'top':rect.top,'mapped':bool(window.winfo_ismapped()),
                     'owned_foreground':foreground})
        return result
    tk.Tk.__init__=lambda window,*a,**k:initialize(tk_init,window,*a,**k)
    tk.Toplevel.__init__=lambda window,*a,**k:initialize(top_init,window,*a,**k)
    tk.Wm.wm_geometry=tk.Wm.geometry=move
    tk.Wm.wm_deiconify=tk.Wm.deiconify=deiconify
    _policy={'virtual_right':right,'virtual_bottom':bottom,'target':[x,y],'shown_windows':seen,
             'ci_focus_allowed':False}
    return _policy
