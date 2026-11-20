"""Explicit frozen GUI smoke: owned synthetic SQLite/DPAPI, fake client only."""
import time

from .refueling import ConfirmedAnnouncement
from .refueling_contract import validate_event, validate_observation
from .refueling_store import RefuelingStore
from .refueling_webhook import DeliveryResult


def smoke_webhook(root,app):
    config=app.config
    if config.adb_serial!='synthetic-device' or app._active() or app.audio_panel.is_running:
        raise RuntimeError('Webhook smoke requires stopped isolated synthetic config')
    panel=app.webhook_panel
    if panel.is_running:raise RuntimeError('Webhook unexpectedly armed on startup')
    root.deiconify();app.notebook.select(panel);root.update()
    if not panel.winfo_ismapped():raise RuntimeError('Webhook tab did not render')
    token='synthetic-frozen-webhook-key-0123456789'
    url='https://example.invalid/api'
    panel.url.set(url);panel.key.set(token);panel.enabled.set(True)
    if not panel.save() or panel.key.get():raise RuntimeError('Webhook settings failed')
    panel.start()  # Deliberately missing queue consent; must not dispatch.
    feedback=panel.label.cget('text')
    if 'ยืนยันผูกคิว' not in feedback:raise RuntimeError('Webhook validation feedback missing')
    for _ in range(3):
        panel.poll()
        if panel.label.cget('text')!=feedback or panel.sender.scope is not None:
            raise RuntimeError('Webhook poll erased action feedback or armed without consent')
    if app.store.load_refueling_key(url,config.camera_id)!=token:
        raise RuntimeError('Webhook scoped DPAPI did not round-trip')
    if token in app.store.config_path.read_text(encoding='utf-8'):
        raise RuntimeError('Webhook key leaked into config')
    store=RefuelingStore(config.data_dir/'refueling',config.camera_id,source_root=config.data_dir)
    now=time.time()
    try:
        result=store.accept('webhook-smoke-ready',ConfirmedAnnouncement('paid_ready',now-6,now-4))
        session=result.updates[0].session.id
        store.add_evidence(session,config.data_dir/'refueling-smoke-source.png',now-7,source_key='webhook-smoke-image')
        store.accept('webhook-smoke-end',ConfirmedAnnouncement('end',now-3,now-1))
        store.accept('webhook-smoke-orphan-end',ConfirmedAnnouncement('end',now-.5,now-.1))
        orphan=store._db.execute('SELECT event_id FROM observation_jobs WHERE source_key=?',
                                ('webhook-smoke-orphan-end',)).fetchone()
        if orphan is None:raise RuntimeError('Webhook smoke did not queue its explicit orphan End')
        observation_event=orphan[0]
    finally:store.close()
    calls={'images':0,'completed':False,'observation':False}
    class FakeClient:
        def __init__(self,target,media):self.target=target
        def upload_image(self,image):
            if image['byte_size']<=0:raise RuntimeError('Missing smoke image')
            calls['images']+=1;return DeliveryResult('delivered')
        def send_event(self,payload,event_id):
            value=validate_event(payload,self.target.camera_id,event_id)
            # A real synthetic RTSP/NCC session can already be queued. Wait
            # for THIS test's session, not the first older completed event.
            if value['event_type']=='refueling.completed' and value['session']['id']==session:
                calls['completed']=True
            return DeliveryResult('delivered')
        def send_observation(self,payload,event_id):
            validate_observation(payload,self.target.camera_id,event_id)
            if event_id==observation_event:calls['observation']=True
            return DeliveryResult('delivered')
    original=panel.sender.client_factory
    panel.sender.client_factory=FakeClient
    try:
        panel.consent.set(True);panel.start()
        deadline=time.monotonic()+15
        while not (calls['completed'] and calls['observation']) and time.monotonic()<deadline:
            root.update();panel.poll();time.sleep(.02)
        panel.stop()
        deadline=time.monotonic()+5
        while panel.is_running and time.monotonic()<deadline:
            root.update();panel.poll();time.sleep(.02)
        if panel.is_running or not calls['completed'] or not calls['observation'] or calls['images']!=1:
            raise RuntimeError('Webhook frozen GUI dispatch did not complete/reap: '+str(dict(running=panel.is_running,**calls)))
        store=RefuelingStore(config.data_dir/'refueling',config.camera_id)
        try:
            if store.delivery_counts().get('delivered',0)<4:
                raise RuntimeError('Webhook ACK outcomes were not durable')
            if store._db.execute('SELECT status FROM observation_jobs WHERE event_id=?',
                                 (observation_event,)).fetchone()[0]!='delivered':
                raise RuntimeError('Webhook observation ACK was not durable')
        finally:store.close()
    finally:
        panel.stop();panel.sender.client_factory=original
    return {'webhook_gui_rendered':True,'webhook_scoped_dpapi':True,'webhook_action_feedback_persistent':True,
            'webhook_fake_delivery_completed':True,'webhook_observation_delivery_completed':True,'webhook_real_http':False}
