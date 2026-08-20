#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_ENGINE_CONFIG = ROOT / "config.json"
DEFAULT_PROJECT_CONFIG = Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/config/hardware.json")
DEFAULT_NAME = "zhangsan"
SKILL_NAME = os.getenv("FITNESS_SKILL_NAME", "push_up").strip().lower()
if SKILL_NAME not in {"push_up", "squat", "pull_up"}:
    raise RuntimeError(f"unsupported FITNESS_SKILL_NAME: {SKILL_NAME}")


def _project_config() -> dict[str, Any]:
    path = Path(os.getenv("ROBOT_PROJECT_CONFIG", str(DEFAULT_PROJECT_CONFIG)))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _runtime_paths(project: dict[str, Any]) -> tuple[Path, Path]:
    project_runtime = Path((project.get("paths") or {}).get("runtime_dir") or "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime")
    runtime = project_runtime / "fitness" / SKILL_NAME
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime / f"{SKILL_NAME}.pid", runtime / f"{SKILL_NAME}_state.json"


def _effective_config(path: Path, project: dict[str, Any]) -> dict[str, Any]:
    from pipeline import load_config

    config = load_config(path)
    back = (project.get("cameras") or {}).get("back") or {}
    identity = (project.get("execution") or {}).get("fitness_identity") or {}
    project_runtime = Path((project.get("paths") or {}).get("runtime_dir") or "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime")
    config["camera"].update(
        {
            "source": str(back.get("device") or config["camera"]["source"]),
            "width": int(back.get("width") or config["camera"]["width"]),
            "height": int(back.get("height") or config["camera"]["height"]),
            "fps": int(back.get("fps") or config["camera"]["fps"]),
        }
    )
    if identity.get("face_database"):
        config["face"]["database"] = str(identity["face_database"])
    config["paths"]["runtime_dir"] = str(project_runtime / "fitness" / SKILL_NAME)
    return config


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
        os.kill(value, 0)
        return value
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None


def _result(action: str, state: dict[str, Any], ok: bool = True, error: str | None = None) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "skill": SKILL_NAME,
        "action": action,
        "status": state.get("state") or ("ok" if ok else "error"),
        "result": state,
        "count": int(state.get("count") or state.get("current_count") or 0),
        "current_count": int(state.get("current_count") or state.get("count") or 0),
        "session_count": int(state.get("session_count") or 0),
        "elapsed_seconds": float(state.get("elapsed_seconds") or 0.0),
        "error": error,
    }


def _query(state_file: Path) -> int:
    state = _read_state(state_file)
    if not state:
        state = {"state": "idle", "running": False, "count": 0, "current_count": 0, "session_count": 0}
    print(json.dumps(_result("query", state), ensure_ascii=False), flush=True)
    return 0


def _stop(pid_file: Path, state_file: Path) -> int:
    pid = _read_pid(pid_file)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = None
    deadline = time.monotonic() + 4.0
    while pid is not None and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    state = _read_state(state_file)
    if not state:
        state = {"state": "idle", "running": False, "count": 0, "current_count": 0, "session_count": 0}
    print(json.dumps(_result("stop", state), ensure_ascii=False), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"registered-face anchored continuous-ReID {SKILL_NAME} skill")
    parser.add_argument("action", nargs="?", default="run", choices=["run", "start", "query", "stop", "check"])
    parser.add_argument("--camera", "--source", dest="camera")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--name", default=os.getenv("FITNESS_IDENTITY", os.getenv("PUSHUP_IDENTITY", DEFAULT_NAME)))
    parser.add_argument(
        "--identity-policy",
        choices=["face_and_reid", "anonymous"],
        default="face_and_reid",
    )
    parser.add_argument("--output")
    parser.add_argument("--start-gate")
    parser.add_argument("--initial-count", type=int, default=0)
    parser.add_argument("--resume-from-interrupt", action="store_true")
    parser.add_argument("--initial-elapsed-seconds", type=float, default=0.0)
    parser.add_argument("--frame-step", type=int, default=1)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--config", default=str(DEFAULT_ENGINE_CONFIG))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project = _project_config()
    pid_file, state_file = _runtime_paths(project)
    if args.dry_run:
        back = (project.get("cameras") or {}).get("back") or {}
        payload = {
            "ok": True,
            "status": "dry_run",
            "skill": SKILL_NAME,
            "action": args.action,
            "result": {
                "camera": args.camera or back.get("device") or "/dev/video31",
                "duration": args.duration,
                "name": args.name,
                "identity_policy": args.identity_policy,
            },
            "error": None,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0
    if args.action == "query":
        return _query(state_file)
    if args.action == "stop":
        return _stop(pid_file, state_file)

    config = _effective_config(Path(args.config), project)
    if args.action == "check":
        from engine import command_check

        return command_check(config)
    running_pid = _read_pid(pid_file)
    if running_pid is not None and running_pid != os.getpid():
        state = _read_state(state_file)
        print(json.dumps(_result(args.action, state, False, f"{SKILL_NAME}_already_running:{running_pid}"), ensure_ascii=False), flush=True)
        return 2

    args.source = str(args.camera or config["camera"]["source"])
    args.exercise = SKILL_NAME
    args.state_file = str(state_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        from engine import command_count

        return command_count(args, config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        payload = _result(args.action, _read_state(state_file), False, f"{type(exc).__name__}: {exc}")
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 1
    finally:
        try:
            if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
