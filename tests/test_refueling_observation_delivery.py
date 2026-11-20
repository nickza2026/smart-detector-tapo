"""Synthetic announcements, disposable SQLite and injected HTTP only."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
import threading
import time
from unittest.mock import patch

from smart_tapo.refueling_contract import validate_observation, validate_event
from smart_tapo.storage_diagnostics import StorageSchemaError
from smart_tapo.refueling_api_log import ApiAttemptLog

from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore, TTL_SECONDS
from smart_tapo.refueling_webhook import WebhookTarget, RefuelingWebhookClient, dispatch_one


class ObservationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.
        self.store = RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.requests = []
        self.timeout = False
        outer = self

        class Opener:
            def open(self, req, timeout):
                outer.requests.append(req)
                if outer.timeout:
                    raise TimeoutError('synthetic timeout')
                response = io.BytesIO(json.dumps({'accepted': True,
                    'event_id': req.headers['Idempotency-key']}).encode())
                response.code = 201
                response.headers = {}
                return response

        self.target = WebhookTarget('https://receiver.invalid/api', 'synthetic-token-0123456789', 'cam1')
        self.client = RefuelingWebhookClient(self.target, self.store.media, opener=Opener())

    def accept(self, key='private-source', kind='end', start=100):
        return self.store.accept(key, ConfirmedAnnouncement(kind, start, start+2))

    def send(self):
        return dispatch_one(self.store, self.client, clock=lambda: self.now)

    def test_orphan_end_reaches_actual_dispatch_and_http_ack(self):
        self.accept()
        result = self.send()
        self.assertIsNotNone(result, 'confirmed orphan end must reach transport')
        self.assertEqual(result.status, 'delivered')
        self.assertEqual(len(self.requests), 1)
        req = self.requests[0]
        self.assertEqual(req.full_url, 'https://receiver.invalid/api/v1/refueling/observations')
        value = json.loads(req.data)
        self.assertEqual(value['event_type'], 'refueling.observation')
        self.assertEqual(value['observation']['kind'], 'end')
        self.assertEqual(value['observation']['evidence'], [])
        self.assertIsNone(value['observation']['amount_baht'])
        self.assertNotIn(b'private-source', req.data)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.store.delivery_counts()['delivered'], 1)
        self.assertIsNone(self.send())

    def reopen(self):
        self.store.close()
        self.store = RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)

    def jobs(self):
        return self.store.pending_jobs(self.target.destination_key, limit=100)

    def test_timeout_reopen_retains_bytes_hash_identity_and_original_ttl(self):
        self.accept()
        before = self.jobs()[0]
        self.timeout = True
        self.assertEqual(self.send().status, 'retry')
        self.assertEqual(self.store.delivery_counts()['queued'], 1)
        self.reopen()
        self.assertEqual(self.jobs(), [])
        self.now += 6
        after = self.jobs()[0]
        for field in ('event_id', 'payload', 'expires_at', 'destination_key'):
            self.assertEqual(before[field], after[field])
        self.assertEqual(after['attempts'], 1)
        self.timeout = False
        self.assertEqual(self.send().status, 'delivered')
        self.assertEqual(self.requests[0].data, self.requests[1].data)
        self.assertEqual(self.requests[0].headers['Idempotency-key'], self.requests[1].headers['Idempotency-key'])

    def test_gap_ready_end_share_only_known_audio_group_and_canonical_bytes_unchanged(self):
        self.accept('ready', 'paid_ready', 100)
        self.store.mark_audio_gap(200)
        self.jobs()  # Destination binding is unrelated to immutable fact bytes.
        canonical = [tuple(row) for row in self.store._db.execute('SELECT * FROM jobs')]
        self.accept('partial-ready', 'paid_ready', 300)
        self.accept('partial-end', 'end', 313)
        partial = [json.loads(job['payload'])['observation'] for job in self.jobs() if job.get('kind')=='observation']
        self.assertEqual(len(partial), 2)
        self.assertIsNotNone(partial[0]['audio_group_id'])
        self.assertEqual(partial[0]['audio_group_id'], partial[1]['audio_group_id'])
        self.assertEqual([p['kind'] for p in partial], ['paid_ready', 'end'])
        self.assertEqual(canonical, [tuple(row) for row in self.store._db.execute('SELECT * FROM jobs')])
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM images').fetchone()[0], 0)
        for _ in range(4):
            self.assertEqual(self.send().status, 'delivered')
        self.assertEqual(sum(r.full_url.endswith('/observations') for r in self.requests), 2)
        self.assertEqual(sum(r.full_url.endswith('/events') for r in self.requests), 2)

    def test_normal_cycle_has_no_observations_and_validates_legacy_body(self):
        self.accept('r', 'paid_ready', 100)
        self.accept('e', 'end', 120)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            value = validate_event(job['payload'], 'cam1', job['event_id'])
            self.assertNotIn('observation', value)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM observation_jobs').fetchone()[0], 0)
        for _ in range(2):
            self.assertEqual(self.send().status, 'delivered')
        self.assertTrue(all(req.full_url.endswith('/events') for req in self.requests))

    def test_duplicate_source_stale_overlap_are_not_extra_jobs(self):
        self.accept('r', 'paid_ready', 100)
        self.store.mark_audio_gap(200)
        self.accept('p1', 'paid_ready', 300)
        self.assertEqual(self.accept('p1', 'paid_ready', 310).ignored_reason, 'duplicate_source_key')
        self.assertEqual(self.accept('overlap', 'paid_ready', 300.5).ignored_reason, 'overlapping_observation')
        self.assertEqual(self.accept('stale', 'paid_ready', 290).ignored_reason, 'duplicate_or_stale_observation')
        self.assertEqual(self.accept('overlap-end', 'end', 300.5).ignored_reason, 'overlapping_observation')
        self.assertTrue(self.store._meta('awaiting_end_after_gap'))
        self.accept('e', 'end', 313)
        self.assertEqual(self.accept('e-again', 'end', 313).ignored_reason, 'duplicate_or_stale_observation')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM observation_jobs').fetchone()[0], 2)
        self.now += TTL_SECONDS
        self.assertEqual(self.accept('ancient', 'end', self.now-TTL_SECONDS-3).ignored_reason, 'expired_observation')

    def test_second_gap_does_not_invent_cross_gap_group(self):
        self.accept('r', 'paid_ready', 100)
        self.store.mark_audio_gap(200)
        self.accept('p', 'paid_ready', 300)
        self.store.mark_audio_gap(310)
        self.accept('e', 'end', 320)
        jobs = [json.loads(j['payload']) for j in self.jobs() if j.get('kind')=='observation']
        self.assertIsNotNone(jobs[0]['observation']['audio_group_id'])
        self.assertIsNone(jobs[1]['observation']['audio_group_id'])

    def test_expiry_ready_uses_partial_reason_not_payment_count(self):
        self.accept('r', 'paid_ready', 100)
        self.now += TTL_SECONDS
        self.store.cleanup()
        self.accept('p', 'paid_ready', self.now-20)
        value = json.loads(self.jobs()[0]['payload'])['observation']
        self.assertEqual(value['reason'], 'expired_round_awaiting_completion')
        self.assertIsNone(value['audio_group_id'])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.delivery_counts()['queued'], 1)

    def test_expiry_unread_and_delivered_counts_are_once_only(self):
        self.accept('first')
        self.now += 10
        self.accept('second', 'end', 110)
        self.assertEqual(self.send().status, 'delivered')
        self.now += TTL_SECONDS-10
        self.assertEqual(self.send().status, 'delivered')
        self.assertEqual(self.store.delivery_counts()['delivered'], 1)
        self.now += 10
        self.assertIsNone(self.send())
        self.assertEqual(self.store.delivery_counts()['delivered'], 0)
        self.assertEqual(self.store.delivery_counts()['expired_before_delivery'], 0)

    def test_original_creation_ttl_for_unread_job_and_cascade(self):
        self.accept()
        self.now += TTL_SECONDS
        report = self.store.cleanup()
        self.assertEqual(report['expired_before_delivery'], 1)
        self.assertEqual(self.store.delivery_counts()['expired_before_delivery'], 1)
        self.assertEqual(self.store.delivery_counts()['queued'], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
        self.assertEqual(self.store.cleanup()['expired_before_delivery'], 0)
        self.assertIsNone(self.send())

    def test_rebind_requires_consent_expected_ids_and_preserves_body_ttl(self):
        self.accept()
        first = self.jobs()[0]
        self.store.mark_delivery(first['event_id'], 'failed', error_code='receiver_rejected')
        preview = self.store.rebind_unconfirmed('new-target')
        self.assertEqual(preview, (first['event_id'],))
        self.assertEqual(self.store.delivery_counts()['failed'], 1)
        self.assertEqual(self.store.pending_jobs('new-target'), [])
        with self.assertRaises(ValueError):
            self.store.rebind_unconfirmed('new-target', consent=True)
        self.accept('late', 'end', 200)
        with self.assertRaises(ValueError):
            self.store.rebind_unconfirmed('new-target', consent=True, expected_ids=preview)
        all_ids = self.store.rebind_unconfirmed('new-target')
        self.assertEqual(self.store.rebind_unconfirmed('new-target', consent=True, expected_ids=all_ids), 2)
        after = self.store.pending_jobs('new-target')[0]
        self.assertEqual(after['payload'], first['payload'])
        self.assertEqual(after['expires_at'], first['expires_at'])
        self.assertEqual(self.store.delivery_counts()['failed'], 0)

    def test_no_automatic_old_history_backfill(self):
        self.store._db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',
                              ('old-source', self.now, None, 'completion_without_start', 'end', 100., 102.))
        self.store._db.execute('DROP TABLE observation_jobs')
        self.reopen()
        self.assertEqual(self.jobs(), [])
        self.assertEqual(len(self.store.list_history()), 1)
        self.accept('new', 'end', 200)
        self.assertEqual(len(self.jobs()), 1)

    def test_enqueue_failure_rolls_back_observation_group_and_checkpoint(self):
        self.accept('r', 'paid_ready', 100)
        self.store.mark_audio_gap(200)
        before = self.store._meta('checkpoint')
        self.store._db.execute("CREATE TRIGGER reject_enqueue BEFORE INSERT ON observation_jobs BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accept('p', 'paid_ready', 300)
        self.assertEqual(self.store._meta('checkpoint'), before)
        self.assertIsNone(self.store._meta('partial_audio_checkpoint'))
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM observations WHERE source_key='p'").fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM partial_audio_members').fetchone()[0], 0)
        self.store._db.execute('DROP TRIGGER reject_enqueue')
        self.accept('p', 'paid_ready', 300)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM observation_jobs').fetchone()[0], 1)

    def test_corrupt_new_schema_fails_closed_before_other_ddl(self):
        self.store._db.execute('DROP TABLE observation_jobs')
        self.store._db.execute('CREATE TABLE observation_jobs(event_id TEXT)')
        self.store.close()
        with self.assertRaises(StorageSchemaError):
            RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)
        # Restore only the disposable fixture for cleanup.
        db = sqlite3.connect(self.root/'owned'/'refueling.sqlite3')
        db.execute('DROP TABLE observation_jobs')
        db.commit()
        db.close()
        self.store = RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)

    def test_cross_table_event_id_collision_fails_closed(self):
        self.accept('r', 'paid_ready', 100)
        self.accept('e', 'end', 120)
        self.accept('orphan', 'end', 200)
        event_id = self.store._db.execute('SELECT event_id FROM jobs LIMIT 1').fetchone()[0]
        self.store._db.execute('UPDATE observation_jobs SET event_id=?', (event_id,))
        with self.assertRaises(StorageSchemaError):
            self.store.mark_delivery(event_id, 'delivered')
        with self.assertRaises(StorageSchemaError):
            self.jobs()
        with self.assertRaises(StorageSchemaError):
            self.store.rebind_unconfirmed('new', consent=True,
                expected_ids=self.store.rebind_unconfirmed('new'))
        self.assertEqual(self.store.delivery_counts()['delivered'], 0)

    def test_invalid_new_payload_fails_before_http_and_is_counted(self):
        self.accept()
        self.store._db.execute("UPDATE observation_jobs SET payload=?", (b'{}',))
        self.assertEqual(self.send().error_code, 'invalid_payload')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.delivery_counts()['failed_reasons'], {'invalid_payload': 1})

    def test_strict_validator_rejects_unknown_duplicate_and_fabricated_facts(self):
        self.accept()
        raw = self.jobs()[0]['payload']
        value = json.loads(raw)
        validate_observation(raw, 'cam1', value['event_id'])
        with self.assertRaises(ValueError):
            validate_event(raw)
        variants = []
        for field, replacement in [('id', '0'*32), ('amount_baht', 10), ('evidence', [{}]),
                                   ('audio_group_id', ''), ('reason', 'started'), ('camera_id', 'other'),
                                   ('observed_started_at', '1970-01-01 00:01:40Z'),
                                   ('observed_ended_at', '1970-01-01T00:01:39Z')]:
            altered = json.loads(raw)
            altered['observation'][field] = replacement
            variants.append(json.dumps(altered).encode())
        altered = json.loads(raw)
        altered['observation']['ready_count'] = 1
        variants.append(json.dumps(altered).encode())
        variants.extend([raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'), raw+b'{}'])
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_observation(payload, 'cam1')

    def test_actual_desktop_cancellable_boundary_handles_new_job(self):
        from smart_tapo.desktop_webhook import _attempt
        self.now = time.time()
        self.accept(start=self.now-20)
        stop = threading.Event()
        def factory(*args):
            stop.set()  # Cancellation between queue selection and HTTP send.
            return self.client
        with patch('smart_tapo.desktop_webhook.RefuelingStore',
                   side_effect=lambda *args: RefuelingStore(*args, clock=lambda: self.now)), patch(
                   'smart_tapo.desktop_webhook.dispatch_one',
                   side_effect=lambda store, client: dispatch_one(store, client, clock=lambda: self.now)):
            self.assertEqual(_attempt(self.root/'owned', self.target, stop, factory).status, 'retry')
            self.assertEqual(self.requests, [])
            stop.clear()
            self.now += 6
            self.assertEqual(_attempt(self.root/'owned', self.target, stop, lambda *a: self.client).status, 'delivered')
        self.assertEqual(self.store.delivery_counts()['delivered'], 1)

    def test_observation_attempt_log_uses_new_path_ack_and_no_private_source(self):
        self.accept()
        log = ApiAttemptLog(self.root/'attempts.jsonl', clock=lambda: self.now)
        self.client.log = log
        self.assertEqual(self.send().status, 'delivered')
        row = log.recent()[0]
        self.assertEqual(row['url'], self.target.base_url+'/v1/refueling/observations')
        self.assertEqual(row['http_status'], 201)
        self.assertIn('accepted', row['response'])
        self.assertNotIn('private-source', repr(row))
        self.assertNotIn(self.target.token, repr(row))

    def test_actual_loopback_observation_http_path_headers_body_and_ack(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, dict(self.headers), body))
                data = json.dumps({'accepted': True, 'event_id': self.headers['Idempotency-Key']}).encode()
                self.send_response(201)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = WebhookTarget(f'http://127.0.0.1:{server.server_port}/api', self.target.token, 'cam1', True)
            client = RefuelingWebhookClient(target, self.store.media)
            self.accept()
            self.assertEqual(dispatch_one(self.store, client, clock=lambda: self.now).status, 'delivered')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(len(received), 1)
        path, headers, raw = received[0]
        self.assertEqual(path, '/api/v1/refueling/observations')
        self.assertEqual(headers['Authorization'], 'Bearer '+self.target.token)
        self.assertEqual(headers['X-Camera-Id'], 'cam1')
        self.assertEqual(headers['Idempotency-Key'], json.loads(raw)['event_id'])
        self.assertIn('refueling-api', headers['User-Agent'])
        validate_observation(raw, 'cam1', headers['Idempotency-Key'])

    def test_bad_ack_rejected_and_failed_job_only_explicitly_requeued(self):
        self.accept()
        class BadAck:
            def open(self, req, timeout):
                response = io.BytesIO(b'{"accepted":true,"event_id":"wrong"}')
                response.code = 200
                response.headers = {}
                return response
        self.client.opener = BadAck()
        self.assertEqual(self.send().error_code, 'invalid_receiver_ack')
        self.assertEqual(self.store.delivery_counts()['failed_reasons'], {'invalid_receiver_ack': 1})
        self.assertIsNone(self.send())
        ids = self.store.rebind_unconfirmed(self.target.destination_key)
        self.store.rebind_unconfirmed(self.target.destination_key, expected_ids=ids, consent=True)
        self.assertEqual(len(self.jobs()), 1)

    def test_merged_queue_is_bounded_and_neither_kind_starves(self):
        self.accept('orphan', 'end', 100)
        for i in range(6):
            self.accept(f'r{i}', 'paid_ready', 200+i*10)
            self.accept(f'e{i}', 'end', 204+i*10)
        self.assertEqual(len(self.store.pending_jobs(self.target.destination_key, limit=2)), 2)
        seen = []
        for _ in range(13):
            job = self.jobs()[0]
            seen.append(job['event_id'])
            self.assertEqual(self.send().status, 'delivered')
        self.assertEqual(len(set(seen)), 13)
        self.assertEqual(self.store.delivery_counts()['delivered'], 13)
        self.assertTrue(self.requests[1].full_url.endswith('/observations'))
        self.assertIsNone(self.send())

    def test_observation_job_index_corruption_fails_closed(self):
        self.store._db.execute('DROP INDEX observation_jobs_due')
        self.store._db.execute('CREATE INDEX observation_jobs_due ON observation_jobs(created)')
        self.store.close()
        with self.assertRaises(StorageSchemaError):
            RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)
        db = sqlite3.connect(self.root/'owned'/'refueling.sqlite3')
        db.execute('DROP INDEX observation_jobs_due')
        db.commit()
        db.close()
        self.store = RefuelingStore(self.root/'owned', 'cam1', clock=lambda: self.now)
