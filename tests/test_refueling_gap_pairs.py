"""Post-gap acoustic associations are partial evidence, never payable sessions."""
import tempfile
import unittest
from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore, TTL_SECONDS
from smart_tapo.desktop_refueling import session_values


class GapPairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now=1000
        self.store=RefuelingStore(self.temp.name,'cam',clock=lambda:self.now)
        self.addCleanup(lambda:self.store.close())
        self.old=self.event('old','paid_ready',100).updates[0].session.id
        self.store.mark_audio_gap(200)
        self.jobs=[tuple(row) for row in self.store._db.execute('SELECT * FROM jobs ORDER BY rowid')]

    def event(self,key,kind,at):
        return self.store.accept(key,ConfirmedAnnouncement(kind,at,at+1))

    def pairs(self,limit=200):
        return [r for r in self.store.list_history(limit) if r['row_type']=='observation_pair']

    def test_thirteen_seconds_form_one_partial_pair_without_closing_old_round_or_api(self):
        self.event('PRIVATE-ready-key','paid_ready',300)
        self.event('PRIVATE-end-key','end',313)
        rows=self.store.list_history()
        self.assertEqual(len(rows),2)
        pair=self.pairs()[0]
        self.assertEqual((pair['observed_started_at'],pair['observed_last_ready_at'],pair['observed_end_at'],pair['observed_ready_count']),(300,300,313,1))
        self.assertNotIn('PRIVATE',repr(rows))
        self.assertEqual(pair['evidence'],[])
        self.assertIsNone(self.store.get_session(self.old)['ended_at'])
        self.assertEqual(len(self.store.list_sessions()),1)
        self.assertEqual(self.jobs,[tuple(r) for r in self.store._db.execute('SELECT * FROM jobs ORDER BY rowid')])
        values=session_values(pair)
        self.assertIn('ข้อมูลรอบไม่ครบ',values[4]); self.assertNotIn('จบแล้ว',values[4])
        self.assertEqual(len(self.pairs(1)),1,'History limit must not split a linked pair')

    def test_second_gap_even_without_active_session_prevents_pairing(self):
        self.event('r','paid_ready',300); self.store.mark_audio_gap(305)
        self.event('e','end',313)
        self.assertEqual(self.pairs(),[])
        self.assertEqual(len(self.store.list_history()),3)

    def test_close_reopen_reader_preserves_candidate_and_duplicate_does_not_add_count(self):
        self.event('r','paid_ready',300)
        self.store.close();self.store=RefuelingStore(self.temp.name,'cam',clock=lambda:self.now)
        self.event('r','paid_ready',300)
        self.event('e','end',313); self.event('e','end',313)
        self.assertEqual(len(self.pairs()),1)
        self.assertEqual(self.pairs()[0]['observed_ready_count'],1)

    def test_repeated_readies_link_once_and_overlapping_detection_does_not_count(self):
        self.event('r1','paid_ready',300)
        self.event('overlap','paid_ready',300.02)
        self.event('r2','paid_ready',305)
        self.event('e','end',313)
        pair=self.pairs()[0]
        self.assertEqual((pair['observed_started_at'],pair['observed_last_ready_at'],pair['observed_ready_count']),(300,305,2))

    def test_end_after_existing_missing_end_deadline_is_not_linked(self):
        self.event('r','paid_ready',300);self.event('e','end',601)
        self.assertEqual(self.pairs(),[])

    def test_delayed_ready_predating_gap_does_not_link_across_boundary(self):
        self.event('r','paid_ready',150);self.event('e','end',213)
        self.assertEqual(self.pairs(),[])

    def test_legacy_unassociated_rows_are_not_backfilled_from_timestamps(self):
        self.event('r','paid_ready',300);self.event('e','end',313)
        self.store._db.execute('DELETE FROM partial_audio_members')
        before=self.store._db.total_changes
        self.assertEqual(self.pairs(),[])
        self.assertEqual(len(self.store.list_history()),3)
        self.assertEqual(self.store._db.total_changes,before)

    def test_association_and_candidate_rollback_with_real_checkpoint(self):
        from unittest.mock import patch
        with patch.object(self.store,'_save_engine',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):self.event('r','paid_ready',300)
        self.assertIsNone(self.store._meta('partial_audio_checkpoint'))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM partial_audio_members').fetchone()[0],0)
        self.event('r','paid_ready',300);self.event('e','end',313)
        self.assertEqual(len(self.pairs()),1)

    def test_next_normal_round_works_and_history_read_is_not_a_backfill(self):
        self.event('r','paid_ready',300);self.event('e','end',313)
        fresh=self.event('new','paid_ready',400).updates[0].session.id
        self.event('done','end',413)
        before=self.store._db.total_changes
        rows=self.store.list_history()
        self.assertEqual(self.store._db.total_changes,before)
        self.assertEqual(len(self.pairs()),1)
        self.assertEqual(self.store.get_session(fresh)['state'],'completed')
        self.assertEqual(len(rows),3)

    def test_cleanup_removes_associations_and_no_expired_candidate_is_used(self):
        self.event('r','paid_ready',300);self.event('e','end',313)
        self.now+=TTL_SECONDS;self.store.cleanup()
        self.assertEqual(self.store.list_history(),[])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM partial_audio_members').fetchone()[0],0)

    def test_sustained_repeats_cannot_retain_first_evidence_past_ttl(self):
        self.event('r','paid_ready',300)
        # Continuous repeats inside the existing 300-second deadline, but
        # spanning the complete retention period. No devices or real waiting.
        for at in range(599,1000+TTL_SECONDS+600,299):
            self.now=max(self.now,at+2)
            self.event('repeat-'+str(at),'paid_ready',at)
        self.store.cleanup()
        checkpoint=self.store._meta('partial_audio_checkpoint')
        self.assertTrue(checkpoint is None or checkpoint['created']>self.now-TTL_SECONDS,
                        'Latest Ready must not indefinitely preserve the initial evidence')

    def test_cleanup_expires_unfinished_candidate_from_creation(self):
        self.event('r','paid_ready',300)
        self.assertIsNotNone(self.store._meta('partial_audio_checkpoint'))
        self.now+=TTL_SECONDS
        self.store.cleanup()
        self.assertIsNone(self.store._meta('partial_audio_checkpoint'))

    def test_real_history_reader_and_tk_table_show_one_partial_pair_without_image_request(self):
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from smart_tapo.smoke_window import install
        install()
        import tkinter as tk
        from smart_tapo.desktop_refueling import _query, RefuelingHistoryPanel
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'refueling';now=time.time()
            store=RefuelingStore(directory,'cam')
            try:
                store.accept('old',ConfirmedAnnouncement('paid_ready',now-100,now-99))
                store.mark_audio_gap(now-80)
                store.accept('r',ConfirmedAnnouncement('paid_ready',now-20,now-19))
                store.accept('e',ConfirmedAnnouncement('end',now-7,now-6))
            finally:store.close()
            result=_query(directory,'cam',False,None)
            root=tk.Tk();root.withdraw()
            try:
                panel=RefuelingHistoryPanel(root,lambda:SimpleNamespace(data_dir=Path(temporary),camera_id='cam'))
                panel.pack();root.deiconify();panel._render(result);root.update()
                self.assertTrue(panel.tree.winfo_ismapped())
                self.assertEqual(len(panel.tree.get_children()),2)
                pair=next(row for row in result['rows'] if row['row_type']=='observation_pair')
                values=panel.tree.item(pair['id'],'values')
                self.assertEqual(values[2],'1')
                self.assertIn('ข้อมูลรอบไม่ครบ',values[4]);self.assertNotIn('จบแล้ว',values[4])
                self.assertNotEqual(values[0],'ไม่พบ');self.assertNotEqual(values[3],'ไม่พบ')
                panel.tree.selection_set(pair['id']);panel._selection();panel.show_image()
                self.assertIsNone(panel.request);self.assertIsNone(panel.viewer)
                panel.close()
            finally:root.destroy()
