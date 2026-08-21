from __future__ import annotations

import json
import contextlib
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import runtime_dir
from .models import TaskStep


FITNESS_SKILLS = {"squat", "push_up", "pull_up"}
BASE_MOTION_SKILLS = {"move_forward", "move_backward", "move_left", "move_right", "person_tracking", "pet_tracking"}
CAMERA_CAPTURE_SKILLS = {"front_camera_capture", "back_camera_capture", "camera_capture"}
CAMERA_RECORD_SKILLS = {"front_camera_record", "back_camera_record", "camera_record"}
PERIPHERAL_SKILL_TO_NAME = {
    "projector_control": "projector",
    "light_control": "light",
    "fan_control": "fan",
}


class RobotStateCollector:
    """Best-effort robot state snapshot and command-state cache.
    Hardware does not expose every state through one API, so this collector uses
    ROS pose when available and keeps a command-derived cache for head/peripheral
    state that is updated after successful skill execution.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root = runtime_dir(config)
        self.cache_path = self.root / "robot_state_cache.json"
        state_cfg = config.get("robot_state", {})
        self.pose_read_attempts = max(1, int(state_cfg.get("pose_read_attempts", 3)))
        self.pose_read_retry_delay = max(0.0, float(state_cfg.get("pose_read_retry_delay_seconds", 0.25)))
        self.pose_topic_timeout = max(0.2, float(state_cfg.get("pose_topic_timeout_seconds", 1.5)))
        self.pose_command_timeout = max(self.pose_topic_timeout + 0.5, float(state_cfg.get("pose_command_timeout_seconds", 3.0)))
        self.refresh_pose_after_base_motion = bool(state_cfg.get("refresh_pose_after_base_motion", False))

    def snapshot(self, active_task_group_id: str | None = None, active_step: TaskStep | None = None, fast: bool = False) -> dict[str, Any]:
        cache = self._load_cache()
        if fast:
            pose = self._cached_pose_for_fast_snapshot(cache)
        else:
            ros_pose = self._read_pose_from_ros()
            if ros_pose.get("valid"):
                cache["pose"] = ros_pose
                cache["updated_at"] = time.time()
                self._save_cache(cache)
                pose = ros_pose
            else:
                pose = ros_pose
        resources = self._resource_snapshot()
        active = self._active_snapshot(active_task_group_id, active_step)
        return {
            "timestamp": time.time(),
            "snapshot_mode": "fast" if fast else "full",
            "active_task_group_id": active_task_group_id,
            "active_step": active_step.__dict__.copy() if active_step else None,
            "active": active,
            "pose": pose,
            "base": cache.get("base", {"state": "unknown"}),
            "head": cache.get("head", {"valid": False, "source": "unknown"}),
            "motors": cache.get("motors", {}),
            "peripherals": cache.get("peripherals", {}),
            "cameras": cache.get("cameras", {}),
            "audio": self._audio_snapshot(resources),
            "navigation": cache.get("navigation", {}),
            "face_db": cache.get("face_db", {}),
            "skills": cache.get("skills", {}),
            "last_effect": cache.get("last_effect"),
            "resources": resources,
            "cache": cache,
        }

    def record_step_effect(self, step: TaskStep, result: dict[str, Any] | None = None) -> None:
        result = result or {}
        if result.get("interrupted") or not result.get("ok"):
            return
        cache = self._load_cache()
        skill = step.skill_name
        args = dict(step.arguments or {})
        now = time.time()

        parsed = self._parsed_result(result)
        if skill == "navigation_goto":
            pose = self._pose_from_navigation_args(args)
            if pose:
                pose.update({"valid": True, "source": "navigation_goto_command", "timestamp": now})
                cache["pose"] = pose
            cache["base"] = {"state": "stopped", "last_command": skill, "timestamp": now}
            cache["navigation"] = {"last_goal": pose or args, "status": parsed.get("status") or "completed", "timestamp": now}
        elif skill in BASE_MOTION_SKILLS:
            cache["base"] = {"state": "stopped", "last_command": skill, "arguments": args, "timestamp": now, "pose_uncertain": True}
            self._refresh_or_invalidate_pose_after_base_motion(cache, skill, now)
        elif skill == "head_control":
            action, angle = self._head_action_angle(args)
            cache["head"] = {
                "valid": True,
                "action": action,
                "angle": angle,
                "source": "head_control_command",
                "timestamp": now,
            }
            motors = dict(cache.get("motors") or {})
            motors["head_stepper"] = {"angle": angle, "timestamp": now}
            cache["motors"] = motors
        elif skill in PERIPHERAL_SKILL_TO_NAME:
            action = args.get("action") or parsed.get("action")
            if skill == "light_control":
                result_payload = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
                power = result_payload.get("power")
                if isinstance(power, bool):
                    action = "on" if power else "off"
                if str(action or "").lower() not in {"on", "off"}:
                    action = None
            if action:
                self._update_peripheral(cache, PERIPHERAL_SKILL_TO_NAME[skill], action, now)
        elif skill in CAMERA_CAPTURE_SKILLS | CAMERA_RECORD_SKILLS:
            self._update_camera_state(cache, skill, args, result, parsed, now)
        elif skill == "face_registration":
            self._update_face_db_state(cache, args, parsed, now)
        elif skill.startswith("reminder_"):
            cache["reminders"] = {"last_command": skill, "arguments": args, "timestamp": now}

        self._record_skill_effect(cache, skill, args, result, parsed, now)
        cache["updated_at"] = now
        self._save_cache(cache)

    def _active_snapshot(self, active_task_group_id: str | None, active_step: TaskStep | None) -> dict[str, Any]:
        if active_step is None:
            return {"task_group_id": active_task_group_id, "skill_name": None, "resources": []}
        return {
            "task_group_id": active_task_group_id,
            "step_id": active_step.step_id,
            "skill_name": active_step.skill_name,
            "arguments": dict(active_step.arguments or {}),
            "resources": list(active_step.resources or []),
            "status": active_step.status,
        }

    def _audio_snapshot(self, resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "recording": "mic" in resources,
            "speaking": "speaker" in resources,
            "mic": "busy" if "mic" in resources else "idle",
            "speaker": "busy" if "speaker" in resources else "idle",
        }

    def _parsed_result(self, result: dict[str, Any]) -> dict[str, Any]:
        parsed = result.get("parsed_json")
        if isinstance(parsed, dict):
            return parsed
        stdout = result.get("stdout")
        if isinstance(stdout, str):
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        return data
        return {}

    def _refresh_or_invalidate_pose_after_base_motion(self, cache: dict[str, Any], skill_name: str, timestamp: float) -> None:
        if self.refresh_pose_after_base_motion:
            fresh_pose = self._read_pose_from_ros()
            if fresh_pose.get("valid"):
                cache["pose"] = fresh_pose
                return
        previous_pose = dict(cache.get("pose") or {})
        previous_pose.pop("previous_pose", None)
        cache["pose"] = {
            "valid": False,
            "source": f"{skill_name}_changed_base_without_pose_feedback",
            "timestamp": timestamp,
            "previous_pose": previous_pose,
        }

    def _cached_pose_for_fast_snapshot(self, cache: dict[str, Any]) -> dict[str, Any]:
        pose = dict(cache.get("pose") or {"valid": False, "source": "fast_cached_unavailable"})
        ts = pose.get("timestamp")
        if isinstance(ts, (int, float)):
            pose["cache_age_sec"] = round(max(0.0, time.time() - float(ts)), 3)
        pose["from_cache"] = True
        return pose

    def _update_camera_state(self, cache: dict[str, Any], skill: str, args: dict[str, Any], result: dict[str, Any], parsed: dict[str, Any], timestamp: float) -> None:
        camera_name = "back" if skill.startswith("back_") else "front" if skill.startswith("front_") else str(args.get("camera_name") or args.get("camera") or "front")
        cameras = dict(cache.get("cameras") or {})
        cameras[camera_name] = {
            "last_action": "record" if skill in CAMERA_RECORD_SKILLS else "capture",
            "output_path": result.get("output_path") or parsed.get("output_path") or parsed.get("path"),
            "arguments": args,
            "timestamp": timestamp,
        }
        cache["cameras"] = cameras

    def _update_face_db_state(self, cache: dict[str, Any], args: dict[str, Any], parsed: dict[str, Any], timestamp: float) -> None:
        name = args.get("name") or parsed.get("name")
        cache["face_db"] = {
            "last_registered_name": name,
            "last_result": parsed,
            "timestamp": timestamp,
        }

    def _record_skill_effect(self, cache: dict[str, Any], skill: str, args: dict[str, Any], result: dict[str, Any], parsed: dict[str, Any], timestamp: float) -> None:
        compact = {
            "ok": bool(result.get("ok")),
            "returncode": result.get("returncode"),
            "output_path": result.get("output_path"),
            "parsed_json": parsed,
            "last_progress": result.get("last_progress"),
        }
        skills = dict(cache.get("skills") or {})
        skills[skill] = {
            "last_arguments": args,
            "last_result": compact,
            "timestamp": timestamp,
        }
        cache["skills"] = skills
        cache["last_effect"] = {"skill_name": skill, "arguments": args, "result": compact, "timestamp": timestamp}

    def _update_peripheral(self, cache: dict[str, Any], name: str, action: Any, timestamp: float) -> None:
        peripherals = dict(cache.get("peripherals") or {})
        peripherals[name] = {"action": action, "source": "skill_command", "timestamp": timestamp}
        cache["peripherals"] = peripherals

    def _head_action_angle(self, args: dict[str, Any]) -> tuple[str, int | None]:
        action = str(args.get("action") or "level").strip().lower()
        explicit = args.get("angle")
        if explicit is not None:
            try:
                return "angle", int(explicit)
            except Exception:
                return action, None
        defaults = self.config.get("robot_state", {}).get("head_angles", {})
        if action == "up":
            return action, int(defaults.get("up", 205))
        if action == "down":
            return action, int(defaults.get("down", 163))
        return "level", int(defaults.get("level", 185))

    def _pose_from_navigation_args(self, args: dict[str, Any]) -> dict[str, Any] | None:
        if args.get("x") is None or args.get("y") is None:
            point = args.get("point") or args.get("destination") or args.get("name")
            if point:
                return self._pose_from_named_point(str(point))
            return None
        try:
            return {
                "frame_id": str(args.get("frame_id") or "map"),
                "x": float(args["x"]),
                "y": float(args["y"]),
                "yaw": float(args.get("yaw") or 0.0),
            }
        except Exception:
            return None

    def _pose_from_named_point(self, point: str) -> dict[str, Any] | None:
        configured = self.config.get("robot_state", {}).get("navigation_points_path")
        candidates = [
            Path(configured) if configured else None,
            Path(self.config.get("paths", {}).get("single_function_dir", "/home/test/qwen_single_function")) / "points" / "named_points.json",
            Path("/home/test/qwen_single_function/points/named_points.json"),
        ]
        for path in [item for item in candidates if item is not None]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            points = data.get("points") if isinstance(data.get("points"), dict) else data
            raw = points.get(point) if isinstance(points, dict) else None
            if not isinstance(raw, dict):
                continue
            try:
                return {
                    "frame_id": str(raw.get("frame_id") or "map"),
                    "x": float(raw["x"]),
                    "y": float(raw["y"]),
                    "yaw": float(raw.get("yaw") or 0.0),
                    "point": point,
                }
            except Exception:
                continue
        return None

    def _resource_snapshot(self) -> dict[str, Any]:
        lock_dir = self.root / "locks"
        items: dict[str, Any] = {}
        if not lock_dir.exists():
            return items
        for path in sorted(lock_dir.glob("*.lock")):
            try:
                stat = path.stat()
                if not self._lock_is_held(path):
                    continue
                items[path.stem] = {"path": str(path), "mtime": stat.st_mtime, "state": "busy"}
            except OSError:
                continue
        return items

    def _lock_is_held(self, path: Path) -> bool:
        try:
            import fcntl
        except ImportError:
            return True
        try:
            with path.open("a", encoding="utf-8") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                finally:
                    with contextlib.suppress(Exception):
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return False

    def _read_pose_from_ros(self) -> dict[str, Any]:
        setup = "; ".join(f"[ -f {shlex.quote(str(path))} ] && source {shlex.quote(str(path))} || true" for path in self.config.get("paths", {}).get("ros_setup_files", []))
        prefix = f"{setup}; " if setup else ""
        direct_ros = os.environ.get("ROBOT_ROS_ENV_READY") == "1" and shutil.which("ros2") is not None
        topics = ("/amcl_pose", "/odom")
        attempts: list[dict[str, Any]] = []
        latest_pose: dict[str, Any] | None = None
        for attempt in range(1, self.pose_read_attempts + 1):
            for topic in topics:
                shell_command = f"{prefix}timeout {self.pose_topic_timeout:.3f} ros2 topic echo --once {topic}"
                command = ["ros2", "topic", "echo", "--once", topic] if direct_ros else ["bash", "-lc", shell_command]
                started = time.time()
                try:
                    completed = subprocess.run(
                        command,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=min(self.pose_command_timeout, self.pose_topic_timeout + 0.3) if direct_ros else self.pose_command_timeout,
                        check=False,
                    )
                    elapsed = round(time.time() - started, 3)
                    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
                    pose = self._parse_pose_text(output)
                    attempts.append(
                        {
                            "attempt": attempt,
                            "topic": topic,
                            "returncode": completed.returncode,
                            "elapsed_sec": elapsed,
                            "ok": bool(pose),
                            "stderr_tail": (completed.stderr or "")[-200:],
                        }
                    )
                    if pose:
                        pose["source"] = topic
                        pose["valid"] = True
                        pose["timestamp"] = time.time()
                        latest_pose = pose
                        break
                except subprocess.TimeoutExpired as exc:
                    attempts.append(
                        {
                            "attempt": attempt,
                            "topic": topic,
                            "timeout": True,
                            "elapsed_sec": round(time.time() - started, 3),
                            "ok": False,
                            "error": str(exc)[-200:],
                        }
                    )
                except Exception as exc:
                    attempts.append(
                        {
                            "attempt": attempt,
                            "topic": topic,
                            "elapsed_sec": round(time.time() - started, 3),
                            "ok": False,
                            "error": str(exc)[-200:],
                        }
                    )
            if latest_pose:
                break
            if attempt < self.pose_read_attempts and self.pose_read_retry_delay:
                time.sleep(self.pose_read_retry_delay)
        if latest_pose:
            latest_pose["read_attempts"] = attempts
            return latest_pose
        return {"valid": False, "source": "ros_unavailable", "timestamp": time.time(), "read_attempts": attempts}

    def _parse_pose_text(self, text: str) -> dict[str, Any] | None:
        def number_after(label: str) -> float | None:
            match = re.search(rf"{re.escape(label)}:\s*(-?\d+(?:\.\d+)?)", text)
            if not match:
                return None
            try:
                return float(match.group(1))
            except Exception:
                return None

        xs = [float(item) for item in re.findall(r"\bx:\s*(-?\d+(?:\.\d+)?)", text)]
        ys = [float(item) for item in re.findall(r"\by:\s*(-?\d+(?:\.\d+)?)", text)]
        if not xs or not ys:
            return None
        x = xs[0]
        y = ys[0]
        z_values = [float(item) for item in re.findall(r"\bz:\s*(-?\d+(?:\.\d+)?)", text)]
        w_values = [float(item) for item in re.findall(r"\bw:\s*(-?\d+(?:\.\d+)?)", text)]
        qz = z_values[-1] if z_values else 0.0
        qw = w_values[-1] if w_values else 1.0
        yaw = math.atan2(2.0 * qw * qz, 1.0 - 2.0 * qz * qz)
        frame_match = re.search(r"frame_id:\s*'?\"?([^'\"\n]+)", text)
        return {"frame_id": frame_match.group(1).strip() if frame_match else "map", "x": x, "y": y, "yaw": yaw}

    def _load_cache(self) -> dict[str, Any]:
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_cache(self, data: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.cache_path)


class RobotStateRestorer:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        state_cfg = config.get("robot_state", {})
        self.pose_distance_threshold = float(state_cfg.get("pose_distance_threshold_m", 0.35))
        self.yaw_threshold = float(state_cfg.get("yaw_threshold_rad", 0.45))
        self.head_angle_threshold = int(state_cfg.get("head_angle_threshold", 3))
        self.resume_head_wait_seconds = max(0.0, float(state_cfg.get("resume_head_wait_seconds", 0.6)))
        self.resume_parallel_hardware = bool(state_cfg.get("resume_parallel_hardware", True))

    def diff(self, saved: dict[str, Any] | None, current: dict[str, Any] | None, requirements: list[str] | set[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        saved = saved or {}
        current = current or {}
        items: list[dict[str, Any]] = []
        required = self._normalize_requirements(requirements)

        saved_pose = saved.get("pose") if isinstance(saved.get("pose"), dict) else {}
        current_pose = current.get("pose") if isinstance(current.get("pose"), dict) else {}
        if self._needs_pose(required) and saved_pose.get("valid") and current_pose.get("valid"):
            distance = math.hypot(float(saved_pose.get("x", 0.0)) - float(current_pose.get("x", 0.0)), float(saved_pose.get("y", 0.0)) - float(current_pose.get("y", 0.0)))
            yaw_delta = abs(self._angle_delta(float(saved_pose.get("yaw", 0.0)), float(current_pose.get("yaw", 0.0))))
            if distance > self.pose_distance_threshold or yaw_delta > self.yaw_threshold:
                items.append({"kind": "pose", "distance_m": round(distance, 3), "yaw_delta_rad": round(yaw_delta, 3), "saved": saved_pose, "current": current_pose})
        elif self._needs_pose(required) and saved_pose.get("valid") and not current_pose.get("valid"):
            items.append({"kind": "pose_unknown_current", "saved": saved_pose, "current": current_pose})

        saved_head = saved.get("head") if isinstance(saved.get("head"), dict) else {}
        current_head = current.get("head") if isinstance(current.get("head"), dict) else {}
        if self._needs_head(required) and saved_head.get("valid") and current_head.get("valid"):
            try:
                delta = abs(int(saved_head.get("angle")) - int(current_head.get("angle")))
                if delta > self.head_angle_threshold:
                    items.append({"kind": "head", "angle_delta": delta, "saved": saved_head, "current": current_head})
            except Exception:
                pass

        saved_peripherals = saved.get("peripherals") if isinstance(saved.get("peripherals"), dict) else {}
        current_peripherals = current.get("peripherals") if isinstance(current.get("peripherals"), dict) else {}
        for name, saved_value in saved_peripherals.items():
            if not self._needs_peripheral(required, str(name)):
                continue
            current_value = current_peripherals.get(name)
            saved_state = self._peripheral_state(saved_value)
            current_state = self._peripheral_state(current_value)
            if saved_state != current_state:
                items.append({"kind": "peripheral", "name": name, "saved": saved_value, "current": current_value, "saved_state": saved_state, "current_state": current_state})

        return {"scene_changed": bool(items), "items": items, "saved": saved, "current": current, "requirements": None if required is None else sorted(required)}

    def build_restore_steps(self, diff: dict[str, Any]) -> list[TaskStep]:
        steps: list[TaskStep] = []
        for item in diff.get("items") or []:
            kind = item.get("kind")
            saved = item.get("saved") if isinstance(item.get("saved"), dict) else {}
            if kind in {"pose", "pose_unknown_current"} and saved.get("valid"):
                steps.append(
                    TaskStep(
                        skill_name="navigation_goto",
                        arguments={
                            "action": "goto",
                            "x": saved.get("x"),
                            "y": saved.get("y"),
                            "yaw": saved.get("yaw", 0.0),
                            "frame_id": saved.get("frame_id", "map"),
                        },
                        reason="restore interrupted task pose before resuming",
                    )
                )
            elif kind == "head" and saved.get("valid") and saved.get("angle") is not None:
                steps.append(
                    TaskStep(
                        skill_name="head_control",
                        arguments={
                            "action": "angle",
                            "angle": int(saved["angle"]),
                            "wait": self.resume_head_wait_seconds,
                        },
                        reason="restore interrupted task head angle before resuming",
                    )
                )
            elif kind == "peripheral":
                name = item.get("name")
                action = saved.get("action") if isinstance(saved, dict) else None
                action = action or item.get("saved_state")
                skill_name = {
                    "projector": "projector_control",
                    "light": "light_control",
                    "fan": "fan_control",
                }.get(str(name))
                if skill_name and action:
                    steps.append(TaskStep(skill_name=skill_name, arguments={"action": action}, reason=f"restore interrupted task {name} state"))
        navigation_steps = [step for step in steps if step.skill_name == "navigation_goto"]
        hardware_steps = [step for step in steps if step.skill_name != "navigation_goto"]
        if self.resume_parallel_hardware and hardware_steps:
            for step in hardware_steps:
                step.arguments = dict(step.arguments or {})
                step.arguments["_scheduler"] = {
                    "parallel_group": "resume_hardware_restore",
                    "can_parallel": True,
                }
        return navigation_steps + hardware_steps

    def _angle_delta(self, a: float, b: float) -> float:
        return (a - b + math.pi) % (2.0 * math.pi) - math.pi

    def _peripheral_state(self, value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("action", "state", "status", "value"):
                raw = value.get(key)
                if raw is not None:
                    return str(raw)
            return None
        if value is None:
            return None
        return str(value)

    def _normalize_requirements(self, requirements: list[str] | set[str] | tuple[str, ...] | None) -> set[str] | None:
        if requirements is None:
            return None
        return {str(item).strip().lower() for item in requirements if str(item).strip()}

    def _needs_pose(self, requirements: set[str] | None) -> bool:
        if requirements is None:
            return True
        return bool(requirements & {"pose", "base", "current_pose", "robot_pose", "location", "position"})

    def _needs_head(self, requirements: set[str] | None) -> bool:
        if requirements is None:
            return True
        return bool(requirements & {"head", "camera", "front_camera", "back_camera", "camera_view"})

    def _needs_peripheral(self, requirements: set[str] | None, name: str) -> bool:
        if requirements is None:
            return True
        return "peripheral" in requirements or "peripherals" in requirements or name in requirements
