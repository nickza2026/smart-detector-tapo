"""Bounded, generation-fenced desktop video transport; never a disk fallback."""
from dataclasses import dataclass, asdict
import hashlib
import json
import queue
import uuid
import math
import re
import time

from .frames import Frame

MAX_PAYLOAD = 64 * 1024 * 1024
SLOTS = 2


def binding(config):
    return hashlib.sha256(repr(config).encode()).hexdigest()


@dataclass(frozen=True)
class MemoryFrame:
    generation: int
    binding: str
    metadata: bytes
    png: bytes

    @classmethod
    def create(cls, generation, config, png, session, sequence, wall, received, proof):
        if type(png) is not bytes or not 0 < len(png) <= MAX_PAYLOAD:
            raise ValueError('RAM frame payload limit')
        identity = session + '-' + str(sequence) + '-' + uuid.uuid4().hex
        frame = Frame(identity, config.camera_id, session, sequence, wall, received,
                      *config.expected_size, identity + '.png', True, source_proof=proof)
        return cls(generation, binding(config), json.dumps(asdict(frame), allow_nan=False).encode(), png)

    def unpack(self, config):
        if (type(self.generation) is not int or self.generation < 0
                or type(self.png) is not bytes or not 0 < len(self.png) <= MAX_PAYLOAD
                or type(self.metadata) is not bytes or len(self.metadata) > 16384
                or self.binding != binding(config)):
            raise ValueError('Invalid RAM envelope')
        frame = Frame(**json.loads(self.metadata))
        if (frame.camera_id != config.camera_id or (frame.width, frame.height) != config.expected_size
                or type(frame.width) is not int or type(frame.height) is not int
                or type(frame.sequence) is not int or frame.sequence < 1
                or type(frame.fresh) is not bool
                or not isinstance(frame.session_id, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', frame.session_id) is None
                or not isinstance(frame.frame_id, str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,160}', frame.frame_id) is None
                or frame.image_name != frame.frame_id + '.png'
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                       for value in (frame.captured_at, frame.captured_monotonic))):
            raise ValueError('Invalid RAM identity')
        return frame


class FrameChannel:
    def __init__(self, context, output):
        # A manager-owned Queue has no producer-process feeder thread: stopping
        # a capture cannot truncate a pipe message or strand its surviving worker.
        self.output = output
        self.epoch = context.Value('Q', 0)
        # Both values are protected by epoch's recursive lock. Never read the
        # cutoff independently: a new epoch with an old cutoff admits old pixels.
        self._fresh_after = context.RawValue('d', time.monotonic())
        self._invalidated = context.Event()

    def invalidate(self):
        # Fatal Capture exit must close the voting gate without waiting on a
        # Worker's decision/SQLite commit lock. A later explicit fence reopens it.
        self._invalidated.set()

    def snapshot(self):
        if self._invalidated.is_set():
            return -1, float('inf')
        with self.epoch.get_lock():
            if self._invalidated.is_set():
                return -1, float('inf')
            return self.epoch.value, self._fresh_after.value

    @property
    def generation(self):
        return self.snapshot()[0]

    def fence(self):
        with self.epoch.get_lock():
            self._fresh_after.value = max(self._fresh_after.value, time.monotonic())
            self.epoch.value += 1
            self._invalidated.clear()
            return self.epoch.value

    def publish(self, envelope):
        if (not isinstance(envelope, MemoryFrame) or type(envelope.png) is not bytes
                or not 0 < len(envelope.png) <= MAX_PAYLOAD
                or type(envelope.metadata) is not bytes or len(envelope.metadata) > 16384):
            raise ValueError('Invalid RAM payload')
        # Drop newest, preserving sequence loss. Never evict concurrently with
        # the consumer or block on a stopped worker/queue feeder.
        received = json.loads(envelope.metadata).get('captured_monotonic')
        if type(received) not in (int, float) or not math.isfinite(received):
            raise ValueError('Invalid RAM receive time')
        with self.epoch.get_lock():
            generation, fresh_after = self.snapshot()
            if envelope.generation != generation or received <= fresh_after:
                return False
            try:
                self.output.put_nowait(envelope)
                return True
            except queue.Full:
                return False

    def receive(self, timeout=.1):
        try:
            return self.output.get(timeout=timeout)
        except queue.Empty:
            return None
