#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


DEFAULTS = {
    "front": {"device": "/dev/video22", "width": 640, "height": 480, "fps": 15.0, "warmup_frames": 5, "jpeg_quality": 90, "fourcc": "mp4v"},
    "back": {"device": "/dev/video31", "width": 640, "height": 480, "fps": 15.0, "warmup_frames": 5, "jpeg_quality": 90, "fourcc": "mp4v"},
}


def parser(skill_name: str, fixed_camera: str | None, fixed_action: str) -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=f"Standalone V11 camera skill: {skill_name}")
    value.add_argument("action", nargs="?", choices=["capture", "record", "check"], default=fixed_action)
    if fixed_camera is None:
        value.add_argument("--camera", "--camera-name", dest="camera", choices=["front", "back"], default="front")
    value.add_argument("--device")
    value.add_argument("--output", "--output-path", dest="output")
    value.add_argument("--duration", "--seconds", dest="duration", type=float, default=3.0)
    value.add_argument("--width", type=int)
    value.add_argument("--height", type=int)
    value.add_argument("--fps", type=float)
    value.add_argument("--warmup-frames", type=int)
    value.add_argument("--json-params")
    value.add_argument("--dry-run", action="store_true")
    value.add_argument("--json", action="store_true")
    return value


def main(skill_name: str, fixed_camera: str | None, fixed_action: str) -> int:
    args = parser(skill_name, fixed_camera, fixed_action).parse_args()
    payload = {}
    if args.json_params:
        payload = json.loads(args.json_params)
        if not isinstance(payload, dict):
            raise ValueError("--json-params must decode to an object")
    camera = fixed_camera or str(payload.get("camera_name") or payload.get("camera") or args.camera or "front")
    if camera not in DEFAULTS:
        raise ValueError(f"unsupported camera: {camera}")
    action = str(payload.get("action") or args.action or fixed_action)
    if fixed_action in {"capture", "record"}:
        action = fixed_action
    cfg = DEFAULTS[camera]
    root = Path(__file__).resolve().parents[1]
    extension = ".jpg" if action == "capture" else ".mp4"
    output = Path(payload.get("output_path") or args.output or root / "runtime/media" / f"{camera}_camera{extension}")
    device = str(payload.get("device") or args.device or cfg["device"])
    width = int(payload.get("width") or args.width or cfg["width"])
    height = int(payload.get("height") or args.height or cfg["height"])
    fps = float(payload.get("fps") or args.fps or cfg["fps"])
    warmup_value = payload.get("warmup_frames")
    if warmup_value is None:
        warmup_value = args.warmup_frames if args.warmup_frames is not None else cfg["warmup_frames"]
    warmup = int(warmup_value)
    duration = float(payload.get("seconds") or payload.get("duration") or args.duration)
    result = {"camera": camera, "device": device, "output": str(output), "width": width, "height": height, "fps": fps}
    if args.dry_run:
        print(json.dumps({"ok": True, "skill": skill_name, "action": action, "status": "dry_run", "result": result, "error": None}, ensure_ascii=False))
        return 0
    import cv2
    cap = cv2.VideoCapture(device)
    writer = None
    if not cap.isOpened():
        print(json.dumps({"ok": False, "skill": skill_name, "action": action, "status": "error", "result": result, "error": f"camera_open_failed:{device}"}, ensure_ascii=False))
        return 2
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        for _ in range(max(0, warmup)):
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"camera_read_failed:{device}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if action == "check":
            result["frame_shape"] = list(frame.shape)
        elif action == "capture":
            if not cv2.imwrite(str(output), frame, [cv2.IMWRITE_JPEG_QUALITY, int(cfg["jpeg_quality"])]):
                raise RuntimeError(f"image_write_failed:{output}")
        else:
            h, w = frame.shape[:2]
            writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*cfg["fourcc"]), fps, (w, h))
            if not writer.isOpened():
                raise RuntimeError(f"video_writer_open_failed:{output}")
            writer.write(frame)
            deadline = time.monotonic() + max(0.1, duration)
            while time.monotonic() < deadline:
                ok, frame = cap.read()
                if ok and frame is not None:
                    writer.write(frame)
                time.sleep(max(0.0, 1.0 / max(1.0, fps) / 2.0))
        print(json.dumps({"ok": True, "skill": skill_name, "action": action, "status": "completed", "result": result, "error": None}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "skill": skill_name, "action": action, "status": "error", "result": result, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if writer is not None:
            writer.release()
        cap.release()
