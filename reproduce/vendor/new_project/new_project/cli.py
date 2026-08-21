from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .audio import AudioManager
from .config import ensure_runtime_dirs, load_config
from .dialogue import RobotOrchestrator
from .models import TaskStatus
from .reminder_service import ReminderScheduler, ReminderStore
from .resources import ResourceManager
from .skill_registry import SkillRegistry
from .storage import JsonStore
from .wakeup import WakeupListener


class DaemonInstanceLock:
    def __init__(self, config: dict[str, Any]):
        self.path = Path(config["paths"]["runtime_dir"]) / "daemon.lock"
        self.file: Any | None = None

    def acquire(self) -> tuple[bool, int | None]:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, self._read_pid()
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return True, None

    def release(self) -> None:
        if self.file is None:
            return
        with contextlib.suppress(Exception):
            import fcntl

            self.file.seek(0)
            self.file.truncate()
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
        self.file = None

    def _read_pid(self) -> int | None:
        if self.file is None:
            return None
        with contextlib.suppress(Exception):
            self.file.seek(0)
            text = self.file.read().strip()
            return int(text) if text else None
        return None


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_self_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ensure_runtime_dirs(config)
    registry = SkillRegistry(config)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for key in ["self_program_dir", "single_function_dir", "skill_spec_dir"]:
        path = Path(config["paths"][key])
        add(f"path:{key}", path.exists(), str(path))
    add("skill_specs_count", len(registry.names()) >= 20, str(len(registry.names())))
    for command in [config["audio"]["record_command"], config["audio"]["play_command"], "v4l2-ctl"]:
        add(f"command:{command}", shutil.which(command) is not None, shutil.which(command) or "")
    for name, cfg in config["cameras"].items():
        add(f"camera:{name}", Path(cfg["device"]).exists(), cfg["device"])
    add("mic_device_configured", bool(config["audio"]["input_device"]), config["audio"]["input_device"])
    add("speaker_device_configured", bool(config["audio"]["output_device"]), config["audio"]["output_device"])
    if config.get("audio", {}).get("voice_io_backend") == "doubao_realtime":
        env_path = Path("/home/test/.doubao_realtime_env")
        env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        required = ["DOUBAO_REALTIME_APP_ID", "DOUBAO_REALTIME_ACCESS_KEY", "DOUBAO_REALTIME_RESOURCE_ID", "DOUBAO_REALTIME_APP_KEY"]
        for key in required:
            add(f"doubao_env:{key}", key in env_text, str(env_path))
    runtime_report = JsonStore(config).validate_runtime_state()
    add("runtime_state_invariants", bool(runtime_report.get("ok")), json.dumps(runtime_report.get("issues") or [], ensure_ascii=False))

    if args.audio:
        try:
            audio = AudioManager(config, ResourceManager(config))
            path = audio.record_then_play(seconds=args.audio_seconds)
            add("audio_record_then_play", True, str(path))
        except Exception as exc:
            add("audio_record_then_play", False, str(exc))

    if args.camera:
        orchestrator = RobotOrchestrator(config)
        for skill in ["front_camera_capture", "back_camera_capture"]:
            from .models import TaskStep

            result = orchestrator.executor.execute_step(TaskStep(skill_name=skill), dry_run=False)
            add(f"capture:{skill}", bool(result.get("ok")), json.dumps(result, ensure_ascii=False))

    result = {"ok": all(item["ok"] for item in checks), "checks": checks}
    print_json(result)
    return 0 if result["ok"] else 1


def cmd_taskgroup_self_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    from .resume_self_check import run_resume_self_check

    result = run_resume_self_check(config)
    checks = result.get("checks") or []
    failures = [item for item in checks if not item.get("ok")]
    summary = {
        "ok": bool(result.get("ok")),
        "runtime_dir": result.get("runtime_dir"),
        "checks": len(checks),
        "failed": [item.get("name") for item in failures],
    }
    if args.verbose or failures:
        summary["details"] = checks
    print_json(summary)
    return 0 if summary["ok"] else 1


def cmd_camera_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config_path = Path(config["_config_path"])
    cameras = config.setdefault("cameras", {})
    before = json.loads(json.dumps(cameras, ensure_ascii=False))

    updates: dict[str, dict[str, Any]] = {}
    if args.front_device:
        updates.setdefault("front", {})["device"] = args.front_device
    if args.back_device:
        updates.setdefault("back", {})["device"] = args.back_device
    if args.front_width is not None:
        updates.setdefault("front", {})["width"] = args.front_width
    if args.front_height is not None:
        updates.setdefault("front", {})["height"] = args.front_height
    if args.back_width is not None:
        updates.setdefault("back", {})["width"] = args.back_width
    if args.back_height is not None:
        updates.setdefault("back", {})["height"] = args.back_height

    for camera_name, values in updates.items():
        cameras.setdefault(camera_name, {}).update(values)

    after = json.loads(json.dumps(cameras, ensure_ascii=False))
    missing_devices = []
    if args.require_device_exists:
        for camera_name in ["front", "back"]:
            device = str(cameras.get(camera_name, {}).get("device") or "")
            if device and not Path(device).exists():
                missing_devices.append({"camera": camera_name, "device": device})
    probes = []
    if args.probe or args.probe_frame:
        for camera_name in ["front", "back"]:
            device = str(cameras.get(camera_name, {}).get("device") or "")
            probes.append(_probe_camera_device(camera_name, device, probe_frame=bool(args.probe_frame), timeout=float(args.probe_timeout)))

    changed = before != after
    wrote = False
    probe_failures = [item for item in probes if not item.get("ok")]
    if args.write and changed and not missing_devices and not probe_failures:
        payload = dict(config)
        payload.pop("_config_path", None)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(config_path)
        wrote = True

    result = {
        "ok": not missing_devices and not probe_failures,
        "config_path": str(config_path),
        "changed": changed,
        "write_requested": bool(args.write),
        "wrote": wrote,
        "before": before,
        "after": after,
        "missing_devices": missing_devices,
        "probes": probes,
    }
    print_json(result)
    return 0 if result["ok"] and not probe_failures else 1


