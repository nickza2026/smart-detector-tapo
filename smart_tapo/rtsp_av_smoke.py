"""Real owned-loopback AV/GUI smoke; generated media, no user devices/providers."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import time

import numpy as np
from PIL import Image,ImageDraw


def smoke_audio_prealert(app, profile, fake, now):
    """Frozen actual Desktop poll bridge; held fake sender, no HTTP or camera."""
    from concurrent.futures import Future
    from .desktop_health_alerts import HealthAlertSupervisor
    class Held:
        def __init__(self):self.jobs=[]
        def submit(self,fn,*args):
            future=Future();self.jobs.append((future,fn,args));return future
        def run(self):
            if self.jobs:
                future,fn,args=self.jobs.pop(0)
                if future.set_running_or_notify_cancel():future.set_result(fn(*args))
        def shutdown(self,**kwargs):
            for future,_,_ in self.jobs:future.cancel()
    listener=app.audio_panel.listener_panel
    original=(app.health_alerts,app._health_credentials,app.config)
    calls=[];held=Held()
    supervisor=HealthAlertSupervisor(lambda *args:calls.append(args[2]) or 'success',
                                      lambda:now[0],require_audio_preflight=True)
    supervisor._executor.shutdown(wait=False)
    supervisor._executor=held
    try:
        listener.stop()
        app.health_alerts=supervisor;app._health_credentials=('synthetic-key','https://notify.example')
        app.config=replace(app.config,notify_enabled=True,health_failure_seconds=1,health_repeat_seconds=60)
        app.form['rtsp_url'].set(app.config.rtsp_url)
        listener.profile_path.set(str(profile));listener.consent.set(True);listener.start_button.invoke()
        listener._recovery.attempts=3;listener._recovery.fail(now[0])
        listener._watching=False;listener._health_reason='audio_listener_failed';fake.is_running=False
        initial=len(fake.calls);generation=listener._health_generation
        app._poll_health()
        for index in range(2):
            now[0]+=1 if index==0 else 60
            listener.poll();app._poll_health();held.run()
            if len(fake.calls)!=initial+index+1 or len(calls)!=index:
                raise RuntimeError('Audio notice did not await its own one-start preflight')
            now[0]+=8;listener.poll();app._poll_health();held.run()
            if len(calls)!=index+1:raise RuntimeError('Failed preflight muted health indefinitely')
        now[0]+=60;listener.poll();app._poll_health()
        now[0]+=1
        fake.events=[dict(kind='listener_progress',waiting_audio=False,stage='persisted',
            heartbeat=now[0],pcm_started_at=listener._health_since,audio_frames=16000,pcm_session='d'*32)]
        listener.poll();app._poll_health();held.run()
        if len(calls)!=2 or not listener.health_snapshot()['healthy'] or listener._health_generation!=generation:
            raise RuntimeError('Fresh preflight PCM did not cancel fault within the original scope')
        listener.stop();app._poll_health();held.run()
        if len(calls)!=2:raise RuntimeError('Manual audio stop produced a late preflight notice')
        return True
    finally:
        listener.stop();supervisor.close()
        app.health_alerts,app._health_credentials,app.config=original


def smoke_audio_rearm(root, app, profile):
    """Actual widgets/frozen module, fake producer; never open a new source."""
    listener=app.audio_panel.listener_panel
    original=(listener.runtime,listener.clock,app.config,listener.profile_path.get())
    class Runtime:
        is_running=False
        profile_fingerprint='synthetic-rearm-fingerprint'
        def __init__(self):self.calls=[];self.events=[]
        def start(self,config,device,path,**options):
            self.calls.append(config);self.is_running=True
        def stop(self):self.is_running=False
        def poll(self):
            events,self.events=self.events,[]
            return events
    fake=Runtime();now=[100.]
    try:
        listener.runtime=fake;listener.clock=lambda:now[0]
        listener.profile_path.set(str(profile));listener.consent.set(True)
        app.notebook.select(app.audio_panel);app.audio_panel.modes.select(listener);root.update()
        listener.start_button.invoke()
        if len(fake.calls)!=1:raise RuntimeError('Audio manual start blocked')
        app._remember_history_port(8081 if app.config.history_web_port!=8081 else 8082)
        app._half_second_preset()
        fake.is_running=False;fake.events=[dict(kind='error',code='audio_timeout')]
        listener.poll();now[0]+=15;listener.poll()
        if len(fake.calls)!=2 or fake.calls[0]!=fake.calls[1] or not listener._health_expected:
            raise RuntimeError('Unrelated settings cancelled consented audio recovery')
        fake.is_running=False;fake.events=[dict(kind='error',code='audio_timeout')]
        listener.poll()
        app.config=replace(app.config,rtsp_url='rtsp://127.0.0.1:8554/synthetic-rearm-new')
        app.form['rtsp_url'].set(app.config.rtsp_url)
        now[0]+=15;listener.poll()
        if listener.is_running or listener.consent.get():raise RuntimeError('Changed source retained consent')
        listener.consent.set(True)
        if listener.start_button.instate(['disabled']):raise RuntimeError('Audio start remained locked after reconsent')
        listener.start_button.invoke()
        if len(fake.calls)!=3 or fake.calls[-1].rtsp_url!=app.config.rtsp_url:
            raise RuntimeError('Audio rearm did not start the newly consented source')
        listener.stop()
        app.form['rtsp_url'].set('rtsp://127.0.0.1:8554/synthetic-unsaved')
        listener.consent.set(True)
        if not listener.start_button.instate(['disabled']) or 'บันทึก' not in listener.start_hint.get():
            raise RuntimeError('Unsaved audio source did not expose its admission blocker')
        prealert=smoke_audio_prealert(app,profile,fake,now)
        return {'rtsp_audio_recovery_controls_gui':True,'rtsp_audio_prealert_restart_gui':prealert}
    finally:
        listener.stop()
        listener.runtime,listener.clock,app.config,old_path=original
        app.store.save(app.config)
        for key in ('rtsp_url','capture_interval_seconds','clock_check_interval_seconds'):
            app.form[key].set(str(getattr(app.config,key)))
        listener.profile_path.set(old_path);listener.consent.set(False)


def smoke_av_gui(root,app,history):
    from tests.rtsp_av_fixture import SyntheticAV,jpeg_payload
    from .continuous_smoke import smoke_prepared_reference,_phrase,_end_phrase
    from .profile_setup import create_private_profile
    from .desktop_calibration import is_ready
    from .desktop_rtsp_smoke import pump
    from .refueling_store import RefuelingStore
    result=dict(gui_created=True,auto_config_created=app.store.config_path.is_file(),
        rtsp_video_backend=True,rtsp_audio_backend=True,real_notifications=0,
        real_user_stream=False,rtsp_owned_loopback_only=True,real_windows_audio=False,
        real_adb=False,external_delivery=False)
    if any(key in app.form for key in ('adb_path','adb_serial','memu_executable','camera_name')):
        raise RuntimeError('Legacy audio input control remains exposed')
    if hasattr(app.audio_panel,'probe') or hasattr(app.audio_panel,'devices'):
        raise RuntimeError('Windows audio source remains exposed')
    result['rtsp_no_windows_or_adb_controls']=True
    from .capture.rtsp import PtsContinuity
    tracker=PtsContinuity()
    paced=[tracker.observe(i*.06,100+(i//2)*.12) for i in range(80)]
    result['rtsp_same_tick_pts_continuity']=all(paced[-40:])
    burst=PtsContinuity()
    result['rtsp_same_tick_burst_rejected']=not any(burst.observe(i*.06,100+(i//2)*.001) for i in range(200))
    from .rtsp_audio import PcmTimeline,AudioDiscontinuity
    audio_clock=PcmTimeline()
    for index in range(600):
        begin=audio_clock.accept(20+index*.1+(index//30)*.001,1600,100.1+index*.1)
        if abs(begin-(100+index*.1))>1e-8:raise RuntimeError('Audio jitter moved sample clock')
    result['rtsp_audio_jitter_sample_clock']=True
    for bad_pts in (20.03,20.12,20.2,20.,19.):
        audio_clock=PcmTimeline();audio_clock.accept(20.,1600,100.1)
        try:audio_clock.accept(bad_pts,1600,100.2)
        except AudioDiscontinuity:pass
        else:raise RuntimeError('Audio real gap/overlap accepted')
    result['rtsp_audio_real_gap_rejected']=True
    reference=smoke_prepared_reference(app.store.root/'synthetic-audio-reference')
    manifest=json.loads(reference.read_text())
    templates=[dict(kind=t['kind'],file=reference.parent/t['file'],start=t['start'],end=t['end'],threshold=t['threshold']) for t in manifest['templates']]
    profile=create_private_profile(app.config.data_dir,app.config.camera_id,templates)
    pcm=np.repeat(np.concatenate((_end_phrase(),_phrase(),_phrase(),_end_phrase(),np.zeros(160000))),2)
    image=Image.new('RGB',(320,240),'#8192a3');draw=ImageDraw.Draw(image)
    for y in range(0,240,12):
        for x in range(0,320,12):draw.rectangle((x,y,x+5,y+5),fill=((x*7)%255,(y*3)%255,(x+y)%255))
    occupied=image.copy();ImageDraw.Draw(occupied).rectangle((120,80,195,155),fill='white')
    listener=app.audio_panel.listener_panel
    result.update(smoke_audio_rearm(root,app,profile))
    events=[];poll=listener.runtime.poll
    def observe():
        rows=poll();events.extend(rows);return rows
    listener.runtime.poll=observe
    with SyntheticAV(image,pcm) as source:
        app.form['rtsp_url'].set(source.url)
        app.form['stuck_consecutive_frames'].set('10')
        app.config=replace(app.config,adb_serial='synthetic-device') # test-only webhook fence, never ADB use
        if not app.save_settings():raise RuntimeError('Synthetic RTSP settings failed')
        app.notebook.select(app.calibration);root.deiconify();root.update()
        pump(root,lambda:not app.capture_ref_button.instate(['disabled']),label='idle RTSP reference')
        app.capture_ref_button.invoke()
        pump(root,lambda:not app.busy and app.reference_image is not None,label='AV reference')
        if app.reference_image.size!=(320,240) or app.config.video.box()!=(0,0,320,240):raise RuntimeError('RTSP geometry not adopted')
        app.confirm_empty.set(True)
        if not app.save_reference():raise RuntimeError('RTSP calibration failed')
        result.update(rtsp_reference_wire_gui=True,rtsp_geometry_adopted_gui=True,
            rtsp_calibration_source_bound=is_ready(app.config,app.store.root))
        # RAM GUI keeps only an evidence quota, not an FPS×five-minute spool.
        if app.controller.video_storage_mode!='ram':raise RuntimeError('GUI is not using RAM video')
        result['video_storage_ram_gui']=True
        app.config=replace(app.config,max_image_storage_mb=512.)
        if 'max_image_storage_mb' in app.form:app.form['max_image_storage_mb'].set('512')
        app.form['capture_interval_seconds'].set('0.2')
        if not app.save_settings():raise RuntimeError('RAM evidence storage settings failed')
        saved=json.loads(app.store.config_path.read_text(encoding='utf-8'))
        if saved['max_image_storage_mb']!=512.:
            raise RuntimeError('RAM evidence quota changed from visible value')
        if app.config.stuck_minimum_seconds<9 or not is_ready(app.config,app.store.root):
            raise RuntimeError('Storage plan changed proof duration or calibration')
        result['ram_evidence_quota_saved_gui']=True
        app.notebook.select(0);root.update()
        app.storage_estimate_label.master.master.yview_moveto(1.0);root.update()
        label=app.storage_estimate_label
        if (not label.winfo_ismapped() or label.winfo_rooty()<root.winfo_rooty()
                or label.winfo_rooty()+label.winfo_height()>root.winfo_rooty()+root.winfo_height()):
            raise RuntimeError('Storage estimate not rendered within offscreen window')
        result['ram_storage_mode_visible_gui']='RAM' in app.storage_estimate.get() and 'MB' in app.storage_estimate.get()
        app.notebook.select(app.audio_panel);app.audio_panel.modes.select(listener)
        listener.profile_path.set(str(profile));listener.consent.set(True);root.update()
        listener.start_button.invoke()
        pump(root,lambda:any(e.get('frames',0)>=16000 for e in events),label='real RTSP PCM',
             diagnostic=lambda:dict(status=listener.status.get(),events=events[-2:]))
        audio_pid=listener.runtime._process.pid
        result['rtsp_audio_started_first']=True
        try:
            # Reproduce the reported migration: rejected old portrait metadata
            # remains beside fresh landscape RTSP frames, never delete it.
            from .frames import publish_frame
            import io
            old=replace(app.config,expected_size=(720,1280))
            buffer=io.BytesIO();Image.new('RGB',old.expected_size,'gray').save(buffer,'PNG')
            old_markers=[publish_frame(old,buffer.getvalue(),'legacy-portrait',i,True,
                captured_at=time.time()-100,captured_monotonic=time.monotonic()-100) for i in range(1,5)]
            app.start_role('worker');app.start_role('capture')
            pump(root,lambda:all(app.controller.snapshot()[key]['state']=='running' for key in ('capture','worker')),label='independent video roles')
            pids=[audio_pid,*[app.controller.snapshot()[key]['pid'] for key in ('capture','worker')]]
            if len(set(pids))!=3 or not listener.runtime.is_running:raise RuntimeError('AV roles did not overlap')
            result['rtsp_three_distinct_pids']=True
            result['rtsp_live_overlap']=True
            app.open_tracking_view()
            tracking=app.tracking_popup
            def tracked():
                tracking.poll()
                return (tracking._key is not None and
                        any(tracking.canvas.type(item)=='polygon' for item in tracking.canvas.find_all()))
            pump(root,tracked,timeout=20,label='real Worker tracking IPC and polygon')
            packet=app.controller.tracking_snapshot()
            if packet is None or tracking._key[:3] != (packet['session'],packet['sequence'],packet['generation']):
                raise RuntimeError('Tracking view does not describe the current Worker frame')
            result['tracking_worker_ipc_gui']=True
            source.video=jpeg_payload(occupied)
            def shadow():
                try:
                    with closing(sqlite3.connect(app.config.data_dir/'worker.sqlite3',timeout=.1)) as db:
                        return db.execute("SELECT count(*) FROM notifications WHERE status='shadow'").fetchone()[0]>=1
                except sqlite3.Error:return False
            pump(root,shadow,label='vision shadow',diagnostic=lambda:app.log.get('end-6l','end'))
            with closing(sqlite3.connect(app.config.data_dir/'worker.sqlite3')) as db:
                span=db.execute('SELECT n.created-e.started FROM notifications n JOIN events e ON e.id=n.event LIMIT 1').fetchone()[0]
            if span<9 or not all(p.is_file() for p in old_markers):raise RuntimeError('Legacy marker recovery shortened proof or deleted evidence')
            result['worker_ten_frame_proof_preserved']=True
            raw=app.config.data_dir/'frames'/app.config.camera_id
            expected_old=set(old_markers)|{p.with_name(p.name.replace('.ready.json','.png')) for p in old_markers}
            remaining=set(raw.iterdir())
            if remaining!=expected_old:raise RuntimeError('RAM processing wrote raw spool files')
            with closing(sqlite3.connect(app.config.data_dir/'worker.sqlite3')) as db:
                if db.execute('SELECT count(*) FROM frames').fetchone()[0]:
                    raise RuntimeError('RAM processing persisted per-frame rows')
                if not db.execute('SELECT count(*) FROM evidence').fetchone()[0]:
                    raise RuntimeError('RAM event evidence missing')
            result.update(worker_legacy_spool_ignored=True,ram_no_raw_spool_pngs=True,
                ram_no_frame_database_rows=True,ram_event_evidence_saved=True)
            result.update(shadow_notification=True,rtsp_wire_to_vision_to_history=True)
            def completed():
                try:
                    with closing(sqlite3.connect(app.config.data_dir/'refueling/refueling.sqlite3',timeout=.1)) as db:
                        rows=[json.loads(row[0]) for row in db.execute('SELECT data FROM sessions')]
                        return any(row['ready_count']==2 and row['ended_at'] is not None for row in rows)
                except sqlite3.Error:return False
            pump(root,completed,timeout=48,label='actual RTSP NCC ready repeat end',diagnostic=lambda:dict(status=listener.status.get(),events=events[-2:]))
            result.update(rtsp_pcm_real_decoder=True,rtsp_ncc_ready_repeat_end=True,rtsp_audio_real_sqlite=True)
        finally:
            listener.stop();app.controller.stop_all()
            tracking=getattr(app,'tracking_popup',None)
            tracking_stopped=True
            if tracking is not None:
                tracking.poll()
                tracking_stopped=not bool(tracking.canvas.find_all())
                tracking.close()
            pump(root,lambda:not app._active() and not app.audio_panel.is_running,timeout=20,label='AV owned stop')
            if not tracking_stopped:raise RuntimeError('Stopped tracking kept an old frame')
            result['tracking_stop_fenced_gui']=tracking is not None
        result['worker_stopped']=True;result['rtsp_audio_stopped_reaped']=not listener.runtime.is_running
        result['rtsp_protocol_describe_setup_play']=all(m in source.requests for m in ('DESCRIBE','SETUP','PLAY'))
        if source.errors:raise RuntimeError('Owned AV fixture failed: '+','.join(source.errors))
    result.update(history(root,app))
    # Render actual decoder/NCC history, not fabricated announcement rows.
    panel=app.refueling_panel;app.notebook.select(panel);panel.refresh()
    pump(root,lambda:bool(panel.tree.get_children()),label='actual audio history GUI')
    result['rtsp_audio_history_gui']=True
    from .startup import bind,validate_binding
    startup=replace(app.config,windows_startup_audio=True)
    binding=bind(startup,str(profile),None)
    validate_binding(replace(startup,windows_startup_enabled=True,windows_startup_binding=binding))
    result['rtsp_startup_binding_v2']=True
    from .webhook_smoke import smoke_webhook
    # Existing fake-only API flow uses this owned generated image, not customer
    # evidence and not an assertion of synchronized physical AV provenance.
    Image.new('RGB',(32,32),'blue').save(app.config.data_dir/'refueling-smoke-source.png')
    result.update(smoke_webhook(root,app))
    app.notebook.select(app.audio_panel);app.audio_panel.modes.select(app.offline_audio_panel);root.update()
    from .desktop_offline_audio import smoke_offline_audio
    result.update(smoke_offline_audio(root,app.offline_audio_panel,app.config))
    from .desktop_notify_test import smoke_notify_test
    result.update(smoke_notify_test(root,app.notify_test_panel))
    result['rtsp_status_visible_gui']='รับภาพ RTSP' in app.log.get('1.0','end')
    result['calibration_ready']=is_ready(app.config,app.store.root)
    result['audio_progress_frames']=max((e.get('frames',0) for e in events),default=0)
    # Actual controls, no native dialog and no data migration: stage a dedicated
    # temporary folder, verify that only an explicit restart-Save can apply it.
    from unittest.mock import patch
    picker=app.storage_picker
    canvas=app.settings_canvas
    root.deiconify()
    app.notebook.select(app.settings)
    # Map the containing viewport before scrolling; a native child outside the
    # viewport need not map until after it is brought into view.
    pump(root,lambda:app.settings.winfo_ismapped() and canvas.winfo_height()>1,
        label='storage viewport mapped',diagnostic=lambda:dict(state=root.state(),
            selected=app.notebook.select(),settings=str(app.settings),height=canvas.winfo_height()))
    canvas.yview_moveto(1.)
    checks={}
    def ready():
        checks.update(mapped=bool(picker.winfo_ismapped()),idle=not app._storage_busy(),
            enabled=bool(picker.instate(['!disabled'])),top=picker.winfo_rooty()>=canvas.winfo_rooty(),
            bottom=picker.winfo_rooty()+picker.winfo_height()<=canvas.winfo_rooty()+canvas.winfo_height(),
            left=picker.winfo_rootx()>=canvas.winfo_rootx(),
            right=picker.winfo_rootx()+picker.winfo_width()<=canvas.winfo_rootx()+canvas.winfo_width())
        return all(checks.values())
    pump(root,ready,label='storage picker visible and enabled',diagnostic=lambda:dict(checks))
    current=app.config.data_dir;draft=app.form['data_dir'].get()
    with patch('smart_tapo.desktop.filedialog.askdirectory',return_value='') as dialog:
        picker.invoke()
        dialog.assert_called_once()
    if app.form['data_dir'].get()!=draft or app.config.data_dir!=current:
        raise RuntimeError('Cancel changed storage')
    result['storage_folder_cancel_keeps_scope']=True
    target=app.store.root/'owned-new-volume-data'
    with patch('smart_tapo.desktop.filedialog.askdirectory',return_value=str(target)) as dialog:
        picker.invoke()
        dialog.assert_called_once()
    if app._candidate().data_dir!=target or app.config.data_dir!=current or target.exists():
        raise RuntimeError('Storage draft applied before explicit Save')
    app.form['data_dir'].set(draft)
    result.update(storage_folder_picker_gui=True,storage_folder_draft_not_applied=True)
    if not result['rtsp_status_visible_gui'] or not result['calibration_ready']:raise RuntimeError('RTSP state not mapped')
    return result
