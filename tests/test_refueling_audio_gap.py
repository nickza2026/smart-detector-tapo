"""Disposable SQLite tests for uncertain source boundaries (no audio/device IO)."""
import json
import tempfile
import unittest

from smart_tapo.refueling import ConfirmedAnnouncement, RefuelingEngine
from smart_tapo.refueling_store import RefuelingStore, TTL_SECONDS
from smart_tapo.refueling_contract import validate_event
from smart_tapo.desktop_refueling import session_values


def ready(at):
    return ConfirmedAnnouncement('paid_ready', at, at+1)


def end(at):
    return ConfirmedAnnouncement('end', at, at+1)


class AudioGapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=1000
        self.store=RefuelingStore(self.temp.name,'cam',clock=lambda:self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_gap_boundary_restart_and_fresh_round(self):
        original=self.store.accept('a',ready(100)).updates[0].session
        old_payload=self.store._db.execute('SELECT payload FROM jobs').fetchone()[0]
        watermark=self.store._meta('checkpoint')['processed_until']
        decision=self.store.mark_audio_gap(500)
        self.assertEqual(len(decision.updates),1)
        row=self.store.get_session(original.id)
        self.assertEqual((row['state'],row['ended_at'],row['revision']),('interrupted',None,2))
        self.assertIsNone(row['missing_end_at'])
        self.assertIn('หลักฐานเสียงขาดช่วง / ไม่ทราบเวลาจบ',session_values(row)[4])
        self.assertEqual(self.store._meta('checkpoint')['processed_until'],watermark)
        self.assertIsNone(self.store._meta('checkpoint')['current'])
        self.assertIsNone(self.store._meta('checkpoint')['last_utterance'])
        jobs=self.store._db.execute('SELECT payload FROM jobs ORDER BY rowid').fetchall()
        self.assertEqual(jobs[0][0],old_payload)
        payload=validate_event(bytes(jobs[1][0]))
        self.assertEqual(payload['event_type'],'refueling.attention_required')
        self.assertEqual(payload['session']['issues'][0]['code'],'audio_source_interrupted')
        self.assertEqual(self.store.mark_audio_gap(600).updates,())
        self.store.tick(900)
        self.assertIsNone(self.store.get_session(original.id)['missing_end_at'])
        other=RefuelingStore(self.temp.name,'cam',clock=lambda:self.now)
        try:
            ignored=other.accept('b',ready(910))
            self.assertEqual(ignored.ignored_reason,'awaiting_end_after_source_gap')
            observation=other._db.execute("SELECT session_id,reason FROM observations WHERE source_key='b'").fetchone()
            self.assertIsNone(observation[0])
            self.assertEqual(other.accept('b',ready(910)).ignored_reason,'duplicate_source_key')
            other.accept('c',end(920))
            fresh=self.store.accept('d',ready(930)).updates[-1].session
            self.assertNotEqual(fresh.id,original.id)
            self.assertEqual(fresh.ready_count,1)
            self.assertIsNone(self.store.get_session(original.id)['ended_at'])
        finally:
            other.close()

    def test_closed_store_restart_preserves_boundary(self):
        first=self.store.accept('a',ready(100)).updates[0].session
        self.store.mark_audio_gap(200)
        self.store.close()
        self.store=RefuelingStore(self.temp.name,'cam',clock=lambda:self.now)
        self.assertEqual(self.store.accept('b',ready(300)).ignored_reason,'awaiting_end_after_source_gap')
        self.store.accept('c',end(310))
        self.assertIsNone(self.store.get_session(first.id)['ended_at'])
        self.assertNotEqual(self.store.accept('d',ready(320)).updates[0].session.id,first.id)

    def test_idle_gap_and_ttl(self):
        self.assertEqual(self.store.mark_audio_gap().updates,())
        self.assertEqual(self.store.list_sessions(),[])
        first=self.store.accept('a',ready(100)).updates[0].session
        self.store.mark_audio_gap(200)
        self.now+=TTL_SECONDS
        self.store.cleanup()
        self.assertIsNone(self.store.get_session(first.id))
        self.assertTrue(self.store.delivery_counts()['awaiting_end_after_gap'])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_atomic_rollback(self):
        first=self.store.accept('a',ready(100)).updates[0].session
        original=self.store._save_engine
        def fail(engine):
            raise RuntimeError('injected')
        self.store._save_engine=fail
        with self.assertRaises(RuntimeError):self.store.mark_audio_gap(200)
        self.store._save_engine=original
        self.assertEqual(self.store.get_session(first.id)['state'],'open')
        self.assertFalse(self.store.delivery_counts()['awaiting_end_after_gap'])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_existing_missing_issue_and_timestamp_validation(self):
        first=self.store.accept('a',ready(100)).updates[0].session
        self.store.tick(401)
        self.store.mark_audio_gap(500)
        row=self.store.get_session(first.id)
        self.assertEqual(row['missing_end_at'],401)
        for at in (True,-1,float('nan'),253402300800):
            with self.assertRaises(ValueError):self.store.mark_audio_gap(at)

    def test_old_checkpoint_and_strict_optional_field(self):
        engine=RefuelingEngine('cam')
        engine.accept(ready(100))
        checkpoint=engine.checkpoint()
        del checkpoint['current']['source_interrupted_at']
        restored=RefuelingEngine('cam')
        restored.restore_checkpoint(checkpoint)
        self.assertIsNone(restored.current.source_interrupted_at)
        for value in (-1,200):
            checkpoint['current']['source_interrupted_at']=value
            with self.assertRaises(ValueError):restored.restore_checkpoint(checkpoint)
        checkpoint['current'].pop('source_interrupted_at')
        checkpoint['current']['unknown']=None
        with self.assertRaises(ValueError):restored.restore_checkpoint(checkpoint)


if __name__=='__main__':unittest.main()
