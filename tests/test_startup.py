"""Synthetic login-start regressions: no Windows registration, ADB, audio or SDK."""
from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from smart_tapo.config import Config, load_config
from smart_tapo.startup import (ENTRY, StartupCoordinator, bind, checked_path, device_identity,
                                entry_content, manage_entry, matching_device, validate_binding)
from smart_tapo.audio_endpoint import attach_endpoint_ids
from smart_tapo.startup_navigation import LoginNavigation, android_identity
from smart_tapo.capture.page_mode import PageMode


DEVICE = dict(index=7, name='CABLE Input [Loopback]', rate=48000, channels=2, endpoint_id='{saved-guid}')


class RtspStartupTests(unittest.TestCase):
    def config(self, **changes):
        from types import SimpleNamespace
        values = dict(capture_backend='rtsp', rtsp_url='rtsp://127.0.0.1:8554/tapo_c500',
                      camera_id='camera-1', expected_size=(1920, 1080), camera_name='',
                      adb_serial='', tapo_package='', windows_startup_audio=False,
                      windows_startup_binding='', data_dir=Path('data'))
        values.update(changes)
        return SimpleNamespace(**values)

    def test_video_only_binding_never_constructs_android_or_opens_network(self):
        config = self.config()
        config.windows_startup_binding = bind(config)
        with patch('smart_tapo.startup_navigation.Adb', side_effect=AssertionError('no Android')):
            navigation = LoginNavigation(config)
            self.assertTrue(navigation(threading.Event()))
        saved = validate_binding(config)
        self.assertEqual(saved['schema'], 'rtsp-av-startup-v2')
        self.assertNotIn('rtsp://', config.windows_startup_binding)
        self.assertNotIn('adb_serial', saved)

    def test_source_url_geometry_and_legacy_binding_fail_closed(self):
        config = self.config()
        binding = bind(config)
        for changes in ({'rtsp_url': 'rtsp://127.0.0.1:8554/other'},
                        {'expected_size': (1280, 720)},
                        {'capture_backend': 'adb'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_binding(self.config(windows_startup_binding=binding, **changes))
        with self.assertRaises(ValueError):
            validate_binding(self.config(windows_startup_binding=json.dumps({'camera_id': 'camera-1'})))

    def test_rtsp_audio_requires_profile_not_endpoint_or_android(self):
        from types import SimpleNamespace
        with self.assertRaises((ValueError, OSError)):
            bind(self.config(windows_startup_audio=True))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'profile.json'; path.write_text('{}')
            config = self.config(windows_startup_audio=True)
            with patch('smart_tapo.announcement_profile.load_announcement_profile', return_value=SimpleNamespace(fingerprint='frozen')):
                config.windows_startup_binding = bind(config, str(path))
                saved = validate_binding(config)
                self.assertEqual(saved['audio_source'], 'rtsp')
                self.assertEqual(saved['fingerprint'], 'frozen')
                for key in ('device', 'android_identity', 'adb_serial'):
                    self.assertNotIn(key, saved)
                with patch('smart_tapo.startup_navigation.Adb', side_effect=AssertionError('no Android')):
                    self.assertTrue(LoginNavigation(config)(threading.Event()))


class StartupEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.exe = self.folder / 'โปรแกรม Smart Tapo.exe'
        self.exe.touch()
        self.config = self.folder / 'ข้อมูล config.json'
        self.config.write_text('{}', encoding='utf-8')

    def test_enable_disable_only_in_fake_startup_folder_unicode_and_spaces(self):
        manage_entry(True, self.exe, self.config, folder=self.folder)
        content = (self.folder / ENTRY).read_text(encoding='utf-16')
        self.assertIn('--windows-login', content)
        self.assertIn('""' + str(self.exe) + '""', content)
        self.assertIn('""' + str(self.config) + '""', content)
        self.assertIn('FileExists', content)
        manage_entry(True, self.exe, self.config, folder=self.folder)
        manage_entry(False, config=self.config, folder=self.folder)
        self.assertFalse((self.folder / ENTRY).exists())
        self.assertTrue(self.exe.exists())

    def test_other_config_owned_entry_is_not_stolen(self):
        manage_entry(True, self.exe, self.config, folder=self.folder)
        other = self.folder / 'other config.json'
        other.write_text('{}', encoding='utf-8')
        before = (self.folder / ENTRY).read_bytes()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                manage_entry(enabled, self.exe, other, folder=self.folder)
        self.assertEqual((self.folder / ENTRY).read_bytes(), before)

    def test_percent_environment_expansion_in_paths_is_rejected(self):
        executable = self.folder / '%TEMP% app.exe'
        executable.touch()
        with self.assertRaises(ValueError):
            entry_content(executable, self.config)

    def test_foreign_duplicate_entry_never_overwritten_or_deleted(self):
        entry = self.folder / ENTRY
        entry.write_text('foreign', encoding='utf-16')
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                manage_entry(enabled, self.exe, self.config, folder=self.folder)
        self.assertEqual(entry.read_text(encoding='utf-16'), 'foreign')

    def test_missing_or_temporary_executable_rejected(self):
        for name in ('missing.exe', 'python.exe', 'pythonw.exe', 'worker.py'):
            path = self.folder / name
            if name != 'missing.exe':
                path.touch()
            with self.subTest(name=name), self.assertRaises(ValueError):
                entry_content(path, self.config)
        for part in ('_MEI12345', '.venv'):
            directory = self.folder / part
            directory.mkdir()
            executable = directory / 'SmartTapo.exe'
            executable.touch()
            with self.assertRaises(ValueError):
                entry_content(executable, self.config)

    def test_old_configuration_migrates_off_without_global_writes(self):
        config = load_config(self.config)
        self.assertFalse(config.windows_startup_enabled)
        self.assertFalse(config.windows_startup_audio)
        self.assertEqual(config.windows_startup_binding, '')
        self.assertFalse((self.folder / ENTRY).exists())

    def test_link_ancestor_rejected_without_following(self):
        fake = Mock(st_mode=0, st_file_attributes=0x400)
        with patch.object(Path, 'lstat', return_value=fake):
            with self.assertRaises(ValueError):
                checked_path(self.config)

    def test_nonboolean_startup_flags_rejected(self):
        for key in ('windows_startup_enabled', 'windows_startup_audio'):
            with self.assertRaises(ValueError):
                replace(Config(), **{key: 1}).validate()
        with self.assertRaises(ValueError):
            replace(Config(), windows_startup_audio=True).validate()


class StartupIdentityTests(unittest.TestCase):
    def test_stable_guid_not_volatile_portaudio_index(self):
        binding = device_identity(DEVICE)
        new = dict(DEVICE, index=123)
        self.assertEqual(matching_device(binding, [dict(DEVICE, endpoint_id='{wrong}'), new]), new)
        for devices in ([], [dict(DEVICE, endpoint_id='{wrong}')], [new, dict(new)],
                        [dict(DEVICE, rate=44100)], [dict(DEVICE, name='Other output')]):
            with self.subTest(devices=devices), self.assertRaises(ValueError):
                matching_device(binding, devices)

    def test_index_name_format_without_guid_is_not_startup_identity(self):
        no_guid = dict(DEVICE)
        del no_guid['endpoint_id']
        with self.assertRaises(ValueError):
            device_identity(no_guid)

    def test_endpoint_enrichment_does_not_choose_default(self):
        devices = [dict(index=0, name='CABLE Input [Loopback]', rate=48000, channels=2),
                   dict(index=1, name='Other [Loopback]', rate=48000, channels=2)]
        enriched = attach_endpoint_ids(devices, {'CABLE Input': '{specific}'})
        self.assertEqual(enriched[0]['endpoint_id'], '{specific}')
        self.assertNotIn('endpoint_id', enriched[1])

    def test_camera_binding_invalidated_not_silently_rebound(self):
        config = replace(Config(), adb_serial='127.0.0.1:21503')
        binding = bind(config)
        config = replace(config, windows_startup_enabled=True, windows_startup_binding=binding)
        validate_binding(config)
        for key, value in (('camera_id', 'new'), ('camera_name', 'wrong camera'),
                           ('adb_serial', '127.0.0.1:21513'), ('tapo_package', 'com.other.app')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_binding(replace(config, **{key: value}))

    def test_missing_or_changed_profile_pauses_before_audio(self):
        config = replace(Config(), adb_serial='127.0.0.1:21503')
        binding = json.loads(bind(config))
        binding.update(profile='not-a-real-profile.json', digest='a' * 64)
        config = replace(config, windows_startup_enabled=True, windows_startup_audio=True,
                         windows_startup_binding=json.dumps(binding))
        with self.assertRaises(ValueError):
            validate_binding(config)
        with patch('smart_tapo.startup.profile_digest', return_value='b' * 64):
            with self.assertRaises(ValueError):
                validate_binding(config)


class CoordinatorTests(unittest.TestCase):
    def test_slow_adb_retries_paced_indefinitely_without_busy_spin(self):
        now = [0.]
        calls = []
        def attempt(stop):
            calls.append(1)
            raise ValueError('ADB pending')
        coordinator = StartupCoordinator(attempt, clock=lambda: now[0], retry_seconds=15)
        coordinator.start()
        for expected in range(1, 12):
            coordinator.poll()
            coordinator.thread.join(timeout=2)
            self.assertEqual(len(calls), expected)
            for _ in range(100):
                coordinator.poll()
            self.assertEqual(len(calls), expected)
            now[0] += 15
        coordinator.stop()
        now[0] += 1000
        coordinator.poll()
        self.assertEqual(len(calls), 11)

    def test_success_consumed_once_without_waiting_retry_deadline(self):
        attempt = Mock(return_value={'ready': True})
        coordinator = StartupCoordinator(attempt, clock=lambda: 0.)
        coordinator.start()
        coordinator.poll()
        coordinator.thread.join(timeout=2)
        self.assertEqual(coordinator.poll(), {'ready': True})
        self.assertIsNone(coordinator.poll())
        attempt.assert_called_once()

    def test_stop_during_every_phase_discards_late_result_and_never_restarts(self):
        for phase in ('MEmu', 'ADB', 'identity', 'Tapo', 'camera', 'Live', 'audio'):
            with self.subTest(phase=phase):
                entered, release = threading.Event(), threading.Event()
                def attempt(stop):
                    entered.set()
                    release.wait(2)
                    return phase
                coordinator = StartupCoordinator(attempt, clock=lambda: 0.)
                coordinator.start()
                coordinator.poll()
                self.assertTrue(entered.wait(2))
                coordinator.stop()
                release.set()
                coordinator.thread.join(timeout=2)
                self.assertFalse(coordinator.is_running)
                self.assertIsNone(coordinator.poll())
                self.assertIsNone(coordinator.result)
                self.assertFalse(coordinator.enabled)

    def test_off_has_no_work_or_global_writes(self):
        attempt = Mock()
        coordinator = StartupCoordinator(attempt)
        for _ in range(20):
            coordinator.poll()
        attempt.assert_not_called()


class FakeStop:
    def __init__(self):
        self.stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, delay):
        return self.stopped


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Config(), adb_serial='127.0.0.1:21503',
                              windows_startup_binding=json.dumps({'android_identity': ['MEmu001', '0123456789abcdef']}))
        self.adb = Mock()
        self.adb.run.side_effect = lambda *args: b'0123456789abcdef' if 'android_id' in args else b'MEmu001'
        self.adb.read_page_mode.return_value = PageMode('live', 'observed_live_layout')
        self.adb.ensure_live.return_value = PageMode('live', 'observed_live_layout')
        self.navigation = LoginNavigation(self.config, adb=self.adb)
        self.stop = FakeStop()

    def test_stop_inside_each_real_navigation_phase_never_returns_ready(self):
        for phase in ('connect', 'serial', 'android_id', 'read_page', 'launch', 'select_camera', 'ensure_live'):
            with self.subTest(phase=phase):
                stop, adb = FakeStop(), Mock()
                def run(*args):
                    if ((phase == 'serial' and 'ro.serialno' in args)
                            or (phase == 'android_id' and 'android_id' in args)
                            or (phase == 'launch' and 'monkey' in args)):
                        stop.stopped = True
                    return b'0123456789abcdef' if 'android_id' in args else b'MEmu001'
                adb.run.side_effect = run
                if phase == 'connect':
                    adb.connect.side_effect = lambda event: setattr(event, 'stopped', True)
                mode = PageMode('unknown', 'unrecognized_foreground') if phase == 'launch' else (
                    PageMode('unknown', 'camera_not_confirmed') if phase == 'select_camera' else
                    PageMode('live', 'observed_live_layout'))
                def read_page():
                    if phase == 'read_page':
                        stop.stopped = True
                    return mode
                adb.read_page_mode.side_effect = read_page
                def ensure_live(event):
                    if phase == 'ensure_live':
                        stop.stopped = True
                    return PageMode('live', 'observed_live_layout')
                adb.ensure_live.side_effect = ensure_live
                navigation = LoginNavigation(self.config, adb=adb)
                with patch.object(navigation, '_select_saved_camera', side_effect=lambda event: setattr(event, 'stopped', True)):
                    with self.assertRaises(ValueError):
                        navigation(stop)
                if phase not in ('launch', 'ensure_live'):
                    self.assertFalse(any('monkey' in call.args for call in adb.run.call_args_list))

    def test_existing_live_does_not_launch_or_select_again(self):
        self.assertTrue(self.navigation(self.stop))
        self.assertFalse(any('monkey' in call.args or 'tap' in call.args for call in self.adb.run.call_args_list))

    def test_online_wrong_instance_never_launches_tapo_or_taps(self):
        self.adb.run.return_value = b'wrong'
        self.adb.run.side_effect = None
        with self.assertRaises(ValueError):
            self.navigation(self.stop)
        self.adb.read_page_mode.assert_not_called()
        self.adb.ensure_live.assert_not_called()
        self.assertFalse(any('monkey' in call.args for call in self.adb.run.call_args_list))

    def test_boot_failure_is_bounded_and_does_not_launch_tapo(self):
        self.adb.connect.side_effect = ValueError('boot pending')
        with self.assertRaises(ValueError):
            self.navigation(self.stop)
        self.adb.run.assert_not_called()

    def test_explicit_stop_after_boot_before_identity_prevents_all_actions(self):
        self.adb.connect.side_effect = lambda stop: setattr(stop, 'stopped', True)
        with self.assertRaises(ValueError):
            self.navigation(self.stop)
        self.adb.run.assert_not_called()

    def test_live_pending_does_not_start_roles_or_audio(self):
        self.adb.ensure_live.return_value = PageMode('unknown', 'incomplete_live_layout')
        with self.assertRaises(ValueError):
            self.navigation(self.stop)

    def test_identity_missing_cannot_be_guessed_from_serial_or_port(self):
        self.adb.run.side_effect = None
        for value in (b'', b'null', b'unknown'):
            self.adb.run.return_value = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                android_identity(self.adb, self.stop)

    def test_existing_memu_does_not_spawn_a_second_process(self):
        navigation = LoginNavigation(self.config, running=lambda: True)
        with patch('smart_tapo.startup_navigation.subprocess.Popen') as launch:
            self.assertIsNone(navigation._launch(['MEmu.exe']))
            launch.assert_not_called()

    def test_missing_memu_path_does_not_spawn(self):
        navigation = LoginNavigation(replace(self.config, memu_executable='missing-memu.exe'), running=lambda: False)
        with patch('smart_tapo.startup_navigation.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                navigation._launch(['missing-memu.exe'])
            launch.assert_not_called()

    def test_only_known_home_fresh_selected_camera_can_be_tapped(self):
        state = Mock(recovering=False)
        xml = b'<hierarchy><node package="com.tplink.iot" text="Home"/><node package="com.tplink.iot" text="Camera" bounds="[0,0][100,100]"/></hierarchy>'
        config = replace(self.config, camera_name='Camera')
        navigation = LoginNavigation(config, adb=self.adb)
        self.adb.ui.return_value = xml
        home = config.tapo_package + '/.view.main.MainActivity'
        for activity, should_tap in ((home, True), ('com.other.app/.Home', False),
                                     (config.tapo_package + '/.LoginActivity', False),
                                     (config.tapo_package + '/.view.ipcamerav3.play.VideoPlayV3Activity', False),
                                     (config.tapo_package + '/.view.cloudvideo.TapoPlaybackAndDownloadActivity', False)):
            with self.subTest(activity=activity), \
                    patch('smart_tapo.startup_navigation.ui_lock', return_value=nullcontext()), \
                    patch('smart_tapo.startup_navigation.navigation_lock', return_value=nullcontext()), \
                    patch('smart_tapo.startup_navigation.navigation_scope', return_value=nullcontext()), \
                    patch('smart_tapo.startup_navigation.navigation_snapshot', return_value=state), \
                    patch('smart_tapo.startup_navigation.foreground_activity', return_value=activity):
                self.adb.run.reset_mock()
                if should_tap:
                    navigation._select_saved_camera(self.stop)
                else:
                    with self.assertRaises(ValueError):
                        navigation._select_saved_camera(self.stop)
                taps = [call for call in self.adb.run.call_args_list if 'tap' in call.args]
                self.assertEqual(len(taps), int(should_tap))


if __name__ == '__main__':
    unittest.main()
