"""Store tests use only disposable local directories, never cameras or services."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore, TTL_SECONDS, MAX_IMAGE_BYTES


class RefuelingStoreTests(unittest.TestCase):
    def test_cleanup_and_queue_predicates_use_indexes_not_repeated_full_scans(self):
        for table,column in [('observations','session_id'),('images','session_id'),('jobs','session_id')]:
            plan=' '.join(str(tuple(row)) for row in self.store._db.execute(
                f'EXPLAIN QUERY PLAN SELECT rowid FROM {table} WHERE {column}=?',('unused',)))
            self.assertIn('SEARCH',plan)

    def test_canonical_source_span_is_durable_not_just_current_checkpoint(self):
        self.ready()
        self.store.accept('two',ConfirmedAnnouncement('paid_ready',200,202))
        columns={row[1] for row in self.store._db.execute('PRAGMA table_info(observations)')}
        self.assertTrue({'kind','started_at','ended_at'}.issubset(columns))
        rows=self.store._db.execute('SELECT kind,started_at,ended_at FROM observations ORDER BY created,rowid').fetchall()
        self.assertEqual([tuple(row) for row in rows],[('paid_ready',100.,102.),('paid_ready',200.,202.)])

    def test_expiring_an_old_closed_round_does_not_overtake_pending_new_audio(self):
        self.ready('old',100)
        self.store.accept('end-old',ConfirmedAnnouncement('end',150,152))
        self.now+=TTL_SECONDS-10
        self.ready('recent',self.now-20)
        self.store.accept('end-recent',ConfirmedAnnouncement('end',self.now-10,self.now-8))
        self.now+=10
        self.store.cleanup()
        pending=self.ready('pending-new',self.now-5)
        self.assertEqual(len(pending.updates),1)
        self.assertEqual(pending.updates[0].kind,'started')

    def test_expired_open_round_waits_for_thanks_without_inventing_another_paid_round(self):
        self.ready('old',100);self.now+=TTL_SECONDS
        self.store.cleanup()
        self.assertEqual(self.ready('repeat-after-expiry',self.now+1).updates,())
        self.assertEqual(self.store.list_sessions(),[])
        self.assertTrue(self.store.delivery_counts()['awaiting_end_after_expiry'])
        self.assertEqual(self.store.accept('thanks-after-expiry',ConfirmedAnnouncement('end',self.now+10,self.now+12)).updates,())
        self.assertFalse(self.store.delivery_counts()['awaiting_end_after_expiry'])
        self.assertEqual(len(self.ready('next-paid',self.now+20).updates),1)

    def test_media_budget_failure_keeps_original_and_previous_revision(self):
        sid=self.ready().updates[0].session.id
        source=self.photo()
        with patch('smart_tapo.refueling_store.MAX_TOTAL_MEDIA_BYTES',MAX_IMAGE_BYTES):
            self.store.add_evidence(sid,source,99,source_key='first')
            with self.assertRaises(ValueError):self.store.add_evidence(sid,source,101,source_key='second')
        self.assertTrue(source.exists())
        self.assertEqual(len(self.store.get_session(sid)['evidence']),1)
        self.assertEqual(self.store.get_session(sid)['revision'],2)

    def test_prior_payment_photo_update_keeps_capture_time_but_not_backdated_delivery_event(self):
        sid=self.ready().updates[0].session.id
        self.store.add_evidence(sid,self.photo(),99)
        job=self.store.pending_jobs('dest')[-1]
        payload=json.loads(job['payload'])
        self.assertEqual(payload['occurred_at'],'1970-01-01T00:16:40Z')
        self.assertEqual(payload['session']['evidence'][0]['captured_at'],'1970-01-01T00:01:39Z')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.
        self.store = self.open_store()
        self.addCleanup(self.store.close)

    def open_store(self, **kwargs):
        return RefuelingStore(self.root / 'new', 'cam1', clock=lambda: self.now,
                              source_root=self.root, **kwargs)

    def ready(self, key='one', at=100):
        return self.store.accept(key, ConfirmedAnnouncement('paid_ready', at, at + 2))

    def photo(self, name='sample.png', fmt='PNG'):
        path = self.root / name
        Image.new('RGB', (10, 10), 'red').save(path, format=fmt)
        return path

    def test_restart_duplicate_and_latest_ready_timeout(self):
        sid = self.ready().updates[0].session.id
        other = self.open_store()
        try:
            self.assertEqual(other.accept('one', ConfirmedAnnouncement('paid_ready', 100, 102)).updates, ())
            other.accept('two', ConfirmedAnnouncement('paid_ready', 200, 202))
            self.assertEqual(self.store.tick(500), ())
            update = self.store.tick(501)[0]
            self.assertIsNone(update.session.ended_at)
            self.assertEqual(update.session.ready_count, 2)
            self.assertEqual(self.store.get_session(sid)['revision'], 3)
        finally:
            other.close()

    def test_transaction_failure_does_not_resurrect_memory(self):
        sid = self.ready().updates[0].session.id
        with patch.object(self.store, '_persist_update', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                self.ready('second', 200)
        self.assertEqual(self.store.get_session(sid)['ready_count'], 1)
        self.ready('second', 200)
        self.assertEqual(self.store.get_session(sid)['ready_count'], 2)

    def test_checkpoint_failure_rolls_back_observation_and_jobs(self):
        with patch.object(self.store, '_save_engine', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.ready()
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.pending_jobs('dest'), [])
        self.assertEqual(len(self.ready().updates), 1)

    def test_immutable_retry_ids_and_attempts(self):
        self.ready()
        job = self.store.pending_jobs('dest')[0]
        self.assertIsInstance(job['payload'], bytes)
        self.assertEqual(self.store.pending_jobs('dest')[0]['attempts'], 0)
        self.store.mark_delivery(job['event_id'], 'retry', retry_at=self.now + 10, error_code='timeout')
        self.assertEqual(self.store.pending_jobs('dest'), [])
        self.now += 10
        retry = self.store.pending_jobs('dest')[0]
        self.assertEqual((job['payload'], job['event_id']), (retry['payload'], retry['event_id']))
        self.assertEqual(retry['attempts'], 1)
        self.store.mark_delivery(job['event_id'], 'delivered')
        self.assertEqual(self.store.delivery_counts()['delivered'], 1)
        self.assertEqual(self.store.pending_jobs('dest'), [])

    def test_evidence_revisions_snapshot_source_preserved(self):
        sid = self.ready().updates[0].session.id
        source = self.photo()
        original = source.read_bytes()
        image = self.store.add_evidence(sid, source, 110, source_key='photo-one')
        self.assertEqual(image['sha256'], hashlib.sha256(original).hexdigest())
        source.write_bytes(b'replaced source')
        self.assertEqual(Path(image['path']).read_bytes(), original)
        self.assertEqual(self.store.add_evidence(sid, source, 999, source_key='photo-one'), image)
        jobs = self.store.pending_jobs('dest')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(json.loads(jobs[0]['payload'])['session']['evidence'], [])
        payload = json.loads(jobs[1]['payload'])
        self.assertEqual(payload['event_type'], 'refueling.updated')
        self.assertEqual(len(payload['session']['evidence']), 1)
        self.assertNotIn('path', payload['session']['evidence'][0])
        self.ready('repeat', 200)
        self.assertEqual(self.store.get_session(sid)['revision'], 3)
        self.assertTrue(source.exists())

    def test_completed_round_accepts_photo_without_reopening(self):
        sid = self.ready().updates[0].session.id
        self.store.accept('end', ConfirmedAnnouncement('end', 120, 122))
        self.store.add_evidence(sid, self.photo('sample.jpg', 'JPEG'), 121)
        self.assertEqual(self.store.get_session(sid)['state'], 'completed')
        self.assertEqual(self.store.get_session(sid)['revision'], 3)
        sid2 = self.ready('new', 150).updates[0].session.id
        self.assertNotEqual(sid, sid2)

    def test_reject_invalid_oversize_escape_and_symlink(self):
        sid = self.ready().updates[0].session.id
        invalid = self.root / 'invalid.png'
        invalid.write_bytes(b'not an image')
        with self.assertRaises(ValueError):
            self.store.add_evidence(sid, invalid, 110)
        invalid.write_bytes(b'x' * (MAX_IMAGE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.store.add_evidence(sid, invalid, 110)
        with tempfile.TemporaryDirectory() as outside:
            escape = Path(outside) / 'image.png'
            Image.new('RGB', (1, 1)).save(escape)
            with self.assertRaises(ValueError):
                self.store.add_evidence(sid, escape, 110)
        source = self.photo()
        link = self.root / 'link.png'
        try:
            link.symlink_to(source)
        except OSError:
            pass  # Windows may not grant symlink creation to the test account.
        else:
            with self.assertRaises(ValueError):
                self.store.add_evidence(sid, link, 110)
        with patch('smart_tapo.refueling_store.MAX_IMAGE_PIXELS', 50):
            with self.assertRaises(ValueError):
                self.store.add_evidence(sid, source, 110)
        self.assertEqual(self.store.get_session(sid)['revision'], 1)

    def test_image_failure_cleans_only_new_owned_files(self):
        sid = self.ready().updates[0].session.id
        photo = self.photo()
        with patch.object(self.store, '_persist_update', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.store.add_evidence(sid, photo, 110)
        self.assertEqual(list(self.store.media.iterdir()), [])
        self.assertTrue(photo.exists())
        self.assertEqual(self.store.get_session(sid)['evidence'], [])

    def test_missing_or_corrupt_media_fails_closed(self):
        sid = self.ready().updates[0].session.id
        image = self.store.add_evidence(sid, self.photo(), 110)
        Path(image['path']).write_bytes(b'corrupt')
        jobs = self.store.pending_jobs('dest')
        self.assertEqual(len(jobs), 1)  # the earlier empty-manifest event remains valid
        self.assertEqual(self.store.delivery_counts()['failed'], 1)
        self.assertEqual(self.store.delivery_counts()['delivered'], 0)

    def test_destination_pinning_and_image_receipts(self):
        sid = self.ready().updates[0].session.id
        image = self.store.add_evidence(sid, self.photo(), 110)
        self.store.pending_jobs('first')
        self.assertEqual(self.store.pending_jobs('second'), [])
        self.store.mark_image_uploaded(image['id'], 'first')
        self.assertTrue(self.store.image_uploaded(image['id'], 'first'))
        self.assertFalse(self.store.image_uploaded(image['id'], 'second'))
        self.store.forget_image_uploads('first')
        self.assertFalse(self.store.image_uploaded(image['id'], 'first'))
        self.ready('repeat', 200)
        self.assertEqual(len(self.store.pending_jobs('second')), 1)
        self.assertEqual(len(self.store.pending_jobs('first')), 2)

    def test_ttl_original_creation_boundary_and_preservation(self):
        sid = self.ready().updates[0].session.id
        source = self.photo()
        unrelated = self.store.media / 'unrelated.png'
        unrelated.write_bytes(b'leave alone')
        config = self.root / 'config.local.json'
        config.write_text('preserve', encoding='utf-8')
        first_job = self.store.pending_jobs('dest')[0]
        self.store.mark_delivery(first_job['event_id'], 'delivered')
        self.now += TTL_SECONDS - 1
        image = self.store.add_evidence(sid, source, 120)
        self.assertEqual(self.store.cleanup()['sessions_deleted'], 0)
        self.now += 1
        self.assertEqual(self.store.pending_jobs('dest'), [])
        report = self.store.cleanup()
        self.assertEqual(report['sessions_deleted'], 1)
        self.assertEqual(report['expired_before_delivery'], 1)
        self.assertFalse(Path(image['path']).exists())
        self.assertTrue(source.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(config.exists())
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.delivery_counts()['expired_before_delivery'], 1)
        self.assertEqual(self.ready('old-replay', 130).updates, ())
        self.assertEqual(self.store.cleanup()['expired_before_delivery'], 0)

    def test_expire_completed_and_pending(self):
        self.ready()
        self.store.accept('end', ConfirmedAnnouncement('end', 120, 122))
        self.now += TTL_SECONDS
        report = self.store.cleanup()
        self.assertEqual(report['sessions_deleted'], 1)
        self.assertEqual(report['expired_before_delivery'], 2)
        self.assertEqual(self.store.pending_jobs('dest'), [])

    def test_orphan_end_diagnostic_and_safe_keys(self):
        result = self.store.accept('orphan', ConfirmedAnnouncement('end', 100, 102))
        self.assertEqual(result.ignored_reason, 'completion_without_start')
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.pending_jobs('dest'), [])
        for key in ('../bad', 'x' * 129, 'ไทย', 'bad\n'):
            with self.assertRaises(ValueError):
                self.ready(key)
        with self.assertRaises(ValueError):
            self.store.mark_delivery('anything', 'retry', error_code='secret body !')

    def test_missing_file_and_upload_receipt_never_imply_delivery(self):
        sid = self.ready().updates[0].session.id
        image = self.store.add_evidence(sid, self.photo(), 110)
        self.store.pending_jobs('dest')
        self.store.mark_image_uploaded(image['id'], 'dest')
        Path(image['path']).unlink()
        self.assertEqual(len(self.store.pending_jobs('dest')), 1)
        self.assertEqual(self.store.delivery_counts()['failed'], 1)
        self.assertEqual(self.store.delivery_counts()['delivered'], 0)

    def test_cleanup_failure_is_reported_and_retried(self):
        sid = self.ready().updates[0].session.id
        image = self.store.add_evidence(sid, self.photo(), 110)
        self.now += TTL_SECONDS
        unlink = Path.unlink
        def fail_owned(path, *args, **kwargs):
            if str(path) == image['path']:
                raise PermissionError('injected')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_owned):
            report = self.store.cleanup()
        self.assertEqual(report['media_delete_failed'], 1)
        self.assertTrue(Path(image['path']).exists())
        self.assertIsNone(self.store.get_session(sid))
        self.assertEqual(self.store.cleanup()['media_deleted'], 1)
        self.assertFalse(Path(image['path']).exists())

    def test_crash_intent_cleanup_does_not_scan_unrelated_files(self):
        identifier = 'a' * 32
        owned = self.store.media / (identifier + '.tmp')
        unrelated = self.store.media / ('b' * 32 + '.png')
        with self.store._transaction():
            self.store._db.execute('INSERT INTO file_intents VALUES (?,?)', (identifier, self.now))
        owned.write_bytes(b'partial crash write')
        unrelated.write_bytes(b'not owned by this journal')
        self.now += TTL_SECONDS
        report = self.store.cleanup()
        self.assertEqual(report['media_deleted'], 1)
        self.assertFalse(owned.exists())
        self.assertTrue(unrelated.exists())

    def test_aggregate_overlap_and_sticky_issues_survive_restart(self):
        sid = self.ready().updates[0].session.id
        self.store.accept('overlap', ConfirmedAnnouncement('paid_ready', 100.5, 102.5))
        self.assertEqual(self.store.get_session(sid)['ready_count'], 1)
        self.store.tick(401)
        other = self.open_store()
        try:
            other.accept('repeat', ConfirmedAnnouncement('paid_ready', 450, 452))
            other.accept('end', ConfirmedAnnouncement('end', 460, 462))
        finally:
            other.close()
        session = self.store.get_session(sid)
        self.assertEqual(session['ready_count'], 2)
        self.assertIsNotNone(session['missing_end_at'])
        self.assertIsNotNone(session['repeated_ready_at'])
        self.assertEqual(session['ended_at'], 460)

    def test_concurrent_connections_have_one_current_round(self):
        def accept_one(index):
            connection = self.open_store()
            try:
                connection.accept('source-' + str(index), ConfirmedAnnouncement('paid_ready', 100, 102))
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(accept_one, range(9)))
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['ready_count'], 1)
        self.assertEqual(len(self.store.pending_jobs('dest')), 1)

    def test_camera_ownership(self):
        with self.assertRaises(ValueError):
            RefuelingStore(self.root / 'new', 'different')


if __name__ == '__main__':
    unittest.main()
