"""Single-writer refueling aggregate from confirmed, source-ordered audio.

No capture, database, webhook, or money mutation. Caller must first validate
Live provenance, arbitrate ambiguous acoustic candidates and persist/deduplicate
source observations. tick() advances the processed audio timeline, not a wall
clock ahead of unclassified/pending audio. The caller must commit checkpoints
atomically with the corresponding immutable updates before transport.
"""
from __future__ import annotations

from dataclasses import asdict,dataclass,replace
import math
import re
import uuid

MISSING_END_SECONDS=300
REPEATED_READY='ready_prompt_repeated'
MISSING_END='completion_not_detected'


def _time(value):
    if (type(value) not in (int,float) or value<0 or value>253402300799
            or not math.isfinite(value)):
        raise ValueError('Invalid observation timestamp')
    return float(value)


@dataclass(frozen=True)
class ConfirmedAnnouncement:
    kind:str
    started_at:float
    ended_at:float

    def __post_init__(self):
        if self.kind not in ('paid_ready','end'):raise ValueError('Unknown announcement kind')
        start,end=_time(self.started_at),_time(self.ended_at)
        if not 0<end-start<=20:raise ValueError('Invalid acoustic observation span')
        object.__setattr__(self,'started_at',start)
        object.__setattr__(self,'ended_at',end)


@dataclass(frozen=True)
class RefuelingSession:
    id:str
    camera_id:str
    started_at:float
    last_ready_at:float
    ready_count:int=1
    revision:int=1
    ended_at:float|None=None
    repeated_ready_at:float|None=None
    missing_end_at:float|None=None
    source_interrupted_at:float|None=None

    @property
    def issue_codes(self):
        return tuple(code for code,value in ((REPEATED_READY,self.repeated_ready_at),(MISSING_END,self.missing_end_at),
                                            ('audio_source_interrupted',self.source_interrupted_at))
                     if value is not None)

    @property
    def has_issue(self):return bool(self.issue_codes)

    @property
    def state(self):
        return 'completed' if self.ended_at is not None else ('interrupted' if self.source_interrupted_at is not None else 'open')

    @property
    def completion_deadline(self):return self.last_ready_at+MISSING_END_SECONDS


@dataclass(frozen=True)
class SessionUpdate:
    kind:str
    session:RefuelingSession
    observed_at:float

    @property
    def event_key(self):
        """Draft stable outbox key; persist this snapshot atomically in future."""
        return f'{self.session.id}:{self.session.revision}'


@dataclass(frozen=True)
class RefuelingDecision:
    updates:tuple[SessionUpdate,...]=()
    ignored_reason:str|None=None


