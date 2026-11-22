"""Explicit smoke only: fresh SQLite, generated PNG, fake ADB; no delivery."""
import json
import io
import time
from unittest.mock import patch
from xml.sax.saxutils import escape

from PIL import Image
from .refueling import ConfirmedAnnouncement
from .refueling_contract import validate_event
from .refueling_store import RefuelingStore
from .refueling_webhook import WebhookTarget,RefuelingWebhookClient
from .page_signature import collect_page_signature,device_preview


def smoke_refueling(root,app):
    config=app.config
    if config.adb_serial!='synthetic-device' or config.notify_enabled or app._active():
        raise ValueError('Refueling smoke requires stopped isolated synthetic config')
    now=time.time()
    source=config.data_dir/'refueling-smoke-source.png'
    Image.new('RGB',(32,32),'blue').save(source)
    directory=config.data_dir/'refueling'
    store=RefuelingStore(directory,config.camera_id,source_root=config.data_dir)
    try:
        identifier=store.accept('smoke-ready',ConfirmedAnnouncement('paid_ready',now-30,now-24)).updates[0].session.id
        store.add_evidence(identifier,source,now-31,source_key='smoke-photo')
        store.accept('smoke-repeat',ConfirmedAnnouncement('paid_ready',now-20,now-14))
        store.accept('smoke-thanks',ConfirmedAnnouncement('end',now-10,now-8))
        target=WebhookTarget('https://example.invalid','synthetic-only-token-0123456789',config.camera_id)
        RefuelingWebhookClient(target,store.media)  # construction MUST perform no request
        jobs=store.pending_jobs(target.destination_key)
        for job in jobs:validate_event(job['payload'],config.camera_id,job['event_id'])
    finally:store.close()
    store=RefuelingStore(directory,config.camera_id)
    try:
        row=store.get_session(identifier)
        if row['ready_count']!=2 or not row['has_issue'] or row['ended_at'] is None or len(row['evidence'])!=1:
            raise RuntimeError('Packaged SQLite checkpoint/history mismatch')
    finally:store.close()
    panel=app.refueling_panel
    app.notebook.select(panel);panel.refresh()
    deadline=time.monotonic()+20
    while identifier not in panel.tree.get_children() and time.monotonic()<deadline:
        panel.poll(visible=True);root.update();time.sleep(.02)
    if identifier not in panel.tree.get_children():raise RuntimeError('Packaged refueling row not rendered')
    panel.tree.selection_set(identifier);panel._selection();panel.show_image()
    deadline=time.monotonic()+15
    while panel.viewer is None and time.monotonic()<deadline:
        panel.poll(visible=True);root.update();time.sleep(.02)
    if panel.viewer is None or panel.photo is None:raise RuntimeError('Packaged refueling image did not render')
    panel.viewer.deiconify();root.update()
    image_mapped=bool(panel.viewer.winfo_ismapped());panel.viewer.destroy();panel.viewer=panel.photo=None
    class FakeAdb:
        def ui(self):
            return '<hierarchy><node text="'+escape(config.camera_name)+'"/><node text="Pan &amp; Tilt" resource-id="'+config.tapo_package+':id/pan_tilt"/></hierarchy>'
        def run(self,*args):
            if args[1]=='dumpsys':return ('mResumedActivity: '+config.tapo_package+'/.SyntheticLiveActivity').encode()
            return b''
    outputs=[]
    def collect(cfg,label):
        path=collect_page_signature(cfg,label,adb=FakeAdb());outputs.append(path);return path
    with patch('smart_tapo.page_signature.collect_page_signature',side_effect=collect):
        app.collect_page_signature('live')
        deadline=time.monotonic()+15
        while app.busy and time.monotonic()<deadline:root.update();time.sleep(.02)
    if app.busy or not outputs:raise RuntimeError('Packaged explicit page collector failed')
    signature=json.loads(outputs[0].read_text(encoding='utf-8'))
    if signature['verified_mode']!='unknown' or signature['screenshot_saved'] or signature['raw_xml_saved']:
        raise RuntimeError('Page signature incorrectly became trusted Live proof')
    encoded=io.BytesIO();Image.new('RGB',config.expected_size,'blue').save(encoded,format='PNG')
    class FakePreview:
        def run(self,*args):
            if args!=('exec-out','screencap','-p'):raise RuntimeError('Unexpected preview command')
            return encoded.getvalue()
    with patch('smart_tapo.page_signature.device_preview',side_effect=lambda cfg:device_preview(cfg,adb=FakePreview())):
        app.preview_adb_device()
        deadline=time.monotonic()+15
        while app.busy and time.monotonic()<deadline:root.update();time.sleep(.02)
    preview=getattr(app,'device_preview_window',None)
    if app.busy or preview is None or not preview.winfo_ismapped():raise RuntimeError('Explicit preview did not render')
    editor=app.device_preview_photo
    from .config import Rect
    original=app.config;baseline=original.baseline_path.read_bytes()
    if editor.selection!=original.clock_roi or editor.save():raise RuntimeError('Clock editor auto-saved')
    # Explicit synthetic operator selection; never an inferred real camera ROI.
    editor.selection=Rect(0,174,280,21);editor.edited=True;editor.confirm.set(True)
    if not editor.save() or app.config.clock_roi!=editor.selection:raise RuntimeError('Clock editor did not save')
    if app.config.roi!=original.roi or app.config.baseline_path.read_bytes()!=baseline:
        raise RuntimeError('Clock editor changed bank calibration')
    config=app.config
    preview.destroy();app.device_preview_window=app.device_preview_photo=None
    root.deiconify()  # Smoke starts withdrawn; controls need a real mapped root.
    app.notebook.select(app.audio_panel)
    listener=app.audio_panel.listener_panel
    app.audio_panel.modes.select(listener)
    deadline=time.monotonic()+3
    while not listener.start_button.winfo_ismapped() and time.monotonic()<deadline:root.update();time.sleep(.02)
    if not listener.start_button.winfo_ismapped():
        raise RuntimeError('Listener controls did not map: '+repr((root.geometry(),app.audio_panel.winfo_height(),
            app.audio_panel.winfo_ismapped(),listener.winfo_height(),listener.winfo_ismapped(),app.audio_panel.modes.select())))
    listener.create_profile()
    dialog=listener.profile_dialog
    if dialog is None:raise RuntimeError('Explicit profile editor missing')
    dialog.deiconify();root.update()
    if not dialog.winfo_ismapped():raise RuntimeError('Profile editor did not render')
    dialog.request_close()
    from .continuous_smoke import smoke_continuous,smoke_prepared_reference
    from .announcement_profile import load_announcement_profile
    listener.prepared_manifest=smoke_prepared_reference(config.data_dir/'prepared-profile')
    listener.use_prepared_profile()
    deadline=time.monotonic()+15
    while listener.is_running and time.monotonic()<deadline:root.update();listener.poll();time.sleep(.02)
    installed=listener.profile_path.get()
    if listener.is_running or not installed or listener.consent.get():raise RuntimeError('Prepared profile UI did not finish safely')
    load_announcement_profile(installed,config.camera_id)
    listener.use_prepared_profile()
    deadline=time.monotonic()+15
    while listener.is_running and time.monotonic()<deadline:root.update();listener.poll();time.sleep(.02)
    if listener.is_running or listener.profile_path.get()!=installed:raise RuntimeError('Prepared profile was not reused')
    from .desktop_listener import error_text
    detail=error_text({'code':'audio_clock_discontinuity','diagnostics':{'lag_seconds':2.872,'read_seconds':3.}})
    if '[audio_clock_discontinuity]' not in detail or 'lag=2.87s' not in detail or 'read=3.00s' not in detail:
        raise RuntimeError('Specific listener stop diagnostics missing')
    listener.status.set(detail);root.update()
    continuous=smoke_continuous(config.data_dir/'continuous-fixture',config)
    from .watch_smoke import smoke_watch
    watch=smoke_watch(config.data_dir/'watch-fixture',config,installed,panel=listener)
    return {'refueling_checkpoint_recovered':True,'refueling_history_rendered':True,
            'refueling_full_image_mapped':image_mapped,'refueling_http_client_packaged':True,
            'page_signature_sanitized':True,'explicit_adb_preview_mapped':True,'device_preview_saved':False,
            'listener_controls_mapped':True,'profile_editor_mapped':True,'prepared_profile_bound':True,
            'prepared_profile_reused':True,'prepared_profile_auto_listening':False,**continuous,**watch,
            'listener_stop_diagnostics':True,'clock_region_gui_saved':True,'clock_region_no_baseline_change':True,
            'refueling_real_listening':False,'refueling_external_delivery':False}
