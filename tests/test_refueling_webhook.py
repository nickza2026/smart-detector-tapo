"""Actual loopback HTTP, synthetic pictures and disposable SQLite only."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from smart_tapo.refueling import ConfirmedAnnouncement
from smart_tapo.refueling_store import RefuelingStore,TTL_SECONDS
from smart_tapo.refueling_api_log import ApiAttemptLog,describe,excerpt
from smart_tapo.refueling_webhook import WebhookTarget,RefuelingWebhookClient,dispatch_one


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=1000.
        self.store=RefuelingStore(self.root/'owned','cam1',clock=lambda:self.now,source_root=self.root)
        self.addCleanup(self.store.close)
        self.sid=self.store.accept('ready-1',ConfirmedAnnouncement('paid_ready',100,106)).updates[0].session.id
        self.images={};self.events={};self.requests=[];self.fail_status=None;self.bad_ack=False;self.drop_ack=False
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,status,value):
                data=json.dumps(value).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
                if status==429:self.send_header('Retry-After','7')
                if status==302:self.send_header('Location',outer.url+'/not-allowed')
                self.end_headers();self.wfile.write(data)
            def handle_request(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                outer.requests.append((self.command,self.path,dict(self.headers),body))
                if outer.fail_status:return self.reply(outer.fail_status,{'code':'test_rejection'})
                if self.headers.get('Authorization')!='Bearer test-only-token-0123456789':return self.reply(401,{})
                if self.command=='PUT':
                    identifier=self.path.rsplit('/',1)[-1];digest=hashlib.sha256(body).hexdigest()
                    if self.headers.get('X-Content-SHA256')!=digest:return self.reply(422,{})
                    if identifier in outer.images and outer.images[identifier]!=body:return self.reply(409,{'code':'idempotency_conflict'})
                    outer.images[identifier]=body
                    return self.reply(201,{'stored':True,'image_id':identifier,'sha256':digest})
                value=json.loads(body);identifier=self.headers['Idempotency-Key']
                if any(i['id'] not in outer.images for i in value['session']['evidence']):return self.reply(409,{'code':'images_missing'})
                if identifier in outer.events and outer.events[identifier]!=body:return self.reply(409,{'code':'idempotency_conflict'})
                outer.events[identifier]=body
                if outer.drop_ack:
                    outer.drop_ack=False;self.close_connection=True;return
                return self.reply(200,{'accepted':not outer.bad_ack,'event_id':identifier})
            do_PUT=handle_request
            do_POST=handle_request
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close(),thread.join(2)))
        self.target=WebhookTarget(self.url,'test-only-token-0123456789','cam1',True)
        self.client=RefuelingWebhookClient(self.target,self.store.media)

    def send(self):return dispatch_one(self.store,self.client,clock=lambda:self.now)

    def picture(self):
        path=self.root/'sample.png';Image.new('RGB',(12,12),'blue').save(path)
        return self.store.add_evidence(self.sid,path,99,source_key='photo1')

    def test_real_json_plus_image_pipeline_is_idempotent_and_has_no_local_paths(self):
        image=self.picture()
        self.store.accept('ready-2',ConfirmedAnnouncement('paid_ready',200,206))
        self.store.accept('thanks',ConfirmedAnnouncement('end',250,252))
        while self.store.pending_jobs(self.target.destination_key):self.assertEqual(self.send().status,'delivered')
        self.assertEqual(len(self.images),1)
        self.assertEqual(len([r for r in self.requests if r[0]=='PUT']),1)
        latest=json.loads(list(self.events.values())[-1])
        self.assertTrue(latest['session']['has_issue'])
        self.assertEqual(latest['session']['ready_count'],2)
        self.assertIsNotNone(latest['session']['ended_at'])
        self.assertEqual(latest['session']['evidence'][0]['sha256'],image['sha256'])
        for raw in self.events.values():
            self.assertNotIn(str(self.root).encode(),raw)
            self.assertNotIn(self.target.token.encode(),raw)

    def test_lost_response_retries_exact_same_event_body_and_identifier(self):
        self.drop_ack=True
        self.assertEqual(self.send().status,'retry')
        first=self.requests[-1]
        self.now+=6
        self.assertEqual(self.send().status,'delivered')
        self.assertEqual((first[2]['Idempotency-Key'],first[3]),(self.requests[-1][2]['Idempotency-Key'],self.requests[-1][3]))
        self.assertEqual(len(self.events),1)

    def test_redirect_auth_error_and_invalid_ack_never_claim_delivered(self):
        for status in (302,401):
            self.fail_status=status
            self.assertEqual(self.client.send_event(self.store.pending_jobs(self.target.destination_key)[0]['payload'],
                                                   self.sid+':1').status,'failed')
        self.fail_status=None;self.bad_ack=True
        self.assertEqual(self.send().status,'failed')
        self.assertEqual(self.store.delivery_counts()['delivered'],0)
        self.assertFalse(any(r[1]=='/not-allowed' for r in self.requests))

    def test_429_and_5xx_are_bounded_retry_results(self):
        job=self.store.pending_jobs(self.target.destination_key)[0]
        self.fail_status=429
        result=self.client.send_event(job['payload'],job['event_id'])
        self.assertEqual((result.status,result.retry_after),('retry',7))
        self.fail_status=503
        self.assertEqual(self.client.send_event(job['payload'],job['event_id']).status,'retry')

    def test_missing_server_image_cache_is_reuploaded_without_duplicate_photo(self):
        self.picture()
        self.assertEqual(self.send().status,'delivered')  # start, no photo yet
        self.assertEqual(self.send().status,'delivered')  # image update
        self.images.clear()
        self.store.accept('repeat',ConfirmedAnnouncement('paid_ready',200,206))
        self.assertEqual(self.send().status,'retry')
        self.now+=6
        self.assertEqual(self.send().status,'delivered')
        self.assertEqual(len(self.images),1)

    def test_three_day_expiry_makes_no_network_request_and_preserves_source(self):
        self.picture();self.now+=TTL_SECONDS
        self.assertIsNone(self.send())
        self.assertEqual(self.requests,[])
        self.assertTrue((self.root/'sample.png').exists())
        self.assertIsNone(self.store.get_session(self.sid))
        self.assertGreater(self.store.delivery_counts()['expired_before_delivery'],0)

    def test_image_corruption_and_secret_field_fail_before_http(self):
        image=self.picture();Path(image['path']).write_bytes(b'wrong')
        self.assertEqual(self.client.upload_image(image).status,'failed')
        job=self.store.pending_jobs(self.target.destination_key)[0]
        value=json.loads(job['payload']);value['session']['TOKEN']='private'
        self.assertEqual(self.client.send_event(json.dumps(value).encode(),job['event_id']).status,'failed')
        self.assertEqual(self.requests,[])

    def test_target_validation_and_destination_changes_do_not_leak_token(self):
        for url in ('http://example.com','https://user:pass@example.com','https://example.com/?token=x','https://example.com/#x'):
            with self.assertRaises(ValueError):WebhookTarget(url,'test-only-token-0123456789','cam1')
        self.assertNotIn(self.target.token,repr(self.target))
        changed=WebhookTarget(self.url,'changed-test-token-0123456789','cam1',True)
        self.assertNotEqual(changed.destination_key,self.target.destination_key)
        self.store.pending_jobs(self.target.destination_key)
        self.assertEqual(self.store.pending_jobs(changed.destination_key),[])

    def test_invalid_money_boolean_count_or_unknown_fields_are_never_exported(self):
        job=self.store.pending_jobs(self.target.destination_key)[0]
        for name,bad in [('amount_baht',10),('ready_count',True),('ended_at','2026-10-28T00:00:00Z')]:
            value=json.loads(job['payload']);value['session'][name]=bad
            result=self.client.send_event(json.dumps(value).encode(),job['event_id'])
            self.assertEqual(result.status,'failed')
        self.assertEqual(self.requests,[])


class AttemptLogTests(WebhookTests):
    def setUp(self):
        super().setUp()
        self.log=ApiAttemptLog(self.root/'logs'/'refueling-api.jsonl',clock=lambda:self.now)
        self.client=RefuelingWebhookClient(self.target,self.store.media,log=self.log)

    def test_rejection_records_url_status_and_receiver_body_without_token(self):
        self.fail_status=401
        self.assertEqual(self.send().error_code,'receiver_auth_rejected')
        record=self.log.recent()[-1]
        self.assertEqual((record['method'],record['url'],record['http_status']),
                         ('POST',self.url+'/v1/refueling/events',401))
        self.assertEqual((record['status'],record['error_code']),('failed','receiver_auth_rejected'))
        self.assertIn('test_rejection',record['response'])
        written=(self.root/'logs'/'refueling-api.jsonl').read_text(encoding='utf-8')
        self.assertNotIn(self.target.token,written)
        self.assertIn('HTTP 401',describe(record))

    def test_echoed_token_is_masked_and_body_is_bounded(self):
        self.assertEqual(excerpt(b'x'*5000+self.target.token.encode(),self.target.token),'x'*500)
        self.assertEqual(excerpt('bad '+self.target.token+'\n',self.target.token),'bad ***')

    def test_transport_failure_and_local_rejection_are_recorded(self):
        job=self.store.pending_jobs(self.target.destination_key)[0]
        self.server.shutdown();self.server.server_close()
        self.assertEqual(self.send().status,'retry')
        record=self.log.recent()[-1]
        self.assertIsNone(record['http_status'])
        self.assertTrue(record['error_detail'])
        self.assertIn('ไม่ได้รับคำตอบ HTTP',describe(record))
        value=json.loads(job['payload']);value['session']['amount_baht']=10
        self.client.send_event(json.dumps(value).encode(),job['event_id'])
        record=self.log.recent()[-1]
        self.assertEqual((record['url'],record['error_code']),(None,'invalid_payload'))
        self.assertIn('ไม่ได้ยิง API',describe(record))

    def test_failed_jobs_report_their_reasons(self):
        self.fail_status=401
        self.send()
        self.assertEqual(self.store.delivery_counts()['failed_reasons'],{'receiver_auth_rejected':1})

    def test_requests_carry_an_application_user_agent(self):
        self.assertEqual(self.send().status,'delivered')
        self.assertTrue(self.requests[-1][2]['User-Agent'].startswith('SmartTapo/v'))
        self.assertNotIn('Python-urllib',self.requests[-1][2]['User-Agent'])

    def test_cloudflare_signature_ban_is_not_reported_as_a_bad_key(self):
        class Banned:
            code=403;headers={'Server':'cloudflare'}
            def read(self,size=-1):return b'{"title":"Error 1010: Access denied","error_code":1010}'
            def close(self):pass
        self.client.opener=type('Opener',(),{'open':lambda self,req,timeout:Banned()})()
        self.assertEqual(self.send().error_code,'receiver_firewall_blocked')
        self.assertEqual(self.store.delivery_counts()['failed_reasons'],{'receiver_firewall_blocked':1})
        self.assertIn('1010',self.log.recent()[-1]['response'])

    def test_broken_log_never_changes_delivery(self):
        self.client.log=lambda record:(_ for _ in ()).throw(OSError('disk'))
        self.assertEqual(self.send().status,'delivered')
