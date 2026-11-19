"""Actual disposable SQLite lock/reopen/idempotency; no transport or user files."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore
from smart_tapo.storage_diagnostics import classify_storage_error


class StorageRecoveryIntegrationTests(unittest.TestCase):
    def test_lock_then_release_reuses_observation_key_and_preserves_outbox(self):
        with TemporaryDirectory() as directory:
            store = RefuelingStore(Path(directory)/'refueling', 'cam1', clock=lambda: 1000.,
                                   busy_timeout_seconds=.02)
            lock = sqlite3.connect(str(store.db_path), isolation_level=None, timeout=.02)
            try:
                store.accept('first', ConfirmedAnnouncement('paid_ready', 990., 992.))
                lock.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    store.accept('second', ConfirmedAnnouncement('paid_ready', 993., 995.))
                self.assertEqual(classify_storage_error(caught.exception), 'storage_busy')
                lock.execute('ROLLBACK')
                store.accept('second', ConfirmedAnnouncement('paid_ready', 993., 995.))
                replay = store.accept('second', ConfirmedAnnouncement('paid_ready', 993., 995.))
                self.assertEqual(replay.ignored_reason, 'duplicate_source_key')
                self.assertEqual(store.list_sessions()[0]['ready_count'], 2)
                self.assertEqual(store._db.execute('SELECT count(*) FROM observations').fetchone()[0], 2)
                self.assertEqual(store._db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
            finally:
                lock.close()
                store.close()

    def test_locked_open_recovers_without_replacing_old_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/'refueling'
            store = RefuelingStore(root, 'cam1', clock=lambda: 1000.)
            store.accept('old', ConfirmedAnnouncement('paid_ready', 990., 992.))
            lock = sqlite3.connect(str(store.db_path), isolation_level=None, timeout=.02)
            try:
                store.close()
                lock.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    RefuelingStore(root, 'cam1', busy_timeout_seconds=.02)
                self.assertEqual(classify_storage_error(caught.exception), 'storage_busy')
                lock.execute('ROLLBACK')
                reopened = RefuelingStore(root, 'cam1', clock=lambda: 1001., busy_timeout_seconds=.02)
                try:
                    self.assertEqual(reopened.list_sessions()[0]['ready_count'], 1)
                    self.assertEqual(reopened._db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
                    reopened.mark_audio_gap()
                    decision = reopened.accept('after-gap', ConfirmedAnnouncement('paid_ready', 1002., 1003.))
                    self.assertEqual(decision.ignored_reason, 'awaiting_end_after_source_gap')
                    self.assertEqual(len(reopened.list_sessions()), 1)
                finally:
                    reopened.close()
            finally:
                lock.close()