def _probe_camera_device(camera_name: str, device: str, probe_frame: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = {
        "camera": camera_name,
        "device": device,
        "exists": bool(device and Path(device).exists()),
        "v4l2_ok": False,
        "frame_ok": None,
        "ok": False,
    }
    if not result["exists"]:
        result["error"] = "device_not_found"
        return result

    v4l2 = shutil.which("v4l2-ctl")
    if not v4l2:
        result["error"] = "v4l2-ctl_not_found"
        return result
    try:
        completed = subprocess.run(
            [v4l2, f"--device={device}", "--all"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1.0, timeout),
            check=False,
        )
        result["v4l2_ok"] = completed.returncode == 0
        result["v4l2_returncode"] = completed.returncode
        result["v4l2_output_tail"] = (completed.stdout or "")[-1200:]
    except Exception as exc:
        result["error"] = f"v4l2_probe_failed: {exc}"
        return result

    if probe_frame:
        code = (
            "import cv2,sys;"
            "cap=cv2.VideoCapture(sys.argv[1]);"
            "ok=cap.isOpened();"
            "frame_ok=False;"
            "\nif ok:\n"
            "    ok,frame=cap.read(); frame_ok=bool(ok and frame is not None and getattr(frame,'size',0)>0)\n"
            "cap.release();"
            "print('frame_ok=' + str(frame_ok));"
            "sys.exit(0 if frame_ok else 2)"
        )
        try:
            frame = subprocess.run(
                [sys.executable, "-c", code, device],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(1.0, timeout),
                check=False,
            )
            result["frame_ok"] = frame.returncode == 0
            result["frame_returncode"] = frame.returncode
            result["frame_output_tail"] = (frame.stdout or "")[-1200:]
        except Exception as exc:
            result["frame_ok"] = False
            result["frame_error"] = str(exc)

    result["ok"] = bool(result["v4l2_ok"] and (result["frame_ok"] is not False))
    return result


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    orchestrator = RobotOrchestrator(config)
    history = orchestrator.store.recent_history(limit=int(config.get("planner", {}).get("history_context_items", 8)))
    print_json(orchestrator.planner.plan(args.text, history=history))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    orchestrator = RobotOrchestrator(config)
    result = orchestrator.handle_text(args.text, execute=args.execute, dry_run=not args.execute, enqueue=args.execute or args.enqueue)
    print_json(result)
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    orchestrator = RobotOrchestrator(config)
    result = orchestrator.handle_voice_once(seconds=args.seconds, execute=args.execute, dry_run=not args.execute)
    print_json(result)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    orchestrator = RobotOrchestrator(config)
    print_json(orchestrator.resume_last_interrupted(execute=args.execute, dry_run=not args.execute))
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    orchestrator = RobotOrchestrator(config)
    print_json(orchestrator.answer_followup(args.task_group_id, args.text, execute=args.execute, dry_run=not args.execute, enqueue=args.execute or args.enqueue))
    return 0


def _voice_once_with_retries(orchestrator: RobotOrchestrator, args: argparse.Namespace) -> dict[str, Any]:
    result = orchestrator.handle_voice_once(seconds=args.seconds, execute=args.execute, dry_run=not args.execute)
    retry_count = 0
    while not result.get("ok") and result.get("error") == "asr_empty" and retry_count < args.asr_retries:
        retry_count += 1
        result = orchestrator.handle_voice_once(seconds=args.seconds, execute=args.execute, dry_run=not args.execute)
    return result


def _wait_for_robot_speech_settle(orchestrator: RobotOrchestrator, args: argparse.Namespace, reason: str, wakeup_event: Any | None = None) -> None:
    if wakeup_event is not None and reason == "command_listen":
        delay = getattr(args, "post_wake_listen_delay_seconds", None)
    else:
        delay = getattr(args, "post_speech_listen_delay_seconds", None)
    try:
        slept = orchestrator.audio.wait_for_speech_settle(float(delay) if delay is not None else None)
    except Exception:
        slept = 0.0
    if slept > 0:
        orchestrator.store.append_event(
            "listen_delayed_after_robot_speech",
            {
                "reason": reason,
                "elapsed_seconds": round(float(slept), 4),
                "wakeup_event_id": getattr(wakeup_event, "event_id", None),
            },
        )


def _listen_heard_with_retries(orchestrator: RobotOrchestrator, args: argparse.Namespace, wakeup_event: Any | None = None) -> dict[str, Any]:
    _wait_for_robot_speech_settle(orchestrator, args, "command_listen", wakeup_event=wakeup_event)
    if getattr(orchestrator, "realtime_voice", None) is not None:
        attempts = max(0, int(args.asr_retries)) + 1
        last: dict[str, Any] = {}
        for index in range(attempts):
            if index:
                _wait_for_robot_speech_settle(orchestrator, args, "command_listen_retry", wakeup_event=wakeup_event)
            started = time.time()
            decision = orchestrator.realtime_voice.decide_once(seconds=args.seconds, mode="command")
            heard = {"decision": decision, "text": str(decision.get("user_text") or decision.get("reply") or ""), "error": decision.get("error", "")}
            last = heard
            timing = decision.get("timing") if isinstance(decision.get("timing"), dict) else {}
            orchestrator.store.append_event(
                "realtime_command_listen_finished",
                {
                    "wakeup_event_id": getattr(wakeup_event, "event_id", None),
                    "attempt": index + 1,
                    "elapsed_seconds": round(time.time() - started, 4),
                    "retryable_empty": _heard_is_empty_or_noop_command(heard),
                    "decision_type": decision.get("decision_type"),
                    "reason": decision.get("reason"),
                    "error": decision.get("error"),
                    "has_user_text": bool(decision.get("user_text")),
                    "has_task_groups": bool(decision.get("task_groups")),
                    "has_ask_user": bool(decision.get("ask_user")),
                    "trace_id": decision.get("trace_id") or timing.get("trace_id"),
                    "semantic_adjudication_completed": bool(decision.get("semantic_adjudication_completed")),
                    "timing_summary_ms": dict(timing.get("durations_ms") or {}),
                },
            )
            if timing:
                orchestrator.store.append_event(
                    "realtime_stage_timing",
                    {
                        "wakeup_event_id": getattr(wakeup_event, "event_id", None),
                        "attempt": index + 1,
                        **timing,
                    },
                )
            if not _heard_is_empty_or_noop_command(heard):
                return heard
            if index + 1 < attempts:
                orchestrator.store.append_event(
                    "command_listen_retry",
                    {
                        "wakeup_event_id": getattr(wakeup_event, "event_id", None),
                        "attempt": index + 1,
                        "error": decision.get("error") or decision.get("reason") or "empty_realtime_command",
                    },
                )
                orchestrator.audio.speak_text("\u6211\u6ca1\u6709\u542c\u6e05\u695a\uff0c\u8bf7\u518d\u8bf4\u4e00\u904d")
        return last
    attempts = max(0, int(args.asr_retries)) + 1
    last: dict[str, Any] = {}
    for index in range(attempts):
        started = time.time()
        orchestrator.store.append_event(
            "listen_started",
            {"wakeup_event_id": getattr(wakeup_event, "event_id", None), "attempt": index + 1},
        )
        heard = orchestrator.audio.listen_once(seconds=args.seconds)
        last = heard
        orchestrator.store.append_event(
            "listen_finished",
            {
                "wakeup_event_id": getattr(wakeup_event, "event_id", None),
                "attempt": index + 1,
                "elapsed_seconds": round(time.time() - started, 4),
                "has_text": bool(heard.get("text")),
                "error": heard.get("error"),
            },
        )
        if heard.get("text"):
            return heard
        if index + 1 < attempts:
            orchestrator.audio.speak_text("我没有听清楚，请再说一遍")
    return last


def _heard_is_empty_or_noop_command(heard: dict[str, Any] | None) -> bool:
    if not isinstance(heard, dict):
        return True
    decision = heard.get("decision")
    if isinstance(decision, dict):
        if not decision.get("ok", True):
            return True
        if decision.get("ask_user") or decision.get("task_groups"):
            return False
        decision_type = str(decision.get("decision_type") or "")
        user_text = str(decision.get("user_text") or "").strip()
        reply = str(decision.get("reply") or "").strip()
        reason = str(decision.get("reason") or decision.get("error") or "").strip()
        if reason in {
            "robot_prompt_echo",
            "empty_model_text",
            "no_valid_speech_detected",
            "model_audio_idle_timeout",
            "model_timeout",
            "doubao_no_text_response",
        }:
            return True
        if decision_type == "noop":
            return True
        if decision_type == "answer" and (user_text or reply):
            return False
        return not bool(user_text or reply)
    text = str(heard.get("text") or "").strip()
    if text:
        return False
    return True


def _result_is_empty_or_noop_command(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("noop"):
        return True
    decision = result.get("decision")
    if isinstance(decision, dict):
        return _heard_is_empty_or_noop_command({"decision": decision})
    if not result.get("ok") and str(result.get("error") or "") in {
        "barge_in_command_empty",
        "command_empty",
        "doubao_decision_empty",
        "asr_empty",
        "no_valid_speech_detected",
    }:
        return True
    return False


def _empty_wakeup_voice_result(
    orchestrator: RobotOrchestrator,
    heard: dict[str, Any] | None,
    interrupt_result: dict[str, Any] | None,
    event: Any,
) -> dict[str, Any]:
    interrupted = bool(isinstance(interrupt_result, dict) and interrupt_result.get("interrupted"))
    task_group_id = interrupt_result.get("task_group_id") if isinstance(interrupt_result, dict) else None
    error = "barge_in_command_empty" if interrupted else "command_empty"
    voice_result = {
        "ok": False,
        "error": error,
        "heard": heard or {},
        "message": (
            "interrupted task is paused; new command was not recognized"
            if interrupted
            else "new command was not recognized"
        ),
    }
    orchestrator.store.append_event(
        "barge_in_command_empty_kept_interrupted" if interrupted else "command_empty_ignored",
        {
            "wakeup_event_id": getattr(event, "event_id", None),
            "task_group_id": task_group_id,
            "heard_error": heard.get("error") if isinstance(heard, dict) else None,
            "decision": heard.get("decision") if isinstance(heard, dict) else None,
        },
    )
    if interrupted:
        try:
            interrupted_group = orchestrator.store.load_task_group(task_group_id) if task_group_id else None
        except Exception:
            interrupted_group = None
        orchestrator.audio.speak_text(orchestrator.speech_policy.interrupted_command_retry(interrupted_group))
    else:
        orchestrator.audio.speak_text("我没有听清楚，请再说一遍")
    return voice_result


def _followup_voice_with_retries(orchestrator: RobotOrchestrator, task_group_id: str, args: argparse.Namespace) -> dict[str, Any]:
    retry_count = 0
    max_echo_retries = max(0, int(getattr(args, "followup_echo_retries", 1)))
    listen_args = argparse.Namespace(**vars(args))
    listen_args.post_speech_listen_delay_seconds = float(getattr(args, "followup_post_speech_listen_delay_seconds", 0.25))
    while True:
        _wait_for_robot_speech_settle(orchestrator, listen_args, "followup_listen")
        result = orchestrator.handle_followup_voice(task_group_id, seconds=args.seconds, execute=args.execute, dry_run=not args.execute)
        error = str(result.get("error") or "")
        if result.get("ok"):
            return result
        if _is_retryable_followup_listen_error(error) and retry_count < args.asr_retries:
            retry_count += 1
            orchestrator.store.append_event(
                "followup_listen_retry",
                {"task_group_id": task_group_id, "error": error, "attempt": retry_count},
            )
            try:
                pending_group = orchestrator.store.load_task_group(task_group_id)
                pending = orchestrator._pending_followup(pending_group) or {}
            except Exception:
                pending = {}
            orchestrator.audio.speak_text(orchestrator.speech_policy.followup_retry(str(pending.get("question") or "")))
            continue
        if error == "robot_question_echo" and retry_count < max_echo_retries:
            retry_count += 1
            continue
        if _is_retryable_followup_listen_error(error):
            return _pending_followup_wait_result(orchestrator, task_group_id, result, dry_run=not args.execute)
        return result


def _is_retryable_followup_listen_error(error: str) -> bool:
    error = str(error or "")
    if not error:
        return False
    retryable = {
        "asr_empty",
        "no_valid_speech_detected",
        "empty_followup_text",
        "doubao_followup_empty",
        "doubao_followup_missing_decision",
        "empty_model_text",
        "model_timeout",
        "model_audio_idle_timeout",
        "assistant_followup_prose",
        "robot_question_echo",
    }
    if error in retryable:
        return True
    return error.startswith("doubao_realtime_error") or error.startswith("doubao_realtime_connection")


def _pending_followup_wait_result(
    orchestrator: RobotOrchestrator,
    task_group_id: str,
    heard: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    try:
        task_group = orchestrator.store.load_task_group(task_group_id)
        pending = orchestrator._pending_followup(task_group) or {}
    except Exception as exc:
        return {
            "ok": False,
            "task_group_id": task_group_id,
            "error": str(heard.get("error") or "followup_listen_failed"),
            "heard": heard,
            "keep_waiting": False,
            "state_error": str(exc),
        }
    question = str(pending.get("question") or "刚才想确认的事情")
    ask_user = {
        "task_title": pending.get("task_title") or task_group.title,
        "question": question,
        "missing_slots": list(pending.get("missing_slots") or []),
        "optional_slots": list(pending.get("optional_slots") or []),
        "candidate_skills": list(pending.get("candidate_skills") or []),
    }
    decision = {
        "decision_type": "ask_user",
        "reply": question,
        "task_groups": [orchestrator._task_group_to_decision_payload(task_group)],
        "ask_user": ask_user,
        "confidence": 1.0,
    }
    orchestrator.store.append_event(
        "followup_kept_waiting_after_listen_failure",
        {"task_group_id": task_group_id, "error": heard.get("error"), "question": question},
    )
    safety = orchestrator.suspend_waiting_followup_safely(
        task_group_id,
        dry_run=dry_run,
        reason="followup_listen_failure",
    )
    orchestrator.audio.speak_text(orchestrator.speech_policy.followup_retry(question, final=True))
    return {
        "ok": True,
        "task_group_id": task_group_id,
        "error": str(heard.get("error") or "followup_listen_failed"),
        "heard": heard,
        "keep_waiting": True,
        "safety": safety,
        "decision": decision,
        "execution": {"executed": []},
    }


def _pending_followup_task_group_id(orchestrator: RobotOrchestrator, result: dict[str, Any]) -> str | None:
    decision = result.get("decision") if isinstance(result, dict) else None
    ask_user = decision.get("ask_user") if isinstance(decision, dict) else None
    if not isinstance(ask_user, dict):
        return None

    ids: list[str] = []
    result_ids = result.get("task_group_ids")
    if isinstance(result_ids, list):
        ids.extend(str(item) for item in result_ids if item)
    task_group_id = result.get("task_group_id")
    if task_group_id:
        ids.append(str(task_group_id))

    session_id = result.get("session")
    if session_id:
        try:
            session = orchestrator.store.load_session(str(session_id))
            ids.extend(str(item) for item in session.task_group_ids if item)
        except Exception:
            pass

    seen: set[str] = set()
    unique_ids = []
    for item in ids:
        if item not in seen:
            seen.add(item)
            unique_ids.append(item)

    task_title = str(ask_user.get("task_title") or "").strip()
    candidates = []
    for item in unique_ids:
        try:
            task_group = orchestrator.store.load_task_group(item)
        except Exception:
            continue
        last_followup = task_group.followups[-1] if task_group.followups else {}
        unanswered = bool(last_followup) and not last_followup.get("answer")
        needs_info = task_group.status == TaskStatus.NEEDS_INFO.value
        title_match = bool(task_title and task_group.title == task_title)
        if not (needs_info or unanswered or title_match):
            continue
        score = 0
        if needs_info:
            score += 100
        if unanswered:
            score += 40
        if title_match:
            score += 30
        if not task_group.steps:
            score += 5
        candidates.append((score, item))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return unique_ids[0] if unique_ids else None


def _followup_state_snapshot(orchestrator: RobotOrchestrator, task_group_id: str) -> dict[str, Any]:
    try:
        task_group = orchestrator.store.load_task_group(task_group_id)
        pending = orchestrator._pending_followup(task_group) or {}
    except Exception as exc:
        return {"task_group_id": task_group_id, "error": str(exc), "signature": "unavailable"}
    state = {
        "task_group_id": task_group_id,
        "status": task_group.status,
        "slots": dict(task_group.slots or {}),
        "question": str(pending.get("question") or ""),
        "question_timestamp": pending.get("timestamp"),
        "missing_slots": list(pending.get("missing_slots") or []),
        "optional_slots": list(pending.get("optional_slots") or []),
        "runtime_followup": pending.get("runtime_followup"),
        "step_states": [(step.step_id, step.status) for step in task_group.steps],
    }
    state["signature"] = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
    return state


def _pause_followup_flow_safely(
    orchestrator: RobotOrchestrator,
    task_group_id: str | None,
    *,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    safety = {"ok": True, "skipped": True, "reason": "no_task_group"}
    if task_group_id:
        handler = getattr(orchestrator, "suspend_waiting_followup_safely", None)
        if callable(handler):
            try:
                safety = handler(task_group_id, dry_run=dry_run, reason=reason)
            except Exception as exc:
                safety = {"ok": False, "error": str(exc)}
    orchestrator.store.append_event(
        "followup_flow_safely_paused",
        {"task_group_id": task_group_id, "reason": reason, "safety": safety},
    )
    orchestrator.audio.speak_text("这件事我已经保留好了。准备好后再叫我，我们可以从这里接着说。")
    return {"paused": True, "reason": reason, "task_group_id": task_group_id, "safety": safety}


def _complete_followup_flow(orchestrator: RobotOrchestrator, voice_result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    followups = []
    current = voice_result
    max_no_progress = max(1, int(args.max_followups))
    max_total_turns = max(max_no_progress + 1, int(getattr(args, "max_followup_total_turns", 12)))
    max_overlay_turns = max(1, int(getattr(args, "max_followup_overlay_turns", 4)))
    max_elapsed = max(30.0, float(getattr(args, "max_followup_elapsed_seconds", 240.0)))
    started = time.monotonic()
    no_progress_turns = 0
    overlay_turns = 0
    total_turns = 0
    last_task_group_id: str | None = None
    pause_reason: str | None = None
    while True:
        task_group_id = _pending_followup_task_group_id(orchestrator, current)
        if not task_group_id:
            break
        last_task_group_id = task_group_id
        if total_turns >= max_total_turns:
            pause_reason = "max_total_followup_turns"
            break
        if time.monotonic() - started >= max_elapsed:
            pause_reason = "max_followup_elapsed_seconds"
            break
        before = _followup_state_snapshot(orchestrator, task_group_id)
        followup_result = _followup_voice_with_retries(orchestrator, task_group_id, args)
        followups.append(followup_result)
        current = followup_result
        total_turns += 1
        if followup_result.get("keep_waiting"):
            break
        if not followup_result.get("ok"):
            break
        next_task_group_id = _pending_followup_task_group_id(orchestrator, current) or task_group_id
        after = _followup_state_snapshot(orchestrator, next_task_group_id)
        non_slot_turn = bool(followup_result.get("dialogue_overlay") or followup_result.get("non_slot_turn"))
        if non_slot_turn:
            overlay_turns += 1
            if overlay_turns >= max_overlay_turns:
                pause_reason = "max_followup_overlay_turns"
                break
            continue
        overlay_turns = 0
        if before.get("signature") == after.get("signature"):
            no_progress_turns += 1
            if no_progress_turns >= max_no_progress:
                pause_reason = "max_followup_no_progress_turns"
                break
        else:
            no_progress_turns = 0
    if followups:
        voice_result["followup_results"] = followups
    voice_result["followup_flow"] = {
        "mode": "state_driven",
        "total_turns": total_turns,
        "no_progress_turns": no_progress_turns,
        "overlay_turns": overlay_turns,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if pause_reason:
        voice_result["followup_flow"].update(
            _pause_followup_flow_safely(
                orchestrator,
                last_task_group_id,
                reason=pause_reason,
                dry_run=not args.execute,
            )
        )
    return voice_result


def _latest_dialog_result(result: dict[str, Any]) -> dict[str, Any]:
    followups = result.get("followup_results") if isinstance(result, dict) else None
    if isinstance(followups, list) and followups:
        last = followups[-1]
        if isinstance(last, dict):
            return last
    return result


def _result_has_interrupted_execution(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    execution = result.get("execution")
    if isinstance(execution, dict):
        executed = execution.get("executed")
        if isinstance(executed, list):
            for item in executed:
                if isinstance(item, dict) and item.get("status") == TaskStatus.INTERRUPTED.value:
                    return True
    followups = result.get("followup_results")
    if isinstance(followups, list):
        return any(_result_has_interrupted_execution(item) for item in followups if isinstance(item, dict))
    confirmations = result.get("resume_confirmations")
    if isinstance(confirmations, list):
        return any(_result_has_interrupted_execution(item) for item in confirmations if isinstance(item, dict))
    resume = result.get("resume")
    if isinstance(resume, dict) and _result_has_interrupted_execution(resume):
        return True
    command = result.get("command_result")
    if isinstance(command, dict) and _result_has_interrupted_execution(command):
        return True
    return False


def _listen_resume_reply_with_retries(orchestrator: RobotOrchestrator, args: argparse.Namespace) -> dict[str, Any]:
    _wait_for_robot_speech_settle(orchestrator, args, "resume_confirmation_listen")
    if getattr(orchestrator, "realtime_voice", None) is not None:
        attempts = max(0, int(args.resume_confirm_retries)) + 1
        last: dict[str, Any] = {}
        for index in range(attempts):
            if index:
                _wait_for_robot_speech_settle(orchestrator, args, "resume_confirmation_retry")
            result = orchestrator.realtime_voice.decide_once(seconds=args.resume_confirm_seconds, mode="resume")
            last = result
            text = str(result.get("asr_text") or result.get("text") or "").strip()
            if text:
                return {"ok": True, "heard": result, "text": text}
            orchestrator.store.append_event(
                "resume_confirmation_doubao_empty",
                {"error": result.get("error"), "attempt": index + 1, "attempts": attempts},
            )
            if index + 1 < attempts:
                interrupted = orchestrator.store.peek_interrupted()
                question = orchestrator.speech_policy.resume_question(interrupted) if interrupted else "还要接着刚才的事吗？"
                orchestrator.audio.speak_text(orchestrator.speech_policy.followup_retry(question))
        interrupted = orchestrator.store.peek_interrupted()
        orchestrator.audio.speak_text(orchestrator.speech_policy.paused(interrupted) if interrupted else "好，刚才的事先放一放。")
        return {"ok": False, "error": last.get("error") or "doubao_resume_empty", "heard": last}
    attempts = max(0, int(args.resume_confirm_retries)) + 1
    last: dict[str, Any] = {}
    for index in range(attempts):
        heard = orchestrator.audio.listen_once(seconds=args.resume_confirm_seconds)
        last = heard
        if heard.get("text"):
            return {"ok": True, "heard": heard, "text": heard["text"]}
        orchestrator.store.append_event("resume_confirmation_asr_empty", {"audio_path": heard.get("audio_path"), "error": heard.get("error")})
        if index + 1 < attempts:
            orchestrator.audio.speak_text("我没有听清楚，请再说一遍")
    interrupted = orchestrator.store.peek_interrupted()
    orchestrator.audio.speak_text(orchestrator.speech_policy.paused(interrupted) if interrupted else "好，刚才的事先放一放。")
    return {"ok": False, "error": "asr_empty", "heard": last}


def _scene_restore_decision(
    orchestrator: RobotOrchestrator,
    args: argparse.Namespace,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = preview if isinstance(preview, dict) else orchestrator.preview_resume_scene_restore()
    if not preview.get("scene_changed"):
        return {"restore_scene": False, "preview": preview, "prompted": False}
    if preview.get("neutralized_for_new_session"):
        # Changes introduced by the interruption transition belong to the
        # runtime and must be restored automatically before task resumption.
        return {
            "restore_scene": True,
            "preview": preview,
            "prompted": False,
            "reason": "automatic_restore_after_runtime_neutralization",
        }
    orchestrator.audio.speak_text(str(preview.get("question") or "要先回到刚才的位置再继续吗？"))
    answer = _listen_resume_reply_with_retries(orchestrator, args)
    if not answer.get("ok"):
        return {"restore_scene": True, "preview": preview, "prompted": True, "answer": answer, "defaulted": "restore_scene"}
    action = orchestrator.classify_scene_restore_reply(str(answer.get("text") or ""))
    return {"restore_scene": action != "skip_restore", "preview": preview, "prompted": True, "answer": answer, "action": action}


def _speak_resume_ack_and_preview(orchestrator: RobotOrchestrator) -> dict[str, Any] | None:
    try:
        preview = orchestrator.preview_resume_scene_restore()
    except Exception as exc:
        orchestrator.store.append_event("resume_scene_preview_failed", {"error": str(exc)})
        preview = None
    interrupted = orchestrator.store.peek_interrupted()
    ack = orchestrator.speech_policy.resume_ack(interrupted) if interrupted else "好，我们接着来。"
    if isinstance(preview, dict) and preview.get("neutralized_for_new_session"):
        threading.Thread(
            target=orchestrator.audio.speak_text,
            args=(ack,),
            name="resume-ack-speech",
            daemon=True,
        ).start()
    else:
        orchestrator.audio.speak_text(ack)
    return preview


def _complete_resume_confirmation_flow(orchestrator: RobotOrchestrator, voice_result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if not voice_result.get("ok"):
        return voice_result
    if _result_is_empty_or_noop_command(voice_result):
        orchestrator.store.append_event(
            "resume_confirmation_skipped_for_empty_command",
            {
                "session_id": voice_result.get("session"),
                "error": voice_result.get("error"),
                "noop": bool(voice_result.get("noop")),
            },
        )
        return voice_result
    confirmations = []
    current = voice_result
    configured_max = int(orchestrator.config.get("planner", {}).get("resume_prompt_max_per_session", 1))
    max_rounds = min(max(0, int(args.max_resume_rounds)), max(0, configured_max))
    for _ in range(max_rounds):
        latest = _latest_dialog_result(current)
        if _result_has_interrupted_execution(latest):
            break
        if _pending_followup_task_group_id(orchestrator, latest):
            break
        prompt = orchestrator.ask_resume_confirmation()
        if prompt is None:
            break

        answer = _listen_resume_reply_with_retries(orchestrator, args)
        item: dict[str, Any] = {"prompt": prompt, "answer": answer}
        if not answer.get("ok"):
            confirmations.append(item)
            break

        answer_text = str(answer.get("text") or "")
        # Resume/cancel is state control, so classify the authoritative ASR text
        # locally. A model-generated action must never override what was heard.
        action = orchestrator.classify_resume_reply(answer_text)
        orchestrator.store.append_event(
            "resume_confirmation_answer",
            {"task_group_id": prompt.get("task_group_id"), "answer_text": answer_text, "action": action},
        )
        item["action"] = action

        if action == "resume":
            scene_preview = _speak_resume_ack_and_preview(orchestrator)
            scene_decision = _scene_restore_decision(orchestrator, args, preview=scene_preview)
            item["scene_restore"] = scene_decision
            resume_result = orchestrator.resume_last_interrupted(
                execute=args.execute,
                dry_run=not args.execute,
                restore_scene=bool(scene_decision.get("restore_scene")),
                scene_preview=scene_decision.get("preview"),
            )
            item["resume"] = resume_result
            confirmations.append(item)
            current = resume_result
            break

        if action == "cancel":
            item["cancel"] = orchestrator.cancel_last_interrupted(reason="user_declined_resume")
            confirmations.append(item)
            current = item
            break

        heard = answer.get("heard") if isinstance(answer.get("heard"), dict) else {}
        command_decision = heard.get("command_decision") if isinstance(heard.get("command_decision"), dict) else None
        if command_decision is None:
            command_decision = orchestrator.planner._local_fallback_plan(answer_text)
            command_decision.update(
                {
                    "user_text": answer_text,
                    "asr_text": answer_text,
                    "authoritative_user_text": True,
                    "recovered_from_resume_asr": True,
                }
            )
        command_result = orchestrator.handle_voice_decision(
            command_decision,
            execute=args.execute,
            dry_run=not args.execute,
            wakeup_event=None,
            enqueue=True,
        )
        command_result = _complete_followup_flow(orchestrator, command_result, args)
        item["command_result"] = command_result
        confirmations.append(item)
        current = command_result
        break

    if confirmations:
        voice_result["resume_confirmations"] = confirmations
    return voice_result


def _direct_interrupted_control_result(
    orchestrator: RobotOrchestrator,
    decision: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    if orchestrator.store.peek_interrupted() is None:
        return None
    text = str(decision.get("asr_text") or decision.get("user_text") or "").strip()
    cleaned = re.sub(r"[\s，。,.？！!?、；;：:]+", "", text)
    if not cleaned:
        return None
    resume = (
        "恢复" in cleaned
        or cleaned in {"继续", "接着", "继续刚才", "接着刚才", "继续刚才的任务", "接着刚才的任务"}
    )
    cancel = (
        cleaned in {"取消", "不用了", "不要了", "不用继续", "不要继续"}
        or "取消刚才" in cleaned
        or "不恢复" in cleaned
    )
    if not resume and not cancel:
        return None
    if cancel:
        return {
            "ok": True,
            "interrupted_control": "cancel",
            "user_text": text,
            "cancel": orchestrator.cancel_last_interrupted(reason="explicit_voice_cancel"),
        }
    scene_preview = _speak_resume_ack_and_preview(orchestrator)
    scene_decision = _scene_restore_decision(orchestrator, args, preview=scene_preview)
    resume_result = orchestrator.resume_last_interrupted(
        execute=args.execute,
        dry_run=not args.execute,
        restore_scene=bool(scene_decision.get("restore_scene")),
        scene_preview=scene_decision.get("preview"),
    )
    return {
        "ok": bool(resume_result.get("ok", True)),
        "interrupted_control": "resume",
        "user_text": text,
        "scene_restore": scene_decision,
        "resume": resume_result,
    }


def _run_wakeup_session(
    orchestrator: RobotOrchestrator,
    event: Any,
    interrupt_result: dict[str, Any],
    args: argparse.Namespace,
    *,
    speak_wake_reply: bool = True,
) -> dict[str, Any]:
    session_started = time.monotonic()
    stage_marks: dict[str, float] = {"session_started": session_started}
    if speak_wake_reply:
        started = time.time()
        orchestrator.store.append_event("wake_reply_started", {"wakeup_event_id": getattr(event, "event_id", None)})
        orchestrator.audio.speak_wake_reply(args.wake_reply)
        orchestrator.store.append_event(
            "wake_reply_finished",
            {"wakeup_event_id": getattr(event, "event_id", None), "elapsed_seconds": round(time.time() - started, 4)},
        )
    stage_marks["wake_reply_finished"] = time.monotonic()
    try:
        heard = _listen_heard_with_retries(orchestrator, args, wakeup_event=event)
        stage_marks["command_decision_finished"] = time.monotonic()
        neutralize_result = orchestrator.neutralize_interrupted_task_for_new_session(
            interrupt_result,
            dry_run=not args.execute,
            fast_snapshot=bool(interrupt_result.get("interrupted")),
        )
        stage_marks["neutralization_finished"] = time.monotonic()
        if not neutralize_result.get("ok", True):
            voice_result = {
                "ok": False,
                "error": "interrupt_neutralization_failed",
                "heard": heard,
                "neutralize": neutralize_result,
            }
            orchestrator.audio.speak_text("我没能安全恢复设备状态，所以没有执行新的动作。")
        elif _heard_is_empty_or_noop_command(heard):
            voice_result = _empty_wakeup_voice_result(orchestrator, heard, interrupt_result, event)
        else:
            if "decision" in heard:
                voice_result = _direct_interrupted_control_result(orchestrator, heard["decision"], args)
                if voice_result is None:
                    voice_result = orchestrator.handle_voice_decision(heard["decision"], execute=args.execute, dry_run=not args.execute, wakeup_event=event)
            else:
                voice_result = orchestrator.handle_heard_voice(heard, execute=args.execute, dry_run=not args.execute, wakeup_event=event)
            voice_result = _complete_followup_flow(orchestrator, voice_result, args)
            voice_result = _complete_resume_confirmation_flow(orchestrator, voice_result, args)
    except Exception as exc:
        neutralize_result = {"ok": False, "error": "not_run"}
        voice_result = {"ok": False, "error": str(exc)}
        orchestrator.audio.speak_text("这次没有处理成功，刚才的内容已经保留了。")
    stage_marks["voice_pipeline_finished"] = time.monotonic()
    origin = stage_marks["session_started"]
    offsets_ms = {
        key: round((value - origin) * 1000.0, 3)
        for key, value in stage_marks.items()
    }
    decision = heard.get("decision") if isinstance(locals().get("heard"), dict) else None
    trace_id = decision.get("trace_id") if isinstance(decision, dict) else None
    orchestrator.store.append_event(
        "wakeup_session_stage_timing",
        {
            "wakeup_event_id": getattr(event, "event_id", None),
            "trace_id": trace_id,
            "session_id": voice_result.get("session") if isinstance(voice_result, dict) else None,
            "offsets_ms_from_session_start": offsets_ms,
            "durations_ms": {
                "wake_reply": round((stage_marks["wake_reply_finished"] - session_started) * 1000.0, 3),
                "command_decision": round(
                    (stage_marks.get("command_decision_finished", stage_marks["wake_reply_finished"]) - stage_marks["wake_reply_finished"]) * 1000.0,
                    3,
                ),
                "post_decision_pipeline": round(
                    (stage_marks["voice_pipeline_finished"] - stage_marks.get("command_decision_finished", stage_marks["wake_reply_finished"])) * 1000.0,
                    3,
                ),
                "session_total": round((stage_marks["voice_pipeline_finished"] - session_started) * 1000.0, 3),
            },
        },
    )
    return {"wakeup_event": event.__dict__, "interrupt": interrupt_result, "neutralize": neutralize_result, "voice_result": voice_result}


def _should_speak_wake_reply(config: dict[str, Any], args: argparse.Namespace) -> bool:
    explicit = getattr(args, "speak_wake_reply", None)
    if explicit is not None:
        return bool(explicit)
    return bool(config.get("wakeup", {}).get("speak_reply", False))


def _find_existing_daemon_pids() -> list[int]:
    current = os.getpid()
    pids: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    for proc_dir in proc_root.iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            pid = int(proc_dir.name)
        except ValueError:
            continue
        if pid == current:
            continue
        with contextlib.suppress(Exception):
            raw = (proc_dir / "cmdline").read_bytes()
            parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
            if len(parts) >= 4 and Path(parts[0]).name.startswith("python") and parts[1:4] == ["-m", "new_project.cli", "daemon"]:
                pids.append(pid)
                continue
            command = " ".join(parts)
            if Path(parts[0]).name.startswith("python") and " -m new_project.cli daemon" in f" {command} ":
                pids.append(pid)
                continue
            script = parts[1] if len(parts) > 1 else ""
            if Path(parts[0]).name.startswith("python") and script.endswith("/new_project/cli.py") and "daemon" in parts[2:]:
                pids.append(pid)
                continue
    return sorted(set(pids))


def _child_pids(pid: int) -> list[int]:
    try:
        completed = subprocess.run(["pgrep", "-P", str(pid)], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        return []
    result = []
    for line in completed.stdout.splitlines():
        with contextlib.suppress(ValueError):
            result.append(int(line.strip()))
    return result


def _terminate_existing_daemons(pids: list[int], timeout: float = 3.0) -> dict[str, Any]:
    targets: list[int] = []
    for pid in pids:
        targets.extend(_child_pids(pid))
        targets.append(pid)
    targets = sorted(set(targets), reverse=True)
    for pid in targets:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.2, timeout)
    alive = set(targets)
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            if not Path(f"/proc/{pid}").exists():
                alive.discard(pid)
        if alive:
            time.sleep(0.1)
    for pid in list(alive):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    final_alive = [pid for pid in alive if Path(f"/proc/{pid}").exists()]
    return {"terminated": targets, "alive": final_alive, "ok": not final_alive}


def _wait_previous_session_or_force_stop(
    orchestrator: RobotOrchestrator,
    previous_future: concurrent.futures.Future,
    args: argparse.Namespace,
    event: Any,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    previous_result: dict[str, Any] | None = None
    previous_error: str | None = None
    force_stop_result: dict[str, Any] | None = None
    wait_started = time.time()
    try:
        previous_result = previous_future.result(timeout=float(args.interrupt_wait_seconds))
    except concurrent.futures.TimeoutError:
        previous_error = "previous_session_still_stopping"
    except Exception as exc:
        previous_error = str(exc)

    orchestrator.store.append_event(
        "previous_task_stopped",
        {
            "wakeup_event_id": getattr(event, "event_id", None),
            "elapsed_seconds": round(time.time() - wait_started, 4),
            "ok": previous_error is None,
            "error": previous_error,
        },
    )
    if previous_error != "previous_session_still_stopping":
        return previous_result, previous_error, force_stop_result

    force_started = time.time()
    try:
        force_stop_result = orchestrator.executor.interrupt_current()
    except Exception as exc:
        force_stop_result = {"ok": False, "error": str(exc)}
    force_wait_seconds = min(3.0, max(0.8, float(getattr(args, "interrupt_wait_seconds", 3.0)) / 2.0))
    try:
        previous_result = previous_future.result(timeout=force_wait_seconds)
        previous_error = None
    except concurrent.futures.TimeoutError:
        previous_error = "previous_session_still_stopping_after_force_stop"
    except Exception as exc:
        previous_error = str(exc)

    orchestrator.store.append_event(
        "previous_task_force_stop_attempted",
        {
            "wakeup_event_id": getattr(event, "event_id", None),
            "elapsed_seconds": round(time.time() - force_started, 4),
            "wait_seconds": force_wait_seconds,
            "ok": previous_error is None,
            "error": previous_error,
            "force_stop_result": force_stop_result,
        },
    )
    return previous_result, previous_error, force_stop_result


def _finish_barge_in_transition(
    orchestrator: RobotOrchestrator,
    event: Any,
    interrupt_result: dict[str, Any],
    previous_future: concurrent.futures.Future,
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, dict[str, Any]]:
    started = time.time()
    previous_result, previous_error, force_stop_result = _wait_previous_session_or_force_stop(
        orchestrator,
        previous_future,
        args,
        event,
    )
    if previous_error is None:
        neutralize_result = orchestrator.neutralize_interrupted_task_for_new_session(
            interrupt_result,
            dry_run=not args.execute,
            fast_snapshot=True,
        )
    else:
        neutralize_result = {"ok": False, "neutralized": False, "error": previous_error}
    orchestrator.store.append_event(
        "barge_in_transition_finished",
        {
            "wakeup_event_id": getattr(event, "event_id", None),
            "task_group_id": interrupt_result.get("task_group_id"),
            "elapsed_seconds": round(time.time() - started, 4),
            "ok": previous_error is None and neutralize_result.get("ok", True),
            "previous_error": previous_error,
        },
    )
    return previous_result, previous_error, force_stop_result, neutralize_result


def _run_barge_in_wakeup_session(
    orchestrator: RobotOrchestrator,
    event: Any,
    interrupt_result: dict[str, Any],
    previous_future: concurrent.futures.Future,
    args: argparse.Namespace,
) -> dict[str, Any]:
    # Stop/neutralize the old TaskGroup while the new CommandSession is being
    # recorded. These paths use disjoint resources, so the user no longer pays
    # their latency one after another.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as transition_pool:
        transition_future = transition_pool.submit(
            _finish_barge_in_transition,
            orchestrator,
            event,
            interrupt_result,
            previous_future,
            args,
        )
        heard = _listen_heard_with_retries(orchestrator, args, wakeup_event=event)
        previous_result, previous_error, force_stop_result, neutralize_result = transition_future.result()
    if previous_error is not None:
        voice_result = {"ok": False, "error": previous_error, "heard": heard, "force_stop_result": force_stop_result}
        orchestrator.audio.speak_text("刚才的动作还没有完全停下，所以新的安排暂时没有执行。")
    else:
        if not neutralize_result.get("ok", True):
            voice_result = {
                "ok": False,
                "error": "interrupt_neutralization_failed",
                "heard": heard,
                "neutralize": neutralize_result,
            }
            orchestrator.audio.speak_text("我没能安全恢复设备状态，所以没有执行新的动作。")
        elif _heard_is_empty_or_noop_command(heard):
            voice_result = _empty_wakeup_voice_result(orchestrator, heard, interrupt_result, event)
        else:
            if "decision" in heard:
                voice_result = _direct_interrupted_control_result(orchestrator, heard["decision"], args)
                if voice_result is None:
                    voice_result = orchestrator.handle_voice_decision(heard["decision"], execute=args.execute, dry_run=not args.execute, wakeup_event=event)
            else:
                voice_result = orchestrator.handle_heard_voice(heard, execute=args.execute, dry_run=not args.execute, wakeup_event=event)
            voice_result = _complete_followup_flow(orchestrator, voice_result, args)
            voice_result = _complete_resume_confirmation_flow(orchestrator, voice_result, args)
    return {
        "wakeup_event": event.__dict__,
        "interrupt": interrupt_result,
        "neutralize": neutralize_result,
        "previous_session": previous_result,
        "previous_error": previous_error,
        "voice_result": voice_result,
    }


def _print_finished_future(future: concurrent.futures.Future | None) -> bool:
    if future is None or not future.done():
        return False
    try:
        print_json(future.result())
    except Exception as exc:
        print_json({"ok": False, "error": str(exc)})
    return True


def _drain_pending_wakeups(listener: WakeupListener, orchestrator: RobotOrchestrator, reason: str) -> int:
    drain = getattr(listener, "drain", None)
    if not callable(drain):
        return 0
    events = drain()
    if events:
        orchestrator.store.append_event(
            "wakeup_events_dropped",
            {"reason": reason, "count": len(events), "event_ids": [event.event_id for event in events]},
        )
    return len(events)


class _DaemonShutdownRequested(Exception):
    def __init__(self, signum: int | None = None):
        self.signum = signum
        super().__init__(f"daemon shutdown requested: {signum}")


def _shutdown_daemon_runtime(
    orchestrator: RobotOrchestrator,
    listener: WakeupListener,
    pool: concurrent.futures.ThreadPoolExecutor,
    active_future: concurrent.futures.Future | None,
    planner_config: dict[str, Any],
    *,
    reason: str,
) -> None:
    try:
        listener.stop()
    except (Exception, KeyboardInterrupt):
        pass
    try:
        orchestrator.audio.stop_speech()
    except (Exception, KeyboardInterrupt):
        pass

    interrupt_result: dict[str, Any] = {"ok": True, "interrupted": False, "reason": "no_active_task"}
    try:
        state = orchestrator.store.load_state()
        active_id = state.get("active_task_group_id")
    except Exception:
        active_id = None
    if active_future is not None and not active_future.done():
        try:
            interrupt_result = orchestrator.executor.interrupt_current()
            if active_id and interrupt_result.get("interrupted") and not interrupt_result.get("task_group_id"):
                interrupt_result["task_group_id"] = active_id
        except Exception as exc:
            interrupt_result = {"ok": False, "interrupted": False, "error": str(exc)}
    elif active_id:
        interrupt_result = {"ok": True, "interrupted": True, "task_group_id": active_id, "reason": "active_task_without_future"}

    if interrupt_result.get("interrupted") and interrupt_result.get("task_group_id"):
        try:
            orchestrator.neutralize_interrupted_task_for_new_session(interrupt_result, dry_run=False)
        except Exception as exc:
            orchestrator.store.append_event("daemon_shutdown_neutralize_failed", {"reason": reason, "error": str(exc)})

    if planner_config.get("clear_interrupted_on_daemon_shutdown", True):
        orchestrator.store.clear_runtime_interrupt_state(
            reason=reason,
            clear_active=True,
            clear_interrupted=True,
            clear_current_session=True,
        )
    sanitized = {key: value for key, value in interrupt_result.items() if key != "process"}
    orchestrator.store.append_event("daemon_shutdown", {"reason": reason, "interrupt": sanitized})
    try:
        orchestrator.close()
    except (Exception, KeyboardInterrupt):
        pass
    pool.shutdown(wait=False, cancel_futures=True)


def _load_ros_environment_once(config: dict[str, Any]) -> dict[str, Any]:
    if not bool(config.get("daemon", {}).get("load_ros_environment_on_start", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}
    if os.environ.get("ROBOT_ROS_ENV_READY") == "1":
        return {"ok": True, "skipped": True, "reason": "already_loaded"}
    setup_files = [str(path) for path in config.get("paths", {}).get("ros_setup_files", []) if Path(path).exists()]
    if not setup_files:
        return {"ok": False, "skipped": True, "reason": "no_setup_files"}
    script = 'for setup_file in "$@"; do source "$setup_file"; done; env -0'
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["bash", "-c", script, "robot-ros-env", *setup_files],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 4)}
    if completed.returncode != 0:
        return {
            "ok": False,
            "returncode": completed.returncode,
            "error": completed.stderr.decode("utf-8", "replace")[-500:],
            "elapsed_seconds": round(time.monotonic() - started, 4),
        }
    loaded = 0
    for entry in completed.stdout.split(b"\0"):
        if b"=" not in entry:
            continue
        raw_key, raw_value = entry.split(b"=", 1)
        key = raw_key.decode("utf-8", "replace")
        if not key or key in {"_", "SHLVL"}:
            continue
        os.environ[key] = raw_value.decode("utf-8", "replace")
        loaded += 1
    os.environ["ROBOT_ROS_ENV_READY"] = "1"
    return {
        "ok": True,
        "setup_files": setup_files,
        "variables_loaded": loaded,
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }


def cmd_daemon(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    existing_daemons = _find_existing_daemon_pids()
    replace_arg = getattr(args, "replace_existing_daemon", None)
    replace_existing = bool(config.get("daemon", {}).get("replace_existing_on_start", True) if replace_arg is None else replace_arg)
    existing_cleanup: dict[str, Any] = {"ok": True, "skipped": True, "pids": existing_daemons}
    if existing_daemons:
        if not replace_existing:
            print_json({"ok": False, "error": "daemon_already_running", "pids": existing_daemons})
            return 2
        existing_cleanup = _terminate_existing_daemons(existing_daemons)
    daemon_lock = DaemonInstanceLock(config)
    locked, locked_pid = daemon_lock.acquire()
    if not locked:
        print_json({"ok": False, "error": "daemon_lock_held", "pid": locked_pid})
        return 2
    ros_environment = _load_ros_environment_once(config)
    orchestrator = RobotOrchestrator(config)
    planner_config = config.get("planner", {})
    startup_clear = {
        "ok": True,
        "skipped": True,
        "reason": "keep_interrupted_on_start" if args.keep_interrupted_on_start else "daemon_start_clear_disabled",
    }
    if not args.keep_interrupted_on_start and planner_config.get("clear_interrupted_on_daemon_start", True):
        startup_clear = orchestrator.store.clear_runtime_interrupt_state(
            reason="daemon_start_clear_interrupt",
            clear_active=True,
            clear_interrupted=True,
            clear_current_session=True,
        )
    startup_queue_clear = orchestrator.store.clear_task_queue(reason="daemon_start_clear_task_queue")
    startup_followup_clear = orchestrator.store.clear_waiting_followups(reason="daemon_start_clear_waiting_followups")
    startup_recovery = orchestrator.recover_persisted_runtime_state()
    doubao_warmup = {"ok": True, "skipped": True, "reason": "doubao_realtime_not_configured"}
    if getattr(orchestrator, "realtime_voice", None) is not None and bool(config.get("voice_decision", {}).get("warmup_on_start", False)):
        doubao_warmup = orchestrator.realtime_voice.warmup()
    elif getattr(orchestrator, "realtime_voice", None) is not None:
        doubao_warmup = {"ok": True, "backend": "doubao_realtime", "skipped": True, "reason": "warmup_disabled"}
    listener = WakeupListener(config)
    listener.start()
    reminder_config = config.get("reminders", {})
    reminder_runtime_dir = reminder_config.get("runtime_dir", "/home/test/single_function/runtime/agent_0625")
    reminder_store = ReminderStore(reminder_runtime_dir)
    reminder_audio = AudioManager(config, orchestrator.resources)
    self_echo_ignore_until = 0.0

    def _speak_due_reminder(item: dict[str, Any]) -> bool:
        nonlocal self_echo_ignore_until
        content = str(item.get("content") or "提醒").strip()
        message = f"提醒时间到了，{content}"
        orchestrator.store.append_event(
            "reminder_firing",
            {"reminder_id": item.get("id"), "content": content, "due_at": item.get("due_at")},
        )
        try:
            # A due reminder may arrive while a voice turn is recording. Taking
            # the mic lock first prevents the reminder audio from being captured
            # as a new user command; speak_text then serializes on the speaker.
            with orchestrator.resources.acquire(["mic"]):
                if config.get("audio", {}).get("voice_io_backend") in {"doubao_realtime", "wake_skill_agent"}:
                    print(f"ROBOT_SAY:{message}", flush=True)
                    spoken = bool(reminder_audio.speak_text_wake_skill_agent(message))
                else:
                    spoken = bool(reminder_audio.speak_text(message))
            self_echo_ignore_until = time.monotonic() + float(reminder_config.get("self_echo_suppression_seconds", 2.5))
            orchestrator.store.append_event(
                "reminder_fired" if spoken else "reminder_tts_failed",
                {"reminder_id": item.get("id"), "content": content},
            )
            return spoken
        except Exception as exc:
            orchestrator.store.append_event(
                "reminder_tts_failed",
                {"reminder_id": item.get("id"), "content": content, "error": str(exc)},
            )
            return False

    reminder_scheduler = ReminderScheduler(
        reminder_store,
        _speak_due_reminder,
        poll_seconds=float(reminder_config.get("poll_seconds", 0.2)),
    )
    reminder_scheduler.start()
    print_json(
        {
            "ok": True,
            "daemon": "started",
            "topic": config["wakeup"]["topic"],
            "startup_clear": startup_clear,
            "startup_queue_clear": startup_queue_clear,
            "startup_followup_clear": startup_followup_clear,
            "startup_recovery": startup_recovery,
            "doubao_warmup": doubao_warmup,
            "existing_daemon_cleanup": existing_cleanup,
            "ros_environment": ros_environment,
            "reminder_scheduler": {"ok": True, "runtime_dir": str(reminder_runtime_dir)},
        }
    )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    active_future: concurrent.futures.Future | None = None
    last_wakeup_ts = 0.0

    def _request_shutdown(signum: int, frame: Any) -> None:
        raise _DaemonShutdownRequested(signum)

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGTERM, getattr(signal, "SIGTSTP", None)):
        if signum is None:
            continue
        try:
            previous_handlers[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, _request_shutdown)
        except Exception:
            pass
    try:
        while True:
            if _print_finished_future(active_future):
                active_future = None
            event = listener.wait(timeout=0.2)
            if event is None:
                continue
            now = time.monotonic()
            if now < self_echo_ignore_until:
                orchestrator.store.append_event(
                    "wakeup_ignored_self_echo",
                    {
                        "wakeup_event": event.__dict__,
                        "remaining_seconds": round(self_echo_ignore_until - now, 4),
                    },
                )
                _drain_pending_wakeups(listener, orchestrator, "self_echo_suppression_window")
                continue
            if now - last_wakeup_ts < float(args.wakeup_debounce_seconds):
                continue
            last_wakeup_ts = now
            self_echo_ignore_until = now + max(
                float(args.wakeup_debounce_seconds),
                float(args.wakeup_self_echo_suppression_seconds),
            )
            orchestrator.store.append_event("wakeup_received", {"wakeup_event": event.__dict__})
            _drain_pending_wakeups(listener, orchestrator, "duplicate_wakeup_after_accept")

            if _print_finished_future(active_future):
                active_future = None

            state = orchestrator.store.load_state()
            active_task_group_id = state.get("active_task_group_id")

            if active_future is not None and not active_future.done():
                if active_task_group_id:
                    interrupt_result = orchestrator.begin_interrupt_active_task_fast(event)
                    orchestrator.audio.stop_speech()
                    if _should_speak_wake_reply(config, args):
                        started = time.time()
                        orchestrator.store.append_event("wake_reply_started", {"wakeup_event_id": event.event_id, "barge_in": True})
                        orchestrator.audio.speak_wake_reply(args.wake_reply)
                        orchestrator.store.append_event(
                            "wake_reply_finished",
                            {"wakeup_event_id": event.event_id, "barge_in": True, "elapsed_seconds": round(time.time() - started, 4)},
                        )
                    previous_future = active_future
                    active_future = pool.submit(
                        _run_barge_in_wakeup_session,
                        orchestrator,
                        event,
                        interrupt_result,
                        previous_future,
                        args,
                    )
                    _drain_pending_wakeups(listener, orchestrator, "duplicate_wakeup_during_interrupt")
                    continue
                else:
                    orchestrator.store.append_event("wakeup_ignored_busy", {"wakeup_event": event.__dict__})
                    _drain_pending_wakeups(listener, orchestrator, "busy_without_active_task")
                    continue

            interrupt_result = orchestrator.begin_interrupt_active_task_fast(event)
            active_future = pool.submit(
                _run_wakeup_session,
                orchestrator,
                event,
                interrupt_result,
                args,
                speak_wake_reply=_should_speak_wake_reply(config, args),
            )
    except KeyboardInterrupt:
        reminder_scheduler.stop()
        _shutdown_daemon_runtime(
            orchestrator,
            listener,
            pool,
            active_future,
            planner_config,
            reason="daemon_shutdown_keyboard_interrupt",
        )
        daemon_lock.release()
        return 0
    except _DaemonShutdownRequested as exc:
        reminder_scheduler.stop()
        reason = f"daemon_shutdown_signal_{exc.signum}"
        _shutdown_daemon_runtime(orchestrator, listener, pool, active_future, planner_config, reason=reason)
        daemon_lock.release()
        if exc.signum == getattr(signal, "SIGTSTP", None):
            previous = previous_handlers.get(int(exc.signum))
            if previous not in (None, signal.SIG_IGN, signal.SIG_DFL):
                try:
                    signal.signal(exc.signum, previous)
                except Exception:
                    pass
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot command-session/task-group runtime")
    parser.add_argument("--config", default=None, help="config path, default config/hardware.json")
    sub = parser.add_subparsers(dest="command", required=True)

    self_check = sub.add_parser("self-check")
    self_check.add_argument("--audio", action="store_true", help="record then play a short sample")
    self_check.add_argument("--audio-seconds", type=float, default=1.0)
    self_check.add_argument("--camera", action="store_true", help="capture one image from each camera")
    self_check.set_defaults(func=cmd_self_check)

    taskgroup_self_check = sub.add_parser("taskgroup-self-check")
    taskgroup_self_check.add_argument("--verbose", action="store_true", help="include every scenario check detail")
    taskgroup_self_check.set_defaults(func=cmd_taskgroup_self_check)

    camera_config = sub.add_parser("camera-config")
    camera_config.add_argument("--front-device")
    camera_config.add_argument("--back-device")
    camera_config.add_argument("--front-width", type=int)
    camera_config.add_argument("--front-height", type=int)
    camera_config.add_argument("--back-width", type=int)
    camera_config.add_argument("--back-height", type=int)
    camera_config.add_argument("--require-device-exists", action="store_true")
    camera_config.add_argument("--probe", action="store_true", help="run v4l2-ctl --all against configured/proposed camera devices")
    camera_config.add_argument("--probe-frame", action="store_true", help="also try to read one frame with OpenCV; use only when safe to access the device")
    camera_config.add_argument("--probe-timeout", type=float, default=5.0)
    camera_config.add_argument("--write", action="store_true", help="persist changes to config; default is dry-run")
    camera_config.set_defaults(func=cmd_camera_config)

    plan = sub.add_parser("plan")
    plan.add_argument("--text", required=True)
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run")
    run.add_argument("--text", required=True)
    run.add_argument("--execute", action="store_true", help="really execute hardware skills; default is plan+queue dry run")
    run.add_argument("--enqueue", action="store_true", help="save planned task groups into the persistent queue without executing")
    run.set_defaults(func=cmd_run)

    voice = sub.add_parser("voice-once")
    voice.add_argument("--seconds", type=float, default=None)
    voice.add_argument("--execute", action="store_true")
    voice.set_defaults(func=cmd_voice)

    resume = sub.add_parser("resume")
    resume.add_argument("--execute", action="store_true")
    resume.set_defaults(func=cmd_resume)

    answer = sub.add_parser("answer")
    answer.add_argument("--task-group-id", required=True)
    answer.add_argument("--text", required=True)
    answer.add_argument("--execute", action="store_true")
    answer.add_argument("--enqueue", action="store_true")
    answer.set_defaults(func=cmd_answer)

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--seconds", type=float, default=None)
    daemon.add_argument("--asr-retries", type=int, default=1)
    daemon.add_argument("--max-followups", type=int, default=3, help="consecutive no-progress follow-up limit")
    daemon.add_argument("--max-followup-total-turns", type=int, default=12)
    daemon.add_argument("--max-followup-overlay-turns", type=int, default=4)
    daemon.add_argument("--max-followup-elapsed-seconds", type=float, default=240.0)
    daemon.add_argument("--resume-confirm-seconds", type=float, default=4.0)
    daemon.add_argument("--resume-confirm-retries", type=int, default=1)
    daemon.add_argument("--max-resume-rounds", type=int, default=1)
    daemon.add_argument("--interrupt-wait-seconds", type=float, default=3.0)
    daemon.add_argument("--wakeup-debounce-seconds", type=float, default=0.8)
    daemon.add_argument("--wakeup-self-echo-suppression-seconds", type=float, default=2.5)
    daemon.add_argument("--post-wake-listen-delay-seconds", type=float, default=0.08)
    daemon.add_argument("--post-speech-listen-delay-seconds", type=float, default=0.25)
    daemon.add_argument("--followup-post-speech-listen-delay-seconds", type=float, default=0.25)
    daemon.add_argument("--followup-echo-retries", type=int, default=1)
    daemon.add_argument("--speak-wake-reply", dest="speak_wake_reply", action="store_true", help="let new_project say the wake reply")
    daemon.add_argument("--no-wake-reply", dest="speak_wake_reply", action="store_false", help="do not let new_project say the wake reply")
    daemon.set_defaults(speak_wake_reply=None)
    daemon.add_argument("--replace-existing-daemon", action="store_true", help="terminate older new_project daemon instances before starting")
    daemon.add_argument("--no-replace-existing-daemon", dest="replace_existing_daemon", action="store_false", help="fail if another daemon is already running")
    daemon.set_defaults(replace_existing_daemon=None)
    daemon.add_argument("--keep-interrupted-on-start", action="store_true", help="keep persisted active/interrupted task state when daemon starts")
    daemon.add_argument("--execute", action="store_true")
    daemon.add_argument("--wake-reply", default="我在")
    daemon.set_defaults(func=cmd_daemon)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
