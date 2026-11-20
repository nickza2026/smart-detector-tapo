"""Full-rate banknote passage classification around the ROI (RTSP decoder only).

A note that is pulled back disappears where it emerged; an accepted note
leaves the emergence point (or the zone) before it disappears. Anything that
does not start inside the ROI, lasts too long or changes most of the zone is
not classified, so it can never hide or create an event.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
import re
import uuid

import cv2
import numpy as np

SCALE = .5
DIFFERENCE = 30
MIN_AREA = 600
QUIET_FRAMES = 3
MAX_SECONDS = 4.
MAX_ZONE_FRACTION = .6
DISPLACEMENT_RATIO = .45
BACKGROUND_RATE = .05
RETAIN_SECONDS = 60.
RETAIN_COUNT = 16
VERDICTS = ('passed', 'retracted')


@dataclass(frozen=True)
class Passage:
    id: str
    verdict: str
    started: float
    ended: float

    def record(self):
        return asdict(self)


def parse_passages(value):
    """Strict decode of the signed proof field; anything malformed yields nothing."""
    if not isinstance(value, list) or len(value) > RETAIN_COUNT:
        return ()
    result = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'id', 'verdict', 'started', 'ended'}
                or not isinstance(item['id'], str) or re.fullmatch('[0-9a-f]{32}', item['id']) is None
                or item['verdict'] not in VERDICTS
                or any(type(item[k]) not in (int, float) or not math.isfinite(item[k]) or item[k] < 0
                       for k in ('started', 'ended'))
                or not 0 <= item['ended'] - item['started'] <= MAX_SECONDS):
            return ()
        result.append(Passage(**item))
    return tuple(result)


class PassageTracker:
    def __init__(self, roi, size):
        width, height = size
        x0, y0 = max(0, roi.x - roi.width), max(0, roi.y - roi.height)
        x1, y1 = min(width, roi.x + 2 * roi.width), min(height, roi.y + 2 * roi.height)
        if x1 - x0 < 16 or y1 - y0 < 16 or not (0 <= roi.x < width and 0 <= roi.y < height):
            raise ValueError('ROI is outside the decoded frame')
        self.zone = (x0, y0, x1, y1)
        self.slot = tuple(round(v * SCALE) for v in (roi.x - x0, roi.y - y0, roi.x + roi.width - x0, roi.y + roi.height - y0))
        self.threshold = DISPLACEMENT_RATIO * max(roi.width, roi.height) * SCALE
        self.background = None
        self.track = None
        self.quiet = 0
        self.recent = deque(maxlen=RETAIN_COUNT)

    def _gray(self, pixels):
        x0, y0, x1, y1 = self.zone
        crop = pixels[y0:y1, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        small = cv2.resize(gray, (max(1, round((x1 - x0) * SCALE)), max(1, round((y1 - y0) * SCALE))),
                           interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(small, (7, 7), 0).astype(np.float32)

    def _largest(self, mask):
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        best = None
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area >= MIN_AREA and (best is None or area > best[0]):
                left, top, w, h = (int(v) for v in stats[label, :4])
                best = (area, (left, top, left + w, top + h), tuple(float(v) for v in centroids[label]))
        return best, int(np.count_nonzero(mask))

    def update(self, pixels, wall):
        """Feed every decoded frame; returns the recent passages (newest last)."""
        gray = self._gray(pixels)
        if self.background is None or self.background.shape != gray.shape:
            self.background, self.track, self.quiet = gray, None, 0
            return self.passages(wall)
        mask = (cv2.absdiff(gray, self.background) > DIFFERENCE).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        blob, changed = self._largest(mask)
        if changed > MAX_ZONE_FRACTION * mask.size:
            # Exposure or view change: nothing here is a classifiable note.
            self.background, self.track, self.quiet = gray, None, 0
            return self.passages(wall)
        if self.track is None and blob is not None:
            sx0, sy0, sx1, sy1 = self.slot
            cx, cy = blob[2]
            h, w = mask.shape
            left, top, right, bottom = blob[1]
            inside = sx0 <= cx <= sx1 and sy0 <= cy <= sy1
            border = left <= 0 or top <= 0 or right >= w or bottom >= h
            self.track = dict(started=wall, origin=blob[2], last=blob[2], valid=inside and not border)
            self.quiet = 0
        elif self.track is not None:
            if blob is None:
                self.quiet += 1
                if self.quiet >= QUIET_FRAMES:
                    self._finish(wall)
            else:
                self.quiet = 0
                self.track['last'] = blob[2]
                self.track['ended'] = wall
                if wall - self.track['started'] > MAX_SECONDS:
                    self.track['valid'] = False
                if wall - self.track['started'] > 3 * MAX_SECONDS:
                    # A lingering object or lighting change: relearn, never classify.
                    self.background, self.track, self.quiet = gray, None, 0
                    return self.passages(wall)
        if self.track is None:
            # Only an idle zone adapts; a lingering object is never learned mid-track.
            cv2.accumulateWeighted(gray, self.background, BACKGROUND_RATE, mask=(1 - mask))
        return self.passages(wall)

    def _finish(self, wall):
        track, self.track, self.quiet = self.track, None, 0
        ended = track.get('ended', track['started'])
        if not track['valid'] or ended - track['started'] > MAX_SECONDS:
            return
        moved = math.dist(track['origin'], track['last'])
        verdict = 'passed' if moved >= self.threshold else 'retracted'
        self.recent.append(Passage(uuid.uuid4().hex, verdict, track['started'], ended))

    def passages(self, wall):
        while self.recent and wall - self.recent[0].ended > RETAIN_SECONDS:
            self.recent.popleft()
        return tuple(self.recent)
