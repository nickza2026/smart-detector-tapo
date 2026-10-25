from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Frame:
    frame_id: str
    camera_id: str
    session_id: str
    sequence: int
    captured_at: float
    captured_monotonic: float
    width: int
    height: int
    image_name: str
    fresh: bool
    health: str = "healthy"


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(4):
            try:
                os.replace(str(temp), str(path))
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temp.exists():
            temp.unlink()


def publish_frame(config, png, session_id, sequence, fresh, health="healthy",
                  captured_at=None, captured_monotonic=None):
    """Publish the immutable image before its ready marker; never overwrite a frame."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise ValueError("Invalid capture session ID")
    frame_id = session_id + "-" + str(sequence).zfill(12) + "-" + uuid.uuid4().hex
    folder = config.data_dir / "frames" / config.camera_id
    if not folder.resolve().is_relative_to(config.data_dir.resolve()) or folder.is_symlink():
        raise ValueError("Frame folder outside managed data directory")
    image = folder / (frame_id + ".png")
    frame = Frame(frame_id, config.camera_id, session_id, sequence,
                  time.time() if captured_at is None else captured_at,
                  time.monotonic() if captured_monotonic is None else captured_monotonic,
                  config.expected_size[0], config.expected_size[1], image.name, bool(fresh), health)
    atomic_bytes(image, png)
    marker = folder / (frame_id + ".ready.json")
    atomic_bytes(marker, json.dumps(asdict(frame), allow_nan=False).encode("utf-8"))
    return marker


def read_frame(marker, config):
    marker = Path(marker)
    expected = (config.data_dir / "frames" / config.camera_id).resolve()
    if not expected.is_relative_to(config.data_dir.resolve()):
        raise ValueError("Frame folder outside managed data directory")
    if marker.is_symlink() or marker.resolve().parent != expected:
        raise ValueError("Frame marker outside managed spool")
    frame = Frame(**json.loads(marker.read_text(encoding="utf-8")))
    if frame.camera_id != config.camera_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", frame.frame_id):
        raise ValueError("Invalid frame identity")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", frame.session_id):
        raise ValueError("Invalid frame session")
    if type(frame.sequence) is not int or frame.sequence < 1 or type(frame.fresh) is not bool:
        raise ValueError("Invalid sequence/freshness")
    if frame.image_name != frame.frame_id + ".png" or marker.name != frame.frame_id + ".ready.json":
        raise ValueError("Mismatched frame names")
    image = marker.parent / frame.image_name
    if image.is_symlink() or image.resolve().parent != expected:
        raise ValueError("Frame image outside managed spool")
    import math
    for timestamp in (frame.captured_at, frame.captured_monotonic):
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("Invalid capture timestamp")
    if (frame.width, frame.height) != config.expected_size:
        raise ValueError("Screenshot dimensions changed")
    return frame, image


def image_storage_bytes(config):
    total = 0
    for subfolder in ("frames", "events"):
        root = config.data_dir / subfolder
        if root.is_symlink():
            raise ValueError("Managed image directory must not be a symlink")
        for path in root.rglob("*") if root.exists() else []:
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".png", ".jpg", ".tmp"):
                total += path.stat().st_size
    return total
