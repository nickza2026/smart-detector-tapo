"""Owned synthetic spawn/SQLite smoke only. Never hardware, SDK or network."""
from dataclasses import replace
from datetime import datetime,timedelta
import hashlib
import json
import multiprocessing
from pathlib import Path
import queue
import time
from types import SimpleNamespace
import wave

import numpy as np

from .announcement_stream import LiveLease
from .capture.page_mode import PROFILE,PageMode
from .continuous_listener import ContinuousListener
from .profile_setup import create_private_profile
from .refueling_store import RefuelingStore

DEVICE=dict(index=0,name='Synthetic output loopback',rate=8000,channels=2)


def _phrase():
    t=np.arange(48000)/8000;y=np.zeros(len(t))
    for i,f in enumerate((410,730,1150,590,1640,870,1280,480)):
        mask=(t>=1+i*.32)&(t<1+i*.32+.25);u=t[mask]-(1+i*.32)
        y[mask]=np.sin(np.pi*u/.25)**2*(.2*np.sin(2*np.pi*f*u)+.08*np.sin(2*np.pi*f*1.7*u))
    return y


def _end_phrase():
    t=np.arange(48000)/8000;y=np.zeros(len(t))
    for start,length,f in ((1.,1.1,830),(3.,1.6,1470)):
        mask=(t>=start)&(t<start+length);u=t[mask]-start
        y[mask]=.25*np.sin(np.pi*u/length)**2*np.sin(2*np.pi*f*u)
    return y


