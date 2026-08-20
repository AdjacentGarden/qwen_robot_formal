#!/usr/bin/env python3
"""Zero-input model initialization probe used only by the A/B benchmark."""

from __future__ import annotations

import argparse
import json
import time

import preload_runtime as preload


def timed(name, function):
    started = time.perf_counter()
    result = function()
    return {"name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3), "result": result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=("all", "face", "pose"))
    args = parser.parse_args()
    started = time.perf_counter()
    rows = []
    held = []

    rows.append(timed("dependencies", preload.import_dependencies))
    if args.target in {"all", "face"}:
        paths = preload.model_paths()
        held, metadata = preload.read_model_files(paths)
        rows.append({"name": "model_file_pages", "elapsed_ms": 0.0, "result": metadata})
        rows.append(timed("retinaface", preload.warm_face_detector))
        rows.append(timed("facenet", preload.warm_face_embedding))
    if args.target == "all":
        module, config = preload.load_fitness_config()
        rows.append(timed("yolo", lambda: preload.warm_yolo(module, config)))
        rows.append(timed("reid", lambda: preload.warm_reid(module, config)))
    if args.target in {"all", "pose"}:
        rows.append(timed("mediapipe_pose", preload.warm_pose))

    print(json.dumps({
        "ok": True,
        "target": args.target,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "held_model_mb": round(sum(len(v) for v in held) / 1048576.0, 2),
        "stages": rows,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
