"""Native Tk updater tests; deliberately no desktop config/business services."""
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch,Mock
from smart_tapo.desktop_updates import UpdatesPanel
from smart_tapo import updates


class UpdatesGuiTests(unittest.TestCase):
    def setUp(self):
        from smart_tapo.smoke_window import install
        install()
        self.root=tk.Tk();self.root.withdraw()
        self.panel=UpdatesPanel(self.root)

    def tearDown(self):
        self.panel.close();self.root.destroy()

    def test_no_implicit_network_or_install(self):
        self.assertIsNone(self.panel.thread)
        self.assertEqual(str(self.panel.install_button['state']),'disabled')
        self.assertEqual(str(self.panel.download_button['state']),'disabled')

    def test_background_missing_key_message(self):
        with patch.object(self.panel.client,'check',side_effect=updates.UpdateError('No approved pinned signing key; installation disabled')):
            self.panel.check()
            deadline=time.monotonic()+3
            while self.panel.results.empty() and time.monotonic()<deadline:time.sleep(.01)
            self.panel.poll()
        self.assertIn('pinned signing key',self.panel.status.get())
        self.assertEqual(str(self.panel.install_button['state']),'disabled')

    def test_destroy_cancels_callbacks(self):
        self.panel.destroy()
        self.assertTrue(self.panel.closed)

    def test_closed_panel_cannot_check(self):
        self.panel.close()
        with patch.object(self.panel.client,'check') as check:
            self.panel.check()
            check.assert_not_called()

    def test_periodic_check_rearms_and_stops_without_implicit_network(self):
        with patch.object(self.panel,'check') as check:
            self.panel.start_periodic()
            first=self.panel.periodic_timer
            self.panel.start_periodic()
            self.assertEqual(first,self.panel.periodic_timer)
            self.panel._periodic_check()
            check.assert_called_once()
            self.assertIsNotNone(self.panel.periodic_timer)
            self.panel.close()
            self.panel._periodic_check()
            check.assert_called_once()
            self.assertIsNone(self.panel.periodic_timer)

    def _downloaded(self,directory):
        self.panel.downloaded=Path(directory)/'candidate.exe'
        self.panel.downloaded.write_bytes(b'synthetic-not-executed')
        self.panel.release=(SimpleNamespace(version=45),'unused')
        self.panel.install_request=Mock(return_value=True)

    def test_busy_or_unknown_never_prompts_or_installs(self):
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',b'x'*32),patch('smart_tapo.desktop_updates.messagebox.askyesno') as confirm:
            self._downloaded(directory)
            for state in (None,{},dict(workers=False)):
                self.panel.state_getter=lambda:state
                self.panel.install()
            confirm.assert_not_called()
            self.panel.install_request.assert_not_called()

    def test_rechecks_business_state_after_confirmation(self):
        idle={k:False for k in updates.BUSINESS_FLAGS}
        busy=dict(idle,listener=True)
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',b'x'*32),patch('smart_tapo.desktop_updates.messagebox.askyesno',return_value=True):
            self._downloaded(directory)
            self.panel.state_getter=Mock(side_effect=[idle,busy])
            self.panel.install()
            self.panel.install_request.assert_not_called()
            self.assertFalse(self.panel.installing)

    def test_stopped_but_interrupted_history_names_the_actual_blocker(self):
        state={k:False for k in updates.BUSINESS_FLAGS}
        state['interrupted_round']=True
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',b'x'*32),patch('smart_tapo.desktop_updates.messagebox.askyesno') as confirm:
            self._downloaded(directory)
            self.panel.state_getter=lambda:state
            self.panel.install()
            self.assertIn('interrupted_round',self.panel.status.get())
            self.assertNotIn('บันทึกการตั้งค่า หยุดงานทั้งหมด',self.panel.status.get())
            confirm.assert_not_called();self.panel.install_request.assert_not_called()

    def test_multiple_blockers_are_specific_and_unknown_is_not_fabricated(self):
        state={k:False for k in updates.BUSINESS_FLAGS}
        state.update(listener=True,webhook=True)
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',b'x'*32),patch('smart_tapo.desktop_updates.messagebox.askyesno') as confirm:
            self._downloaded(directory)
            self.panel.state_getter=lambda:state
            self.panel.install()
            self.assertIn('listener',self.panel.status.get())
            self.assertIn('webhook',self.panel.status.get())
            self.assertNotIn('interrupted_round',self.panel.status.get())
            self.panel.state_getter=lambda:None
            self.panel.install()
            self.assertIn('ตรวจสถานะไม่ได้',self.panel.status.get())
            self.assertNotIn('interrupted_round',self.panel.status.get())
            confirm.assert_not_called();self.panel.install_request.assert_not_called()

    def test_missing_key_blocks_even_confirmed_idle_install(self):
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',None),patch('smart_tapo.desktop_updates.messagebox.askyesno') as confirm:
            self._downloaded(directory)
            self.panel.state_getter=lambda:{k:False for k in updates.BUSINESS_FLAGS}
            self.panel.install()
            confirm.assert_not_called(); self.panel.install_request.assert_not_called()

    def test_normal_startup_ack_branch_uses_owned_store_after_visible_gui(self):
        from smart_tapo import desktop
        events=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Mock(); root.deiconify.side_effect=lambda:events.append('visible')
            root.update_idletasks.side_effect=lambda:events.append('idle')
            app=Mock(); app._active.return_value=False
            store=SimpleNamespace(root=Path(directory))
            def ack(*args): events.append('ack')
            with patch('sys.argv',['desktop','--update-restart','--update-startup-plan',str(Path(directory)/'plan.json'),'--update-token','a'*64]),patch.object(desktop.tk,'Tk',return_value=root),patch.object(desktop,'DesktopStore',return_value=store),patch.object(desktop,'DesktopApp',return_value=app),patch.object(desktop,'ProcessLock'),patch('smart_tapo.update_install.acknowledge_startup',side_effect=ack) as acknowledge:
                self.assertEqual(desktop.main(),0)
            self.assertEqual(events,['visible','idle','ack'])
            acknowledge.assert_called_once()
            app.startup_panel.begin.assert_not_called()
            app.updates_panel.check.assert_not_called()

    def test_bad_update_cli_rejected_before_store(self):
        from smart_tapo import desktop
        cases=[['--update-restart','--windows-login'],['--update-token','a'*64],['--update-startup-plan','missing']]
        for args in cases:
            with patch('sys.argv',['desktop',*args]),patch.object(desktop,'DesktopStore') as store:
                with self.assertRaises(SystemExit): desktop.main()
                store.assert_not_called()

    def test_helper_dispatch_does_not_import_gui_or_config(self):
        import runpy,sys
        fake=SimpleNamespace(apply_update=Mock(return_value=2))
        original=__import__
        seen=[]
        def guarded(name,*args,**kw):
            seen.append(name)
            if name in ('smart_tapo.desktop','smart_tapo.config','tkinter'):
                raise AssertionError('Helper imported GUI/config')
            return original(name,*args,**kw)
        entry=Path(__file__).resolve().parents[1]/'entrypoints/desktop.py'
        with patch('sys.argv',[str(entry),'--update-helper','owned-plan','--update-token','a'*64]),patch.dict(sys.modules,{'smart_tapo.update_install':fake}),patch('builtins.__import__',side_effect=guarded):
            with self.assertRaises(SystemExit) as result: runpy.run_path(str(entry),run_name='__main__')
        self.assertEqual(result.exception.code,2)
        fake.apply_update.assert_called_once_with('owned-plan','a'*64)
        self.assertNotIn('smart_tapo.desktop',seen)

    def test_only_confirmed_idle_request_reaches_installer(self):
        idle={k:False for k in updates.BUSINESS_FLAGS}
        with tempfile.TemporaryDirectory() as directory,patch('sys.frozen',True,create=True),patch.object(updates,'PINNED_PUBLIC_KEY',b'x'*32),patch('smart_tapo.desktop_updates.messagebox.askyesno',return_value=True):
            self._downloaded(directory)
            self.panel.state_getter=lambda:idle
            self.panel.install()
            self.panel.install_request.assert_called_once()
            self.assertTrue(self.panel.installing)
