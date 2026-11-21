"""Offline diagnostics tests; all stores belong to disposable directories."""
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from smart_tapo.refueling_store import RefuelingStore


class StoreTypedFailureTests(unittest.TestCase):
    def test_camera_mismatch_is_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RefuelingStore(directory, 'cam1')
            store.close()
            with self.assertRaises(ValueError) as caught:
                RefuelingStore(directory, 'cam2')
            self.assertEqual(type(caught.exception).__name__, 'StorageCameraMismatchError')

    def test_root_path_is_typed(self):
        with self.assertRaises(ValueError) as caught:
            RefuelingStore(Path.cwd().anchor, 'cam1')
        self.assertEqual(type(caught.exception).__name__, 'StoragePathUnsafeError')

    def test_hardlinked_database_is_typed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'owned test data')
            target = root / 'store'
            target.mkdir()
            os.link(source, target / 'refueling.sqlite3')
            with self.assertRaises(ValueError) as caught:
                RefuelingStore(target, 'cam1')
            self.assertEqual(type(caught.exception).__name__, 'StoragePathUnsafeError')
            self.assertEqual(source.read_bytes(), b'owned test data')


class StorageClassificationTests(unittest.TestCase):
    def test_sqlite_codes_including_extended_codes(self):
        from smart_tapo.storage_diagnostics import classify_storage_error
        cases = {
            sqlite3.SQLITE_BUSY: 'storage_busy', sqlite3.SQLITE_LOCKED: 'storage_busy',
            sqlite3.SQLITE_FULL: 'storage_full', sqlite3.SQLITE_READONLY: 'storage_permission',
            sqlite3.SQLITE_PERM: 'storage_permission', sqlite3.SQLITE_AUTH: 'storage_permission',
            sqlite3.SQLITE_CORRUPT: 'storage_corrupt', sqlite3.SQLITE_NOTADB: 'storage_corrupt',
            sqlite3.SQLITE_SCHEMA: 'storage_schema', sqlite3.SQLITE_IOERR: 'storage_io_error',
            sqlite3.SQLITE_ERROR: 'storage_error', sqlite3.SQLITE_CONSTRAINT: 'storage_error',
        }
        for code, expected in cases.items():
            for extended in (code, code | (3 << 8)):
                with self.subTest(code=extended):
                    exc = sqlite3.OperationalError('secret path SQL rtsp://password')
                    exc.sqlite_errorcode = extended
                    self.assertEqual(classify_storage_error(exc), expected)

    def test_os_codes_and_windows_codes(self):
        from smart_tapo.storage_diagnostics import classify_storage_error
        for code, expected in ((errno.ENOSPC, 'storage_full'),
                               (errno.EACCES, 'storage_permission'),
                               (errno.EPERM, 'storage_permission'),
                               (errno.EROFS, 'storage_permission'),
                               (errno.EBUSY, 'storage_busy'),
                               (errno.EAGAIN, 'storage_busy'),
                               (errno.EIO, 'storage_io_error'),
                               (errno.ENOENT, 'storage_error')):
            self.assertEqual(classify_storage_error(OSError(code, 'secret')), expected)
        for code, expected in ((5, 'storage_permission'), (19, 'storage_permission'),
                               (32, 'storage_busy'), (33, 'storage_busy'),
                               (39, 'storage_full'), (112, 'storage_full'),
                               (1117, 'storage_io_error'), (3, 'storage_error')):
            exc = OSError('secret')
            exc.winerror = code
            self.assertEqual(classify_storage_error(exc), expected)

    def test_typed_errors_and_untrusted_messages(self):
        from smart_tapo.storage_diagnostics import (
            classify_storage_error, StoragePathUnsafeError,
            StorageCameraMismatchError, StorageSchemaError,
        )
        for kind, expected in ((StoragePathUnsafeError, 'storage_path_unsafe'),
                               (StorageCameraMismatchError, 'storage_camera_mismatch'),
                               (StorageSchemaError, 'storage_schema')):
            self.assertEqual(classify_storage_error(kind('secret')), expected)
        for exc in (ValueError('Store belongs to another camera'),
                    sqlite3.OperationalError('database is locked'),
                    RuntimeError('database disk image is malformed'),
                    IOError('unclassified')):
            self.assertEqual(classify_storage_error(exc), 'storage_error')

    def test_event_allowlists_and_recovery_boundary(self):
        from smart_tapo.storage_diagnostics import storage_failure_event, STORAGE_RECOVERY_CODES
        self.assertEqual(STORAGE_RECOVERY_CODES, frozenset({
            'storage_busy', 'storage_full', 'storage_permission', 'storage_io_error'}))
        for stage in ('storage_open', 'storage_gap', 'storage_commit', 'storage_cleanup', 'storage_close'):
            self.assertEqual(storage_failure_event(ValueError('secret SQL/path/url'), stage),
                             {'code': 'storage_error', 'stage': stage})
        for stage in ('secret/path', '', None, [], 'storage_open\n'):
            with self.assertRaises(ValueError):
                storage_failure_event(ValueError('secret'), stage)

    def test_unsupported_schema_is_typed_and_not_repaired(self):
        from smart_tapo.storage_diagnostics import classify_storage_error
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'refueling.sqlite3'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE observations(secret TEXT)')
                connection.execute("INSERT INTO observations VALUES ('preserve')")
            connection.close()
            before = db.read_bytes()
            with self.assertRaises(ValueError) as caught:
                RefuelingStore(directory, 'cam1')
            self.assertEqual(classify_storage_error(caught.exception), 'storage_schema')
            self.assertEqual(db.read_bytes(), before)

    def test_real_non_database_file_is_corrupt_and_preserved(self):
        from smart_tapo.storage_diagnostics import classify_storage_error
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'refueling.sqlite3'
            original = b'owned test non-database contents'
            db.write_bytes(original)
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                RefuelingStore(directory, 'cam1')
            self.assertEqual(classify_storage_error(caught.exception), 'storage_corrupt')
            self.assertEqual(db.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
