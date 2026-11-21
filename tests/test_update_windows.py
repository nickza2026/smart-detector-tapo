"""Native adapter contract tests: mock every native/process operation."""
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from smart_tapo.update_windows import WindowsProcesses, packaged_executable
from smart_tapo.updates import UpdateError


class WindowsAdapter(unittest.TestCase):
    def setUp(self):
        self.proc=WindowsProcesses.__new__(WindowsProcesses)
        self.proc.child_handle=Mock(return_value=10)
        self.proc.open=Mock(return_value=20)
        self.proc.close=Mock()
        self.proc.alive=Mock(return_value=True)
        self.parent={'pid':100,'path':r'C:\app\app.exe','created':1000}
        self.worker={'pid':101,'path':r'C:\app\app.exe','created':1001}
        self.proc.identity=Mock(side_effect=lambda h: self.parent if h==10 else self.worker)
        self.proc.parent_pid=Mock(return_value=100)
    def test_onefile_worker_binds_only_native_child(self):
        self.assertEqual(self.proc.bind_startup(object(),self.worker.copy()),20)
        self.proc.open.assert_called_once_with(101,terminate=True)
    def test_recycled_pid_rejected(self):
        claimed=self.worker.copy(); claimed['created']=999
        with self.assertRaises(UpdateError): self.proc.bind_startup(object(),claimed)
        self.proc.close.assert_called_once_with(20)
    def test_unrelated_parent_rejected(self):
        self.proc.parent_pid.return_value=999
        with self.assertRaises(UpdateError): self.proc.bind_startup(object(),self.worker.copy())
    def test_other_image_rejected(self):
        self.worker['path']=r'C:\evil.exe'
        with self.assertRaises(UpdateError): self.proc.bind_startup(object(),self.worker.copy())
    def test_worker_older_than_launch_rejected(self):
        self.worker['created']=999
        with self.assertRaises(UpdateError): self.proc.bind_startup(object(),self.worker.copy())
    def test_owner_dies_during_binding(self):
        self.proc.alive.side_effect=[True,False]
        with self.assertRaises(UpdateError): self.proc.bind_startup(object(),self.worker.copy())
        self.proc.close.assert_called_once_with(20)
    def test_direct_process_supported(self):
        self.worker=self.parent.copy()
        self.assertEqual(self.proc.bind_startup(object(),self.parent.copy()),20)
        self.proc.parent_pid.assert_not_called()
    def test_source_mode_is_blocked(self):
        with patch('smart_tapo.update_windows.sys.frozen',False,create=True):
            with self.assertRaises(UpdateError): packaged_executable()
    def test_launch_never_shell_or_global_environment(self):
        before=os.environ.copy()
        with patch('smart_tapo.update_windows.subprocess.Popen') as popen:
            self.proc.launch(Path('C:/app/app.exe'),['--update-helper','safe'],hidden=True)
        args,kwargs=popen.call_args
        self.assertFalse(kwargs['shell']); self.assertTrue(kwargs['close_fds'])
        self.assertEqual(kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'],'1')
        self.assertEqual(dict(os.environ),before)
        self.assertEqual(args[0][1:],['--update-helper','safe'])
    def test_enumeration_failure_resumes_exact_owned_launch(self):
        import ctypes
        self.proc.k=Mock()
        self.proc.k.CreateToolhelp32Snapshot.return_value=ctypes.c_void_p(-1).value
        dll=Mock(); dll.NtSuspendProcess.return_value=0; dll.NtResumeProcess.return_value=0
        with patch('smart_tapo.update_windows.ctypes.WinDLL',return_value=dll):
            with self.assertRaisesRegex(UpdateError,'enumerate'):
                self.proc.kill_launch(object())
        dll.NtSuspendProcess.assert_called_once_with(10)
        dll.NtResumeProcess.assert_called_once_with(10)
        self.proc.open.assert_not_called()

    def test_snapshot_exception_resumes_exact_owned_launch(self):
        self.proc.k=Mock()
        self.proc.k.CreateToolhelp32Snapshot.side_effect=OSError('enumeration fault')
        dll=Mock(); dll.NtSuspendProcess.return_value=0; dll.NtResumeProcess.return_value=0
        with patch('smart_tapo.update_windows.ctypes.WinDLL',return_value=dll):
            with self.assertRaises(OSError): self.proc.kill_launch(object())
        dll.NtResumeProcess.assert_called_once_with(10)

    def test_visible_restart_not_hidden(self):
        with patch('smart_tapo.update_windows.subprocess.Popen') as popen:
            self.proc.launch(Path('C:/app/app.exe'),[],hidden=False)
        self.assertEqual(popen.call_args.kwargs['creationflags'],0)


if __name__=='__main__': unittest.main()
