"""Explicit private-file smoke only: compiled matcher, no devices/providers.

Never called by normal startup. Manifest paths/hashes are supplied by the test
operator; source media is read, not copied into the application or report.
"""
import hashlib,json,re,time,uuid
from pathlib import Path
import numpy as np

def smoke_audio_cases(manifest_path,output_path,data_root):
    from .announcement_profile import load_announcement_profile
    from .announcement_stream import AnnouncementStream,RtspAudioLease
    from .refueling_store import RefuelingStore
    from .rtsp_audio import decoded_pcm,commit_batch
    output=Path(output_path)
    if output.exists():raise ValueError('offline_audio_report_already_exists')
    manifest=Path(manifest_path)
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size>32768:raise ValueError('invalid_offline_audio_manifest')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    if (type(data) is not dict or set(data)!={'profile','cases'} or type(data['cases']) is not list
            or not 1<=len(data['cases'])<=8):raise ValueError('invalid_offline_audio_manifest')
    profile=load_announcement_profile(Path(data['profile']),'provided-recordings')
    if any(t.temporal_threshold is None for t in profile.templates):raise ValueError('temporal_profile_required')
    rows=[]
    for item in data['cases']:
        if (type(item) is not dict or set(item)!={'path','sha256','expected'} or type(item['sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}',item['sha256']) is None
                or item['expected']!=['paid_ready','end']):raise ValueError('invalid_offline_audio_case')
        path=Path(item['path'])
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.suffix.lower()!='.mp4':raise ValueError('invalid_offline_audio_file')
        def digest():
            with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
        if digest()!=item['sha256']:raise ValueError('offline_audio_hash_mismatch')
        run=uuid.uuid4().hex;core=AnnouncementStream(profile,rate=16000,channels=1,run_id=run,
            utc_origin=1000,monotonic_origin=100,source_fingerprint=item['sha256'])
        store=RefuelingStore(Path(data_root)/run,profile.camera_id,clock=lambda:2000)
        frames=0;hits=[];started=time.monotonic()
        def feed(samples,tail):
            nonlocal frames
            begin=100+frames/16000;now=begin+len(samples)/16000
            batch=core.push(samples,start_frame=frames,now=now,lease=RtspAudioLease(run,profile.camera_id,begin-.01,now+1,item['sha256'],run))
            frames+=len(samples)
            hits.extend(dict(kind=x.announcement.kind,onset=x.announcement.started_at-1000,synthetic_tail=tail) for x in batch.observations)
            commit_batch(store,batch)
        try:
            import av
            with path.open('rb') as stream,av.open(stream,mode='r',format='mov',options={'protocol_whitelist':'file'}) as container:
                for samples,pts in decoded_pcm(container,av):feed(samples,False)
            original_frames=frames
            for _ in range(200):feed(np.zeros((1600,1),dtype='float32'),True)
            sessions=store.list_sessions()
            if [x['kind'] for x in hits]!=item['expected']:raise ValueError('offline_audio_event_mismatch')
            if len(sessions)!=1 or sessions[0]['ready_count']!=1 or sessions[0]['state']!='completed':raise ValueError('offline_audio_session_mismatch')
            if digest()!=item['sha256']:raise ValueError('offline_audio_input_changed')
            rows.append(dict(input_sha256=item['sha256'],audio_seconds=original_frames/16000,hits=hits,
                elapsed_seconds=round(time.monotonic()-started,3),synthetic_tail_seconds=20,ring_bytes=core._buffer.nbytes))
        finally:core.discard();store.close()
    report=dict(passed=True,cases=rows,profile_fingerprint=profile.fingerprint,
        real_user_stream=False,local_user_files=True,uploaded=False,played=False,production_verified=False)
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
    return dict(offline_private_audio_cases=True,offline_private_audio_temporal_profile=True,
                offline_private_audio_sessions=True)
