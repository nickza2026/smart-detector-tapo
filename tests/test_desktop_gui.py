from __future__ import annotations

import tempfile
import os
import subprocess
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from smart_tapo.desktop import DesktopApp, main
from smart_tapo.config import Rect
from smart_tapo.desktop_calibration import is_ready
from smart_tapo.desktop_storage import DesktopStore
from tests.test_alignment_regression import textured_reference


class FakeController:
    video_storage_mode='ram'
    def __init__(self):
        self.active=False
        self.worker_calls=0
        self.stops=0
        self.closed=False
    def is_running(self,role):return self.active
    def snapshot(self):return {r:{'state':'running' if self.active else 'stopped','pid':None} for r in ('capture','worker')}
    def drain_messages(self):return []
    def start_worker(self,*args):self.worker_calls+=1
    def stop_all(self):self.stops+=1;self.active=False
    def close(self):
        if self.active:raise RuntimeError('Stop roles before transport close')
        self.closed=True


class DesktopGuiTests(unittest.TestCase):
    def test_notify_history_button_is_visible_at_minimum_size_on_every_tab(self):
        self.root.deiconify()
        for width,height in ((850,650),(1000,678),(1000,820)):
            self.root.geometry(f'{width}x{height}')
            for tab in self.app.notebook.tabs():
                with self.subTest(size=(width,height),tab=tab):
                    self.app.notebook.select(tab)
                    self.root.update()
                    button=self.app.notify_history_button
                    self.assertTrue(button.winfo_ismapped())
                    x=button.winfo_rootx()-self.root.winfo_rootx()
                    y=button.winfo_rooty()-self.root.winfo_rooty()
                    self.assertGreaterEqual(button.winfo_width(),100)
                    self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
                    self.assertLessEqual(x+button.winfo_width(),self.root.winfo_width())
                    self.assertLessEqual(y+button.winfo_height(),self.root.winfo_height())

    def test_endpoint_metadata_mode_skips_config_gui_and_registration(self):
        import json
        report=Path(self.temp.name)/'endpoint-metadata.json'
        for mapping,code in (({'Synthetic':'{12345678-1234-1234-1234-123456789abc}'},0),({},1)):
            with self.subTest(available=bool(mapping)), \
                    patch('sys.argv',['SmartTapo','--endpoint-metadata-report',str(report)]), \
                    patch('smart_tapo.audio_endpoint.render_endpoint_ids',return_value=mapping), \
                    patch('smart_tapo.desktop.DesktopStore') as store, \
                    patch('smart_tapo.desktop.tk.Tk') as root:
                self.assertEqual(main(),code)
                store.assert_not_called();root.assert_not_called()
                payload=json.loads(report.read_text(encoding='utf-8'))
                self.assertEqual(payload['unique_render_endpoints'],len(mapping))
                self.assertFalse(payload['capture_started'])
                self.assertFalse(payload['startup_registered'])
                self.assertNotIn('Synthetic',report.read_text(encoding='utf-8'))

    def test_notify_manual_test_is_independent_of_worker_and_notify_flag(self):
        import threading
        started,release=threading.Event(),threading.Event()
        def sender(*args):
            started.set();release.wait(3);return 'success'
        panel=self.app.notify_test_panel
        panel.sender=Mock(side_effect=sender)
        panel.confirm=Mock(return_value=True)
        self.addCleanup(panel.close)
        self.app.notify_key.set('synthetic-key')
        self.controller.active=True
        self.root.after_cancel(self.app.timer);self.app._poll()
        self.assertFalse(panel.button.instate(['disabled']))
        try:
            panel.button.invoke()
            self.assertTrue(started.wait(1))
            self.root.after_cancel(self.app.timer);self.app._poll()
            self.assertTrue(panel.button.instate(['disabled']))
            panel.start()
            self.assertEqual(panel.sender.call_count,1)
            self.assertTrue(self.controller.active)
            self.assertEqual(self.controller.stops,0)
            self.assertFalse(self.app.notify.get())
            self.assertFalse(self.app.config.notify_enabled)
        finally:
            release.set()
            if panel._future is not None:panel._future.result(timeout=3)
            panel.poll()

    def test_close_waits_for_owned_notify_request_without_claiming_retraction(self):
        import threading
        started,release=threading.Event(),threading.Event()
        def sender(*args):
            started.set();release.wait(3);return 'ambiguous'
        panel=self.app.notify_test_panel
        panel.sender=Mock(side_effect=sender);panel.confirm=Mock(return_value=True)
        self.addCleanup(panel.close)
        self.app.notify_key.set('synthetic-key')
        panel.start();self.assertTrue(started.wait(1))
        try:
            with patch.object(self.root,'destroy') as destroy:
                self.app.close()
                self.root.after_cancel(self.app.timer);self.app._poll()
                destroy.assert_not_called()
                self.assertIn('เรียกคืนไม่ได้',panel.status.get())
                release.set();panel._future.result(timeout=3)
                self.root.after_cancel(self.app.timer);self.app._poll()
                destroy.assert_called_once()
                self.assertEqual(panel.sender.call_count,1)
        finally:
            release.set()
            if panel._future is not None:panel._future.result(timeout=3)
            panel.poll()

    def test_device_preview_is_explicit_and_blocked_when_capture_is_active(self):
        self.controller.active=True
        with patch('smart_tapo.page_signature.device_preview') as preview:
            self.app.preview_adb_device()
            preview.assert_not_called()
        self.errors.assert_called_once()

    def test_page_collection_is_explicit_and_rejects_active_capture(self):
        self.controller.active=True
        with patch('smart_tapo.page_signature.collect_page_signature') as collect:
            self.app.collect_page_signature('live')
            collect.assert_not_called()
        self.errors.assert_called_once()

    def test_refueling_menu_does_not_start_listening_or_create_new_database(self):
        self.assertEqual(self.app.notebook.tab(self.app.refueling_panel,'text'),'ประวัติการเติม')
        self.assertFalse((self.app.config.data_dir/'refueling'/'refueling.sqlite3').exists())
        self.assertFalse(self.app.audio_panel.is_running)
    def setUp(self):
        if os.name=='nt':
            from smart_tapo import smoke_window
            smoke_window.install()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.withdraw()
        self.addCleanup(lambda:self.root.destroy() if self.root.tk.call('info','commands',self.root._w) else None)
        self.store=DesktopStore(Path(self.temp.name)/'config.json')
        self.controller=FakeController()
        self.app=DesktopApp(self.root,self.store,self.controller)
        self.addCleanup(self.app._notify_history_maintenance.close)
        self.addCleanup(lambda:self.root.after_cancel(self.app.timer))
        self.errors=self.enterContext(patch('smart_tapo.desktop.messagebox.showerror'))
        self.confirm=self.enterContext(patch('smart_tapo.desktop.messagebox.askyesno',return_value=True))

    def calibrate(self):
        self.app.load_image(textured_reference(self.app.config))
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())

    def test_small_settings_window_scrolls_rtsp_interval_and_reveals_save(self):
        self.root.deiconify()
        self.app.notebook.select(self.app.settings)
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets=list(descendants(self.app.settings))
        clock=next(w for w in widgets if 'textvariable' in w.keys()
                   and str(w.cget('textvariable'))==str(self.app.form['capture_interval_seconds']))
        save=next(w for w in widgets if 'text' in w.keys() and w.cget('text')=='บันทึกการตั้งค่า')
        canvas=self.app.settings_canvas
        for height in (650,820):
            self.root.geometry(f'1000x{height}')
            self.root.update()
            canvas.yview_moveto(1)
            self.root.update()
            picker=self.app.storage_picker
            self.assertTrue(picker.winfo_ismapped(),str(picker.grid_info()))
            self.assertGreaterEqual(picker.winfo_rooty(),canvas.winfo_rooty())
            self.assertLessEqual(picker.winfo_rooty()+picker.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())
            self.assertGreaterEqual(clock.winfo_rooty(),canvas.winfo_rooty())
            self.assertLessEqual(clock.winfo_rooty()+clock.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())
            save.event_generate('<FocusIn>')
            self.root.update()
            self.assertGreaterEqual(save.winfo_rooty(),canvas.winfo_rooty())
            self.assertLessEqual(save.winfo_rooty()+save.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())

    def test_storage_picker_maps_after_tab_return_and_repeated_show(self):
        body=self.app.settings_body
        draft=str(Path(self.temp.name)/'unapplied-draft')
        self.app.form['data_dir'].set(draft)
        self.root.deiconify()
        for _ in range(3):
            self.app.notebook.select(self.app.audio_panel);self.root.update()
            self.root.deiconify()
            self.app.notebook.select(self.app.settings);self.root.update()
            self.app.settings_canvas.yview_moveto(1);self.root.update()
            self.assertTrue(self.app.storage_picker.winfo_ismapped(),str(dict(
                body=self.app.settings_body.winfo_ismapped(),
                canvas=self.app.settings_canvas.winfo_ismapped())))
            self.assertIs(self.app.settings_body,body)
            self.assertEqual(self.app.form['data_dir'].get(),draft)
            self.assertFalse(Path(draft).exists())

    def test_half_second_rtsp_preset_is_explicit_without_memu_clock(self):
        import json
        before=self.app.config
        self.assertFalse(before.clock_window_enabled)
        self.app._half_second_preset()
        self.assertEqual(self.app.config,before)
        self.assertFalse(self.app.clock_window.get())
        self.assertEqual(self.app.form['clock_check_interval_seconds'].get(),'2')
        self.assertTrue(self.app.save_settings())
        saved=json.loads(self.store.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['capture_interval_seconds'],.5)
        self.assertEqual(saved['clock_check_interval_seconds'],2)
        self.assertFalse(saved['clock_window_enabled'])
        self.assertEqual(saved['stuck_minimum_seconds'],9)

    def test_auto_config_and_missing_reference_are_guided_without_cli(self):
        self.assertTrue(self.store.config_path.exists())
        self.assertFalse(self.app.config.notify_enabled)
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.assertEqual(self.app.notebook.select(),str(self.app.calibration))
        self.errors.assert_called_once()

    def test_empty_confirmation_is_required_then_camera_change_invalidates_it(self):
        self.app.load_image(textured_reference(self.app.config))
        self.assertFalse(self.app.save_reference())
        self.assertFalse(self.app.config.baseline_path.exists())
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())
        self.assertTrue(is_ready(self.app.config,self.store.root))
        self.app.form['rtsp_url'].set('rtsp://127.0.0.1:8554/changed-camera')
        self.assertTrue(self.app.save_settings())
        self.assertFalse(is_ready(self.app.config,self.store.root))

    def test_partial_reference_save_cannot_silently_start_worker(self):
        self.calibrate()
        image=textured_reference(self.app.config)
        image.putpixel((350,280),(255,255,255))
        self.app.load_image(image)
        self.app.confirm_empty.set(True)
        with patch('smart_tapo.desktop.mark_ready',side_effect=OSError('simulated-full-disk')):
            self.assertFalse(self.app.save_reference())
        self.assertFalse(is_ready(self.app.config,self.store.root))
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)

    def test_featureless_reference_is_rejected_in_setup_not_silently_ready(self):
        from PIL import Image
        self.app.load_image(Image.new('RGB',self.app.config.expected_size,'gray'))
        self.app.confirm_empty.set(True)
        self.assertFalse(self.app.save_reference())
        self.assertFalse(self.app.config.baseline_path.exists())
        self.assertFalse(is_ready(self.app.config,self.store.root))

    def test_numeric_input_error_does_not_echo_arbitrary_pasted_text(self):
        self.app.form['stuck_consecutive_frames'].set('synthetic-private-input')
        self.assertFalse(self.app.save_settings())
        self.assertNotIn('synthetic-private-input',self.app.status.get())

    def test_credentials_are_not_added_to_plain_config(self):
        self.app.notify_key.set('synthetic-private-key')
        with patch.object(self.store,'save_notify_key') as saved:
            self.assertTrue(self.app.save_settings())
            saved.assert_called_once_with('synthetic-private-key',self.app.config.notify_api_url,
                                          self.app.config.camera_id)
        self.assertNotIn('synthetic-private',self.store.config_path.read_text(encoding='utf-8'))
        self.assertEqual(self.app._health_credentials,('synthetic-private-key',self.app.config.notify_api_url))

    def test_notify_url_and_importance_are_saved_in_config(self):
        self.app.notify_base.set('https://notify.invalid/')
        self.app.notify_importance.set('critical')
        self.assertTrue(self.app.save_settings())
        self.assertEqual(self.app.config.notify_api_url,'https://notify.invalid/')
        self.assertEqual(self.app.config.notify_importance,'critical')
        self.app.notify_base.set('http://notify.invalid')
        self.assertFalse(self.app.save_settings())
        self.assertEqual(self.app.config.notify_api_url,'https://notify.invalid/')

    def test_enabling_notifications_requires_notify_key(self):
        self.app.notify.set(True)
        with patch.object(self.store,'save_notify_key') as saved:
            self.assertFalse(self.app.save_settings())
            saved.assert_not_called()
        self.assertFalse(self.app.config.notify_enabled)

    def test_selection_stays_in_video_and_requires_confirmation_again(self):
        self.calibrate()
        self.app._drag_begin(SimpleNamespace(x=20,y=20))
        self.app._drag_end(SimpleNamespace(x=3000,y=3000))
        roi=self.app.selection;video=self.app.config.video
        self.assertGreaterEqual(roi.x,video.x)
        self.assertLessEqual(roi.x+roi.width,video.x+video.width)
        self.assertLessEqual(roi.y+roi.height,video.y+video.height)
        self.assertFalse(self.app.confirm_empty.get())

    def test_unsaved_roi_cannot_start_worker_with_old_calibration(self):
        self.calibrate()
        self.app._drag_begin(SimpleNamespace(x=20,y=20))
        self.app._drag_end(SimpleNamespace(x=150,y=110))
        self.assertNotEqual(self.app.selection,self.app.config.roi)
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.assertEqual(self.app.notebook.select(),str(self.app.calibration))

    def test_new_reference_must_not_reuse_previous_ready_state(self):
        self.calibrate()
        self.app.load_image(textured_reference(self.app.config))
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.assertEqual(self.app.notebook.select(),str(self.app.calibration))

    def test_exact_roi_is_preview_only_until_empty_reference_is_confirmed(self):
        self.calibrate()
        config_before=self.store.config_path.read_bytes()
        reference_before=self.app.config.baseline_path.read_bytes()
        for key,value in {'x':249,'y':272,'width':100,'height':80}.items():
            self.app.roi_values[key].set(str(value))
        self.assertTrue(self.app.apply_roi_coordinates())
        self.assertEqual(self.app.selection,Rect(249,272,100,80))
        self.assertFalse(self.app.confirm_empty.get())
        self.assertEqual(self.store.config_path.read_bytes(),config_before)
        self.assertEqual(self.app.config.baseline_path.read_bytes(),reference_before)
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.app.confirm_empty.set(True)
        self.assertTrue(self.app.save_reference())
        self.assertEqual(self.store.load_or_create().roi,Rect(249,272,100,80))
        self.assertTrue(is_ready(self.app.config,self.store.root))
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,1)

    def test_unapplied_coordinate_text_blocks_save_and_worker(self):
        self.calibrate()
        before=self.store.config_path.read_bytes()
        self.app.roi_values['height'].set('80')
        self.app.confirm_empty.set(True)
        self.assertFalse(self.app.save_reference())
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,0)
        self.assertEqual(self.store.config_path.read_bytes(),before)

    def test_coordinate_validation_does_not_echo_input_or_change_files(self):
        self.calibrate()
        selection=self.app.selection
        before=self.store.config_path.read_bytes()
        for key,value in [('x','synthetic-private-input'),('y','-1'),('width','1'),('height','99999')]:
            with self.subTest(key=key):
                for name,var in self.app.roi_values.items():
                    var.set(str(getattr(selection,name)))
                self.app.roi_values[key].set(value)
                self.assertFalse(self.app.apply_roi_coordinates())
                self.assertEqual(self.app.selection,selection)
                self.assertEqual(self.store.config_path.read_bytes(),before)
                self.assertNotIn('synthetic-private-input',self.app.status.get())

    def test_roi_coordinates_follow_drag_and_cannot_apply_during_active_work(self):
        self.calibrate()
        self.app._drag_begin(SimpleNamespace(x=20,y=20))
        self.app._drag_end(SimpleNamespace(x=150,y=110))
        self.assertTrue(self.app._roi_fields_match_selection())
        selection=self.app.selection
        self.controller.active=True
        self.app.roi_values['width'].set('100')
        self.assertFalse(self.app.apply_roi_coordinates())
        self.assertEqual(self.app.selection,selection)

    def test_saved_reference_can_reopen_without_reconfirming(self):
        self.calibrate()
        self.app.load_image(textured_reference(self.app.config),saved=True)
        self.assertFalse(self.app.confirm_empty.get())
        self.assertFalse(self.app._calibration_pending())
        self.app.start_role('worker')
        self.assertEqual(self.controller.worker_calls,1)

    def test_calibration_save_controls_are_not_squeezed_at_minimum_window_size(self):
        self.calibrate()
        self.app.notebook.select(self.app.calibration)
        self.root.deiconify()
        self.root.update()
        for widget in (self.app.apply_roi_button,self.app.save_ref_button,self.app.confirm_box):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_height(),widget.winfo_reqheight())

    def test_preview_resize_preserves_confirmation_and_unapplied_coordinates(self):
        self.calibrate()
        self.app._render_preview()
        self.assertTrue(self.app.confirm_empty.get())
        self.assertFalse(self.app._calibration_pending())
        self.app.roi_values['height'].set('80')
        self.app._render_preview()
        self.assertEqual(self.app.roi_values['height'].get(),'80')
        self.assertTrue(self.app._calibration_pending())

    def test_close_requests_graceful_stop_and_cancels_camera_setup(self):
        self.controller.active=True
        self.app.close()
        self.assertEqual(self.controller.stops,1)
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.operation_stop.is_set())

    def test_normal_ui_exposes_no_adb_discovery_or_windows_audio_device(self):
        self.assertFalse(hasattr(self.app, 'device_picker'))
        self.assertNotIn('adb_serial', self.app.form)
        self.assertFalse(hasattr(self.app.audio_panel, 'device_box'))
        self.assertFalse(hasattr(self.app.audio_panel, 'probe'))
        with patch.object(self.app, 'find_devices') as discovery, \
             patch('smart_tapo.desktop.subprocess.run') as runner:
            self.app.audio_panel.poll()
            self.app.startup_panel.poll()
            discovery.assert_not_called()
            runner.assert_not_called()
        self.assertIn('RTSP', self.app.audio_panel.listener_panel.start_button.cget('text'))

    def test_save_data_folder_exits_through_real_main_finally_without_crash(self):
        import json
        target=Path(self.temp.name)/'new-data-after-restart'
        old=self.app.config.data_dir
        self.app.form['data_dir'].set(str(target))
        self.root.after(30,self.app.save_settings)
        watchdog=self.root.after(5000,self.root.destroy)
        self.root.bind('<Destroy>',lambda event:self.root.after_cancel(watchdog) if event.widget is self.root else None,add='+')
        with patch('sys.argv',['SmartTapo']),patch('smart_tapo.desktop.tk.Tk',return_value=self.root), \
             patch('smart_tapo.desktop.DesktopStore',return_value=self.store), \
             patch('smart_tapo.desktop.DesktopApp',return_value=self.app), \
             patch('smart_tapo.desktop.messagebox.askyesno',return_value=True), \
             patch('smart_tapo.startup.manage_entry') as startup_entry:
            self.assertEqual(main(),0)
        self.assertEqual(Path(json.loads(self.store.config_path.read_text(encoding='utf-8'))['data_dir']),target.resolve())
        self.assertEqual(self.app.config.data_dir,old)
        self.assertTrue(self.controller.closed)
        startup_entry.assert_not_called()

    def test_audio_close_is_safe_after_normal_close_and_destroy(self):
        runtime=self.app.audio_panel.listener_panel.runtime
        with patch.object(runtime,'close',wraps=runtime.close) as close:
            self.app.audio_panel.close()
            self.root.destroy()
            self.app.audio_panel.close()
            close.assert_called_once()

    def test_audio_first_close_after_unexpected_destroy_still_closes_runtime(self):
        runtime=self.app.audio_panel.listener_panel.runtime
        with patch.object(runtime,'close',wraps=runtime.close) as close:
            self.root.destroy()
            self.app.audio_panel.close()
            close.assert_called_once()

    def test_unexpected_window_failure_still_stops_managed_jobs(self):
        fake_root=Mock()
        fake_root.mainloop.side_effect=RuntimeError('synthetic-window-failure')
        self.controller.active=True
        with patch('sys.argv',['SmartTapo']), patch('smart_tapo.desktop.tk.Tk',return_value=fake_root), \
             patch('smart_tapo.desktop.DesktopStore',return_value=self.store), \
             patch('smart_tapo.desktop.DesktopApp',return_value=self.app):
            self.assertEqual(main(),1)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.stops,1)
        self.assertTrue(self.controller.closed)


if __name__=='__main__':unittest.main()
