"""Native Tk tests with fake listener only; run through the offscreen runner."""
from pathlib import Path
import tkinter as tk
import unittest
from concurrent.futures import Future
from dataclasses import replace
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from smart_tapo.config import Config
from smart_tapo.desktop_listener import GENERIC_ERROR, ListenerPanel,error_text


DEVICE = {'index': 2, 'name': 'Synthetic output', 'rate': 48000, 'channels': 2}


class FakeListener:
    def __init__(self):
        self.is_running = False
        self.calls = []
        self.events = []
        self.polls = 0
        self.failure = None

    def start(self, config, device, path, *, consent=False):
        if self.failure:
            raise self.failure
        self.calls.append(('start', config, device, path, consent))
        self.is_running = True

    def stop(self):
        self.calls.append(('stop',))

    def close(self):
        self.calls.append(('close',))

    def poll(self):
        self.polls += 1
        events, self.events = self.events, []
        if any(e['kind'] in ('finished', 'error') for e in events):
            self.is_running = False
        return events


class ListenerRetryTextTests(unittest.TestCase):
    def test_fast_retry_status_does_not_misreport_fractional_delay_as_thirty_seconds(self):
        for delay in (0,.25,.5,1,2):
            with self.subTest(delay=delay):
                status=Mock()
                panel=SimpleNamespace(_prepare_future=None,closing=False,_stopping=False,_active=False,
                    status=status,_buttons=Mock(),runtime=SimpleNamespace(poll=lambda:[
                        {'kind':'listener_retry','retry_seconds':delay,'code':'live_check_unavailable'}]))
                ListenerPanel.poll(panel)
                self.assertIn('ใน '+format(delay,'g')+' วินาที',status.set.call_args.args[0])


