#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import io
import json
import os
import signal
import subprocess
import threading
import time
from contextlib import redirect_stdout, suppress
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STOP_REQUESTED = False


def emit(ok: bool, action: str, status: str, **result) -> int:
    print(json.dumps({
        "ok": ok, "skill": "welcome_projection", "action": action,
        "status": status, "result": result, "error": None if ok else result.get("detail"),
        "metrics": {"ts": round(time.time(), 3)},
    }, ensure_ascii=False), flush=True)
    return 0 if ok else 2


def run(command: list[str], timeout: float = 15.0, required: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if required and completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or f"exit_{completed.returncode}").strip())
    return completed


def projector_light(config: dict, enabled: bool) -> None:
    path = Path(config["dian_module_path"])
    spec = importlib.util.spec_from_file_location("welcome_projection_dian", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("dian_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.StringIO()
    with redirect_stdout(output):
        result = (module.light_on if enabled else module.light_off)()
    if result not in (None, 0):
        raise RuntimeError(f"projector_light_{'on' if enabled else 'off'}_failed:{result}")


def ensure_asset(config: dict) -> None:
    host_path = Path(config["host_video_path"])
    if not host_path.is_file() or host_path.stat().st_size < 1024:
        raise RuntimeError(f"welcome_video_missing:{host_path}")
    run(["sudo", "-n", str(config["helper_path"]), "prepare"], timeout=20.0)


def stop_content(config: dict) -> None:
    run(["sudo", "-n", str(config["helper_path"]), "stop"], required=False)


def play(config: dict, duration: float) -> dict:
    global STOP_REQUESTED
    ensure_asset(config)
    projector_light(config, True)
    total_started = time.monotonic()
    content_started = None
    content_ended = None
    try:
        run(["sudo", "-n", str(config["helper_path"]), "start"], timeout=20.0)
        # The player/container startup can take more than a second.  Start the
        # requested display interval only after the helper has returned, so a
        # three-second scene always remains visible for three full seconds.
        content_started = time.monotonic()
        deadline = content_started + duration
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        content_ended = time.monotonic()
    finally:
        stop_content(config)
        with suppress(Exception):
            projector_light(config, False)
    played_seconds = 0.0 if content_started is None else (content_ended or time.monotonic()) - content_started
    return {
        "played_seconds": round(played_seconds, 3),
        "total_elapsed_seconds": round(time.monotonic() - total_started, 3),
        "stopped": STOP_REQUESTED,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Three-second welcome-home projection")
    parser.add_argument("action", choices=["prepare", "play", "stop", "status"])
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout")
    args = parser.parse_args(argv)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.dry_run:
        return emit(
            True,
            args.action,
            "dry_run",
            duration=min(10.0, max(0.2, args.duration)),
            media_kind="welcome_home_video",
            host_media_path=config["host_video_path"],
            container_media_path=config["container_video_path"],
            expected_sha256=config["expected_sha256"],
        )
    lock_path = Path(config["lock_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if args.action == "prepare":
                ensure_asset(config)
                return emit(True, args.action, "ready")
            if args.action == "stop":
                stop_content(config)
                with suppress(Exception):
                    projector_light(config, False)
                return emit(True, args.action, "stopped")
            if args.action == "status":
                result = run(["sudo", "-n", str(config["helper_path"]), "status"], required=False)
                return emit(True, args.action, "playing" if result.returncode == 0 else "idle")
            duration = min(10.0, max(0.2, float(args.duration)))
            return emit(True, args.action, "completed", **play(config, duration))
        except Exception as exc:
            return emit(False, args.action, "failed", detail=f"{type(exc).__name__}: {exc}")


def request_stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


if __name__ == "__main__":
    raise SystemExit(main())
