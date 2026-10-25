from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

from .config import load_config
from .frames import atomic_bytes
from .vision import load_png


def save_calibration(config, source, confirm_empty=False, replace=False):
    if not confirm_empty:
        raise ValueError("Inspect the image and supply --confirm-empty only when the slot is empty")
    image = load_png(source, config.expected_size)
    if config.baseline_path.resolve() == Path(source).resolve():
        raise ValueError("Reference destination must differ from input image")
    if config.baseline_path.exists() and not replace:
        raise ValueError("Reference already exists; --replace is required to change it")
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    atomic_bytes(config.baseline_path, encoded.getvalue())
    preview = config.baseline_path.with_name("roi-preview.png")
    cropped = BytesIO()
    image.crop(config.roi.box()).save(cropped, format="PNG")
    atomic_bytes(preview, cropped.getvalue())
    return preview


def main():
    parser = argparse.ArgumentParser(description="Save an explicitly confirmed empty-slot reference")
    parser.add_argument("--config", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--confirm-empty", action="store_true",
                        help="Confirm that the slot is empty in this image; do not calibrate on a stuck note")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace an existing calibration")
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        preview = save_calibration(config, args.image, args.confirm_empty, args.replace)
    except ValueError as error:
        parser.error(str(error))
    print("Saved empty reference:", config.baseline_path)
    print("Inspect ROI preview:", preview)
    print("Prototype occupancy only; calibration is not a banknote accuracy test.")


if __name__ == "__main__":
    main()
