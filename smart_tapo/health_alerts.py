"""Pure, explicitly armed health notices; no transport, clock, or persistence.

All times are finite monotonic seconds from the SAME caller clock. Older calls
are ignored (False / []); equal-time calls are allowed. Call observe only for
fresh classified samples, including explicit unhealthy/unknown when inputs go
stale. Silence/static pixels are not evidence of failure. This module cannot
infer sample freshness: the caller MUST report stale channels as unknown.

Sender protocol: take_due -> mark_sending immediately before transport ->
complete with success/failed/ambiguous. Serialize these calls with observations.
Only success means provider acceptance, never phone delivery/acknowledgement.
Queued notices cancel on recovery; claimed notices stay until completion so an
accepted fault followed by health gets exactly one ordered recovery. Disarm
invalidates even claimed notices. No automatic arm on process restart.
"""
from dataclasses import dataclass
import math
from uuid import uuid4

CHANNELS = frozenset(('vision', 'audio'))
SAMPLE_MAX_AGE = 8.0
RECOVERY_SECONDS = 30.0
REASONS = frozenset(('vision_no_frames', 'vision_alignment', 'vision_quality',
                     'vision_worker_failed', 'audio_no_pcm', 'audio_disconnected',
                     'audio_no_track', 'audio_listener_failed', 'audio_storage',
                     'starting', 'unknown', 'ok'))


def _number(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + ' must be a finite number')
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(name + ' is outside its allowed range')
    return value


@dataclass(frozen=True)
class Notice:
    """Opaque delivery token plus safe channel/kind/reason, never user context."""
    channel: str
    kind: str
    reason: str
    generation: int
    incident: int
    serial: int
    # Random per monitor so serials from a later scope cannot reuse an alert ID.
    scope: str = ''


def format_notice(notice):
    """Fixed Thai text; reject forged unsafe fields rather than interpolate them."""
    if (not isinstance(notice, Notice) or notice.channel not in CHANNELS
            or notice.reason not in REASONS or notice.kind not in ('fault', 'reminder', 'recovered')):
        raise ValueError('invalid notice')
    channel = {'vision': 'ระบบภาพ/การจับแนวภาพ', 'audio': 'ระบบเสียง RTSP'}[notice.channel]
    if notice.kind == 'recovered':
        return channel + ': กลับมาทำงานปกติแล้ว'
    reasons = {
        'vision_no_frames': 'ไม่มีภาพใหม่ที่ตรวจสอบความสดได้',
        'vision_alignment': 'จับแนวภาพไม่ได้',
        'vision_quality': 'ภาพยังไม่ผ่านเงื่อนไขการตรวจ',
        'vision_worker_failed': 'ระบบรับหรือประมวลผลภาพหยุดผิดปกติ',
        'audio_no_pcm': 'ไม่มีข้อมูลเสียงใหม่ที่ใช้งานได้',
        'audio_disconnected': 'การเชื่อมต่อเสียง RTSP ขาด',
        'audio_no_track': 'RTSP ไม่มี audio track',
        'audio_listener_failed': 'ระบบฟังเสียงหยุดผิดปกติ',
        'audio_storage': 'ระบบฟังเสียงบันทึกประวัติไม่ได้',
        'starting': 'ยังไม่ได้ข้อมูลพร้อมใช้งานหลังเริ่มเฝ้า',
        'unknown': 'ยังยืนยันการทำงานไม่ได้', 'ok': 'ยังยืนยันการทำงานไม่ได้',
    }
    label = 'ตรวจพบปัญหา' if notice.kind == 'fault' else 'ปัญหายังคงอยู่'
    return f'{channel}: {label} — {reasons[notice.reason]} ({notice.reason})'


@dataclass
class _Channel:
    generation: int
    incident: int = 1
    healthy: bool = False
    reason: str = 'starting'
    bad_since: float = 0
    accepted: bool = False
    next_fault: float = 0
    pending: object = None
    sending: bool = False
    attempts: int = 0
    due: float = 0
    good_since: float | None = None
    good_last: float | None = None


