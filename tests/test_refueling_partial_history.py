"""Confirmed partial announcements remain visible without inventing sessions."""
import tempfile
import time
import unittest
from pathlib import Path

from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore, TTL_SECONDS
from smart_tapo.desktop_refueling import _query


class PartialHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'refueling'
        self.now = time.time()
        self.store = RefuelingStore(self.root, 'cam1', clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def accept(self, key, kind, offset):
        return self.store.accept(key, ConfirmedAnnouncement(kind, self.now + offset, self.now + offset + 2))

    def test_orphan_end_reaches_actual_history_reader_without_session_or_canonical_job(self):
        self.accept('PRIVATE-provider-key', 'end', -20)
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
        rows = _query(self.root, 'cam1', False, None)['rows']
        self.assertEqual(len(rows), 1, 'Confirmed End was persisted but hidden from history')
        self.assertEqual(rows[0]['row_type'], 'observation')
        self.assertEqual(rows[0]['kind'], 'end')
        self.assertNotIn('PRIVATE-provider-key', repr(rows))
        self.assertNotIn('ready_count', rows[0])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_ready_without_end_and_normal_completed_round_are_not_duplicated(self):
        self.accept('ready', 'paid_ready', -60)
        self.assertEqual(len(self.store.list_history()), 1)
        self.assertEqual(self.store.list_history()[0]['row_type'], 'session')
        self.accept('end', 'end', -30)
        self.assertEqual(len(self.store.list_history()), 1)
        self.assertEqual(self.store.list_history()[0]['state'], 'completed')
        self.assertNotIn('row_type', self.store.list_sessions()[0])

    def test_gap_ready_and_end_are_visible_without_closing_previous_round(self):
        sid = self.accept('original', 'paid_ready', -100).updates[0].session.id
        self.store.mark_audio_gap(self.now - 80)
        self.accept('guarded-ready', 'paid_ready', -60)
        self.accept('orphan-end', 'end', -20)
        before = self.store._db.total_changes
        rows = self.store.list_history()
        self.assertEqual(self.store._db.total_changes, before)
        self.assertEqual([r['row_type'] for r in rows], ['observation_pair', 'session'])
        self.assertEqual((rows[0]['observed_started_at'],rows[0]['observed_end_at']),
                         (self.now-60,self.now-20))
        self.assertIsNone(self.store.get_session(sid)['ended_at'])
        self.assertEqual(self.store.get_session(sid)['ready_count'], 1)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_ignored_observation_with_existing_session_id_is_still_visible(self):
        self.accept('ready', 'paid_ready', -40)
        self.accept('stale-end', 'end', -60)
        rows = self.store.list_history()
        self.assertEqual(len(rows), 2)
        ignored = next(r for r in rows if r['row_type'] == 'observation')
        self.assertEqual(ignored['reason'], 'duplicate_or_stale_observation')
        self.assertIsNotNone(self.store._db.execute(
            "SELECT session_id FROM observations WHERE source_key='stale-end'").fetchone()[0])

    def test_limit_ties_expiry_and_legacy_observations_without_backfill(self):
        for number in range(205): self.accept(f'orphan-{number:03}', 'end', -60)
        rows = self.store.list_history(999)
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({r['id'] for r in rows}), 200)
        self.assertEqual(rows, self.store.list_history(999))
        other = RefuelingStore(self.root, 'cam1', clock=lambda: self.now)
        try: self.assertEqual(other.list_history(999), rows)
        finally: other.close()
        self.assertEqual(self.store.list_history(0), [])
        self.now += TTL_SECONDS
        self.assertEqual(self.store.list_history(), [])

    def test_recent_observation_is_not_hidden_by_expired_but_not_cleaned_session(self):
        sid = self.accept('ready', 'paid_ready', -60).updates[0].session.id
        self.accept('end', 'end', -20)
        self.store._db.execute('UPDATE sessions SET created=? WHERE id=?',
                               (self.now-TTL_SECONDS-1, sid))
        rows = self.store.list_history()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['row_type'] == 'observation' for row in rows))


class PartialHistoryPanelTests(unittest.TestCase):
    def test_actual_reader_end_only_reaches_table_without_evidence_request(self):
        from smart_tapo.smoke_window import install
        install()
        import tkinter as tk
        from smart_tapo.desktop_refueling import RefuelingHistoryPanel, session_values
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'refueling'
            with_store = RefuelingStore(directory, 'cam1')
            now = time.time()
            try: with_store.accept('end-only', ConfirmedAnnouncement('end', now-20, now-18))
            finally: with_store.close()
            data = _query(directory, 'cam1', False, None)
            root = tk.Tk(); root.withdraw()
            try:
                panel = RefuelingHistoryPanel(root, lambda: SimpleNamespace(data_dir=Path(temporary), camera_id='cam1'))
                panel._render(data)
                identifiers = panel.tree.get_children()
                self.assertEqual(len(identifiers), 1)
                values = panel.tree.item(identifiers[0], 'values')
                self.assertEqual(values[:3], ('ไม่พบ', 'ไม่พบ', '—'))
                self.assertIn('พบเสียงจบ', values[4])
                self.assertNotIn('จบแล้ว', values[4])
                panel.tree.selection_set(identifiers[0]); panel._selection(); panel.show_image()
                self.assertIsNone(panel.request)
                self.assertIsNone(panel.viewer)
                private = dict(data['rows'][0], reason='PRIVATE-provider-url')
                self.assertNotIn('PRIVATE', str(session_values(private)))
                panel.close()
            finally: root.destroy()


if __name__ == '__main__':
    unittest.main()
