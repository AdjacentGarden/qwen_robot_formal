#!/usr/bin/env python3
"""Startup-only check of the existing broker; never opens camera devices."""
import argparse
import json
import math
import os
import time
from pathlib import Path


def inspect_snapshot(status, manifest, pid, now):
    if status.get("pid") != pid or manifest.get("broker_pid") != pid:
        return {}, "broker_pid_mismatch"
    age = now - float(status.get("updated_at", 0))
    if not math.isfinite(age) or not 0 <= age <= 2:
        return {}, "stale_camera_status"
    sequences = {}
    for name in ("front", "back"):
        camera = status.get("cameras", {}).get(name, {})
        if name not in manifest.get("cameras", {}):
            return {}, f"{name}:missing_manifest"
        if camera.get("state") != "ready":
            return {}, f"{name}:{camera.get('error') or camera.get('state', 'missing')}"
        frame_age = float(camera.get("age_ms", float("inf")))
        if not math.isfinite(frame_age) or not 0 <= frame_age <= 1000:
            return {}, f"{name}:stale_frame"
        if int(camera.get("frames", 0)) < 5 or int(camera.get("sequence", 0)) < 5:
            return {}, f"{name}:insufficient_frames"
        sequences[name] = int(camera["sequence"])
    return sequences, ""


def wait_ready(state, pid, timeout, *, clock=time.monotonic, wall=time.time,
               sleep=time.sleep, alive=None, read=None):
    if alive is None:
        def alive():
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
    if read is None:
        def read():
            return (json.loads((state / "camera_status.json").read_text()),
                    json.loads((state / "cameras.json").read_text()))
    started = clock()
    baseline = None
    baseline_at = 0
    reason = "waiting_for_camera_status"
    while clock() - started < timeout:
        if not alive():
            return False, "camera_broker_exited"
        try:
            status, manifest = read()
            sequences, reason = inspect_snapshot(status, manifest, pid, wall())
        except (OSError, ValueError, TypeError, AttributeError):
            sequences, reason = {}, "camera_status_unavailable_or_invalid"
        if not sequences:
            baseline = None
        elif baseline is None:
            baseline, baseline_at = sequences, clock()
        elif clock() - baseline_at >= 0.4:
            if all(sequences[name] > baseline[name] for name in baseline):
                return True, "front_and_back_frames_advancing"
            reason = "camera_frames_not_advancing"
        sleep(0.1)
    return False, reason


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=12)
    args = parser.parse_args()
    started = time.monotonic()
    ok, detail = wait_ready(args.state, args.pid, args.timeout)
    print(json.dumps({"ok": ok, "check": "startup_front_back_camera",
                      "pid": args.pid, "detail": detail,
                      "elapsed_sec": round(time.monotonic() - started, 3)}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
