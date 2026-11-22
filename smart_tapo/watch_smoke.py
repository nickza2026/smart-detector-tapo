"""Owned synthetic process recovery; no sound playback, real SDK, or network."""
from dataclasses import replace
from datetime import datetime,timedelta
import multiprocessing
from pathlib import Path
import queue
import time
from types import SimpleNamespace

import numpy as np

from .announcement_stream import LiveLease
from .capture.page_mode import PageMode,PROFILE
from .continuous_listener import ContinuousListener
from .continuous_smoke import DEVICE,_phrase,_end_phrase
from .refueling_store import RefuelingStore
from .watch_listener import WatchListener


def _watch_job(phase,final,progress,stop,leases,config,device,path,fingerprint,run_id):
    from . import continuous_listener as module
    initial=leases.get(timeout=3)
    ready,end=_phrase(),_end_phrase()
    samples=(np.concatenate((ready,np.zeros(96000))) if phase==0 else
             np.concatenate((end,ready,ready,end,np.zeros(112000))) if phase==1 else
             np.concatenate((ready,end,np.zeros(112000))))
    pcm=np.clip(samples*32768,-32768,32767).astype('<i2')
    frame=[0];elapsed=[0.];polls=[0]
    origin=initial.issued_at+.5
    utc=datetime.now().astimezone()-timedelta(seconds=180-phase*60)
    def clock():return origin+elapsed[0]
    class Date:
        @staticmethod
        def now(tz):return (utc+timedelta(seconds=elapsed[0])).astimezone(tz)
    class Controls:
        issued=initial.issued_at
        def get(self,**kwargs):return initial
        def get_nowait(self):
            issued=initial.issued_at+int((clock()-initial.issued_at)//5)*5
            if issued<=self.issued:raise queue.Empty
            self.issued=issued
            return LiveLease(run_id,config.camera_id,issued,issued+10,PROFILE)
    class Stop:
        def is_set(self):return stop.is_set()
        def set(self):stop.set()
        def wait(self,delay):
            elapsed[0]+=delay;polls[0]+=1
            if polls[0]%25==0:time.sleep(.005)
            return stop.is_set()
    class Progress:
        def put_nowait(self,event):progress.put_nowait({**event,'heartbeat':time.monotonic()})
        def get_nowait(self):return progress.get_nowait()
        def cancel_join_thread(self):progress.cancel_join_thread()
    class Stream:
        def get_read_available(self):
            if phase>0 and elapsed[0]<12:return 0
            if frame[0]>=len(pcm) and phase<2:raise OSError('synthetic endpoint interruption')
            return 1024
        def read(self,count,**kwargs):
            start=frame[0];frame[0]+=count;elapsed[0]+=count/8000
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
    module.time=SimpleNamespace(monotonic=clock)
    module.datetime=Date
    module.listener_job(final,Progress(),Stop(),Controls(),config,device,path,fingerprint,run_id,
                        api=SimpleNamespace(PyAudio=Audio,paWASAPI=13,paInt16=8))


class _Context:
    def __init__(self,phase):self.phase=phase;self.context=multiprocessing.get_context('spawn')
    def __getattr__(self,key):return getattr(self.context,key)
    def Process(self,**kwargs):
        kwargs['target']=_watch_job;kwargs['args']=(self.phase,*kwargs['args'])
        return self.context.Process(**kwargs)


def smoke_watch(directory,config,path,panel=None):
    directory=Path(directory);directory.mkdir(exist_ok=False)
    config=replace(config,data_dir=directory/'data')
    attempts=[]
    def factory():
        if len(attempts)>=3:raise RuntimeError('Unbounded smoke retries')
        attempt=ContinuousListener(context=_Context(len(attempts)),mode_reader=lambda cfg:PageMode('live','synthetic',PROFILE))
        attempts.append(attempt);return attempt
    runtime=WatchListener(factory=factory)
    original=None;paused=False
    if panel is not None:
        original=(panel.runtime,panel.get_config,panel.get_device)
        panel.runtime=runtime;panel.get_config=lambda:config;panel.get_device=lambda:dict(DEVICE)
        panel.profile_path.set(str(path));panel.consent.set(True)
        panel.start() # Exactly one explicit Start through the actual GUI controls.
    else:runtime.start(config,DEVICE,path,consent=True)
    try:
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if panel is not None:
                panel.poll();panel.update_idletasks()
                paused=paused or 'อัตโนมัติ' in panel.status.get()
            else:
                paused=paused or any(e.get('kind')=='listener_retry' for e in runtime.poll())
            if len(attempts)==3 and runtime._attempt is None and runtime._next_retry is not None:break
            time.sleep(.01)
        else:raise RuntimeError('One-start automatic recovery did not finish three owned attempts')
        if not runtime.is_running or not paused:raise RuntimeError('Watch unexpectedly stopped or failed to expose recovery')
    finally:
        if panel is not None:panel.stop()
        else:runtime.stop()
        runtime.close();deadline=time.monotonic()+10
        while runtime.is_running and time.monotonic()<deadline:runtime.poll();time.sleep(.01)
        if original is not None:panel.runtime,panel.get_config,panel.get_device=original
        if runtime.is_running:raise RuntimeError('Automatic recovery worker not reaped')
    with_store=RefuelingStore(config.data_dir/'refueling',config.camera_id)
    try:
        rows=with_store.list_sessions()
        states=sorted((r['state'],r['ready_count']) for r in rows)
        if states!=[('completed',1),('completed',2),('interrupted',1)]:
            raise RuntimeError('Recovery glued/lost synthetic rounds: '+repr(states))
        interrupted=next(r for r in rows if r['state']=='interrupted')
        if interrupted['ended_at'] is not None or interrupted['missing_end_at'] is not None:raise RuntimeError('Gap invented an end/timeout')
    finally:with_store.close()
    return dict(watch_start_once=True,watch_spawn_attempts=3,watch_auto_recovery=True,watch_stopped_and_reaped=True,
        watch_gap_not_merged=True,watch_completed_rounds=2,watch_no_fake_end=True,watch_real_audio=False,watch_real_adb=False)
