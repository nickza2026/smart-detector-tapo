"""Windows native GUI smoke policy. Installed ONLY by explicit smoke mode."""
import ctypes
from ctypes import wintypes
import os
import re
import tkinter as tk

_policy=None


def install():
    global _policy
    if _policy is not None:return _policy
    if os.name!='nt':raise RuntimeError('Native smoke requires the Windows offscreen policy')
    api=ctypes.WinDLL('user32',use_last_error=True)
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
        seen.append({'left':rect.left,'top':rect.top,'mapped':bool(window.winfo_ismapped())})
        return result
    tk.Tk.__init__=lambda window,*a,**k:initialize(tk_init,window,*a,**k)
    tk.Toplevel.__init__=lambda window,*a,**k:initialize(top_init,window,*a,**k)
    tk.Wm.wm_geometry=tk.Wm.geometry=move
    tk.Wm.wm_deiconify=tk.Wm.deiconify=deiconify
    _policy={'virtual_right':right,'virtual_bottom':bottom,'target':[x,y],'shown_windows':seen}
    return _policy
