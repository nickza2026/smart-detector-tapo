"""Revalidate a zero-link ephemeral leaf once; never accept unsafe metadata."""
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest.mock import patch, Mock

from smart_tapo.refueling_store import _safe, RefuelingStore
from smart_tapo.storage_diagnostics import (StoragePathUnsafeError, StorageMetadataBusyError,
    classify_storage_error, storage_failure_event)


def metadata(links, attributes=32):
    return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=links,
                           st_file_attributes=attributes, st_reparse_tag=0)


class EphemeralGuardRaceTests(unittest.TestCase):
    def run_guard(self, sequence, component='journal', *, ancestor=False):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'ephemeral'
            calls = []
            real = Path.lstat
            def snapshot(path, *args, **kwargs):
                if path == (target.parent if ancestor else target):
                    index = min(len(calls), len(sequence)-1)
                    calls.append(path)
                    value = sequence[index]
                    if isinstance(value, Exception): raise value
                    return value
                return real(path, *args, **kwargs)
            with patch.object(Path, 'lstat', snapshot):
                try:
                    result = _safe(target, component=component)
                    return result == target, None, len(calls)
                except StoragePathUnsafeError as error:
                    self.last_error = error
                    return False, error.guard, len(calls)

    def test_zero_then_absent_ephemeral_leaf_is_safe_not_a_hardlink(self):
        for component in ('journal', 'wal', 'shm'):
            with self.subTest(component=component):
                safe, evidence, calls = self.run_guard([metadata(0), FileNotFoundError()], component)
                self.assertTrue(safe)
                self.assertIsNone(evidence)
                self.assertEqual(calls, 2)

    def test_zero_then_current_single_link_is_revalidated(self):
        safe, evidence, calls = self.run_guard([metadata(0), metadata(1)])
        self.assertEqual((safe, evidence, calls), (True, None, 2))

    def test_persistent_zero_is_still_rejected_after_one_recheck(self):
        safe, evidence, calls = self.run_guard([metadata(0)])
        self.assertFalse(safe)
        self.assertEqual((evidence['links'], calls), (0, 2))
        self.assertEqual(classify_storage_error(self.last_error), 'storage_busy')
        event = storage_failure_event(self.last_error, 'storage_commit')
        self.assertEqual((event['code'], event['guard']['links']), ('storage_busy', 0))

    def test_multiple_links_or_reparse_are_rejected_without_recheck(self):
        tagged = metadata(0); tagged.st_reparse_tag = 0xA0000003
        for info in (metadata(2), metadata(0, 0x400), tagged):
            safe, evidence, calls = self.run_guard([info, metadata(1)])
            self.assertFalse(safe)
            self.assertEqual(calls, 1)
            self.assertEqual(classify_storage_error(self.last_error), 'storage_path_unsafe')

    def test_unsafe_second_snapshot_is_rejected_with_its_exact_evidence(self):
        for info in (metadata(2), metadata(0, 0x400)):
            safe, evidence, calls = self.run_guard([metadata(0), info])
            self.assertFalse(safe)
            self.assertEqual((evidence['links'], evidence['attributes'], calls),
                             (info.st_nlink, info.st_file_attributes, 2))
            self.assertEqual(classify_storage_error(self.last_error), 'storage_path_unsafe')

    def test_database_media_and_ancestors_do_not_gain_ephemeral_retry(self):
        for component, ancestor in (('database', False), ('media', False), ('path', False), ('journal', True)):
            safe, evidence, calls = self.run_guard([metadata(0), metadata(1)], component, ancestor=ancestor)
            self.assertFalse(safe)
            self.assertEqual((evidence['links'], calls), (0, 1))
            self.assertEqual(classify_storage_error(self.last_error), 'storage_path_unsafe')

    def test_zero_metadata_refuses_transaction_before_any_sql(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RefuelingStore(Path(temporary)/'store', 'cam')
            leaf = Path(str(store.db_path)+'-journal'); real = Path.lstat
            def snapshot(path, *args, **kwargs):
                return metadata(0) if path == leaf else real(path, *args, **kwargs)
            connection = Mock()
            try:
                with patch.object(Path, 'lstat', snapshot), patch.object(store, '_db', connection):
                    with self.assertRaises(StoragePathUnsafeError) as caught:
                        with store._transaction():
                            connection.execute('MUST NEVER EXECUTE')
                    self.assertEqual(classify_storage_error(caught.exception), 'storage_busy')
                    connection.execute.assert_not_called()
            finally: store.close()

    def test_busy_subtype_cannot_reclassify_hardlink_reparse_or_unknown_evidence(self):
        valid = dict(reason='non_single_link', component='journal', depth=0,
                     mode=stat.S_IFREG, links=0, attributes=32, reparse_tag=0)
        for evidence in (None, dict(valid, links=2), dict(valid, component='database'),
                         dict(valid, depth=1), dict(valid, attributes=0x400), dict(valid, mode=None),
                         dict(valid, reparse_tag=0xA0000003)):
            self.assertEqual(classify_storage_error(StorageMetadataBusyError(guard=evidence)),
                             'storage_path_unsafe')
