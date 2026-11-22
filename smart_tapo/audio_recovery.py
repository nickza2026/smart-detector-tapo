"""Bounded same-watch recovery; no IO, sleeps, notification or database access."""
from .health_alerts import SAMPLE_MAX_AGE, RECOVERY_SECONDS


class AudioRecovery:
    LIMIT = 3
    DELAY = 15.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = self.exhausted = False
        self.attempts = 0
        self.due = self.good_since = self.good_last = None

    @property
    def pending(self):
        return self.active and not self.exhausted

    def fail(self, now):
        self.active = True
        self.good_since = self.good_last = None
        if self.attempts >= self.LIMIT:
            self.exhausted = True
            self.due = None
        elif self.due is None:
            self.due = now + self.DELAY

    def take_due(self, now):
        if not self.pending or self.due is None or now < self.due:
            return False
        self.attempts += 1
        self.due = None
        return True

    def resume_observing(self):
        """A pre-alert producer returned; retain spent attempts until 30s proof."""
        self.active, self.exhausted = True, False
        self.due = self.good_since = self.good_last = None

    def observe_good(self, observed):
        if not self.pending or self.due is not None:
            return
        if self.good_last is not None and observed <= self.good_last:
            return
        if self.good_last is None or observed - self.good_last > SAMPLE_MAX_AGE:
            self.good_since = observed
        self.good_last = observed
        if observed - self.good_since >= RECOVERY_SECONDS:
            self.reset()


class AudioAlertProbe:
    """One owned reap/start/proof attempt, with a fixed supervisor deadline."""
    def __init__(self, request, now):
        self.request = request
        self.phase = 'stopping'
        self.due = min(request.deadline, now + SAMPLE_MAX_AGE)

    @property
    def pending(self):
        return self.phase in ('stopping', 'receiving')

    def started(self, now):
        self.phase = 'receiving'
        self.due = min(self.request.deadline, now + SAMPLE_MAX_AGE)
