"""One explicit watch consent, bounded same-selection automatic recovery.

No app-start listening, arbitrary navigation, device substitution, or new consent.
Repair is limited to a fresh selected-camera refresh overlay, or an observed
inactivity dialog authorized by recent same-session selected-camera proof.
The low-level controller owns/reaps each attempt before another can be created.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import time

from .continuous_listener import ContinuousListener
from .listener_health import ListenerHealthLog

BACKOFF=(1,2,5,10,30)
# Retry an already-reaped transient failure promptly. Every new attempt still
# obtains fresh selected-camera proof and opens only the frozen selected device.
# Broken profiles/device selection/unknown errors retain the slower backoff.
FAST_BACKOFF=(0,.25,.5,1,2)
FAST_RECOVERY_CODES=frozenset(('audio_clock_discontinuity','audio_timeout',
    'live_navigation_changed','live_check_unavailable'))


class WatchListener:
    def __init__(self,*,factory=None,clock=time.monotonic,health_factory=None):
        self._factory=factory or ContinuousListener
        self._health_factory=health_factory or (ListenerHealthLog if factory is None else None)
        self._health=None
        self._health_failed=False
        self._health_pid=None
        self._clock=clock
        self._attempt=None
        self._watching=self._closing=False
        self._next_retry=None
        self._failures=0
        self._fingerprint=None
        self._events=[]
        self._last_error={'kind':'error','code':'worker_failed'}

    @property
    def health_snapshot(self):
        if self._health is not None:
            return {**self._health.snapshot(),'enabled':True,'io_error':self._health_failed or self._health.snapshot()['io_error']}
        return {'enabled':self._health_factory is not None,'io_error':self._health_failed}

    def _record_health(self,event):
        if self._health is None or self._health_failed:return
        try:
            self._health.record_whitelisted(event,child_pid=self._health_pid,
                device_identity=json.dumps(self._device,sort_keys=True,separators=(',',':')),
                profile_identity=self._fingerprint)
        except Exception:
            self._health_failed=True  # Diagnostics failure must not stop audio.

    def _observe_pid(self):
        process=getattr(self._attempt,'_process',None)
        pid=getattr(process,'pid',None)
        if type(pid) is int and pid>0 and pid!=self._health_pid:
            self._health_pid=pid
            if self._health is not None and not self._health_failed:
                try:self._health.record_whitelisted({'kind':'state'},phase='preparing',child_pid=pid)
                except Exception:self._health_failed=True

    @property
    def is_running(self):
        return self._watching or (self._attempt is not None and self._attempt.is_running)

    def start(self,config,device,profile_path,*,consent=False,expected_fingerprint=None):
        if self._closing or self.is_running:raise ValueError('busy_or_closing')
        if consent is not True:raise ValueError('consent_required')
        self._config=deepcopy(config)
        self._device=deepcopy(device)
        self._path=str(Path(profile_path).absolute())
        if expected_fingerprint is not None and (type(expected_fingerprint) is not str or not re.fullmatch('[0-9a-f]{64}',expected_fingerprint)):
            raise ValueError('invalid_profile_binding')
        self._fingerprint=expected_fingerprint
        self._failures=0
        self._events=[]
        self._next_retry=None
        self._watching=True
        self._observations=0
        if self._health is not None:self._health.close()
        self._health=None
        self._health_failed=False
        if self._health_factory is not None:
            try:self._health=self._health_factory(self._config.data_dir)
            except Exception:self._health_failed=True
        try:self._begin()
        except Exception:
            self._record_health({'kind':'error','code':'listener_start_failed'})
            self._watching=False
            if self._attempt is not None:self._attempt.close()
            raise

    def _begin(self):
        if not self._watching or self._closing or self._attempt is not None:return
        self._attempt=self._factory()
        self._attempt_started=self._clock()
        self._health_pid=None
        self._attempt_observations=0
        self._next_retry=None
        self._record_health({'kind':'attempt_begin'})
        self._attempt.start(self._config,deepcopy(self._device),self._path,
                            consent=True,expected_fingerprint=self._fingerprint)
        self._observe_pid()

    def stop(self):
        active=self._watching
        self._watching=False
        self._next_retry=None
        if self._attempt is not None:self._attempt.stop()
        elif active:self._events.append({'kind':'finished','mode':'listener'})

    def close(self):
        self._closing=True
        self.stop()
        if self._attempt is not None:self._attempt.close()

    def _schedule(self,now):
        policy=FAST_BACKOFF if self._last_error.get('code') in FAST_RECOVERY_CODES else BACKOFF
        delay=policy[min(self._failures,len(policy)-1)]
        self._failures=min(self._failures+1,len(BACKOFF))
        self._next_retry=now+delay
        event={**self._last_error,'kind':'listener_retry','retry_seconds':delay}
        self._record_health(event)
        return event

    def poll(self):
        now=self._clock()
        events,self._events=self._events,[]
        if self._attempt is not None:
            self._observe_pid()
            incoming=self._attempt.poll()
            self._observe_pid()
            for event in incoming:
                kind=event.get('kind')
                if kind in ('error','finished') and self._watching:
                    self._record_health(event)
                    self._last_error=event if kind=='error' else {'kind':'error','code':'worker_failed'}
                    # Never overlap even when an SDK check remains in flight.
                    self._attempt.close()
                else:
                    if kind=='listener_progress':
                        total=event.get('observations_total')
                        if type(total) is int and 0<=total<=1000000000:
                            self._observations=min(1000000000,self._observations+max(0,total-self._attempt_observations))
                            self._attempt_observations=total
                            event={**event,'observations_total':self._observations}
                    self._record_health(event)
                    events.append(event)
                    if kind=='listener_progress' and now-self._attempt_started>=30:
                        self._failures=0
            fingerprint=getattr(self._attempt,'profile_fingerprint',None)
            if self._fingerprint is None and type(fingerprint) is str and re.fullmatch('[0-9a-f]{64}',fingerprint):
                self._fingerprint=fingerprint
            if not self._attempt.is_running:
                self._record_health({'kind':'attempt_end'})
                self._attempt.close()
                self._attempt=None
                if self._watching and not self._closing:events.append(self._schedule(now))
        if self._watching and not self._closing and self._attempt is None and self._next_retry is not None and now>=self._next_retry:
            try:
                self._begin()
                events.append({'kind':'listener_rechecking'})
            except Exception:
                # Synchronous setup failure is also paced and sanitized. Any
                # partially created attempt remains owned until it is reaped.
                self._last_error={'kind':'error','code':'listener_start_failed'}
                self._record_health(self._last_error)
                if self._attempt is not None:self._attempt.close()
                if self._attempt is None or not self._attempt.is_running:
                    if self._attempt is not None:self._record_health({'kind':'attempt_end'})
                    self._attempt=None
                    events.append(self._schedule(now))
        if not self.is_running and self._health is not None:
            self._record_health({'kind':'finished'})
            self._health.close()
        return events