def _fixture_job(final,progress,stop,leases,config,device,path,fingerprint,run_id):
    from . import continuous_listener as module
    initial=leases.get(timeout=3)
    y=_phrase();samples=np.concatenate((y,y,_end_phrase(),np.zeros(80000)))
    pcm=np.clip(samples*32768,-32768,32767).astype('<i2')
    frames=[0];origin=initial.issued_at+.5
    def clock():return origin+frames[0]/8000
    class Controls:
        issued=initial.issued_at
        def get(self,**kwargs):return initial
        def get_nowait(self):
            issued=initial.issued_at+int((clock()-initial.issued_at)//5)*5
            if issued<=self.issued:raise queue.Empty
            self.issued=issued
            return LiveLease(run_id,config.camera_id,issued,issued+10,PROFILE)
    class Stream:
        def get_read_available(self):return 1024
        def read(self,count,**kwargs):
            start=frames[0];frames[0]+=count
            data=np.zeros(count,dtype='<i2');part=pcm[start:start+count];data[:len(part)]=part
            if start>=len(pcm):stop.set()
            return np.stack((data,data),axis=1).tobytes()
        def close(self):pass
    class Audio:
        def get_host_api_info_by_type(self,kind):return {'index':1}
        def get_device_count(self):return 1
        def get_device_info_by_index(self,index):return dict(isLoopbackDevice=True,hostApi=1,name=DEVICE['name'],defaultSampleRate=8000,maxInputChannels=2)
        def open(self,**kwargs):return Stream()
        def terminate(self):pass
    class PastDatetime:
        @staticmethod
        def now(tz):return datetime.now(tz)-timedelta(seconds=60)
    module.time=SimpleNamespace(monotonic=clock)
    module.datetime=PastDatetime
    module.listener_job(final,progress,stop,Controls(),config,device,path,fingerprint,run_id,
                        api=SimpleNamespace(PyAudio=Audio,paWASAPI=13,paInt16=8))


class _Context:
    def __init__(self):self.context=multiprocessing.get_context('spawn')
    def __getattr__(self,key):return getattr(self.context,key)
    def Process(self,**kwargs):
        kwargs['target']=_fixture_job
        return self.context.Process(**kwargs)


def smoke_prepared_reference(directory):
    """Fresh synthetic canonical kit for explicit GUI-import smoke, not real audio."""
    directory=Path(directory);directory.mkdir(exist_ok=False)
    manifest=dict(schema_version=1,profile_id='synthetic_provided',camera_id='provided-recordings',templates=[])
    for kind,name,data,start,end in (('paid_ready','ready.wav',_phrase(),.95,3.7),
                                    ('end','end.wav',_end_phrase(),.9,4.7)):
        file=directory/name
        with file.open('xb') as raw:
            with wave.open(raw,'wb') as writer:
                writer.setparams((1,2,8000,0,'NONE','not compressed'))
                writer.writeframes(np.clip(data*32768,-32768,32767).astype('<i2').tobytes())
        manifest['templates'].append(dict(kind=kind,file=name,start=start,end=end,threshold=.95,
                                         sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
    path=directory/'profile.json'
    with path.open('x',encoding='utf-8') as out:json.dump(manifest,out)
    return path


def smoke_continuous(directory,config):
    """Caller supplies a fresh owned smoke directory, never user data directory."""
    directory=Path(directory)
    directory.mkdir(exist_ok=False)
    sources=directory/'source-fixtures';sources.mkdir()
    y=_phrase();templates=[];originals={}
    for kind,data,start,end in (('paid_ready',y,.95,3.7),('end',_end_phrase(),.9,4.7)):
        file=sources/(kind+'.wav')
        with file.open('xb') as raw:
            with wave.open(raw,'wb') as writer:
                writer.setparams((1,2,8000,0,'NONE','not compressed'))
                writer.writeframes(np.clip(data*32768,-32768,32767).astype('<i2').tobytes())
        originals[file]=hashlib.sha256(file.read_bytes()).hexdigest()
        templates.append(dict(kind=kind,file=file,start=start,end=end,threshold=.95))
    config=replace(config,data_dir=directory/'data')
    path=create_private_profile(config.data_dir,config.camera_id,templates)
    from .announcement_profile import load_announcement_profile
    from .announcement import AnnouncementMatcher,read_wav
    matcher=AnnouncementMatcher(load_announcement_profile(path,config.camera_id).templates)
    # Fixture validity is checked AFTER PCM16 quantization, not just float tones.
    for record in templates:
        pcm,rate=read_wav(record['file'])
        if [hit.label for hit in matcher.analyze_candidates(pcm,rate)]!=[record['kind']]:
            raise RuntimeError('Synthetic fixtures are acoustically ambiguous')
    runtime=ContinuousListener(context=_Context(),mode_reader=lambda cfg:PageMode('live','synthetic',PROFILE))
    runtime.start(config,DEVICE,path,consent=True)
    result=None;deadline=time.monotonic()+30
    try:
        while runtime.is_running and time.monotonic()<deadline:
            for event in runtime.poll():
                if event.get('kind') in ('finished','error'):result=event
            time.sleep(.01)
        if runtime.is_running or not result or result.get('kind')!='finished':raise RuntimeError('Synthetic listener spawn failed')
    finally:
        runtime.close()
        end=time.monotonic()+10
        while runtime.is_running and time.monotonic()<end:runtime.poll();time.sleep(.01)
        if runtime.is_running:raise RuntimeError('Synthetic listener did not reap')
    store=RefuelingStore(config.data_dir/'refueling',config.camera_id)
    try:
        rows=store.list_sessions()
        if len(rows)!=1 or rows[0]['ready_count']!=2 or rows[0]['state']!='completed' or rows[0]['missing_end_at'] is not None:
            raise RuntimeError('Synthetic listener did not durably aggregate ready-repeat/end: '+repr([
                (row['ready_count'],row['state'],row['started_at'],row['ended_at']) for row in rows]))
    finally:store.close()
    if any(hashlib.sha256(file.read_bytes()).hexdigest()!=digest for file,digest in originals.items()):
        raise RuntimeError('Synthetic profile modified source WAV')
    return {'continuous_spawn_reaped':True,'continuous_real_sqlite':True,'continuous_ready_count':2,
            'continuous_completed':True,'continuous_real_audio':False,'continuous_real_adb':False,
            'continuous_external_delivery':False,'continuous_profile_source_unchanged':True}
