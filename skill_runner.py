#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


RESULT_PREFIX = "QWEN_SKILL_RUNNER_RESULT="
CONTROLLER_PATTERNS = (
    "composable_agent.py",
    "voice_skill_agent.py",
    "new_project.cli daemon",
    "wake_skill_agent.py",
)
READ_ONLY_SKILLS = {"navigation_list", "realtime_information", "reminder_query"}
READ_ONLY_ACTIONS = {"status", "check", "query", "list", "current_time", "weather", "location", "nearby", "traffic"}
EXECUTOR_BUILTIN_SKILLS = {
    "front_camera_capture",
    "back_camera_capture",
    "camera_capture",
    "front_camera_record",
    "back_camera_record",
    "camera_record",
}
MAX_STRUCTURED_RESULT_CHARS = 12000
PREFERRED_RESULT_KEYS = (
    "ok",
    "status",
    "skill",
    "action",
    "message",
    "error",
    "name",
    "person_id",
    "score",
    "count",
    "found",
    "result",
)


def emit(value: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def running_controller_conflicts() -> list[dict[str, Any]]:
    own = {os.getpid(), os.getppid()}
    found = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for path in proc.iterdir():
        if not path.name.isdigit() or int(path.name) in own:
            continue
        try:
            command = (path / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            continue
        if command and any(pattern in command for pattern in CONTROLLER_PATTERNS):
            found.append({"pid": int(path.name), "command": command[:300]})
    return found


def is_read_only(skill: str, arguments: dict[str, Any]) -> bool:
    if skill in READ_ONLY_SKILLS:
        return True
    action = str(arguments.get("action") or "").strip().lower()
    return bool(action and action in READ_ONLY_ACTIONS)


def extract_message(result: dict[str, Any]) -> str:
    parsed = result.get("parsed_json")
    if isinstance(parsed, dict) and parsed.get("message"):
        return str(parsed["message"])
    error = result.get("error")
    return str(error or "")


def extract_structured_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the executor's parsed result without exposing process internals.

    The single-function JSON is the authoritative device result.  Previously the
    bridge discarded it and only kept an often-empty ``message`` field, leaving
    the model to guess whether a skill succeeded and what it observed.
    """

    parsed = result.get("parsed_json")
    if not isinstance(parsed, dict):
        return {}
    try:
        encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {}
    if len(encoded) <= MAX_STRUCTURED_RESULT_CHARS:
        return parsed

    compact = {key: parsed[key] for key in PREFERRED_RESULT_KEYS if key in parsed}
    nested = compact.get("result")
    if isinstance(nested, dict):
        compact["result"] = {
            key: nested[key]
            for key in PREFERRED_RESULT_KEYS
            if key in nested and key != "result"
        }
    compact["_truncated"] = True
    return compact


SKILL_SPOKEN_LABELS = {
    "move_forward": "向前移动",
    "move_backward": "向后移动",
    "turn_left": "向左转",
    "turn_right": "向右转",
    "navigation_goto": "导航",
    "navigation_list": "地点查询",
    "head_control": "头部调整",
    "projector_control": "投影操作",
    "welcome_projection": "欢迎画面播放",
    "face_recognition": "人脸识别",
    "person_tracking": "人员识别",
    "pet_search": "寻找豆豆",
    "pet_feeder": "投喂",
    "light_control": "灯光调整",
    "push_up": "俯卧撑计数",
    "pull_up": "引体向上计数",
    "squat": "深蹲计数",
    "reminder_schedule": "设置提醒",
    "reminder_query": "查询提醒",
    "reminder_cancel": "删除提醒",
    "media_playback": "媒体播放",
    "media_player": "媒体播放",
    "realtime_information": "实时信息查询",
}

_NAVIGATION_SUMMARY_COUNTS: dict[str, int] = {}


def navigation_arrival_summary(arguments: dict[str, Any]) -> str:
    point = str(arguments.get("point") or "目标位置").strip()
    spoken = {
        "origin": "原点",
        "white_wall": "客厅白墙",
        "study_projection": "书房",
    }.get(point, point)
    options = (
        f"我已经到{spoken}了，你想让我接着做什么？",
        f"{spoken}到了，需要我接下来帮你做什么？",
        f"我到{spoken}了。要不要继续准备投影或播放内容？",
    )
    count = _NAVIGATION_SUMMARY_COUNTS.get(point, 0)
    _NAVIGATION_SUMMARY_COUNTS[point] = count + 1
    return options[count % len(options)]


def humanize_failure_summary(skill: str, raw_reason: str) -> str:
    """Turn executor tokens into truthful, speakable Chinese.

    The raw error remains available in ``error`` and ``structured_result`` for
    diagnostics.  This only prevents identifiers such as
    ``camera_open_failed`` from being read aloud to a person.
    """

    reason = str(raw_reason or "").strip()
    lowered = reason.lower()
    mappings = (
        (("camera_open_failed", "camera_not_open", "camera_unavailable"), "摄像头这次没能正常打开，所以没有继续。"),
        (("cmd_vel_subscribers_0", "no_cmd_vel_subscriber"), "底盘控制服务还没有准备好，所以这次没有移动。"),
        (("resident_runtime_not_started", "resident_socket"), "机器人执行服务还没有准备好，所以这次没有操作。"),
        (("timeout", "timed_out"), "这次操作没有在预期时间内完成，我已经停止继续执行。"),
        (("permission_denied", "forbidden"), "当前没有执行这项操作的权限，所以我没有继续。"),
        (("not_found", "missing_file", "file_not_found"), "需要的内容没有找到，所以这次没有继续。"),
    )
    for needles, spoken in mappings:
        if any(needle in lowered for needle in needles):
            return spoken
    if any("\u4e00" <= char <= "\u9fff" for char in reason):
        return reason
    label = SKILL_SPOKEN_LABELS.get(skill, "这项操作")
    return f"{label}这次没有完成，我没有继续后面的动作。"


def build_spoken_summary(executor: Any, step: Any, result: dict[str, Any]) -> str:
    """Build a deterministic user-facing summary from the real skill result."""

    message = extract_message(result)
    skill = str(getattr(step, "skill_name", "") or "")
    if not result.get("ok"):
        return humanize_failure_summary(skill, message)
    parsed = extract_structured_result(result)
    arguments = dict(getattr(step, "arguments", {}) or {})
    action = str(arguments.get("action") or parsed.get("action") or "").strip().lower()
    if skill == "reminder_schedule":
        content = str(arguments.get("content") or "这件事").strip()
        return f"提醒设好了，到了时间我叫你：{content}。"
    if skill == "reminder_query":
        reminders = parsed.get("reminders") or (parsed.get("result") or {}).get("reminders")
        if isinstance(reminders, list):
            return "目前没有待办提醒。" if not reminders else f"目前有{len(reminders)}个提醒。"
        return "提醒已经查好了。"
    if skill == "reminder_cancel":
        return "这个提醒已经删掉了。"
    if skill == "navigation_list":
        return "我现在认得三个位置：原点、客厅白墙和书房投影点。"
    if skill == "navigation_goto":
        return navigation_arrival_summary(arguments)
    if skill in {"media_player", "realtime_information"} and message:
        return message
    if skill == "person_tracking" and action == "check":
        return "人员识别和跟随已经准备好了，随时可以开始。"
    if skill in {"push_up", "pull_up", "squat"} and action == "check":
        label = {"push_up": "俯卧撑", "pull_up": "引体向上", "squat": "深蹲"}[skill]
        return f"{label}识别和计数已经准备好了，随时可以开始。"
    if skill == "projector_control" and action == "status":
        return "投影设备的当前状态已经查到了。"
    if parsed:
        try:
            summary = executor.speech_policy.step_summary(step, parsed)
        except Exception:
            summary = ""
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return message


def configure_robot_environment(config: dict[str, Any]) -> None:
    paths = config.get("paths", {})
    car_workspace = str(paths.get("car_workspace") or "/home/test/car_real_copy_zhenghang")
    os.environ["CAR_REAL_WS"] = car_workspace
    os.environ["PET_CONTROLLER_CLI_PATH"] = str(
        Path(car_workspace) / "src" / "demo" / "controller_cli.py"
    )


def resolved_entrypoint(config: dict[str, Any], skill_name: str) -> Path | None:
    if skill_name in EXECUTOR_BUILTIN_SKILLS:
        return None
    actual_skill = "navigation_goto" if skill_name == "navigation_list" else skill_name
    return Path(config["paths"]["single_function_dir"]) / actual_skill / "run.sh"


def apply_realtime_execution_overrides(skill_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Adapt shared task-runner defaults to one-shot Realtime tool calls.

    The shared scene executor intentionally keeps a meeting projection step
    alive with ``hold=true`` so a TaskGroup can own and later finalize it.  A
    Realtime Function Call has different lifecycle ownership: it must return
    after starting the projection, while a later explicit ``off`` call stops
    the loop.  Supplying False also prevents the shared executor's setdefault
    from turning the atomic call back into a 120-second blocking task.
    """

    normalized = dict(arguments or {})
    action = str(normalized.get("action") or "").strip().lower()
    if skill_name == "projector_control" and action == "meeting_presentation_on":
        normalized["hold"] = False
    return normalized


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = conn.recv(size)
        if not chunk:
            raise ConnectionError("resident_runtime_closed_connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    for line in reversed(str(stdout or "").splitlines()):
        try:
            value = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


class RunnerRuntime:
    """Keep the shared registry/executor loaded and call the copied resident skills directly."""

    def __init__(
        self,
        robot_project: Path,
        *,
        single_function_dir: Path | None = None,
        runtime_dir: Path | None = None,
        resident_socket: Path | None = None,
    ) -> None:
        project = Path(robot_project).resolve()
        if not (project / "new_project" / "executor.py").is_file():
            raise ValueError(f"robot_project_not_found:{project}")
        if str(project) not in sys.path:
            sys.path.insert(0, str(project))
        from new_project.config import load_config
        from new_project.executor import SkillExecutor
        from new_project.models import TaskStep
        from new_project.skill_registry import SkillRegistry

        config = copy.deepcopy(load_config(project / "config" / "hardware.json"))
        if single_function_dir is not None:
            config.setdefault("paths", {})["single_function_dir"] = str(Path(single_function_dir).resolve())
        if runtime_dir is not None:
            config.setdefault("paths", {})["runtime_dir"] = str(Path(runtime_dir).resolve())
        configure_robot_environment(config)
        self.project = project
        self.config = config
        self.registry = SkillRegistry(config)
        self.executor = SkillExecutor(config, self.registry)
        self.TaskStep = TaskStep
        self.resident_socket = Path(resident_socket).resolve() if resident_socket else None

    def execute(
        self,
        skill: str,
        arguments: dict[str, Any],
        *,
        dry_run: bool,
        user_text: str = "",
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if skill == "welcome_projection":
            return self._execute_welcome_projection(arguments, dry_run=dry_run, started=started)
        sanitized, removed = self.registry.sanitize_model_arguments(skill, arguments)
        if skill in {"push_up", "pull_up", "squat"}:
            for key in (
                "identity_camera",
                "identity_policy",
                "projector_after_identity",
                "preparation_delay",
                "initial_count",
                "initial_elapsed_seconds",
                "resume_from_interrupt",
            ):
                if key in arguments:
                    sanitized[key] = arguments[key]
                    with contextlib.suppress(ValueError):
                        removed.remove(key)
        sanitized = apply_realtime_execution_overrides(skill, sanitized)
        ok, reason = self.registry.validate_step(skill)
        if not ok:
            return {"ok": False, "error": reason, "removed_arguments": removed, "resources": []}
        entrypoint = resolved_entrypoint(self.config, skill)
        if entrypoint is not None and not entrypoint.is_file():
            return {
                "ok": False,
                "error": f"skill_entrypoint_not_found:{entrypoint}",
                "message": "本地功能入口不存在，没有执行任何硬件动作。",
                "removed_arguments": removed,
                "resources": self.registry.get(skill).get("resources", []),
            }
        if not dry_run and not is_read_only(skill, sanitized):
            conflicts = running_controller_conflicts()
            if conflicts:
                return {
                    "ok": False,
                    "error": "existing_robot_controller_running",
                    "message": "检测到其他机器人控制程序正在运行，为避免资源冲突，本次操作没有执行。",
                    "conflicts": conflicts,
                    "resources": self.registry.get(skill).get("resources", []),
                }

        step = self.TaskStep(
            skill_name=skill,
            arguments=sanitized,
            reason="qwen_audio_realtime_function_call",
        )
        if not dry_run and self.resident_socket is not None:
            if not self.resident_socket.is_socket():
                return {
                    "ok": False,
                    "skill": skill,
                    "arguments": sanitized,
                    "removed_arguments": removed,
                    "resources": self.registry.get(skill).get("resources", []),
                    "message": "常驻机器人执行器没有启动，没有执行硬件动作。",
                    "error": "resident_runtime_not_started",
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                }
            result = self._execute_resident(skill, sanitized, event_callback=event_callback)
        else:
            result = self.executor.execute_step(step, dry_run=dry_run)

        structured_result = extract_structured_result(result)
        spoken_summary = build_spoken_summary(self.executor, step, result)
        return {
            "ok": bool(result.get("ok")),
            "skill": skill,
            "arguments": sanitized,
            "removed_arguments": removed,
            "resources": step.resources or self.registry.get(skill).get("resources", []),
            "message": extract_message(result),
            "spoken_summary": spoken_summary,
            "structured_result": structured_result,
            "error": result.get("error"),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            "transport": "resident_socket" if (not dry_run and self.resident_socket is not None) else "executor",
            "skill_events": list(result.get("skill_events") or []),
        }

    def _execute_welcome_projection(
        self,
        arguments: dict[str, Any],
        *,
        dry_run: bool,
        started: float,
    ) -> dict[str, Any]:
        allowed_actions = {"prepare", "play", "stop", "status"}
        action = str(arguments.get("action") or "play").strip().lower()
        if action not in allowed_actions:
            return {
                "ok": False,
                "skill": "welcome_projection",
                "error": "invalid_welcome_projection_action",
                "resources": ["projector"],
            }
        duration = min(10.0, max(0.2, float(arguments.get("duration", 3.0))))
        sanitized = {"action": action, "duration": duration}
        if not dry_run and not is_read_only("welcome_projection", sanitized):
            conflicts = running_controller_conflicts()
            if conflicts:
                return {
                    "ok": False,
                    "skill": "welcome_projection",
                    "arguments": sanitized,
                    "error": "existing_robot_controller_running",
                    "message": "检测到其他机器人控制程序正在运行，为避免资源冲突，本次操作没有执行。",
                    "resources": ["projector"],
                    "conflicts": conflicts,
                }
        if dry_run:
            script = Path(__file__).resolve().with_name("robot_skills") / "welcome_projection" / "run.py"
            command = [sys.executable, str(script), action, "--duration", str(duration), "--dry-run", "--json"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15.0, check=False)
            parsed = _last_json_object(completed.stdout)
            ok = completed.returncode == 0 and bool((parsed or {}).get("ok"))
            result = {
                "ok": ok,
                "parsed_json": parsed or {},
                "error": None if ok else ((parsed or {}).get("error") or completed.stderr[-500:] or "welcome_dry_run_failed"),
            }
            transport = "standalone_dry_run"
        else:
            if self.resident_socket is None or not self.resident_socket.is_socket():
                return {
                    "ok": False,
                    "skill": "welcome_projection",
                    "arguments": sanitized,
                    "error": "resident_runtime_not_started",
                    "message": "常驻机器人执行器没有启动，没有执行硬件动作。",
                    "resources": ["projector"],
                }
            result = self._execute_resident("welcome_projection", sanitized)
            transport = "resident_socket"
        structured = extract_structured_result(result)
        message = extract_message(result)
        return {
            "ok": bool(result.get("ok")),
            "skill": "welcome_projection",
            "arguments": sanitized,
            "removed_arguments": sorted(set(arguments) - {"action", "duration"}),
            "resources": ["projector"],
            "message": message,
            "spoken_summary": message or ("欢迎画面播放完成。" if result.get("ok") else "欢迎画面没有正常播放。"),
            "structured_result": structured,
            "error": result.get("error"),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            "transport": transport,
        }

    def _execute_resident(
        self,
        skill: str,
        arguments: dict[str, Any],
        *,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        assert self.resident_socket is not None
        if skill == "welcome_projection":
            argv = [str(arguments.get("action") or "play"), "--duration", str(arguments.get("duration") or 3.0), "--json"]
        else:
            argv = self.executor._arguments_to_cli(skill, arguments)
        message = {
            "op": "skill_run",
            "skill": skill,
            "argv": argv,
            "stream": True,
            "payload_len": 0,
        }
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timeout = 3600.0
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        stdout_line_buffer = ""
        final: dict[str, Any] = {}

        def accept_stdout(value: str) -> None:
            nonlocal stdout_line_buffer
            stdout_parts.append(value)
            stdout_line_buffer += value
            while "\n" in stdout_line_buffer:
                line, stdout_line_buffer = stdout_line_buffer.split("\n", 1)
                try:
                    event = json.loads(line.strip())
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("event") not in {"skill_event", "skill_ready"}:
                    continue
                skill_events.append(event)
                if event_callback is not None:
                    event_callback(dict(event))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(self.resident_socket))
            conn.sendall(struct.pack("!I", len(payload)) + payload)
            while True:
                size = struct.unpack("!I", _recv_exact(conn, 4))[0]
                response = json.loads(_recv_exact(conn, size).decode("utf-8"))
                kind = response.get("type")
                if kind == "stdout":
                    accept_stdout(str(response.get("data") or ""))
                    continue
                if kind == "stderr":
                    stderr_parts.append(str(response.get("data") or ""))
                    continue
                final = dict(response)
                final.pop("type", None)
                break
        stdout = "".join(stdout_parts) + str(final.get("stdout") or "")
        stderr = "".join(stderr_parts) + str(final.get("stderr") or "")
        parsed = _last_json_object(stdout)
        resident_ok = bool(final.get("ok")) and int(final.get("exit_code", 0)) == 0
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            resident_ok = False
        error = None
        if not resident_ok:
            error = (
                (parsed or {}).get("error")
                or final.get("error")
                or stderr.strip()[-1000:]
                or f"resident_skill_exit_{final.get('exit_code', 1)}"
            )
        return {
            "ok": resident_ok,
            "stdout": stdout,
            "stderr": stderr,
            "parsed_json": parsed,
            "error": error,
            "resident_result": final,
            "skill_events": skill_events,
        }


def execute_request(
    *,
    robot_project: Path,
    skill: str,
    arguments: dict[str, Any],
    dry_run: bool,
    user_text: str = "",
    single_function_dir: Path | None = None,
    runtime_dir: Path | None = None,
    resident_socket: Path | None = None,
    runtime: RunnerRuntime | None = None,
) -> dict[str, Any]:
    owner = runtime or RunnerRuntime(
        robot_project,
        single_function_dir=single_function_dir,
        runtime_dir=runtime_dir,
        resident_socket=resident_socket,
    )
    return owner.execute(skill, arguments, dry_run=dry_run, user_text=user_text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--robot-project", type=Path, required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--arguments-json", default="{}")
    parser.add_argument("--user-text", default="")
    parser.add_argument("--single-function-dir", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--resident-socket", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        arguments = json.loads(args.arguments_json)
        if not isinstance(arguments, dict):
            raise ValueError("arguments_not_object")
    except Exception as exc:
        emit({"ok": False, "error": f"invalid_arguments:{exc}", "resources": []})
        return 2

    try:
        result = execute_request(
            robot_project=args.robot_project,
            skill=args.skill,
            arguments=arguments,
            dry_run=args.dry_run,
            user_text=args.user_text,
            single_function_dir=args.single_function_dir,
            runtime_dir=args.runtime_dir,
            resident_socket=args.resident_socket,
        )
        emit(result)
        return 0 if result.get("ok") else 5
    except Exception as exc:
        emit(
            {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
                "resources": [],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
