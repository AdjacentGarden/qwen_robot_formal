#!/usr/bin/env python3
import argparse
import fcntl
import importlib.util
import io
import json
import os
import signal
import subprocess
import time
from contextlib import redirect_stdout, suppress
from pathlib import Path

SKILL = "projector_control"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("PROJECTOR_CONTROL_CONFIG", ROOT / "config.json"))
DEFAULT_HEAD_LEVEL_COMMAND = [
    "bash",
    "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/head_control/run.sh",
    "level",
    "--wait",
    "0.45",
    "--discovery-timeout",
    "3.0",
    "--json",
]


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def emit(ok, status, action, result=None, error=None, metrics=None, message=None):
    payload = {
        "ok": bool(ok), "status": status, "skill": SKILL, "action": action,
        "result": result or {}, "error": error,
        "metrics": metrics or {"ts": round(time.time(), 3)},
    }
    if message:
        payload["message"] = message
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def load_dian(path):
    spec = importlib.util.spec_from_file_location("robot_projector_dian", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dian module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call_light(config, on):
    module = load_dian(config["dian_module_path"])
    fn = module.light_on if on else module.light_off
    captured = io.StringIO()
    with redirect_stdout(captured):
        result = fn()
    if result not in (0, None):
        raise RuntimeError(f"dian.{'light_on' if on else 'light_off'} failed: {result}")
    return {
        "command": "light_on" if on else "light_off",
        "return_code": result or 0,
        "diagnostic": captured.getvalue().strip(),
    }


def internal_checkerboard(config):
    from smbus2 import SMBus
    bus = SMBus(int(config.get("i2c_bus", 2)))
    try:
        dlp_addr = int(str(config.get("dlp_addr", "0x1b")), 0)
        bus.write_byte_data(dlp_addr, 0x62, 0x00)
        time.sleep(0.01)
        value = bus.read_byte_data(dlp_addr, 0x63)
        return {"register_0x63": value, "register_hex": hex(value)}
    finally:
        with suppress(Exception):
            bus.close()


def write_state(config, action, mode, **extra):
    path = Path(config["state_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    previous = read_state(config)
    payload = {**previous, "action": action, "mode": mode, "updated_at": time.time(), **extra}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_state(config):
    path = Path(config["state_path"])
    if not path.exists():
        return {"action": "unknown", "mode": "unknown"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"action": "unknown", "mode": "unknown"}


def start_fitness_video(config, timeout):
    light = call_light(config, True)
    command = list(config["fitness_video_start_command"])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or f"exit {completed.returncode}").strip())
    except Exception:
        with suppress(Exception):
            call_light(config, False)
        write_state(config, "off", "off", session_active=False, scrolling=False)
        raise
    write_state(config, "fitness_video_on", "external_fitness_video", session_active=False, scrolling=False)
    return {"light": light, "video_started": True, "stdout": completed.stdout.strip()}


def run_configured_command(config, key, timeout, required=True):
    command = list(config.get(key) or [])
    if not command:
        if required:
            raise RuntimeError(f"missing configured command: {key}")
        return {"command": [], "returncode": 0, "stdout": "", "skipped": True}
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0 and required:
        raise RuntimeError((completed.stderr or completed.stdout or f"exit {completed.returncode}").strip())
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def restore_head_level(config, timeout):
    """Return the head to its neutral angle after every projector shutdown.

    Projector shutdown is the ownership boundary shared by fitness, meeting,
    and standalone voice projection.  Keeping the guarantee here also covers
    direct calls that bypass the dialogue planner.
    """
    command = list(config.get("head_level_command") or DEFAULT_HEAD_LEVEL_COMMAND)
    env = os.environ.copy()
    env.setdefault("CAR_REAL_WS", "/home/test/Car_real_copy")
    attempts = []
    for attempt in range(1, 3):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(3.0, min(float(timeout), 12.0)),
                check=False,
                env=env,
            )
            record = {
                "attempt": attempt,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip()[-2000:],
                "stderr": completed.stderr.strip()[-2000:],
            }
            attempts.append(record)
            if completed.returncode == 0:
                return {"ok": True, "command": command, "attempts": attempts}
        except Exception as exc:
            attempts.append({"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(0.15)
    raise RuntimeError(f"head_level_restore_failed: {attempts}")


def turn_projector_off(config, timeout):
    stop_error = None
    head_error = None
    content = None
    head = None
    try:
        content = run_configured_command(config, "meeting_presentation_stop_command", timeout)
    except Exception as exc:
        stop_error = exc
    light = call_light(config, False)
    try:
        head = restore_head_level(config, timeout)
    except Exception as exc:
        head_error = exc
    write_state(
        config,
        "off",
        "off",
        session_active=False,
        scrolling=False,
        slides=[],
        slide_index=None,
        head_level_ok=head_error is None,
    )
    errors = []
    if stop_error is not None:
        errors.append(f"meeting loop stop failed: {stop_error}")
    if head_error is not None:
        errors.append(str(head_error))
    if errors:
        raise RuntimeError("projector is off but cleanup was incomplete: " + "; ".join(errors))
    return {"light": light, "content": content, "head": head}


def start_meeting_presentation(config, timeout):
    light = call_light(config, True)
    try:
        run_configured_command(config, "meeting_presentation_stop_command", timeout, required=False)
        content = run_configured_command(config, "meeting_presentation_start_command", timeout)
    except BaseException:
        with suppress(Exception):
            run_configured_command(config, "meeting_presentation_stop_command", timeout, required=False)
        with suppress(Exception):
            call_light(config, False)
        write_state(config, "off", "off", session_active=False, scrolling=False)
        raise
    write_state(
        config,
        "meeting_presentation_on",
        "meeting_loop",
        slides=[],
        slide_index=None,
        scrolling=True,
        session_active=True,
    )
    return {"light": light, "content": content, "looping": True}


def meeting_scroll(config, action, timeout):
    state = read_state(config)
    if not state.get("session_active"):
        raise RuntimeError("meeting_projection_session_not_active")
    if action == "pause":
        if state.get("scrolling") is False:
            return {"scrolling": False, "already_paused": True}
        content = run_configured_command(config, "meeting_presentation_stop_command", timeout)
        write_state(config, "meeting_presentation_on", "meeting_loop", scrolling=False, session_active=True)
        return {"scrolling": False, "content": content}
    if state.get("scrolling") is True:
        return {"scrolling": True, "already_running": True}
    content = run_configured_command(config, "meeting_presentation_start_command", timeout)
    write_state(config, "meeting_presentation_on", "meeting_loop", scrolling=True, session_active=True)
    return {"scrolling": True, "content": content}


def hold_meeting_presentation(config, timeout, poll_seconds):
    stopped = False

    def request_stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(json.dumps({
        "event": "skill_progress",
        "skill_name": SKILL,
        "kind": "start",
        "state": "meeting_projection_active",
        "text": "会议内容已经投影好了",
        "state_effect": {"action": "meeting_presentation_on", "mode": "external_meeting_loop"},
    }, ensure_ascii=False), flush=True)
    try:
        while not stopped:
            time.sleep(max(0.05, poll_seconds))
    finally:
        return turn_projector_off(config, timeout)


def main(argv=None):
    config = load_config()
    choices = ["fitness_video_on", "meeting_presentation_on", "meeting_pause", "meeting_resume", "internal_on", "on", "off", "status"]
    parser = argparse.ArgumentParser(description="Projector control skill.")
    parser.add_argument("action", nargs="?", default="status", choices=choices)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=float(config.get("timeout_sec", 20)))
    parser.add_argument("--hold", action="store_true", help="Keep a meeting projection session active until interrupted.")
    parser.add_argument("--poll-seconds", type=float, default=0.2)
    args = parser.parse_args(argv)
    if args.dry_run:
        command_key = {
            "fitness_video_on": "fitness_video_start_command",
            "meeting_presentation_on": "meeting_presentation_start_command",
        }.get(args.action)
        media = {}
        if args.action == "fitness_video_on":
            media = {
                "media_kind": "fitness_video",
                "media_paths": [config["fitness_video_container_path"]],
                "media_sha256": [config["fitness_video_sha256"]],
            }
        elif args.action == "meeting_presentation_on":
            media = {
                "media_kind": "meeting_slide_loop",
                "media_paths": list(config["meeting_slide_container_paths"]),
                "media_sha256": list(config["meeting_slide_sha256"]),
            }
        emit(
            True,
            "dry_run",
            args.action,
            {
                "command": config.get(command_key) if command_key else None,
                "hold": bool(args.hold),
                **media,
            },
        )
        return
    start = time.time()
    lock_path = Path(config["lock_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if args.action == "fitness_video_on":
                result = start_fitness_video(config, args.timeout)
                message = "运动视频已经投影好了"
            elif args.action == "meeting_presentation_on":
                result = start_meeting_presentation(config, args.timeout)
                message = "会议内容已经投影好了"
            elif args.action == "meeting_pause":
                result = meeting_scroll(config, "pause", args.timeout)
                message = "会议内容已暂停滚动"
            elif args.action == "meeting_resume":
                result = meeting_scroll(config, "resume", args.timeout)
                message = "会议内容继续滚动"
            elif args.action == "off":
                result = turn_projector_off(config, args.timeout)
                message = "投影已经关闭"
            elif args.action == "on":
                result = call_light(config, True)
                write_state(config, "on", "light_only", session_active=False, scrolling=False)
                message = "投影已经打开"
            elif args.action == "internal_on":
                result = {"internal": internal_checkerboard(config), "light": call_light(config, True)}
                write_state(config, "internal_on", "internal_pattern", session_active=False, scrolling=False)
                message = "投影已经打开"
            else:
                loop_status = run_configured_command(config, "meeting_presentation_status_command", args.timeout, required=False)
                result = {
                    "cached_state": read_state(config),
                    "meeting_loop": {
                        "running": loop_status.get("returncode") == 0 and loop_status.get("stdout") == "running",
                        "stdout": loop_status.get("stdout"),
                        "returncode": loop_status.get("returncode"),
                    },
                    "internal": internal_checkerboard(config),
                }
                message = "投影状态已查询"
        emit(True, "done", args.action, result, metrics={"elapsed_sec": round(time.time() - start, 3)}, message=message)
    except Exception as exc:
        emit(False, "error", args.action, error=repr(exc), metrics={"elapsed_sec": round(time.time() - start, 3)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
