from __future__ import annotations

import subprocess
import sys
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from smart_tapo.runtime import hidden_console_options
from smart_tapo.capture import Adb, CaptureError
from smart_tapo.config import Config


class SubprocessVisibilityTests(unittest.TestCase):
    def test_windows_uses_no_window_flag(self):
        with patch('smart_tapo.runtime.os.name','nt'), \
             patch('subprocess.CREATE_NO_WINDOW',0x08000000,create=True):
            self.assertEqual(hidden_console_options(),{'creationflags':0x08000000})

    def test_other_platforms_do_not_receive_windows_options(self):
        with patch('smart_tapo.runtime.os.name','posix'):
            self.assertEqual(hidden_console_options(),{})

    @unittest.skipUnless(os.name=='nt','Requires the real Windows console API')
    def test_real_windows_child_has_no_console_and_still_returns_stdout(self):
        code=('import ctypes; f=ctypes.windll.kernel32.GetConsoleWindow; '
              'f.restype=ctypes.c_void_p; print("hidden" if not f() else "visible")')
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
                              timeout=15,check=True,**hidden_console_options())
        self.assertEqual(result.stdout.strip(),'hidden')

    def test_adb_commands_hide_console_and_preserve_binary_io_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            config=replace(Config(),data_dir=Path(directory),adb_serial='emulator-5554')
            runner=Mock(return_value=subprocess.CompletedProcess([],0,b'\x89PNG\r\n\x1a\n',b''))
            adb=Adb(config,runner=runner)
            with patch('smart_tapo.capture.hidden_console_options',return_value={'creationflags':0x08000000}):
                self.assertEqual(adb.run('exec-out','screencap','-p'),b'\x89PNG\r\n\x1a\n')
                adb.run('connect','127.0.0.1:21503',selected=False)
            selected,unselected=runner.call_args_list
            self.assertEqual(selected.args[0],[config.adb_path,'-s','emulator-5554','exec-out','screencap','-p'])
            self.assertEqual(unselected.args[0],[config.adb_path,'connect','127.0.0.1:21503'])
            for call in runner.call_args_list:
                self.assertEqual(call.kwargs['creationflags'],0x08000000)
                self.assertEqual(call.kwargs['stdout'],subprocess.PIPE)
                self.assertEqual(call.kwargs['stderr'],subprocess.PIPE)
                self.assertEqual(call.kwargs['timeout'],config.adb_timeout_seconds)
                self.assertFalse(call.kwargs.get('shell',False))

    def test_memu_launcher_keeps_its_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            config=replace(Config(),data_dir=Path(directory),adb_serial='emulator-5554',memu_executable='MEmu.exe')
            runner=Mock(return_value=subprocess.CompletedProcess([],1,b'',b''))
            launcher=Mock()
            adb=Adb(config,runner=runner,launcher=launcher)
            stop=threading.Event();stop.set()
            with patch('smart_tapo.runtime.os.name','nt'), \
                 patch('subprocess.CREATE_NO_WINDOW',0x08000000,create=True):
                with self.assertRaises(CaptureError):adb.connect(stop)
            launcher.assert_called_once_with(['MEmu.exe'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':unittest.main()
