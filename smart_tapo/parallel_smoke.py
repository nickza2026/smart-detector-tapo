"""Explicit synthetic GUI smoke only; no hardware, SDK or provider traffic."""
import multiprocessing
from pathlib import Path
import queue
import time
from types import SimpleNamespace
import wave

import numpy as np

from .capture.page_mode import PageMode,PROFILE
from .continuous_listener import ContinuousListener
from .continuous_smoke import DEVICE,_phrase,_end_phrase,_fixture_job
from .profile_setup import create_private_profile
from .refueling_store import RefuelingStore


def _held_audio_job(begin,ready,final,progress,stop,leases,*args):
    """Keep an owned synthetic child alive until both companion PIDs are ready."""
    lease=leases.get(timeout=3)
    ready.set();deadline=time.monotonic()+45
    while not begin.is_set() and not stop.is_set():
        if time.monotonic()>=deadline:raise RuntimeError('Parallel smoke gate expired')
        try:lease=leases.get_nowait()
        except queue.Empty:pass
        try:progress.put_nowait(dict(kind='listener_progress',heartbeat=time.monotonic(),frames=0,
                                   observations_total=0,waiting_audio=True))
        except queue.Full:pass
        stop.wait(.1)
    if stop.is_set():
        final.send(dict(kind='finished',mode='listener'));final.close();return
    # Real PCM/NCC/storage pipeline with a synthetic API/clock, not microphones.
    _fixture_job(final,progress,stop,SimpleNamespace(get=lambda **kwargs:lease),*args)


class _Context:
    def __init__(self):
        self.context=multiprocessing.get_context('spawn')
        self.begin=self.context.Event();self.ready=self.context.Event()
    def __getattr__(self,key):return getattr(self.context,key)
    def Process(self,**kwargs):
        kwargs['target']=_held_audio_job
        kwargs['args']=(self.begin,self.ready,*kwargs['args'])
        return self.context.Process(**kwargs)


class ParallelAudioSmoke:
    def __init__(self,root,app):
        self.root,self.app=root,app
        if app.config.notify_enabled or app.config.adb_serial!='synthetic-device':
            raise ValueError('Parallel smoke requires private synthetic config')
        self.panel=app.audio_panel.listener_panel
        self.original=(self.panel.runtime,self.panel.get_device,self.panel.profile_path.get())
        sources=app.store.root/'parallel-source-fixtures';sources.mkdir(exist_ok=False)
        templates=[]
        for kind,data,start,end in (('paid_ready',_phrase(),.95,3.7),('end',_end_phrase(),.9,4.7)):
            file=sources/(kind+'.wav')
            with wave.open(str(file),'wb') as writer:
                writer.setparams((1,2,8000,0,'NONE','not compressed'))
                writer.writeframes(np.clip(data*32768,-32768,32767).astype('<i2').tobytes())
            templates.append(dict(kind=kind,file=file,start=start,end=end,threshold=.95))
        path=create_private_profile(app.config.data_dir,app.config.camera_id,templates)
        self.context=_Context()
        self.runtime=ContinuousListener(context=self.context,
            mode_reader=lambda config:PageMode('live','synthetic',PROFILE))
        self.panel.runtime=self.runtime;self.panel.get_device=lambda:dict(DEVICE)
        self.panel.profile_path.set(str(path));self.panel.consent.set(True)
        self.panel.start()
        if not self.panel.is_running:raise RuntimeError('Explicit synthetic GUI audio Start failed')

    def finish(self):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            self.root.update()
            states=self.app.controller.snapshot()
            process=self.runtime._process
            if (self.context.ready.is_set() and process is not None and process.is_alive()
                    and all(states[role]['state']=='running' for role in ('capture','worker'))):
                pids=[states['capture']['pid'],states['worker']['pid'],process.pid]
                if len(set(pids))!=3:raise RuntimeError('Parallel roles did not own three distinct PIDs')
                break
            time.sleep(.02)
        else:raise RuntimeError('Three owned role processes did not overlap')
        self.context.begin.set()
        deadline=time.monotonic()+35
        while self.runtime.is_running and time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        if self.runtime.is_running or self.runtime._forced or self.runtime._result!={'kind':'finished','mode':'listener'}:
            raise RuntimeError('Parallel PCM/storage fixture did not finish cleanly')
        store=RefuelingStore(self.app.config.data_dir/'refueling',self.app.config.camera_id)
        try:
            rows=store.list_sessions()
            if not any(row['state']=='completed' and row['ready_count']==2 for row in rows):
                raise RuntimeError('Parallel fixture did not durably complete ready-repeat/end')
        finally:store.close()
        self.restore()
        return dict(parallel_audio_started_first=True,parallel_three_distinct_pids=True,
                    parallel_live_overlap=True,parallel_audio_completed=True,parallel_real_sqlite=True,
                    parallel_real_audio=False,parallel_real_adb=False,parallel_external_delivery=False)

    def restore(self):
        self.runtime.close();deadline=time.monotonic()+10
        while self.runtime.is_running and time.monotonic()<deadline:
            self.runtime.poll();time.sleep(.02)
        if self.runtime.is_running:raise RuntimeError('Owned parallel audio child did not reap')
        self.panel.runtime,self.panel.get_device,path=self.original
        self.panel.profile_path.set(path);self.panel._active=False;self.panel._stopping=False
