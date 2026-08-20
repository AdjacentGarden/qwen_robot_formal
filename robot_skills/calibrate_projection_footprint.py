#!/usr/bin/env python3
"""Store the projector footprint inside the front-camera image.

The four points are measured from a photo taken at the 2 m focus distance.
They describe the corners of the *projected image*, not the wall or the whole
camera frame.  The runtime then judges only this quadrilateral.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "exploration.json"


def point(value: str) -> tuple[float, float]:
    try:
        x, y = value.split(",", 1)
        return float(x), float(y)
    except Exception as exc:
        raise argparse.ArgumentTypeError("point must be x,y") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate front-camera/projector footprint mapping")
    parser.add_argument("--image", required=True, help="camera photo showing the four projected corners")
    parser.add_argument(
        "--points", nargs=4, type=point, required=True,
        metavar=("TOP_LEFT", "TOP_RIGHT", "BOTTOM_RIGHT", "BOTTOM_LEFT"),
        help="four pixel coordinates written as x,y in clockwise order",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--preview", help="optional annotated preview output")
    parser.add_argument("--apply", action="store_true", help="persist calibration; without it this is a dry run")
    args = parser.parse_args()

    image_path = Path(args.image).expanduser().resolve()
    frame = cv2.imread(str(image_path))
    if frame is None:
        raise SystemExit(f"cannot read image: {image_path}")
    height, width = frame.shape[:2]
    polygon = np.asarray(args.points, dtype=np.float32)
    if np.any(polygon[:, 0] < 0) or np.any(polygon[:, 0] >= width):
        raise SystemExit("a point is outside the image width")
    if np.any(polygon[:, 1] < 0) or np.any(polygon[:, 1] >= height):
        raise SystemExit("a point is outside the image height")
    area = abs(float(cv2.contourArea(polygon)))
    if area < width * height * 0.04 or not cv2.isContourConvex(np.round(polygon).astype(np.int32)):
        raise SystemExit("points must form a non-self-intersecting convex projector quadrilateral")

    normalized = [[round(float(x) / width, 7), round(float(y) / height, 7)] for x, y in polygon]
    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    calibration = config["projection"].setdefault("camera_projector_calibration", {})
    calibration.update({
        "mode": "physical_four_corner_calibration",
        "calibrated": True,
        "reference_distance_m": float(config["projection"].get("fixed_focus_distance_m", 2.0)),
        "source_image": str(image_path),
        "image_size": [width, height],
        "normalized_footprint_polygon": normalized,
        "note": "Measured physical projector footprint in the front-camera image at the fixed-focus distance.",
    })

    preview_path = Path(args.preview).expanduser().resolve() if args.preview else image_path.with_name(image_path.stem + "_footprint_preview.jpg")
    preview = frame.copy()
    cv2.polylines(preview, [np.round(polygon).astype(np.int32)], True, (0, 255, 0), 3)
    for index, (x, y) in enumerate(polygon, 1):
        cv2.putText(preview, str(index), (int(x) + 6, int(y) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(preview_path), preview):
        raise SystemExit(f"cannot write preview: {preview_path}")

    if args.apply:
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, config_path)
    print(json.dumps({
        "ok": True,
        "applied": bool(args.apply),
        "config": str(config_path),
        "preview": str(preview_path),
        "image_size": [width, height],
        "polygon_area_ratio": round(area / (width * height), 6),
        "normalized_footprint_polygon": normalized,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
