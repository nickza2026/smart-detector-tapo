"""Metadata-only diagnostics in owned fixtures, never a field root-cause claim."""
import stat
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from smart_tapo.refueling_store import _safe, RefuelingStore
from smart_tapo.storage_diagnostics import (StoragePathUnsafeError, storage_failure_event,
    guard_evidence, format_guard_evidence, validate_guard_evidence)


class GuardEvidenceTests(unittest.TestCase):
    def test_rejecting_stat_is_preserved_without_restat_or_path_disclosure(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'PRIVATE-marker.sqlite3'
            target.write_bytes(b'owned fixture')
            real = Path.lstat
            calls = []
            def snapshot(path, *args, **kwargs):
                if path == target:
                    calls.append(path)
                    return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0,
                                           st_file_attributes=32, st_reparse_tag=0)
                return real(path, *args, **kwargs)
            with patch.object(Path, 'lstat', snapshot):
                with self.assertRaises(StoragePathUnsafeError) as caught:
                    _safe(target)
            event = storage_failure_event(caught.exception, 'storage_commit')
            self.assertEqual(event['code'], 'storage_path_unsafe')
            self.assertIsNotNone(event.get('guard'), 'Rejecting stat snapshot was discarded')
            self.assertEqual(event['guard']['links'], 0)
            self.assertEqual(event['guard']['component'], 'path')
            self.assertEqual(len(calls), 1)
            self.assertNotIn('PRIVATE-marker', repr(event))
            self.assertEqual(target.read_bytes(), b'owned fixture')

    def test_genuine_hardlink_stays_blocked_with_two_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'original'
            source.write_bytes(b'owned')
            target = Path(temporary) / 'linked'
            os.link(source, target)
            with self.assertRaises(StoragePathUnsafeError) as caught:
                _safe(target)
            evidence = storage_failure_event(caught.exception, 'storage_open')['guard']
            self.assertEqual(evidence['links'], 2)
            self.assertEqual(evidence['reason'], 'non_single_link')

    def test_journal_component_and_ancestor_depth_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RefuelingStore(root / 'store', 'cam1')
            try:
                target = Path(str(store.db_path) + '-journal')
                real = Path.lstat
                def snapshot(path, *args, **kwargs):
                    if path == target:
                        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0,
                                               st_file_attributes=32, st_reparse_tag=0)
                    return real(path, *args, **kwargs)
                with patch.object(Path, 'lstat', snapshot):
                    with self.assertRaises(StoragePathUnsafeError) as caught:
                        store._guard()
                evidence = storage_failure_event(caught.exception, 'storage_commit')['guard']
                self.assertEqual((evidence['component'], evidence['depth']), ('journal', 0))
                def ancestor(path, *args, **kwargs):
                    if path == root:
                        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_nlink=1,
                                               st_file_attributes=0x410, st_reparse_tag=0xa0000003)
                    return real(path, *args, **kwargs)
                with patch.object(Path, 'lstat', ancestor):
                    with self.assertRaises(StoragePathUnsafeError) as caught:
                        _safe(store.db_path, component='database')
                evidence = caught.exception.guard
                self.assertEqual((evidence['component'], evidence['depth']), ('database', 2))
                self.assertEqual(evidence['reason'], 'linked_path')
            finally:
                store.close()

    def test_untrusted_evidence_is_never_displayed_or_forwarded(self):
        valid = guard_evidence('linked_path', 'media', 0)
        for bad in ({'path': 'rtsp://PRIVATE-secret'}, dict(valid, extra='PRIVATE-secret'),
                    dict(valid, reason='PRIVATE-secret'), dict(valid, component=[]),
                    dict(valid, depth=True), dict(valid, links=2**80), dict(valid, mode=-1)):
            with self.subTest(bad=bad):
                self.assertIsNone(validate_guard_evidence(bad))
                self.assertEqual(format_guard_evidence(bad), '')
                error = StoragePathUnsafeError('PRIVATE-secret', guard=bad)
                self.assertEqual(storage_failure_event(error, 'storage_open'),
                                 {'code': 'storage_path_unsafe', 'stage': 'storage_open'})

    def test_actual_worker_final_preserves_guard_snapshot(self):
        from tests.test_rtsp_storage_diagnostics import StorageWorkerDiagnosticsTests
        evidence = guard_evidence('non_single_link', 'wal', 0,
            SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=0, st_file_attributes=32, st_reparse_tag=0))
        error = StoragePathUnsafeError('PRIVATE file path', guard=evidence)
        result, _, av, _ = StorageWorkerDiagnosticsTests().run_worker(open_error=error)
        self.assertEqual(result['guard'], evidence)
        self.assertEqual((result['code'], result['stage']), ('storage_path_unsafe', 'storage_open'))
        self.assertNotIn('PRIVATE', repr(result))
        av.open.assert_not_called()


