#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from car_real_contract import NAV_GOAL_TOPIC, map_pose_to_gateway

SKILL_NAME = "navigation_goto"
POINTS_DB = Path(
    os.getenv(
        "V8_NAVIGATION_POINTS_DB",
        "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/points/named_points.json",
    )
)
DEFAULT_ACTION_NAME = os.getenv("NAVIGATION_ACTION_NAME", "/navigate_to_pose")
DEFAULT_PATH_ACTION_NAME = os.getenv("NAVIGATION_PATH_ACTION_NAME", "/compute_path_to_pose")
DEFAULT_RECOVERY_COMMAND = os.getenv(
    "NAVIGATION_RECOVERY_COMMAND",
    "/home/test/new_project_optimized_v11_navsafe/startup/recover_navigation.sh",
)
ACTION_WORDS = {"goto", "go", "nav", "navigate", "run"}
LIST_WORDS = {"list", "points", "show"}


class PointNotConfiguredError(ValueError):
    def __init__(self, name: str, point: dict[str, Any]):
        super().__init__(f"point_not_configured:{name}")
        self.name = name
        self.point = dict(point)


class MissingDestinationError(ValueError):
    """The caller requested navigation without a point or complete pose."""


def load_points() -> dict[str, dict[str, Any]]:
    if not POINTS_DB.exists():
        return {}
    with POINTS_DB.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)
    if not isinstance(raw, dict):
        raise ValueError(f"named points file is not an object: {POINTS_DB}")
    points: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        canonical = str(value.get("name") or key).strip()
        if not canonical:
            canonical = str(key).strip()
        point = dict(value)
        point["name"] = canonical
        point.setdefault("id", canonical)
        point.setdefault("display_name", canonical)
        aliases = point.get("aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list):
            aliases = []
        normalized_aliases = []
        for item in [key, point.get("display_name"), *aliases]:
            text = str(item or "").strip()
            if text and text not in {canonical, *normalized_aliases}:
                normalized_aliases.append(text)
        point["aliases"] = normalized_aliases
        points[canonical] = point
    return points


def point_match_tokens(name: str, point: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for item in [name, point.get("id"), point.get("name"), point.get("display_name")]:
        text = str(item or "").strip()
        if text and text not in tokens:
            tokens.append(text)
    aliases = point.get("aliases")
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        for item in aliases:
            text = str(item or "").strip()
            if text and text not in tokens:
                tokens.append(text)
    return tokens


def resolve_point(points: dict[str, dict[str, Any]], raw_name: Any) -> tuple[str, dict[str, Any]] | None:
    query = str(raw_name or "").strip()
    if not query:
        return None
    if query in points:
        return query, points[query]
    query_lower = query.lower()
    for name, point in points.items():
        for token in point_match_tokens(name, point):
            if query == token or query_lower == token.lower():
                return name, point
    return None


def parse_json_params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"--json-params is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("--json-params must be a JSON object")
    return data


def as_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a number: {value!r}") from exc


def is_number_token(value: str) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def yaw_to_quaternion(yaw: float) -> dict[str, float]:
    half = yaw / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


def build_goal_yaml(x: float, y: float, yaw: float, frame_id: str) -> str:
    q = yaw_to_quaternion(yaw)
    return (
        "pose:\n"
        "  header:\n"
        f"    frame_id: \"{frame_id}\"\n"
        "  pose:\n"
        "    position:\n"
        f"      x: {x}\n"
        f"      y: {y}\n"
        "      z: 0.0\n"
        "    orientation:\n"
        f"      x: {q['x']}\n"
        f"      y: {q['y']}\n"
        f"      z: {q['z']}\n"
        f"      w: {q['w']}\n"
    )


def check_action_server(action_name: str, timeout: float) -> tuple[bool, str, str]:
    cmd = ["ros2", "action", "info", action_name]
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=max(1.0, timeout))
    except FileNotFoundError:
        return False, "", "ros2 command not found; source ROS setup before running"
    except subprocess.TimeoutExpired as exc:
        return False, exc.stdout or "", f"action_server_check_timeout after {timeout:g}s"

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        return False, output, "action_info_failed"
    if "Action servers: 0" in output:
        return False, output, "action_server_unavailable"
    return True, output, ""


def check_action_server_with_retry(action_name: str, timeout: float, attempts: int, delay: float) -> tuple[bool, str, str, list[dict[str, Any]]]:
    logs: list[dict[str, Any]] = []
    last_info = ""
    last_error = ""
    max_attempts = max(1, int(attempts))
    for attempt in range(1, max_attempts + 1):
        ok, info, error = check_action_server(action_name, timeout)
        last_info = info
        last_error = error
        logs.append(
            {
                "attempt": attempt,
                "ok": ok,
                "error": error,
                "action_name": action_name,
            }
        )
        if ok:
            return True, info, "", logs
        if error not in {"action_server_unavailable", "action_server_check_timeout after %gs" % timeout, "action_info_failed"}:
            break
        if attempt < max_attempts:
            time.sleep(max(0.0, float(delay)))
    return False, last_info, last_error, logs


def points_payload(points: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized = []
    for name, point in sorted(points.items()):
        normalized.append({
            "name": name,
            "id": point.get("id", name),
            "display_name": point.get("display_name", name),
            "aliases": point.get("aliases", []),
            "x": point.get("x"),
            "y": point.get("y"),
            "yaw": point.get("yaw", 0.0),
            "frame_id": point.get("frame_id", "map"),
            "configured": bool(point.get("configured", True)),
            "room": point.get("room"),
            "purpose": point.get("purpose"),
        })
    return {"ok": True, "skill": SKILL_NAME, "action": "list", "source": str(POINTS_DB), "points": normalized}


def preflight_path_with_rclpy(
    goal: dict[str, Any],
    action_name: str = DEFAULT_PATH_ACTION_NAME,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Ask Nav2 for a path without commanding the base."""
    try:
        import rclpy
        from action_msgs.msg import GoalStatus
        from geometry_msgs.msg import PoseStamped
        from nav2_msgs.action import ComputePathToPose
        from rclpy.action import ActionClient
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
    except Exception as exc:
        return {"ok": False, "error": f"path_preflight_import_failed: {exc}"}

    context = Context()
    executor = node = client = None
    started = time.monotonic()
    deadline = started + max(0.5, float(timeout))
    try:
        rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = Node(
            f"navigation_path_preflight_{os.getpid()}_{time.monotonic_ns() % 1000000}",
            context=context,
        )
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        client = ActionClient(node, ComputePathToPose, action_name)
        if not client.wait_for_server(timeout_sec=max(0.2, min(float(timeout), 2.0))):
            return {"ok": False, "error": "path_preflight_server_unavailable", "action_name": action_name}

        pose = PoseStamped()
        pose.header.frame_id = str(goal["frame_id"])
        pose.header.stamp = node.get_clock().now().to_msg()
        pose.pose.position.x = float(goal["x"])
        pose.pose.position.y = float(goal["y"])
        pose.pose.position.z = 0.0
        quaternion = yaw_to_quaternion(float(goal["yaw"]))
        pose.pose.orientation.x = quaternion["x"]
        pose.pose.orientation.y = quaternion["y"]
        pose.pose.orientation.z = quaternion["z"]
        pose.pose.orientation.w = quaternion["w"]

        request = ComputePathToPose.Goal()
        request.goal = pose
        request.use_start = False
        request.planner_id = "GridBased"
        send_future = client.send_goal_async(request)
        while context.ok() and not send_future.done() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
        if not send_future.done():
            return {"ok": False, "error": "path_preflight_goal_response_timeout"}
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            return {"ok": False, "error": "path_preflight_goal_rejected"}

        result_future = goal_handle.get_result_async()
        while context.ok() and not result_future.done() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
        if not result_future.done():
            with contextlib.suppress(Exception):
                goal_handle.cancel_goal_async()
            return {"ok": False, "error": "path_preflight_result_timeout"}
        wrapped = result_future.result()
        status = int(getattr(wrapped, "status", -1))
        result = getattr(wrapped, "result", None)
        poses = list(getattr(getattr(result, "path", None), "poses", []) or [])
        elapsed = round(time.monotonic() - started, 3)
        if status != GoalStatus.STATUS_SUCCEEDED or not poses:
            return {
                "ok": False,
                "error": "navigation_no_valid_path",
                "status_code": status,
                "path_pose_count": len(poses),
                "elapsed_sec": elapsed,
            }
        return {
            "ok": True,
            "status_code": status,
            "path_pose_count": len(poses),
            "elapsed_sec": elapsed,
        }
    except Exception as exc:
        return {"ok": False, "error": f"path_preflight_failed: {type(exc).__name__}: {exc}"}
    finally:
        with contextlib.suppress(Exception):
            if client is not None:
                client.destroy()
        with contextlib.suppress(Exception):
            if node is not None:
                if executor is not None:
                    executor.remove_node(node)
                node.destroy_node()
        with contextlib.suppress(Exception):
            if executor is not None:
                executor.shutdown(timeout_sec=1.0)
        with contextlib.suppress(Exception):
            context.try_shutdown()


def send_goal_with_rclpy(
    goal: dict[str, Any],
    action_name: str,
    timeout: float,
    server_wait_timeout: float = 3.0,
    server_retry_attempts: int = 3,
    server_retry_delay: float = 0.5,
    goal_response_timeout: float = 6.0,
    goal_response_attempts: int = 2,
    health_response_timeout: float = 2.0,
    cancel_response_timeout: float = 2.0,
    feedback_timeout: float = 10.0,
    progress_timeout: float = 45.0,
    recovery_command: str = DEFAULT_RECOVERY_COMMAND,
    recovery_attempts: int = 0,
) -> dict[str, Any]:
    try:
        import rclpy
        from action_msgs.msg import GoalStatus
        from geometry_msgs.msg import PoseStamped
        from nav2_msgs.action import NavigateToPose
        from lifecycle_msgs.srv import GetState
        from rclpy.action import ActionClient
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
    except Exception as exc:
        return {
            "ok": False,
            "skill": SKILL_NAME,
            "action": "goto",
            "goal": goal,
            "error": f"rclpy_navigation_import_failed: {exc}",
        }

    stop_requested = {"value": False}
    previous_handlers = {}

    def request_stop(signum, _frame):
        stop_requested["value"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(Exception):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, request_stop)

    started = time.monotonic()
    overall_deadline = started + max(1.0, float(timeout))
    attempt_log: list[dict[str, Any]] = []
    recovery_log: list[dict[str, Any]] = []
    server_failures = 0
    response_failures = 0
    recovery_count = 0
    pending_recovery_reason = ""

    def context_ok(context: Any) -> bool:
        with contextlib.suppress(Exception):
            return bool(context.ok())
        return False

    def wait_future(executor: Any, context: Any, future: Any, deadline: float) -> str:
        while context_ok(context) and not future.done():
            if stop_requested["value"]:
                return "stopped"
            if time.monotonic() >= deadline:
                return "timeout"
            executor.spin_once(timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        if stop_requested["value"]:
            return "stopped"
        return "done" if future.done() else "context_shutdown"

    def future_result(future: Any) -> tuple[Any, str]:
        try:
            return future.result(), ""
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def shutdown_attempt(context: Any, executor: Any, node: Any, client: Any, health_client: Any) -> None:
        with contextlib.suppress(Exception):
            if health_client is not None:
                node.destroy_client(health_client)
        with contextlib.suppress(Exception):
            if client is not None:
                client.destroy()
        with contextlib.suppress(Exception):
            if node is not None:
                if executor is not None:
                    executor.remove_node(node)
                node.destroy_node()
        with contextlib.suppress(Exception):
            if executor is not None:
                executor.shutdown(timeout_sec=1.0)
        with contextlib.suppress(Exception):
            context.try_shutdown()

    def cancel_goal(executor: Any, context: Any, goal_handle: Any) -> str:
        with contextlib.suppress(Exception):
            cancel_future = goal_handle.cancel_goal_async()
            state = wait_future(
                executor,
                context,
                cancel_future,
                time.monotonic() + max(0.2, float(cancel_response_timeout)),
            )
            if state != "done":
                return f"cancel_{state}"
            _response, error = future_result(cancel_future)
            return "cancelled" if not error else f"cancel_error: {error}"
        return "cancel_failed"

    max_server_failures = max(1, int(server_retry_attempts))
    max_response_failures = max(1, int(goal_response_attempts))
    max_recoveries = max(0, int(recovery_attempts))
    attempt_number = 0
    try:
        while time.monotonic() < overall_deadline:
            if pending_recovery_reason:
                recovery_started = time.monotonic()
                try:
                    recovered = subprocess.run(
                        ["bash", recovery_command],
                        text=True,
                        capture_output=True,
                        timeout=100.0,
                        check=False,
                    )
                    recovery_elapsed = time.monotonic() - recovery_started
                    overall_deadline += recovery_elapsed
                    recovery_record = {
                        "reason": pending_recovery_reason,
                        "returncode": recovered.returncode,
                        "elapsed_sec": round(recovery_elapsed, 3),
                        "stdout": (recovered.stdout or "").strip()[-2000:],
                        "stderr": (recovered.stderr or "").strip()[-2000:],
                    }
                except Exception as exc:
                    recovery_elapsed = time.monotonic() - recovery_started
                    overall_deadline += recovery_elapsed
                    recovery_record = {
                        "reason": pending_recovery_reason,
                        "returncode": -1,
                        "elapsed_sec": round(recovery_elapsed, 3),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                recovery_log.append(recovery_record)
                pending_recovery_reason = ""
                if recovery_record.get("returncode") != 0:
                    return {
                        "ok": False,
                        "skill": SKILL_NAME,
                        "action": "goto",
                        "goal": goal,
                        "error": "navigation_stack_recovery_failed",
                        "attempts": attempt_log,
                        "recoveries": recovery_log,
                    }
                server_failures = 0
                response_failures = 0

            attempt_number += 1
            context = Context()
            executor = node = client = health_client = None
            attempt_started = time.monotonic()
            attempt: dict[str, Any] = {
                "attempt": attempt_number,
                "node_recreated": True,
                "action_name": action_name,
            }
            try:
                rclpy.init(
                    args=None,
                    context=context,
                    signal_handler_options=SignalHandlerOptions.NO,
                )
                node = Node(
                    f"v8_navigation_goto_{os.getpid()}_{attempt_number}_{time.monotonic_ns() % 1000000}",
                    context=context,
                )
                executor = SingleThreadedExecutor(context=context)
                executor.add_node(node)

                health_client = node.create_client(GetState, "/bt_navigator/get_state")
                health_ready = health_client.wait_for_service(
                    timeout_sec=max(0.1, min(float(server_wait_timeout), float(health_response_timeout)))
                )
                if not health_ready:
                    error = "nav2_health_service_unavailable"
                else:
                    health_future = health_client.call_async(GetState.Request())
                    health_state = wait_future(
                        executor,
                        context,
                        health_future,
                        time.monotonic() + max(0.2, float(health_response_timeout)),
                    )
                    health_response, health_error = future_result(health_future) if health_state == "done" else (None, "")
                    health_label = str(getattr(getattr(health_response, "current_state", None), "label", ""))
                    attempt["bt_navigator_state"] = health_label
                    if health_state == "stopped":
                        error = "navigation_cancelled_before_accept"
                    elif health_state != "done":
                        error = "nav2_health_response_timeout" if health_state == "timeout" else "nav2_health_context_shutdown"
                    elif health_error:
                        error = f"nav2_health_response_error: {health_error}"
                    elif health_label != "active":
                        error = f"bt_navigator_not_active: {health_label or 'unknown'}"
                    else:
                        error = ""

                if error:
                    attempt.update({"ok": False, "error": error})
                    attempt_log.append(attempt)
                    if error == "navigation_cancelled_before_accept":
                        return {
                            "ok": False,
                            "skill": SKILL_NAME,
                            "action": "goto",
                            "goal": goal,
                            "error": error,
                            "attempts": attempt_log,
                        }
                    server_failures += 1
                    if server_failures >= min(2, max_server_failures) and recovery_count < max_recoveries:
                        recovery_count += 1
                        pending_recovery_reason = error
                        continue
                    if server_failures >= max_server_failures:
                        return {
                            "ok": False,
                            "skill": SKILL_NAME,
                            "action": "goto",
                            "goal": goal,
                            "error": error,
                            "attempts": attempt_log,
                        }
                    continue

                client = ActionClient(node, NavigateToPose, action_name)
                server_ready = bool(client.wait_for_server(timeout_sec=max(0.1, float(server_wait_timeout))))
                attempt["action_server_ready"] = server_ready
                if not server_ready:
                    error = "action_server_unavailable"
                    attempt.update({"ok": False, "error": error})
                    attempt_log.append(attempt)
                    server_failures += 1
                    if server_failures >= min(2, max_server_failures) and recovery_count < max_recoveries:
                        recovery_count += 1
                        pending_recovery_reason = error
                        continue
                    if server_failures >= max_server_failures:
                        return {
                            "ok": False,
                            "skill": SKILL_NAME,
                            "action": "goto",
                            "goal": goal,
                            "error": error,
                            "action_name": action_name,
                            "attempts": attempt_log,
                        }
                    continue

                msg = NavigateToPose.Goal()
                pose = PoseStamped()
                pose.header.frame_id = goal["frame_id"]
                pose.header.stamp = node.get_clock().now().to_msg()
                pose.pose.position.x = float(goal["x"])
                pose.pose.position.y = float(goal["y"])
                pose.pose.position.z = 0.0
                q = yaw_to_quaternion(float(goal["yaw"]))
                pose.pose.orientation.x = q["x"]
                pose.pose.orientation.y = q["y"]
                pose.pose.orientation.z = q["z"]
                pose.pose.orientation.w = q["w"]
                msg.pose = pose

                feedback_state: dict[str, Any] = {
                    "accepted_at": 0.0,
                    "first_at": 0.0,
                    "last_at": 0.0,
                    "last_progress_at": time.monotonic(),
                    "best_distance": None,
                    "last_distance": None,
                    "recoveries": None,
                    "count": 0,
                }

                def on_feedback(feedback_message: Any) -> None:
                    now = time.monotonic()
                    feedback = getattr(feedback_message, "feedback", feedback_message)
                    try:
                        distance = float(getattr(feedback, "distance_remaining"))
                    except (TypeError, ValueError, AttributeError):
                        distance = None
                    try:
                        nav_recoveries = int(getattr(feedback, "number_of_recoveries"))
                    except (TypeError, ValueError, AttributeError):
                        nav_recoveries = None
                    if not feedback_state["first_at"]:
                        feedback_state["first_at"] = now
                    feedback_state["last_at"] = now
                    feedback_state["count"] += 1
                    feedback_state["last_distance"] = distance
                    best = feedback_state["best_distance"]
                    if distance is not None and (best is None or distance < float(best) - 0.03):
                        feedback_state["best_distance"] = distance
                        feedback_state["last_progress_at"] = now
                    if nav_recoveries is not None and nav_recoveries != feedback_state["recoveries"]:
                        feedback_state["recoveries"] = nav_recoveries
                        feedback_state["last_progress_at"] = now

                send_future = client.send_goal_async(msg, feedback_callback=on_feedback)
                response_state = wait_future(
                    executor,
                    context,
                    send_future,
                    min(overall_deadline, time.monotonic() + max(0.5, float(goal_response_timeout))),
                )
                if response_state == "stopped":
                    error = "navigation_cancelled_before_accept"
                elif response_state != "done":
                    error = "goal_response_timeout" if response_state == "timeout" else "goal_response_context_shutdown"
                else:
                    goal_handle, response_error = future_result(send_future)
                    if response_error:
                        error = f"goal_response_error: {response_error}"
                    elif goal_handle is None or not goal_handle.accepted:
                        error = "goal_rejected"
                    else:
                        error = ""

                if error:
                    attempt.update(
                        {
                            "ok": False,
                            "error": error,
                            "elapsed_sec": round(time.monotonic() - attempt_started, 3),
                        }
                    )
                    attempt_log.append(attempt)
                    if error == "goal_response_timeout":
                        response_failures += 1
                        if response_failures < max_response_failures:
                            continue
                        if recovery_count < max_recoveries:
                            recovery_count += 1
                            pending_recovery_reason = error
                            continue
                    return {
                        "ok": False,
                        "skill": SKILL_NAME,
                        "action": "goto",
                        "goal": goal,
                        "error": error,
                        "goal_response_timeout": float(goal_response_timeout),
                        "attempts": attempt_log,
                    }

                feedback_state["accepted_at"] = time.monotonic()
                feedback_state["last_progress_at"] = feedback_state["accepted_at"]
                result_future = goal_handle.get_result_async()
                result_state = "context_shutdown"
                while context_ok(context) and not result_future.done():
                    now = time.monotonic()
                    if stop_requested["value"]:
                        result_state = "stopped"
                        break
                    if now >= overall_deadline:
                        result_state = "timeout"
                        break
                    first_at = float(feedback_state["first_at"] or 0.0)
                    last_at = float(feedback_state["last_at"] or 0.0)
                    accepted_at = float(feedback_state["accepted_at"] or now)
                    if first_at <= 0.0 and now - accepted_at >= max(2.0, float(feedback_timeout)):
                        result_state = "feedback_timeout"
                        break
                    if last_at > 0.0 and now - last_at >= max(2.0, float(feedback_timeout)):
                        result_state = "feedback_timeout"
                        break
                    last_distance = feedback_state["last_distance"]
                    if (
                        last_distance is not None
                        and float(last_distance) > 0.15
                        and now - float(feedback_state["last_progress_at"]) >= max(10.0, float(progress_timeout))
                    ):
                        result_state = "progress_timeout"
                        break
                    executor.spin_once(timeout_sec=min(0.1, max(0.0, overall_deadline - now)))
                else:
                    result_state = "done" if result_future.done() else "context_shutdown"
                if result_state != "done":
                    cancel_status = cancel_goal(executor, context, goal_handle)
                    if result_state == "stopped":
                        error = "navigation_cancelled"
                    elif result_state == "feedback_timeout":
                        error = "navigation_feedback_timeout"
                    elif result_state == "progress_timeout":
                        error = "navigation_no_progress_timeout"
                    else:
                        error = "navigation_timeout"
                    attempt.update({
                        "ok": False,
                        "error": error,
                        "cancel_status": cancel_status,
                        "feedback_count": feedback_state["count"],
                        "last_distance": feedback_state["last_distance"],
                    })
                    attempt_log.append(attempt)
                    if (
                        error in {"navigation_feedback_timeout", "navigation_no_progress_timeout"}
                        and recovery_count < max_recoveries
                    ):
                        recovery_count += 1
                        pending_recovery_reason = error
                        continue
                    return {
                        "ok": False,
                        "skill": SKILL_NAME,
                        "action": "goto",
                        "goal": goal,
                        "error": error,
                        "timeout": timeout,
                        "cancel_status": cancel_status,
                        "attempts": attempt_log,
                        "recoveries": recovery_log,
                    }

                wrapped_result, result_error = future_result(result_future)
                if result_error:
                    attempt.update({"ok": False, "error": f"navigation_result_error: {result_error}"})
                    attempt_log.append(attempt)
                    return {
                        "ok": False,
                        "skill": SKILL_NAME,
                        "action": "goto",
                        "goal": goal,
                        "error": attempt["error"],
                        "attempts": attempt_log,
                    }
                status = int(getattr(wrapped_result, "status", -1))
                ok = status == GoalStatus.STATUS_SUCCEEDED
                status_name = {
                    GoalStatus.STATUS_UNKNOWN: "unknown",
                    GoalStatus.STATUS_ACCEPTED: "accepted",
                    GoalStatus.STATUS_EXECUTING: "executing",
                    GoalStatus.STATUS_CANCELING: "canceling",
                    GoalStatus.STATUS_SUCCEEDED: "succeeded",
                    GoalStatus.STATUS_CANCELED: "canceled",
                    GoalStatus.STATUS_ABORTED: "aborted",
                }.get(status, str(status))
                attempt.update(
                    {
                        "ok": ok,
                        "status": status_name,
                        "status_code": status,
                        "elapsed_sec": round(time.monotonic() - attempt_started, 3),
                    }
                )
                attempt_log.append(attempt)
                return {
                    "ok": ok,
                    "skill": SKILL_NAME,
                    "action": "goto",
                    "goal": goal,
                    "status": status_name,
                    "status_code": status,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "error": None if ok else f"navigation_{status_name}",
                    "attempts": attempt_log,
                    "recoveries": recovery_log,
                }
            finally:
                shutdown_attempt(context, executor, node, client, health_client)
                if (
                    not stop_requested["value"]
                    and time.monotonic() < overall_deadline
                    and attempt_log
                    and attempt_log[-1].get("attempt") == attempt_number
                    and attempt_log[-1].get("error") in {
                        "action_server_unavailable",
                        "nav2_health_service_unavailable",
                        "nav2_health_response_timeout",
                        "nav2_health_context_shutdown",
                        "goal_response_timeout",
                    }
                    and server_retry_delay > 0
                ):
                    time.sleep(min(float(server_retry_delay), max(0.0, overall_deadline - time.monotonic())))

        return {
            "ok": False,
            "skill": SKILL_NAME,
            "action": "goto",
            "goal": goal,
            "error": "navigation_timeout",
            "timeout": timeout,
            "attempts": attempt_log,
        }
    finally:
        for sig, handler in previous_handlers.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)


def choose_action(args: argparse.Namespace, params: dict[str, Any], tokens: list[str]) -> tuple[str, list[str]]:
    action = str(args.action_flag or params.get("action") or "").strip().lower()
    if not action and tokens:
        first = tokens[0].strip().lower()
        if first in ACTION_WORDS or first in LIST_WORDS:
            action = first
            tokens = tokens[1:]
    if action in LIST_WORDS:
        return "list", tokens
    return "goto", tokens


def resolve_goal(args: argparse.Namespace, params: dict[str, Any], tokens: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    points = load_points()
    point_name = (
        args.point_flag
        or params.get("point")
        or params.get("destination")
        or params.get("name")
        or params.get("target")
    )
    x = args.x if args.x is not None else params.get("x")
    y = args.y if args.y is not None else params.get("y")
    yaw = args.yaw if args.yaw is not None else params.get("yaw", 0.0)
    frame_id = args.frame_id or str(params.get("frame_id") or "map")

    if not point_name and tokens:
        if len(tokens) >= 2 and is_number_token(tokens[0]) and is_number_token(tokens[1]):
            x = tokens[0]
            y = tokens[1]
            if len(tokens) >= 3 and is_number_token(tokens[2]):
                yaw = tokens[2]
        else:
            point_name = tokens[0]

    if point_name:
        point_name = str(point_name)
        resolved = resolve_point(points, point_name)
        if resolved is None:
            raise KeyError(point_name)
        canonical_name, point = resolved
        if point.get("configured") is False or any(point.get(key) is None for key in ("x", "y", "yaw")):
            raise PointNotConfiguredError(canonical_name, point)
        return {
            "mode": "point",
            "name": canonical_name,
            "display_name": str(point.get("display_name") or canonical_name),
            "requested_name": point_name,
            "x": as_float(point.get("x"), f"point {canonical_name}.x"),
            "y": as_float(point.get("y"), f"point {canonical_name}.y"),
            "yaw": as_float(point.get("yaw", 0.0), f"point {canonical_name}.yaw"),
            "frame_id": str(point.get("frame_id", frame_id or "map")),
        }, points

    if x is None or y is None:
        raise MissingDestinationError(
            "需要提供坐标名字，例如 `a`，或直接坐标，例如 `--x 0.1 --y 0.1` / `0.1 0.1`"
        )
    return {
        "mode": "pose",
        "name": None,
        "x": as_float(x, "x"),
        "y": as_float(y, "y"),
        "yaw": as_float(yaw, "yaw"),
        "frame_id": str(frame_id or "map"),
    }, points


def emit(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Single navigation skill: list points or send a Nav2 NavigateToPose goal.")
    parser.add_argument("tokens", nargs="*", help="point name, `goto POINT`, or direct coordinates `X Y [YAW]`")
    parser.add_argument("--action", dest="action_flag", choices=["goto", "list"])
    parser.add_argument("--json-params", default=None, help="JSON object with point/destination/name or x/y/yaw/frame_id")
    parser.add_argument("--point", dest="point_flag", help="known point name from points/named_points.json")
    parser.add_argument("--x", type=float, help="goal x in map frame")
    parser.add_argument("--y", type=float, help="goal y in map frame")
    parser.add_argument("--yaw", type=float, default=None, help="goal yaw in radians")
    parser.add_argument("--frame-id", default="map")
    parser.add_argument("--action-name", default=DEFAULT_ACTION_NAME)
    parser.add_argument("--path-action-name", default=DEFAULT_PATH_ACTION_NAME)
    parser.add_argument("--path-preflight-timeout", type=float, default=float(os.getenv("NAVIGATION_PATH_PREFLIGHT_TIMEOUT", "4")))
    parser.add_argument("--skip-path-preflight", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NAVIGATION_GOTO_TIMEOUT", "120")))
    parser.add_argument("--server-wait-timeout", type=float, default=float(os.getenv("NAVIGATION_SERVER_WAIT_TIMEOUT", "3")))
    parser.add_argument("--server-retry-attempts", type=int, default=int(os.getenv("NAVIGATION_SERVER_RETRY_ATTEMPTS", "3")))
    parser.add_argument("--server-retry-delay", type=float, default=float(os.getenv("NAVIGATION_SERVER_RETRY_DELAY_SEC", "0.5")))
    parser.add_argument("--goal-response-timeout", type=float, default=float(os.getenv("NAVIGATION_GOAL_RESPONSE_TIMEOUT", "6")))
    parser.add_argument("--goal-response-attempts", type=int, default=int(os.getenv("NAVIGATION_GOAL_RESPONSE_ATTEMPTS", "2")))
    parser.add_argument("--health-response-timeout", type=float, default=float(os.getenv("NAVIGATION_HEALTH_RESPONSE_TIMEOUT", "2")))
    parser.add_argument("--cancel-response-timeout", type=float, default=float(os.getenv("NAVIGATION_CANCEL_RESPONSE_TIMEOUT", "2")))
    parser.add_argument("--feedback-timeout", type=float, default=float(os.getenv("NAVIGATION_FEEDBACK_TIMEOUT", "10")))
    parser.add_argument("--progress-timeout", type=float, default=float(os.getenv("NAVIGATION_PROGRESS_TIMEOUT", "45")))
    parser.add_argument("--recovery-command", default=DEFAULT_RECOVERY_COMMAND)
    parser.add_argument("--recovery-attempts", type=int, default=int(os.getenv("NAVIGATION_RECOVERY_ATTEMPTS", "0")))
    parser.add_argument("--resume-from-interrupt", action="store_true", help="Accepted by the task runtime; navigation resumes by replanning to the same goal.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="kept for compatibility; output is always JSON")
    args = parser.parse_args()

    try:
        params = parse_json_params(args.json_params)
        action, tokens = choose_action(args, params, list(args.tokens))
        if action == "list":
            return emit(points_payload(load_points()))
        goal, points = resolve_goal(args, params, tokens)
    except KeyError as exc:
        return emit({
            "ok": False,
            "skill": SKILL_NAME,
            "action": "goto",
            "error": "unknown_point",
            "point": str(exc).strip("'"),
            "available_points": sorted(load_points().keys()),
            "available_aliases": {
                name: point_match_tokens(name, point)
                for name, point in sorted(load_points().items())
            },
            "source": str(POINTS_DB),
        }, exit_code=2)
    except PointNotConfiguredError as exc:
        return emit({
            "ok": False,
            "skill": SKILL_NAME,
            "action": "goto",
            "error": "point_not_configured",
            "point": exc.name,
            "display_name": str(exc.point.get("display_name") or exc.name),
            "required_fields": ["x", "y", "yaw", "configured=true"],
            "source": str(POINTS_DB),
        }, exit_code=2)
    except MissingDestinationError as exc:
        return emit({
            "ok": False,
            "skill": SKILL_NAME,
            "action": "goto",
            "error": "missing_destination",
            "message": str(exc),
            "retryable": False,
            "source": str(POINTS_DB),
        }, exit_code=2)
    except Exception as exc:
        return emit({"ok": False, "skill": SKILL_NAME, "error": str(exc), "source": str(POINTS_DB)}, exit_code=2)

    goal_yaml = build_goal_yaml(goal["x"], goal["y"], goal["yaw"], goal["frame_id"])
    gateway_goal = map_pose_to_gateway(goal["x"], goal["y"], goal["yaw"])
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "resident_skill_client.py"),
        "navigation_goto",
        "goto",
        str(goal.get("name") or goal.get("requested_name") or "direct_pose"),
    ]
    if args.dry_run:
        return emit({
            "ok": True,
            "skill": SKILL_NAME,
            "action": "goto",
            "dry_run": True,
            "goal": goal,
            "transport": {
                "backend": "Car_real_copy/mapping_navigation_manager",
                "topic": NAV_GOAL_TOPIC,
                "gateway_goal": gateway_goal,
                "yaw_unit": "degrees",
            },
            "cmd": cmd,
            "source": str(POINTS_DB),
        })

    preflight = {"ok": True, "skipped": True}
    if not args.skip_path_preflight:
        preflight = preflight_path_with_rclpy(
            goal,
            action_name=args.path_action_name,
            timeout=args.path_preflight_timeout,
        )
        if not preflight.get("ok"):
            return emit({
                "ok": False,
                "skill": SKILL_NAME,
                "action": "goto",
                "goal": goal,
                "error": preflight.get("error") or "navigation_path_preflight_failed",
                "preflight": preflight,
            }, exit_code=4)

    result = send_goal_with_rclpy(
        goal,
        args.action_name,
        max(1.0, args.timeout),
        server_wait_timeout=args.server_wait_timeout,
        server_retry_attempts=args.server_retry_attempts,
        server_retry_delay=args.server_retry_delay,
        goal_response_timeout=args.goal_response_timeout,
        goal_response_attempts=args.goal_response_attempts,
        health_response_timeout=args.health_response_timeout,
        cancel_response_timeout=args.cancel_response_timeout,
        feedback_timeout=args.feedback_timeout,
        progress_timeout=args.progress_timeout,
        recovery_command=args.recovery_command,
        recovery_attempts=args.recovery_attempts,
    )
    result["preflight"] = preflight
    if result.get("ok"):
        return emit(result)
    error = str(result.get("error") or "")
    if error in {"navigation_timeout", "navigation_feedback_timeout", "navigation_no_progress_timeout"}:
        return emit(result, exit_code=124)
    if error in {"navigation_cancelled", "navigation_cancelled_before_accept"}:
        return emit(result, exit_code=130)
    return emit(result, exit_code=4)


if __name__ == "__main__":
    if "--dry-run" in sys.argv[1:]:
        sys.exit(main())
    client = Path(__file__).resolve().parents[1] / "resident_skill_client.py"
    os.execv(sys.executable, [sys.executable, str(client), "navigation_goto", *sys.argv[1:]])