class HealthMonitor:
    """Two bounded channel states, one pending notice per channel.

    Accepted incidents require 30s of fresh advancing evidence to recover,
    with at most 8s between proofs; one good sample cannot split an incident.
    delay: 1..3600 seconds; repeat: 60..86400 seconds. Three attempts per
    notice, 30/60 seconds apart; exhaustion waits repeat before another batch
    (including recovery). Missed intervals coalesce; no catch-up bursts.
    arm resets an existing scope; disarm never emits recovery. Public methods
    return False for stale times/tokens. take_due returns eligible tokens but
    does not claim them; mark_sending is mandatory before complete.
    """
    def __init__(self, delay=30, repeat=900):
        self.delay = _number(delay, 'delay', 1, 3600)
        self.repeat = _number(repeat, 'repeat', 60, 86400)
        self._states = {}
        self._generation = 0
        self._serial = 0
        self._scope = uuid4().hex
        self._now = float('-inf')

    def _time(self, now):
        _number(now, 'now')
        if now < self._now:
            return False
        self._now = now
        return True

    @staticmethod
    def _channel(channel):
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise ValueError('invalid channel')

    def arm(self, channel, now):
        """Explicitly begin a new unknown/startup scope with delay grace."""
        self._channel(channel)
        if not self._time(now):
            return False
        self._generation += 1
        self._states[channel] = _Channel(self._generation, bad_since=now,
                                         next_fault=now + self.delay)
        return True

    def disarm(self, channel):
        """Cancel all queued/claimed work; never report manual stop as recovery."""
        self._channel(channel)
        self._states.pop(channel, None)

    def observe(self, channel, healthy, reason, now):
        """Consume a fresh classification, not UI text or sample hashes.

        Unknown/starting are always unhealthy even if healthy=True. Invalid
        reason text is replaced with unknown and cannot enter a notice.
        """
        self._channel(channel)
        if not isinstance(healthy, bool):
            raise ValueError('healthy must be bool')
        reason = reason if isinstance(reason, str) and reason in REASONS else 'unknown'
        healthy = healthy and reason not in ('starting', 'unknown')
        if not healthy and reason == 'ok':
            reason = 'unknown'
        if not self._time(now):
            return False
        state = self._states.get(channel)
        if state is None:
            return False
        outstanding_fault = state.accepted or (state.sending and state.pending is not None
            and state.pending.kind in ('fault','reminder') and state.pending.incident==state.incident)
        if healthy and not state.healthy and outstanding_fault:
            # A returning packet is not sustained recovery. Keep this incident
            # accepted until new, fresh producer proofs span the full window.
            if state.good_last is None or now-state.good_last>SAMPLE_MAX_AGE:
                state.good_since=now
            state.good_last=now
            if now-state.good_since<RECOVERY_SECONDS:
                if not state.sending:
                    state.pending=None
                state.reason=reason
                return True
        state.good_since=state.good_last=None
        if healthy != state.healthy:
            if healthy:
                if not state.sending:
                    state.pending = None
            else:
                state.incident += 1
                state.bad_since = now
                state.next_fault = now + self.delay
                state.accepted = False
                if not state.sending:
                    state.pending = None
        state.healthy = healthy
        state.reason = reason
        return True

    def _new(self, channel, state, kind, now):
        self._serial += 1
        state.pending = Notice(channel, kind, state.reason, state.generation,
                               state.incident, self._serial, self._scope)
        state.attempts = 0
        state.due = now

    def take_due(self, now):
        """Return at most one eligible notice per channel; never enqueue history."""
        if not self._time(now):
            return []
        result = []
        for channel, state in self._states.items():
            if state.good_last is not None and now-state.good_last>SAMPLE_MAX_AGE:
                state.good_since=state.good_last=None
                state.reason='unknown'
            if state.good_since is not None:
                continue
            if state.pending is None:
                if state.healthy and state.accepted:
                    self._new(channel, state, 'recovered', now)
                elif not state.healthy and now >= state.next_fault:
                    self._new(channel, state, 'reminder' if state.accepted else 'fault', now)
            if state.pending is not None and not state.sending and now >= state.due:
                result.append(state.pending)
        return result

    def is_current(self, notice):
        """Whether token still belongs to this armed scope and pending delivery."""
        if not isinstance(notice, Notice):
            return False
        state = self._states.get(notice.channel)
        return state is not None and state.pending is notice

    def mark_sending(self, notice):
        """Claim once immediately before transport; False means DO NOT send."""
        if not self.is_current(notice):
            return False
        state = self._states[notice.channel]
        if state.sending or self._now < state.due:
            return False
        state.sending = True
        return True

    def complete(self, notice, outcome, now):
        """Record provider acceptance or paced failure; ignore stale completions."""
        if outcome not in ('success', 'failed', 'ambiguous'):
            raise ValueError('invalid outcome')
        if not self._time(now) or not self.is_current(notice):
            return False
        state = self._states[notice.channel]
        if not state.sending:
            return False
        state.sending = False
        if notice.incident != state.incident:
            state.pending = None
            return False
        if outcome == 'success':
            state.pending = None
            if notice.kind == 'recovered':
                state.accepted = False
            else:
                state.accepted = True
                state.next_fault = now + self.repeat
        elif state.healthy and notice.kind != 'recovered':
            state.pending = None
        else:
            state.attempts += 1
            if state.attempts >= 3:
                state.attempts = 0
                state.due = now + self.repeat
            else:
                state.due = now + min(300, 30 * state.attempts)
        return True
