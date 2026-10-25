from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def box(self):
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def validate(self, size):
        if any(type(v) is not int for v in (self.x, self.y, self.width, self.height)):
            raise ValueError("ROI coordinates must be integers")
        if min(self.x, self.y) < 0 or min(self.width, self.height) <= 0:
            raise ValueError("ROI coordinates/size are invalid")
        if self.x + self.width > size[0] or self.y + self.height > size[1]:
            raise ValueError("ROI extends outside expected screenshot")


@dataclass(frozen=True)
class Config:
    camera_id: str = "cabinet-1"
    data_dir: Path = Path("data")
    baseline_path: Path = Path("calibration/empty.png")
    expected_size: tuple = (720, 1280)
    video: Rect = Rect(0, 132, 720, 405)
    roi: Rect = Rect(340, 265, 100, 110)
    clock_roi: Rect = Rect(0, 132, 280, 21)
    capture_interval_seconds: float = 1.0
    stuck_consecutive_frames: int = 10
    clear_consecutive_frames: int = 2
    max_frame_gap_seconds: float = 1.8
    max_live_age_seconds: float = 5.0
    poll_interval_seconds: float = 0.25
    reminder_interval_seconds: float = 60.0
    notify_enabled: bool = False
    difference_threshold: float = 15.0
    changed_pixel_ratio: float = 0.12
    pixel_threshold: int = 25
    rolling_frames_minutes: float = 5.0
    non_alert_images_minutes: float = 5.0
    alert_evidence_days: float = 7.0
    event_history_days: float = 30.0
    cleanup_interval_seconds: float = 30.0
    max_image_storage_mb: float = 512.0
    adb_path: str = "adb"
    adb_serial: str = ""
    memu_executable: str = ""
    tapo_package: str = "com.tplink.iot"
    camera_name: str = "กล้องในตู้"
    adb_timeout_seconds: float = 15.0
    startup_timeout_seconds: float = 120.0
    ui_check_interval_seconds: float = 10.0
    alignment_enabled: bool = True
    alignment_min_inliers: int = 20
    alignment_min_inlier_ratio: float = 0.45
    alignment_max_shift_pixels: float = 160.0
    alignment_max_rotation_degrees: float = 15.0
    alignment_max_reprojection_error: float = 2.5
    alignment_max_anchor_error: float = 25.0
    alignment_roi_margin_pixels: int = 24
    alignment_reset_pixels: float = 4.0
    alignment_anchor_radius_pixels: float = 220.0
    alignment_spatial_tolerance_pixels: int = 2

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.camera_id):
            raise ValueError("camera_id must be a safe identifier")
        if len(self.expected_size) != 2 or any(type(v) is not int or v <= 0 for v in self.expected_size):
            raise ValueError("expected_size must contain two positive integers")
        for rect in (self.video, self.roi, self.clock_roi):
            rect.validate(self.expected_size)
        for region in (self.roi, self.clock_roi):
            if not (self.video.x <= region.x and self.video.y <= region.y
                    and region.x + region.width <= self.video.x + self.video.width
                    and region.y + region.height <= self.video.y + self.video.height):
                raise ValueError("Detection and clock ROI must be inside the video, not application UI")
        for name in ("stuck_consecutive_frames", "clear_consecutive_frames", "pixel_threshold",
                     "alignment_min_inliers", "alignment_roi_margin_pixels"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + " must be a positive integer")
        for name in ("capture_interval_seconds", "max_frame_gap_seconds", "max_live_age_seconds",
                     "poll_interval_seconds", "reminder_interval_seconds", "difference_threshold",
                     "rolling_frames_minutes", "non_alert_images_minutes", "alert_evidence_days",
                     "event_history_days", "cleanup_interval_seconds", "max_image_storage_mb",
                     "adb_timeout_seconds", "startup_timeout_seconds", "ui_check_interval_seconds",
                     "alignment_max_shift_pixels", "alignment_max_rotation_degrees",
                     "alignment_max_reprojection_error", "alignment_max_anchor_error", "alignment_reset_pixels",
                     "alignment_anchor_radius_pixels"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        if self.pixel_threshold > 255 or self.difference_threshold > 255:
            raise ValueError("Pixel/difference thresholds cannot exceed 255")
        if type(self.notify_enabled) is not bool or type(self.alignment_enabled) is not bool:
            raise ValueError("notify_enabled/alignment_enabled must be booleans")
        if (type(self.alignment_min_inlier_ratio) not in (int,float)
                or not 0 < self.alignment_min_inlier_ratio <= 1):
            raise ValueError("alignment_min_inlier_ratio must be in (0,1]")
        if self.alignment_min_inliers < 4:
            raise ValueError("alignment_min_inliers must be at least 4")
        if (type(self.alignment_spatial_tolerance_pixels) is not int
                or not 0 <= self.alignment_spatial_tolerance_pixels <= 5):
            raise ValueError("alignment_spatial_tolerance_pixels must be an integer in [0,5]")
        if type(self.changed_pixel_ratio) not in (int, float) or not 0 < self.changed_pixel_ratio <= 1:
            raise ValueError("changed_pixel_ratio must be in (0, 1]")
        if self.max_frame_gap_seconds < self.capture_interval_seconds:
            raise ValueError("max_frame_gap_seconds must allow the capture interval")
        return self


def load_config(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = set(Config.__dataclass_fields__)
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("Unknown configuration fields")
    for name in ("video", "roi", "clock_roi"):
        if name in raw:
            raw[name] = Rect(**raw[name])
    if "expected_size" in raw:
        raw["expected_size"] = tuple(raw["expected_size"])
    for name in ("data_dir", "baseline_path"):
        value = Path(raw.get(name, str(getattr(Config(), name))))
        raw[name] = (path.parent / value).resolve() if not value.is_absolute() else value.resolve()
    return Config(**raw).validate()
