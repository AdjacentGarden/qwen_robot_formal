#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNTIME = Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime/person_tracking")
PID_FILE = RUNTIME / "person_tracking.pid"
STATE_FILE = RUNTIME / "person_tracking_state.json"
ENGINE = ROOT / "face_reid_person_tracking.py"
CONFIG = ROOT / "config.json"


def _pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _write_state(**payload) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"skill": "person_tracking", "updated_at": time.time(), **payload}, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE_FILE)


def _stop() -> int:
    pid = _pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
    _write_state(state="stopped", running=False, pid=None)
    print(json.dumps({"ok": True, "skill": "person_tracking", "action": "stop", "status": "stopped"}, ensure_ascii=False), flush=True)
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Registered-face and continuous-ReID person tracking")
    value.add_argument("action", nargs="?", default="track", choices=["find", "track", "run", "stop", "check"])
    value.add_argument("--name", "--target", dest="name", default="zhangsan")
    value.add_argument("--camera", "--source", dest="source")
    value.add_argument("--duration", "--seconds", dest="seconds", type=float, default=30.0)
    value.add_argument("--output")
    value.add_argument("--execute", action="store_true")
    value.add_argument("--base-speed", type=float)
    value.add_argument("--max-linear", type=float)
    value.add_argument("--max-angular", type=float)
    value.add_argument("--steering-gain", type=float)
    value.add_argument("--speed-ema-alpha", type=float)
    value.add_argument("--max-speed-step", type=float)
    value.add_argument("--search-spin-speed", type=float)
    value.add_argument("--raise-head", action="store_true")
    value.add_argument("--dry-run", action="store_true")
    value.add_argument("--json", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    if str(args.name).strip().lower() in {"张三", "zhangsan"}:
        args.name = "zhangsan"
    if args.action == "stop":
        return _stop()
    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "skill": "person_tracking",
            "action": args.action,
            "status": "dry_run",
            "result": {
                "identity": args.name,
                "source": args.source or "/dev/video22",
                "duration": args.seconds,
                "movement_enabled": bool(args.execute),
                "identity_pipeline": "registered_face+continuous_reid",
            },
        }, ensure_ascii=False), flush=True)
        return 0
    if _pid() not in {None, os.getpid()}:
        print(json.dumps({"ok": False, "skill": "person_tracking", "error": "already_running"}), flush=True)
        return 2
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    _write_state(state="starting", running=True, pid=os.getpid(), identity=args.name)
    effective_config = CONFIG
    tuning_values = {
        "maximum_linear_speed": args.max_linear,
        "maximum_angular_speed": args.max_angular,
        "angular_gain": args.steering_gain,
        "forward_speed": args.base_speed,
    }
    if any(value is not None for value in tuning_values.values()):
        payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        tracking = payload.setdefault("tracking", {})
        for key, value in tuning_values.items():
            if value is not None:
                tracking[key] = float(value)
        if args.max_linear is not None and args.base_speed is not None:
            tracking["forward_speed"] = min(float(args.max_linear), float(args.base_speed))
        effective_config = RUNTIME / "effective_config.json"
        effective_config.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [sys.executable, str(ENGINE), "--config", str(effective_config), "--name", args.name, "--seconds", str(max(0.1, args.seconds))]
    if args.source:
        command += ["--source", args.source]
    if args.output:
        command += ["--output", args.output]
    if args.execute:
        command.append("--execute")
    if args.raise_head:
        command.append("--raise-head")
    if args.action == "check":
        command.append("--check")
    os.execv(sys.executable, command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
