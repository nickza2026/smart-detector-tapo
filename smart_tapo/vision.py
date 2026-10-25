from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from io import BytesIO

from PIL import Image, ImageChops, ImageStat


@dataclass(frozen=True)
class Observation:
    label: str
    score: float = 0.0
    ratio: float = 0.0
    view_corners: tuple = ()
    alignment_reason: str = ''
    alignment_inliers: int = 0
    alignment_confidence: float = 0.0
    alignment_anchor_error: float = 0.0
    roi_png: bytes = b''


def load_png(source, expected_size):
    """Accept only a bounded PNG screenshot, not arbitrary image formats."""
    with Image.open(source) as image:
        if image.format != "PNG" or image.size != expected_size:
            raise ValueError("Expected a PNG screenshot of size " + str(expected_size))
        image.load()
        return image.convert("RGB")


class Detector:
    """Prototype ROI-occupancy rule, NOT a trained banknote classifier.

    Lighting, hands and foreign objects can trigger PRESENT. Shadow mode and
    human review are required; no successful money acceptance is inferred.
    The explicitly calibrated empty reference is never learned/updated here.
    """

    def __init__(self, config):
        self.config = config
        if not Path(config.baseline_path).is_file():
            raise ValueError("Missing empty reference; run calibration with --confirm-empty")
        reference = load_png(config.baseline_path, config.expected_size)
        self.reference = reference.crop(config.roi.box()).convert("L")
        self.aligner = None
        if config.alignment_enabled:
            from .alignment import Aligner
            self.aligner = Aligner(config, reference)

    def observe(self, path):
        try:
            current = load_png(path, self.config.expected_size)
            return self.observe_image(current)
        except (OSError, ValueError, Image.DecompressionBombError):
            return Observation("UNKNOWN")

    def observe_image(self, current):
        if current.size != self.config.expected_size:
            return Observation("UNKNOWN")
        alignment = self.aligner.align(current) if self.aligner is not None else None
        if alignment is not None:
            if not alignment.ok:
                return Observation('UNKNOWN', alignment_reason=alignment.reason,
                                   alignment_inliers=alignment.inliers,
                                   alignment_confidence=alignment.confidence,
                                   alignment_anchor_error=alignment.anchor_error)
            aligned = Image.fromarray(alignment.image)
            x = self.config.roi.x - self.config.video.x
            y = self.config.roi.y - self.config.video.y
            crop = aligned.crop((x,y,x+self.config.roi.width,y+self.config.roi.height))
        else:
            crop = current.crop(self.config.roi.box())
        roi = crop.convert("L")
        if alignment is not None:
            from .alignment import spatial_difference
            difference = Image.fromarray(spatial_difference(self.reference,roi,
                                         self.config.alignment_spatial_tolerance_pixels))
        else:
            difference = ImageChops.difference(roi, self.reference)
        score = float(ImageStat.Stat(difference).mean[0])
        histogram = difference.histogram()
        changed = sum(histogram[self.config.pixel_threshold:])
        ratio = changed / (roi.width * roi.height)
        label = "PRESENT" if (score >= self.config.difference_threshold and
                              ratio >= self.config.changed_pixel_ratio) else "EMPTY"
        if alignment is not None and label == 'PRESENT' and score <= alignment.anchor_error * 2:
            label = 'UNKNOWN'  # Residual camera/background error can explain this change.
        encoded = BytesIO()
        crop.save(encoded, format='PNG')
        return Observation(label, score, ratio,
                           alignment.corners if alignment else (),
                           'ambiguous_background_residual' if label == 'UNKNOWN' else '',
                           alignment.inliers if alignment else 0,
                           alignment.confidence if alignment else 0.0,
                           alignment.anchor_error if alignment else 0.0,
                           encoded.getvalue())
