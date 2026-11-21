"""Small Windows process adapter. Handles, not recycled PIDs, own wait/kill rights."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from .updates import UpdateError


class ProcessGone(UpdateError):
    """Native process no longer exists (not access denied/query failure)."""


class WindowsProcesses:
    def __init__(self):
        if os.name != 'nt':
            raise UpdateError('Windows packaged application required')
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE],wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
            'GetProcessTimes': ([wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4,wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE,wintypes.DWORD],wintypes.DWORD),
            'TerminateProcess': ([wintypes.HANDLE,wintypes.UINT],wintypes.BOOL),
            'GetProcessId': ([wintypes.HANDLE],wintypes.DWORD),
        }
        for name,(args,result) in signatures.items():
            f=getattr(self.k,name); f.argtypes=args; f.restype=result

    def open(self,pid,*,terminate=False):
        handle=self.k.OpenProcess(0x100000|0x1000|0x0400|(1 if terminate else 0),False,pid)
        if not handle:
            if ctypes.get_last_error()==87: raise ProcessGone('Process no longer exists')
            raise UpdateError('Cannot bind process handle')
        return handle

    def identity(self,handle):
        name=ctypes.create_unicode_buffer(32768); size=wintypes.DWORD(len(name))
        times=[wintypes.FILETIME() for _ in range(4)]
        if not self.k.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)) or not self.k.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
            raise UpdateError('Cannot verify process identity')
        return {'pid':int(self.k.GetProcessId(handle)), 'path':str(Path(name.value)),
                'created':(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime}

    def parent_pid(self,handle):
        class Basic(ctypes.Structure):
            _fields_=[('reserved',ctypes.c_void_p),('peb',ctypes.c_void_p),
                      ('reserved2',ctypes.c_void_p*2),('pid',ctypes.c_size_t),('ppid',ctypes.c_size_t)]
        query=ctypes.WinDLL('ntdll').NtQueryInformationProcess
        query.argtypes=[wintypes.HANDLE,wintypes.ULONG,ctypes.c_void_p,wintypes.ULONG,ctypes.c_void_p]
        query.restype=wintypes.LONG
        info=Basic()
        if query(handle,0,ctypes.byref(info),ctypes.sizeof(info),None)!=0:
            raise UpdateError('Cannot verify process ancestry')
        return info.ppid

    def bootloader(self,identity):
        """Record onefile's same-image parent before the GUI quits."""
        handle=self.open(identity['pid'])
        try: pid=self.parent_pid(handle)
        finally: self.close(handle)
        try: handle=self.open(pid)
        except ProcessGone: return None
        try:
            parent=self.identity(handle)
            if os.path.normcase(parent['path'])==os.path.normcase(identity['path']) and parent['created']<=identity['created']:
                return parent
            return None
        finally: self.close(handle)

    def current(self):
        handle=self.open(os.getpid())
        try: return self.identity(handle)
        finally: self.close(handle)

    def alive(self,handle):
        result=self.k.WaitForSingleObject(handle,0)
        if result not in (0,258): raise UpdateError('Process wait failed')
        return result==258

    def wait(self,handle,seconds):
        result=self.k.WaitForSingleObject(handle,int(seconds*1000))
        if result not in (0,258): raise UpdateError('Process wait failed')
        return result==0

    def kill(self,handle):
        if self.alive(handle) and not self.k.TerminateProcess(handle,1):
            raise UpdateError('Cannot stop owned update child')
        if not self.wait(handle,10): raise UpdateError('Owned child did not stop')

    def kill_launch(self,process):
        """Stop the owned launch and onefile worker even when no ACK arrived."""
        owner=self.child_handle(process)
        if not self.alive(owner): return
        suspend=ctypes.WinDLL('ntdll').NtSuspendProcess
        suspend.argtypes=[wintypes.HANDLE]; suspend.restype=wintypes.LONG
        # Freeze before enumerating so the owner cannot create another worker
        # between the snapshot and termination. Never kill a PID from a snapshot.
        class Entry(ctypes.Structure):
            _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
                      ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                      ('ppid',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),
                      ('exe',wintypes.WCHAR*260)]
        snapshot=self.k.CreateToolhelp32Snapshot
        snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]; snapshot.restype=wintypes.HANDLE
        first=self.k.Process32FirstW; following=self.k.Process32NextW
        for f in (first,following):
            f.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]; f.restype=wintypes.BOOL
        if suspend(owner)!=0: raise UpdateError('Cannot freeze owned bootloader')
        try:
            snap=snapshot(2,0)
            if snap==ctypes.c_void_p(-1).value:
                raise UpdateError('Cannot enumerate owned worker')
            try:
                entry=Entry(); entry.size=ctypes.sizeof(entry)
                ok=first(snap,ctypes.byref(entry)); owner_id=self.identity(owner)
                if not ok and ctypes.get_last_error()!=18:
                    raise UpdateError('Cannot enumerate owned worker')
                while ok:
                    if entry.ppid==owner_id['pid']:
                        h=self.open(entry.pid,terminate=True)
                        try:
                            identity=self.identity(h)
                            bound=self.bind_startup(process,identity)
                            try: self.kill(bound)
                            finally: self.close(bound)
                        finally: self.close(h)
                    ok=following(snap,ctypes.byref(entry))
                if ctypes.get_last_error()!=18:
                    raise UpdateError('Owned worker enumeration incomplete')
            finally: self.close(snap)
            self.kill(owner)
        finally:
            # Even access/enumeration/termination failures must not strand the
            # exact owned bootloader suspended. Never resume or kill by PID.
            self._resume_owned(owner)

    def _resume_owned(self,owner):
        resume=ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes=[wintypes.HANDLE]; resume.restype=wintypes.LONG
        if resume(owner)!=0 and self.alive(owner):
            raise UpdateError('Cannot resume owned bootloader')

    def close(self,handle): self.k.CloseHandle(handle)

    def launch(self,exe,args,*,hidden=False,cwd=None):
        # Popen owns the process HANDLE from creation; never re-open a child by PID.
        # Child-local reset prevents a restarted onefile application from reusing
        # helper extraction files that disappear when its bootloader exits.
        environment=os.environ.copy()
        environment['PYINSTALLER_RESET_ENVIRONMENT']='1'
        process=subprocess.Popen([str(exe),*args],cwd=str(cwd or Path(exe).parent),
                                 shell=False,close_fds=True,env=environment,
                                 creationflags=subprocess.CREATE_NO_WINDOW if hidden else 0)
        return process

    def child_handle(self,process): return int(process._handle)

    def bind_startup(self,process,identity):
        """Bind the GUI/helper worker, including PyInstaller onefile's child.

        Onefile's Popen HANDLE belongs to its extraction bootloader. Accept only
        that exact process or its direct native child with identical image path,
        while the original bootloader HANDLE is still alive. Never trust a PID
        supplied by an ACK without verifying both HANDLE identities and ancestry.
        Returned handle is separately owned and must be closed by the caller.
        """
        parent_handle=self.child_handle(process)
        parent=self.identity(parent_handle)
        if not self.alive(parent_handle): raise UpdateError('Launch owner already exited')
        handle=self.open(identity['pid'],terminate=True)
        try:
            actual=self.identity(handle)
            if actual!=identity or os.path.normcase(actual['path'])!=os.path.normcase(parent['path']):
                raise UpdateError('Startup process identity mismatch')
            if actual['pid']==parent['pid']:
                if actual!=parent: raise UpdateError('Startup PID reused')
            else:
                if self.parent_pid(handle)!=parent['pid'] or actual['created']<parent['created']:
                    raise UpdateError('Startup worker is not owned by launch')
            if not self.alive(parent_handle): raise UpdateError('Launch owner exited during binding')
            return handle
        except BaseException:
            self.close(handle); raise


def packaged_executable():
    if os.name!='nt' or not getattr(sys,'frozen',False):
        raise UpdateError('Installation requires the packaged Windows executable')
    return Path(sys.executable)
