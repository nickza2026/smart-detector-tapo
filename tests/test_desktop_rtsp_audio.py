"""Synthetic controls only: no endpoint, Windows audio or SDK initialization."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import threading
import tkinter as tk
import unittest
from unittest.mock import patch, Mock

from smart_tapo.config import Config
from smart_tapo.desktop_rtsp_audio import RtspAudioPanel
from smart_tapo.startup import bind, validate_binding
from smart_tapo.startup_navigation import LoginNavigation


class PureFakeRuntime:
    def __init__(self):
        self.is_running = False
        self.calls = []
        self.events = []
    def start(self, config, device, profile_path, **kwargs):
        self.calls.append((config, device, profile_path, kwargs))
        self.is_running = True
    def poll(self):
        events, self.events = self.events, []
        return events
    def stop(self): self.is_running = False
    def close(self): self.is_running = False


class RtspControlsTests(unittest.TestCase):
    def setUp(self):
        from smart_tapo.smoke_window import install
        install()
        self.root = tk.Tk(); self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.config = Config(capture_backend='rtsp', rtsp_url='rtsp://127.0.0.1:8554/synthetic', adb_serial='', camera_name='')
        self.runtime = PureFakeRuntime()
        self.current = True
        self.panel = RtspAudioPanel(self.root, lambda: self.config, runtime=self.runtime, settings_current=lambda: self.current)
        self.listener = self.panel.listener_panel
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)/'existing-profile.json'; self.profile.write_text('{}')
        self.now = 100.
        self.listener.clock = lambda: self.now
        self.runtime.profile_fingerprint = 'frozen-fingerprint'

    def test_no_probe_on_open_explicit_consent_and_no_windows_device(self):
        self.assertEqual(self.runtime.calls, [])
        self.listener.profile_path.set(str(self.profile))
        self.listener.start()
        self.assertEqual(self.runtime.calls, [])
        self.listener.consent.set(True)
        self.listener.start()
        config, device, path, options = self.runtime.calls[0]
        self.assertIsNone(device)
        self.assertEqual(config.rtsp_url, self.config.rtsp_url)
        self.assertEqual(path, str(self.profile))
        self.assertTrue(options['consent'])
        self.assertFalse(self.listener.consent.get())
        self.assertTrue(self.panel.is_running)
        self.panel.stop()
        self.assertFalse(self.panel.is_running)

    def test_pending_settings_block_and_private_errors_are_not_shown(self):
        self.listener.profile_path.set('profile.json'); self.listener.consent.set(True)
        self.current = False
        self.listener.start(); self.assertEqual(self.runtime.calls, [])
        self.runtime.events = [dict(kind='error', code='PRIVATE rtsp://secret', detail='password')]
        self.panel.poll()
        self.assertNotIn('PRIVATE', self.listener.status.get())
        self.assertNotIn('password', self.listener.status.get())

    def test_startup_audio_starts_even_when_image_start_fails(self):
        from smart_tapo.desktop_startup import StartupPanel
        config = replace(self.config, windows_startup_enabled=True, windows_startup_audio=True)
        self.listener.profile_path.set('selected.json')
        app = SimpleNamespace(config=config, audio_panel=self.panel, closing=False, busy=False,
            _active=lambda: False, _audio_active=lambda: False, _candidate=lambda: config,
            _calibration_pending=lambda: False, store=SimpleNamespace(root=Path('synthetic')),
            controller=Mock(), notify_key=Mock())
        app.controller.start_capture.side_effect = RuntimeError('PRIVATE stream error')
        saved = dict(profile=str(self.profile), fingerprint='fingerprint')
        with patch('smart_tapo.desktop_startup.validate_binding', return_value=saved), patch('smart_tapo.desktop_startup.is_ready', return_value=True):
            startup = StartupPanel(self.root, app)
            startup.begin()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertIsNone(self.runtime.calls[0][1])
        self.assertEqual(self.runtime.calls[0][3]['expected_fingerprint'], 'fingerprint')
        self.assertNotIn('PRIVATE', startup.status.get())

    def test_changed_profile_selection_blocks_startup(self):
        from smart_tapo.desktop_startup import StartupPanel
        config = replace(self.config, windows_startup_enabled=True, windows_startup_audio=True)
        app = SimpleNamespace(config=config, audio_panel=self.panel, closing=False, busy=False,
            _active=lambda: False, _audio_active=lambda: False, _candidate=lambda: config,
            _calibration_pending=lambda: False, store=SimpleNamespace(root=Path('synthetic')),
            controller=Mock(), notify_key=Mock())
        saved = dict(profile=str(Path('original.json').absolute()), fingerprint='fingerprint')
        with patch('smart_tapo.desktop_startup.validate_binding', return_value=saved), patch('smart_tapo.desktop_startup.is_ready', return_value=True):
            startup = StartupPanel(self.root, app)
            self.listener.profile_path.set('changed.json')
            startup.begin()
        self.assertEqual(self.runtime.calls, [])
        app.controller.start_capture.assert_not_called()

    def start_watch(self):
        self.listener.profile_path.set(str(self.profile)); self.listener.consent.set(True)
        self.listener.start()
        self.assertEqual(len(self.runtime.calls), 1)

    def test_counter_separates_post_payment_ready_from_end_and_keeps_final_snapshot(self):
        self.start_watch()
        self.runtime.events=[dict(kind='listener_progress',observations_total=3,ready_total=2,end_total=1)]
        self.panel.poll()
        self.assertIn('พร้อมเติมหลังรับเงิน: 2',self.listener.count.get())
        self.assertIn('เสียงจบ: 1',self.listener.count.get())
        self.assertIn('ไม่ใช่จำนวนรอบ',self.listener.count.get())
        self.runtime.is_running=False
        self.runtime.events=[dict(kind='finished',observations_total=4,ready_total=2,end_total=2)]
        self.panel.poll()
        self.assertIn('เสียงจบ: 2',self.listener.count.get())
        self.assertEqual(self.listener._announcements,4)

    def test_typed_counts_accumulate_across_owned_retry_without_double_count(self):
        self.start_watch()
        self.runtime.events=[dict(kind='listener_progress',observations_total=3,ready_total=2,end_total=1)]
        self.panel.poll();self.terminal('audio_timeout')
        self.now+=15.;self.panel.poll()
        self.runtime.events=[dict(kind='listener_progress',observations_total=2,ready_total=1,end_total=1)]
        self.panel.poll()
        self.assertEqual(self.listener._announcements,5)
        self.assertIn('พร้อมเติมหลังรับเงิน: 3',self.listener.count.get())
        self.assertIn('เสียงจบ: 2',self.listener.count.get())
        self.runtime.events=[dict(kind='listener_progress',observations_total=1,ready_total=1,end_total=0)]
        self.panel.poll();self.assertEqual(self.listener._announcements,5)

    def test_invalid_typed_counts_are_unknown_not_guessed_from_total(self):
        self.start_watch()
        self.runtime.events=[dict(kind='listener_progress',observations_total=3,ready_total=True,end_total=2)]
        self.panel.poll()
        self.assertEqual(self.listener._announcements,3)
        self.assertIn('ยังไม่มีข้อมูลแยกชนิด',self.listener.count.get())

    def terminal(self, code):
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code=code)]
        self.panel.poll()

    def test_terminal_timeout_restarts_same_frozen_watch_without_new_consent(self):
        self.start_watch()
        self.runtime.events = [dict(kind='listener_progress', observations_total=3)]
        self.panel.poll()
        self.terminal('audio_timeout')
        self.assertTrue(self.panel.is_running)  # backoff still owns the watch/settings lock
        self.assertFalse(self.listener.consent.get())
        self.assertEqual(str(self.listener.path_entry.cget('state')), 'disabled')
        self.now += 14.99; self.panel.poll(); self.assertEqual(len(self.runtime.calls), 1)
        self.now += .01; self.panel.poll(); self.assertEqual(len(self.runtime.calls), 2)
        frozen, device, profile, options = self.runtime.calls[-1]
        self.assertEqual(frozen.rtsp_url, self.config.rtsp_url); self.assertIsNone(device)
        self.assertEqual(profile, str(self.profile)); self.assertEqual(options['expected_fingerprint'], 'frozen-fingerprint')
        self.assertTrue(options['consent'])
        self.runtime.events = [dict(kind='listener_progress', observations_total=1, awaiting_boundary=True)]
        self.panel.poll(); self.assertIn('ไม่ผูกลูกค้า', self.listener.status.get())
        self.assertEqual(self.listener._announcements, 4)

    def test_retries_are_paced_and_stop_close_cancel_late_retry(self):
        self.start_watch(); self.terminal('worker_failed')
        self.now += 15; self.panel.poll(); self.terminal('audio_timeout')
        self.now += 14.99; self.panel.poll(); self.assertEqual(len(self.runtime.calls), 2)
        self.panel.stop(); self.now += 100; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 2); self.assertFalse(self.panel.is_running)
        self.listener.consent.set(True); self.listener.start()
        self.terminal('audio_timeout'); self.panel.close(); self.now += 100; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 3); self.assertFalse(self.panel.is_running)

    def test_changed_source_or_profile_never_retries(self):
        for change in ('url','profile','profile_changed'):
            with self.subTest(change=change):
                self.runtime.calls.clear(); self.profile.write_text('{}')
                self.listener.closing = False; self.listener._stopping = False
                self.config = replace(self.config, rtsp_url='rtsp://127.0.0.1:8554/synthetic')
                self.start_watch()
                self.terminal('audio_timeout' if change in ('url','profile') else change)
                if change == 'url': self.config = replace(self.config, rtsp_url='rtsp://127.0.0.1:8554/changed')
                if change == 'profile': self.profile.write_text('{"changed":true}')
                self.now += 100; self.panel.poll()
                self.assertEqual(len(self.runtime.calls), 1); self.assertFalse(self.panel.is_running)
                self.panel.stop()

    def test_display_only_saved_change_does_not_cancel_consented_retry(self):
        self.start_watch()
        self.config = replace(self.config, history_web_port=8081)
        self.terminal('audio_timeout')
        self.now += 15
        self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertTrue(self.listener._health_expected)
        self.assertEqual(self.runtime.calls[-1][0].history_web_port, 8080)

    def test_storage_or_camera_change_still_cancels_retry_until_new_consent(self):
        for changes in ({'camera_id': 'other-camera'}, {'data_dir': Path(self.temp.name)/'other-data'}):
            with self.subTest(fields=tuple(changes)):
                self.runtime.calls.clear()
                self.start_watch()
                self.config = replace(self.config, **changes)
                self.terminal('audio_timeout')
                self.now += 15
                self.panel.poll()
                self.assertEqual(len(self.runtime.calls), 1)
                self.assertFalse(self.listener._health_expected)
                self.assertFalse(self.listener.consent.get())
                self.panel.stop()

    def test_disabled_start_explains_pending_saved_settings_without_private_values(self):
        self.listener.profile_path.set(str(self.profile))
        self.current = False
        self.listener.consent.set(True)
        self.assertEqual(str(self.listener.start_button.cget('state')), 'disabled')
        self.assertIn('บันทึก', getattr(self.listener, 'start_hint', self.listener.status).get())
        self.current = True
        self.panel.poll()
        self.assertEqual(str(self.listener.start_button.cget('state')), 'normal')
        self.listener.start_button.invoke()
        self.assertEqual(len(self.runtime.calls), 1)

    def test_storage_retry_is_paced_and_safe(self):
        for code in ('storage_busy', 'storage_full', 'storage_permission', 'storage_io_error'):
            self.runtime.calls.clear()
            self.start_watch()
            self.terminal(code)
            self.assertTrue(self.panel.is_running)
            self.assertEqual(self.listener._retry_at - self.now, 15)
            self.assertIn('ช่วงขาด', self.listener.status.get())
            self.now = self.listener._retry_at
            self.panel.poll()
            self.assertEqual(len(self.runtime.calls), 2)
            self.panel.stop()

    def test_storage_watch_caps_at_three_and_keeps_confirmed_counts(self):
        self.start_watch()
        self.runtime.events = [dict(kind='listener_progress', observations_total=2, ready_total=1, end_total=1)]
        self.panel.poll()
        for index in range(3):
            self.terminal('storage_busy')
            delay = self.listener._retry_at - self.now
            self.assertEqual(delay, 15)
            self.now += delay
            self.panel.poll()
            self.runtime.events = [dict(kind='listener_progress', observations_total=0, ready_total=0, end_total=0)]
            self.panel.poll()
        self.assertEqual(self.listener._announcements, 2)
        self.assertEqual(len(self.runtime.calls), 4)
        self.assertTrue(self.panel.is_running)
        self.terminal('storage_busy')
        self.now += 300; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 4)
        self.assertFalse(self.panel.is_running)

    def test_storage_faults_retry_without_exposing_private_stage(self):
        for code in ('storage_path_unsafe', 'storage_corrupt', 'storage_schema', 'storage_camera_mismatch', 'storage_error'):
            self.runtime.calls.clear()
            self.start_watch()
            self.runtime.is_running = False
            self.runtime.events = [dict(kind='error', code=code, stage='rtsp://SECRET', detail='SQL PRIVATE')]
            self.panel.poll()
            self.assertTrue(self.panel.is_running)
            self.assertNotIn('SECRET', self.listener.health_status.get())
            self.assertNotIn('PRIVATE', self.listener.status.get())
            self.panel.stop()

    def test_progress_diagnostics_are_allowlisted_and_missing_is_unknown(self):
        self.runtime.events = [dict(kind='listener_progress', stage='PRIVATE /secret', audio_frames=12,
                                   audio_rms=float('nan'), ambiguous_total=2)]
        self.panel.poll()
        text = self.listener.health_status.get()
        self.assertIn('12', text)
        self.assertIn('ไม่ทราบ', text)
        self.assertIn('กำกวม', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('nan', text)

    def test_drop_is_not_silence_and_announcements_preserved(self):
        self.runtime.events = [dict(kind='listener_status', code='rtsp_disconnected')]
        self.panel.poll()
        self.assertIn('ไม่ใช่เสียงเงียบ', self.listener.status.get())
        self.runtime.events = [dict(kind='listener_progress', frames=20, observations_total=3, awaiting_boundary=True)]
        self.panel.poll()
        self.assertIn('3', self.listener.count.get())
        self.assertIn('ไม่ผูกลูกค้า', self.listener.status.get())

    def test_healthy_pcm_waiting_for_old_gap_boundary_is_not_current_outage(self):
        self.runtime.events=[dict(kind='listener_progress',frames=16000,observations_total=3,
                                  waiting_audio=False,awaiting_boundary=True)]
        self.panel.poll()
        self.assertIn('กำลังรับข้อมูลเสียง',self.listener.status.get())
        self.assertIn('รอประกาศสิ้นสุด',self.listener.status.get())
        self.assertNotIn('เสียงขาดช่วง —',self.listener.status.get())
        self.assertIn('3',self.listener.count.get())

    def test_audio_clock_failures_show_distinct_safe_reasons(self):
        for code,text in (('audio_timestamp_gap','เวลาเสียง'),('audio_timestamp_invalid','ข้อมูลเวลา'),
                          ('audio_backlog','ล่าช้า')):
            with self.subTest(code=code):
                self.runtime.events=[dict(kind='listener_status',code=code)]
                self.panel.poll()
                self.assertIn(text,self.listener.status.get())
                self.assertNotIn('RTSP ไม่พร้อมหรือข้อมูลไม่ต่อเนื่อง',self.listener.status.get())


class RtspStartupBindingTests(unittest.TestCase):
    def test_new_audio_consent_has_no_android_or_endpoint_and_fails_closed(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory)/'profile.json'; profile.write_text('{}')
            config = Config(capture_backend='rtsp', rtsp_url='rtsp://127.0.0.1:8554/synthetic', windows_startup_audio=True, adb_serial='', camera_name='')
            with patch('smart_tapo.announcement_profile.load_announcement_profile', return_value=SimpleNamespace(fingerprint='synthetic-fingerprint')):
                config = replace(config, windows_startup_binding=bind(config, str(profile)))
                saved = validate_binding(config)
                self.assertEqual(saved['schema'], 'rtsp-av-startup-v2')
                for forbidden in ('device', 'android_identity', 'adb_serial', 'camera_name'):
                    self.assertNotIn(forbidden, saved)
                self.assertNotIn('rtsp://', config.windows_startup_binding)
                with patch('smart_tapo.startup_navigation.Adb', side_effect=AssertionError('ADB must not initialize')):
                    self.assertTrue(LoginNavigation(config)(threading.Event()))
                with self.assertRaises(ValueError):
                    validate_binding(replace(config, rtsp_url='rtsp://127.0.0.1:8554/changed'))
                legacy = dict(saved, schema='rtsp-startup-v1', device={'endpoint_id':'old'})
                with self.assertRaises(ValueError):
                    validate_binding(replace(config, windows_startup_binding=json.dumps(legacy)))
                profile.write_text('{"changed":true}')
                with self.assertRaises(ValueError): validate_binding(config)