class GuardEvidenceUITests(unittest.TestCase):
    def setUp(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.setUp(self)

    def test_last_error_survives_health_refresh_and_manual_restart(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        evidence = guard_evidence('non_single_link', 'journal', 0,
            SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=0, st_file_attributes=32, st_reparse_tag=0))
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe',
                                    stage='storage_commit', guard=evidence)]
        self.panel.poll()
        self.assertTrue(self.panel.is_running)
        for _ in range(2):
            self.panel.poll()
            self.assertIn('component=journal', self.listener.health_status.get())
            self.assertIn('links=0', self.listener.health_status.get())
            self.assertIn('python=', self.listener.health_status.get())
        self.panel.stop()
        self.listener.consent.set(True)
        self.listener.start()
        self.panel.poll()
        self.assertIn('Storage guard:', self.listener.health_status.get())
        self.assertIn('เหตุ guard ล่าสุด', self.listener.health_status.get())

    def test_private_error_payload_is_not_rendered(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe',
            stage='rtsp://PRIVATE-secret', guard={'path': 'PRIVATE-secret'})]
        self.panel.poll()
        self.assertNotIn('PRIVATE', self.listener.health_status.get())
        self.assertNotIn('Storage guard:', self.listener.health_status.get())

    def test_audio_fault_gets_three_fifteen_second_restarts_before_alert_eligibility(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        for attempt in range(3):
            self.runtime.is_running = False
            self.runtime.events = [dict(kind='error', code='storage_path_unsafe')]
            self.panel.poll()
            self.assertTrue(self.listener.health_snapshot(self.now).get('recovery_pending', False))
            calls = len(self.runtime.calls)
            self.now += 14.9
            self.panel.poll()
            self.assertEqual(len(self.runtime.calls), calls)
            self.now += .1
            self.panel.poll()
            self.assertEqual(len(self.runtime.calls), calls + 1)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe')]
        self.panel.poll()
        self.assertFalse(self.listener.health_snapshot(self.now).get('recovery_pending', True))
        self.now += 300
        self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 4)

    def test_health_file_failure_does_not_hide_last_guard(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.health_snapshot = {'io_error': True}
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe', stage='storage_commit',
            guard=guard_evidence('linked_path', 'media', 0))]
        self.panel.poll()
        text = self.listener.health_status.get()
        self.assertIn('บันทึกสุขภาพไม่ได้', text)
        self.assertIn('component=media', text)

    def test_no_pcm_start_is_retried_and_finite(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.assertTrue(self.listener.health_snapshot(self.now)['recovery_pending'])
        for _ in range(3):
            self.now += 8.1; self.panel.poll()
            self.now += 15; self.panel.poll()
        self.now += 8.1; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 4)
        self.assertFalse(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.now += 100; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 4)

    def test_silent_persisted_pcm_rearms_after_stable_window_not_one_packet(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe')]
        self.panel.poll()
        self.now += 15; self.panel.poll()
        base = self.now + .1
        for index in range(7):
            self.now = base + index * 5
            self.runtime.events = [dict(kind='listener_progress', waiting_audio=False, stage='persisted',
                heartbeat=self.now, pcm_started_at=self.now-.01, pcm_session='a'*32,
                audio_frames=1600*(index+1), audio_rms=0.)]
            self.panel.poll()
            if index < 6:
                self.assertTrue(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.assertFalse(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.assertEqual(self.listener._recovery.attempts, 0)

    def test_actual_panel_snapshot_blocks_phone_until_third_restart_fails(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        from tests.test_health_autorecovery_gate import HeldExecutor
        from smart_tapo.desktop_health_alerts import HealthAlertSupervisor
        RtspControlsTests.start_watch(self)
        executor, sent = HeldExecutor(), []
        with patch('smart_tapo.desktop_health_alerts.ThreadPoolExecutor', return_value=executor):
            supervisor = HealthAlertSupervisor(lambda *args: (sent.append(args[2]) or 'success'), lambda: self.now)
        self.addCleanup(supervisor.close)
        config = dict(notify_enabled=True, health_notify_enabled=True,
                      health_failure_seconds=1, health_repeat_seconds=900)
        from smart_tapo.desktop import DesktopApp
        app = SimpleNamespace(health_alerts=supervisor, closing=False, config=config,
            _health_credentials=('fake-token', 'fake-user'),
            controller=SimpleNamespace(health_snapshot=lambda: {'expected': False}),
            audio_panel=SimpleNamespace(listener_panel=self.listener))
        def health():
            DesktopApp._poll_health(app)
            executor.run_next()
        health()
        for _ in range(3):
            self.runtime.is_running = False
            self.runtime.events = [dict(kind='error', code='storage_path_unsafe')]
            self.panel.poll(); health()
            self.now += 14.9; self.panel.poll(); health()
            self.assertEqual(sent, [])
            self.now += .1; self.panel.poll(); health()
            self.assertEqual(sent, [])
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='storage_path_unsafe')]
        self.panel.poll(); health()
        self.assertEqual([notice.kind for notice in sent], ['fault'])
        self.assertEqual(len(self.runtime.calls), 4)

    def test_unreaped_child_cannot_mute_forever_or_spawn_duplicate_producer(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.stop = lambda: None  # Controlled child ignores stop; never reaped.
        self.now += 8.1; self.panel.poll()
        for _ in range(3):
            self.now += 15; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertFalse(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.assertEqual(self.listener._recovery.attempts, 3)

    def test_synchronous_restart_failure_is_still_bounded(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='error', code='worker_failed')]
        self.panel.poll()
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('PRIVATE provider/path')
        self.runtime.start = fail
        for _ in range(3):
            self.now += 15; self.panel.poll()
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.assertNotIn('PRIVATE', self.listener.status.get())

    def test_expected_stop_completion_keeps_scheduled_recovery(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.now += 8.1; self.panel.poll()
        self.runtime.events = [dict(kind='finished', mode='listener')]
        self.panel.poll()
        self.assertTrue(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.now += 15; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 2)

    def test_unexpected_clean_finish_restarts_instead_of_alerting_immediately(self):
        from tests.test_desktop_rtsp_audio import RtspControlsTests
        RtspControlsTests.start_watch(self)
        self.runtime.is_running = False
        self.runtime.events = [dict(kind='finished', mode='listener')]
        self.panel.poll()
        self.assertTrue(self.listener.health_snapshot(self.now)['recovery_pending'])
        self.now += 15; self.panel.poll()
        self.assertEqual(len(self.runtime.calls), 2)


if __name__ == '__main__':
    unittest.main()
