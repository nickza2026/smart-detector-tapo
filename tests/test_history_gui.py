import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from smart_tapo.desktop import DesktopApp
from smart_tapo.desktop_storage import DesktopStore
from smart_tapo.history import HistoryEvent, HistoryPage
from tests.test_desktop_gui import FakeController
from tests import test_history_reader as reader_fixture
from smart_tapo.history import get_images,list_events


class HistoryGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.controller=FakeController()
        self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.root.after_cancel,self.app.timer)
        self.panel=self.app.history_panel
        self.addCleanup(self.panel.close)
        self.errors=self.enterContext(patch('smart_tapo.desktop.messagebox.showerror'))

    def test_history_menu_configurable_day_and_raw_five_minutes(self):
        self.assertEqual(self.app.notebook.tab(self.panel,'text'),'ประวัติการหยอดแบงค์')
        self.assertEqual(self.app.config.deposit_history_hours,24)
        self.assertEqual(self.app.config.rolling_frames_minutes,5)
        self.app.form['deposit_history_hours'].set('48')
        self.assertTrue(self.app.save_settings())
        self.assertEqual(self.store.load_or_create().deposit_history_hours,48)
        self.assertEqual(self.app.config.rolling_frames_minutes,5)
        self.app.form['deposit_history_hours'].set('nan')
        self.assertFalse(self.app.save_settings())
        self.assertEqual(self.app.config.deposit_history_hours,48)

    def test_latest_rows_selection_and_missing_images_are_explicit(self):
        self.panel.poll(False)
        now=time.time()
        newest=HistoryEvent('b','normal_visual',now,now,None,now,2)
        older=HistoryEvent('a','incomplete',now-100,now-99,None,now-98,0)
        self.panel._render(HistoryPage((newest,older)))
        self.assertEqual(self.panel.tree.get_children(),('b','a'))
        self.panel.tree.selection_set('a')
        self.panel._render(HistoryPage((newest,older)))
        self.assertEqual(self.panel.tree.selection(),('a',))
        self.panel._show_viewer('a',())
        self.assertIn('ไม่มีภาพ',self.panel.status.get())
        self.assertIsNone(self.panel.viewer)

    def test_start_stop_web_remembers_port_without_restarting_worker(self):
        self.controller.active=True
        self.panel.port_var.set('9099')
        server=Mock(running=True,port=9099,url='http://127.0.0.1:9099/?access=synthetic')
        with patch('smart_tapo.history_web.HistoryWebServer',return_value=server), \
             patch('smart_tapo.desktop_history.webbrowser.open') as browser:
            self.assertTrue(self.panel.start_web())
            server.start.assert_called_once_with(9099)
            self.assertEqual(self.store.load_or_create().history_web_port,9099)
            self.assertEqual(self.controller.stops,0)
            self.assertNotIn('synthetic',self.panel.web_status.get())
            self.panel.open_web();browser.assert_called_once_with(server.url)
            self.panel.stop_web();server.stop.assert_called_once()

    def test_bad_port_never_starts_server_and_busy_port_is_safe(self):
        self.panel.port_var.set('0')
        with patch('smart_tapo.history_web.HistoryWebServer') as factory:
            self.assertFalse(self.panel.start_web());factory.assert_not_called()
        self.panel.port_var.set('8080')
        server=Mock(running=False);server.start.side_effect=OSError('busy')
        with patch('smart_tapo.history_web.HistoryWebServer',return_value=server):
            self.assertFalse(self.panel.start_web())
        self.assertIn('เปิดเว็บไม่ได้',self.panel.web_status.get())

    def test_no_history_or_web_autostart_and_close_stops_web(self):
        self.assertFalse(self.panel.pending)
        self.assertIsNone(self.panel.server)
        self.panel.server=Mock(running=True)
        self.app.close()
        self.panel.server.stop.assert_called_once()
        self.assertTrue(self.panel.closed)

    def test_history_controls_fit_the_minimum_window(self):
        self.root.geometry('850x650')
        self.app.notebook.select(self.panel)
        self.root.deiconify()
        self.root.update_idletasks()
        status_label=self.panel.winfo_children()[-1]
        self.assertGreater(status_label.winfo_height(),1)
        self.assertLessEqual(status_label.winfo_y()+status_label.winfo_height(),self.panel.winfo_height())
        self.assertGreater(self.panel.tree.winfo_height(),40)

    def test_full_frame_viewer_opens_png_and_does_not_leave_previous_image_on_missing_event(self):
        fixture=reader_fixture.HistoryReaderTests('test_empty_history_never_creates_database_or_directory')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.now=time.time()
        event_id=fixture.event(1)
        fixture.image(event_id)
        self.app.config=fixture.config
        self.panel._render(list_events(self.app.config))
        self.panel._show_viewer(event_id,get_images(self.app.config,event_id))
        self.assertIsNotNone(self.panel.photo)
        self.assertEqual(self.panel.photo.width(),4)
        self.panel._show_viewer('missing',())
        self.assertIsNone(self.panel.viewer)
        self.assertIsNone(self.panel.photo)


if __name__=='__main__':unittest.main()