class RefuelingEngine:
    """One open round per camera, bounded state, immutable update snapshots."""
    def __init__(self,camera_id,*,id_factory=lambda:uuid.uuid4().hex):
        if not isinstance(camera_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',camera_id):
            raise ValueError('Invalid camera identifier')
        self.camera_id=camera_id
        self._id_factory=id_factory
        self._current=None
        self._last_utterance=None
        self._processed_until=0.

    @property
    def current(self):return self._current

    def checkpoint(self):
        return {'schema_version':1,'camera_id':self.camera_id,
                'current':asdict(self._current) if self._current is not None else None,
                'last_utterance':asdict(self._last_utterance) if self._last_utterance is not None else None,
                'processed_until':self._processed_until}

    def restore_checkpoint(self,snapshot):
        """Validate a durable checkpoint completely before changing live state."""
        keys={'schema_version','camera_id','current','last_utterance','processed_until'}
        if (not isinstance(snapshot,dict) or set(snapshot)!=keys or type(snapshot['schema_version']) is not int
                or snapshot['schema_version']!=1 or snapshot['camera_id']!=self.camera_id):
            raise ValueError('Invalid checkpoint schema or camera owner')
        processed=_time(snapshot['processed_until'])
        raw=snapshot['last_utterance']
        last=None
        if raw is not None:
            if not isinstance(raw,dict) or set(raw)!=set(ConfirmedAnnouncement.__dataclass_fields__):
                raise ValueError('Invalid checkpoint source span')
            last=ConfirmedAnnouncement(**raw)
            if last.started_at>processed:raise ValueError('Checkpoint source is beyond watermark')
        raw=snapshot['current']
        current=None
        if raw is not None:
            fields=set(RefuelingSession.__dataclass_fields__)
            if not isinstance(raw,dict) or set(raw) not in (fields,fields-{'source_interrupted_at'}):
                raise ValueError('Invalid checkpoint current round')
            if raw.get('source_interrupted_at') is not None:
                _time(raw['source_interrupted_at'])
                raise ValueError('Interrupted round cannot remain current')
            if (not isinstance(raw['id'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',raw['id'])
                    or raw['camera_id']!=self.camera_id or raw['ended_at'] is not None):
                raise ValueError('Invalid checkpoint round identity')
            if (type(raw['ready_count']) is not int or type(raw['revision']) is not int
                    or not 1<=raw['ready_count']<=raw['revision']<=1000000000):
                raise ValueError('Invalid checkpoint counters')
            start,latest=_time(raw['started_at']),_time(raw['last_ready_at'])
            if not start<=latest<=processed:raise ValueError('Invalid checkpoint timeline')
            repeated=raw['repeated_ready_at']
            if raw['ready_count']==1 and repeated is not None:raise ValueError('Unexpected repeated-ready issue')
            if raw['ready_count']>1:
                if repeated is None or not start<=_time(repeated)<=latest:
                    raise ValueError('Missing repeated-ready issue')
            missing=raw['missing_end_at']
            if missing is not None and not start<=_time(missing)<=processed:
                raise ValueError('Invalid missing-end issue')
            if last is None or last.kind!='paid_ready' or last.started_at!=latest:
                raise ValueError('Checkpoint lacks latest confirmed ready')
            current=RefuelingSession(**raw)
        elif last is not None and last.kind!='end':
            raise ValueError('Checkpoint has ready without an open round')
        self._current,self._last_utterance,self._processed_until=current,last,processed

    def interrupt(self,at):
        """Detach an uncertain round without claiming received audio or completion."""
        at=_time(at)
        session=self._current
        if session is not None and at<max(session.last_ready_at,self._processed_until):
            raise ValueError('Interruption precedes received audio')
        updates=()
        if session is not None:
            session=replace(session,source_interrupted_at=at,revision=session.revision+1)
            updates=(SessionUpdate('audio_source_interrupted',session,at),)
        self._current=self._last_utterance=None
        return updates

    def tick(self,processed_until):
        at=_time(processed_until)
        if at<self._processed_until:return ()
        self._processed_until=at
        session=self._current
        # Strictly MORE than five minutes; never fabricate a completion timestamp.
        if session is None or session.missing_end_at is not None or at<=session.completion_deadline:
            return ()
        self._current=replace(session,missing_end_at=at,revision=session.revision+1)
        return (SessionUpdate('completion_missing',self._current,at),)

    def accept(self,announcement):
        if not isinstance(announcement,ConfirmedAnnouncement):raise ValueError('Confirmed announcement required')
        if announcement.started_at<self._processed_until:
            return RefuelingDecision(ignored_reason='duplicate_or_stale_observation')
        previous=self._last_utterance
        if previous is not None:
            if announcement.ended_at<=previous.ended_at:
                return RefuelingDecision(ignored_reason='duplicate_or_stale_observation')
            overlap=max(0,min(announcement.ended_at,previous.ended_at)-
                        max(announcement.started_at,previous.started_at))
            shorter=min(announcement.ended_at-announcement.started_at,
                        previous.ended_at-previous.started_at)
            # Majority overlap is the same utterance, but tiny STFT-boundary
            # jitter must not suppress a genuine back-to-back repeat.
            if overlap/shorter>=.5:
                return RefuelingDecision(ignored_reason='overlapping_observation')
        at=announcement.started_at
        updates=list(self.tick(at))
        if announcement.kind=='paid_ready':
            if self._current is None:
                identifier=self._id_factory()
                if not isinstance(identifier,str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',identifier):
                    raise ValueError('Invalid generated session identifier')
                self._current=RefuelingSession(identifier,self.camera_id,at,at)
                kind='started'
            else:
                session=self._current
                self._current=replace(session,last_ready_at=at,ready_count=session.ready_count+1,
                                      repeated_ready_at=session.repeated_ready_at if session.repeated_ready_at is not None else at,
                                      revision=session.revision+1)
                kind='ready_repeated'
            self._last_utterance=announcement
            updates.append(SessionUpdate(kind,self._current,at))
        else:
            self._last_utterance=announcement
            if self._current is None:
                return RefuelingDecision(tuple(updates),'completion_without_start')
            session=replace(self._current,ended_at=at,revision=self._current.revision+1)
            updates.append(SessionUpdate('completed',session,at))
            self._current=None
        return RefuelingDecision(tuple(updates))