class ListenerPanelTests(unittest.TestCase):
    def test_specific_stop_reason_and_safe_timing_are_visible_without_private_text(self):
        message=error_text(dict(code='audio_clock_discontinuity',diagnostics=dict(
            lag_seconds=2.872,read_seconds=3.,work_seconds=.04,max_work_seconds=.12,
            account='PRIVATE',check_seconds=float('nan'),wall_seconds=float('inf'))))
        self.assertIn('[audio_clock_discontinuity]',message)
        self.assertIn('lag=2.87s',message)
        self.assertIn('read=3.00s',message)
        self.assertNotIn('PRIVATE',message)
        self.assertNotIn('nan',message);self.assertNotIn('inf',message)
        self.assertEqual(error_text(dict(code='PRIVATE exception',diagnostics={'lag_seconds':2})),GENERIC_ERROR)

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.runtime = FakeListener()
        self.config = Config(data_dir=Path('synthetic-not-created'))
        self.device = dict(DEVICE)
        self.other = False
        self.config_calls = 0
        self.config_failure = False
        self.panel = ListenerPanel(self.root, self.get_config, lambda: self.device,
                                   runtime=self.runtime, other_running=lambda: self.other)
        self.panel.pack(fill='both', expand=True)

    def get_config(self):
        self.config_calls += 1
        if self.config_failure:
            raise ValueError('PRIVATE secret path')
        return self.config

    def ready(self):
        self.panel.profile_path.set('private-profile.json')
        self.panel.consent.set(True)

    def test_prepared_profile_one_click_binds_without_manual_fields_or_listening(self):
        future=Future()
        with patch('smart_tapo.desktop_listener.ThreadPoolExecutor') as executor:
            executor.return_value.submit.return_value=future
            self.panel.profile_path.set('previous-valid.json')
            self.panel.consent.set(True)
            self.panel.use_prepared_profile()
            self.assertTrue(self.panel.is_running)
            self.assertEqual(self.panel.profile_path.get(),'')
            self.assertFalse(self.panel.consent.get())
            self.assertEqual(self.runtime.calls,[])
            future.set_result(Path('prepared-for-camera.json'))
            self.panel.poll()
            self.assertEqual(self.panel.profile_path.get(),'prepared-for-camera.json')
            self.assertFalse(self.panel.is_running)
            self.assertFalse(self.panel.consent.get())
            self.assertEqual(self.runtime.calls,[])

    def test_failed_prepared_profile_never_reuses_previous_valid_path(self):
        future=Future()
        with patch('smart_tapo.desktop_listener.ThreadPoolExecutor') as executor:
            executor.return_value.submit.return_value=future
            self.panel.profile_path.set('previous-valid.json')
            self.panel.use_prepared_profile()
            future.set_exception(ValueError('PRIVATE account path'))
            self.panel.poll()
            self.assertEqual(self.panel.profile_path.get(),'')
            self.assertNotIn('PRIVATE',self.panel.status.get())
            self.assertEqual(str(self.panel.start_button['state']),'disabled')
            self.assertEqual(self.runtime.calls,[])

    def test_prepared_profile_scope_change_discards_completed_selection(self):
        future=Future()
        with patch('smart_tapo.desktop_listener.ThreadPoolExecutor') as executor:
            executor.return_value.submit.return_value=future
            self.panel.use_prepared_profile()
            self.config=replace(self.config,camera_id='different-camera')
            future.set_result(Path('old-camera-profile.json'))
            self.panel.poll()
            self.assertEqual(self.panel.profile_path.get(),'')
            self.assertFalse(self.panel.is_running)
            self.assertEqual(self.runtime.calls,[])

    def test_close_owns_running_preparation_until_poll_and_never_starts_audio(self):
        future=Future();future.set_running_or_notify_cancel()
        with patch('smart_tapo.desktop_listener.ThreadPoolExecutor') as executor:
            executor.return_value.submit.return_value=future
            self.panel.use_prepared_profile()
            self.panel.close()
            self.assertTrue(self.panel.is_running)
            future.set_result(Path('prepared.json'))
            self.panel.poll()
            self.assertFalse(self.panel.is_running)
            self.assertEqual(self.panel.profile_path.get(),'')
            self.assertFalse(any(call[0]=='start' for call in self.runtime.calls))

    def test_construct_and_real_render_never_start_or_load(self):
        self.root.geometry('750x430')
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.config_calls, 0)
        self.assertEqual(str(self.panel.start_button['state']), 'disabled')
        for widget in self.panel.winfo_children():
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
            self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), self.panel.winfo_height())
        for widget in (self.panel.start_button, self.panel.stop_button, self.panel.browse_button):
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())

    def test_profile_device_and_per_run_consent(self):
        self.panel.start()
        self.panel.consent.set(True)
        self.panel.start()
        self.assertEqual(self.runtime.calls, [])
        self.ready()
        self.device = None
        self.panel.poll()
        self.panel.start()
        self.assertEqual(self.runtime.calls, [])
        self.device = dict(DEVICE)
        self.panel.start()
        self.assertFalse(self.panel.consent.get())
        self.panel.stop()
        self.runtime.events = [{'kind': 'finished', 'mode': 'listener'}]
        self.panel.poll()
        self.panel.start()
        self.assertEqual(len([c for c in self.runtime.calls if c[0] == 'start']), 1)
        self.assertFalse(self.panel.consent.get())

    def test_sampler_overlap_blocks_button_and_programmatic_start(self):
        self.ready()
        self.other = True
        self.panel.poll()
        self.assertEqual(str(self.panel.start_button['state']), 'disabled')
        self.panel.start()
        self.assertEqual(self.runtime.calls, [])

    def test_start_freezes_arguments_and_preparing_is_not_listening(self):
        self.ready()
        self.panel.start()
        call = self.runtime.calls[0]
        self.assertIsNot(call[1], self.config)
        self.assertIsNot(call[2], self.device)
        self.assertEqual(call[3:], ('private-profile.json', True))
        self.device['name'] = 'changed'
        self.panel.profile_path.set('different.json')
        self.assertEqual(call[2]['name'], DEVICE['name'])
        self.assertIn('กำลังเตรียม', self.panel.status.get())
        self.runtime.events = [{'kind': 'listener_progress', 'frames': 0, 'observations': 0}]
        self.panel.poll()
        self.assertIn('กำลังเตรียม', self.panel.status.get())
        self.runtime.events = [{'kind': 'listener_progress', 'frames': 1024, 'observations': 2}]
        self.panel.poll()
        self.assertIn('กำลังรับเสียง', self.panel.status.get())
        self.assertIn('2 (ไม่ใช่รายการจ่ายค่าน้ำมัน)', self.panel.count.get())

    def test_failures_are_sanitized_and_forced_stop_not_success(self):
        self.ready()
        self.config_failure = True
        self.panel.start()
        self.assertEqual(self.panel.status.get(), GENERIC_ERROR)
        self.config_failure = False
        self.runtime.failure = ValueError('PRIVATE profile path')
        self.panel.start()
        self.assertEqual(self.panel.status.get(), GENERIC_ERROR)
        for code in ('PRIVATE path', ['PRIVATE'], 'stopped'):
            self.runtime.events = [{'kind': 'error', 'code': code, 'message': 'PRIVATE'}]
            self.panel.poll()
            self.assertNotIn('PRIVATE', self.panel.status.get())
        self.assertIn('อาจไม่ครบ', self.panel.status.get())

    def test_close_keeps_polling_until_reaped_without_restart(self):
        self.ready()
        self.panel.start()
        self.panel.close()
        self.panel.close()
        self.assertTrue(self.panel.is_running)
        self.assertFalse(self.panel.consent.get())
        self.panel.start()
        self.panel.poll()
        self.assertEqual(self.runtime.polls, 1)
        self.runtime.events = [{'kind': 'finished', 'mode': 'listener'}]
        self.panel.poll()
        self.assertFalse(self.panel.is_running)
        self.assertEqual([c[0] for c in self.runtime.calls], ['start', 'close'])
        self.assertEqual(str(self.panel.start_button['state']), 'disabled')
        self.assertIn('อาจไม่ครบ', self.panel.status.get())
