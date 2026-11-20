"""Synthetic failures only: never open a camera, user config, or user database."""
import queue
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from smart_tapo.rtsp_audio import rtsp_audio_job, RtspContinuousListener
from tests.test_announcement_stream import profile


class StorageWorkerDiagnosticsTests(unittest.TestCase):
    def run_worker(self, store=None, open_error=None, batch=None):
        final, progress, av = Mock(), Mock(), Mock()
        stop = threading.Event()
        store = store if store is not None else Mock()
        config = SimpleNamespace(camera_id='cam1', data_dir='unused-synthetic',
                                 rtsp_url='rtsp://offline.invalid/not-opened')
        def pcm(*args):
            yield np.zeros((1600, 1), dtype='float32'), 0.
            stop.set()
        with patch.dict('sys.modules', {'av': av}), \
                patch('smart_tapo.rtsp_audio.load_announcement_profile', return_value=profile()), \
                patch('smart_tapo.rtsp_audio.RefuelingStore', side_effect=open_error,
                      return_value=store), \
                patch('smart_tapo.rtsp_audio.decoded_pcm', side_effect=pcm), \
                patch('smart_tapo.rtsp_audio.AnnouncementStream') as core:
            core.return_value.push.return_value = batch or SimpleNamespace(
                observations=(), finalized_until=None, ambiguous_candidates=0)
            rtsp_audio_job(final, progress, stop, config, 'unused', profile().fingerprint, 'f'*64)
        return final.send.call_args.args[0], progress, av, store

    def test_busy_open_reports_specific_safe_stage_before_transport(self):
        error = sqlite3.OperationalError('PRIVATE credentials in provider details')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        result, _, av, _ = self.run_worker(open_error=error)
        self.assertEqual(result['code'], 'storage_busy')
        self.assertEqual(result['stage'], 'storage_open')
        self.assertNotIn('PRIVATE', repr(result))
        av.open.assert_not_called()

    def test_permission_commit_does_not_become_transport_or_tick(self):
        store = Mock()
        store.accept.side_effect = PermissionError(13, 'PRIVATE path')
        batch = SimpleNamespace(observations=(SimpleNamespace(source_key='key',
            announcement=SimpleNamespace(kind='paid_ready')),), finalized_until=123., ambiguous_candidates=0)
        result, _, _, _ = self.run_worker(store=store, batch=batch)
        self.assertEqual(result['code'], 'storage_permission')
        self.assertEqual(result['stage'], 'storage_commit')
        store.tick.assert_not_called()
        store.close.assert_called_once()

    def test_gap_error_still_closes_store_and_preserves_original_stage(self):
        store = Mock()
        store.mark_audio_gap.side_effect = PermissionError(13, 'PRIVATE')
        result, _, av, _ = self.run_worker(store=store)
        self.assertEqual(result['code'], 'storage_permission')
        self.assertEqual(result['stage'], 'storage_gap')
        store.close.assert_called_once()
        av.open.assert_not_called()

    def test_pcm_health_distinguishes_received_zero_samples_from_no_data(self):
        result, progress, _, _ = self.run_worker()
        event = next(call.args[0] for call in progress.put_nowait.call_args_list
                     if call.args[0]['kind'] == 'listener_progress')
        self.assertEqual(event['audio_frames'], 1600)
        self.assertEqual(event['audio_rms'], 0.)
        self.assertEqual(event['stage'], 'persisted')
        self.assertEqual(result['kind'], 'finished')

    def test_partial_commit_retains_count_of_already_persisted_observation(self):
        store = Mock()
        store.accept.side_effect = [None, PermissionError(13, 'PRIVATE')]
        batch = SimpleNamespace(observations=tuple(SimpleNamespace(source_key=f'key-{i}',
            announcement=SimpleNamespace(kind=kind)) for i, kind in enumerate(('paid_ready', 'end'))),
            finalized_until=123., ambiguous_candidates=0)
        result, _, _, _ = self.run_worker(store=store, batch=batch)
        self.assertEqual(result['ready_total'], 1)
        self.assertEqual(result['end_total'], 0)
        self.assertEqual(result['observations_total'], 1)
        store.tick.assert_not_called()

    def test_manual_stop_does_not_hide_storage_failure_during_shutdown(self):
        listener = RtspContinuousListener(clock=lambda: 1.)
        listener._process = Mock()
        listener._process.is_alive.return_value = False
        listener._recv = Mock()
        listener._recv.poll.return_value = False
        listener._progress = Mock()
        listener._progress.get_nowait.side_effect = queue.Empty
        listener._stop = Mock()
        listener._stop_at = 0.
        listener._kill_at = listener._forced = None
        listener._heartbeat = 1.
        listener._result = dict(kind='error', code='storage_full', stage='storage_close')
        result = listener.poll()[-1]
        self.assertEqual(result['code'], 'storage_full')
        self.assertEqual(result['stage'], 'storage_close')
        self.assertFalse(listener.is_running)


if __name__ == '__main__':
    unittest.main()
