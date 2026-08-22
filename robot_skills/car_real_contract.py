#!/usr/bin/env python3
"""Pure contract helpers for the unmodified ``/home/test/Car_real_copy`` API.

The Qwen project and Android App use canonical Nav2 map coordinates (metres and
radians).  Car_real_copy's AI gateway intentionally exposes a reversed frame
and degrees.  Keeping this conversion in one side-effect-free module prevents
individual Skills from silently disagreeing about direction or angle units.
"""

from __future__ import annotations

import math
from typing import Any


CAR_REAL_WORKSPACE = "/home/test/Car_real_copy"
EXTERNAL_CMD_VEL_TOPIC = "/cmd_vel_external"
NAV_GOAL_TOPIC = "/motion_controller/nav_goal_with_options"
NAV_CANCEL_SERVICE = "/motion_controller/cancel_nav_goal"
SENSOR_GATE_SERVICE = "/motion_controller/set_sensor_gate_enabled"
MANAGER_SHUTDOWN_SERVICE = "/mapping_manager/shutdown"
HEAD_ANGLE_TOPIC = "/step_motor_angle"

MANAGER_MOTION_STATE = "NAVIGATION"
MANAGER_FATAL_STATES = {"SAFE_STOP", "NOT_RUNNING", "STOPPED"}

NAV_SUCCESS_PREFIXES = ("succeeded",)
NAV_FAILURE_PREFIXES = (
    "aborted",
    "cancelled",
    "rejected_",
    "failed_",
    "unknown_result",
    "wall_alignment_failed_",
    "wall_alignment_cancelled",
    "wall_alignment_rejected:",
    "wall_alignment_service_error",
)
NAV_ACTIVE_PREFIXES = (
    "cached_waiting_sensor_gate_ready",
    "queued_waiting_nav2",
    "recovered;queued_waiting_nav2",
    "sending_to_nav2",
    "accepted_by_nav2",
    "waiting_nav2_accept",
    "navigating",
    "navigation_succeeded;",
    "cancel_requested",
    "wall_alignment_requested",
    "wall_alignment_accepted",
    "wall_alignment_running:",
)


def normalize_degrees(value: float) -> float:
    """Normalize an angle to the NavGoal contract's inclusive [-180, 180]."""

    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non_finite_yaw")
    normalized = (value + 180.0) % 360.0 - 180.0
    # Preserve +180 for positive inputs so boundary diagnostics stay intuitive.
    if math.isclose(normalized, -180.0, abs_tol=1e-12) and value > 0.0:
        return 180.0
    return normalized


def map_pose_to_gateway(x: float, y: float, yaw_radians: float) -> dict[str, float]:
    """Convert canonical map pose to Car_real_copy's direction_reverse=-1 API.

    motion_controller converts gateway ``(gx, gy, gyaw_deg)`` back to Nav2 as
    ``(-gx, -gy, radians(gyaw_deg) + pi)``.  This function is that transform's
    inverse, preserving the physical goal currently used by Qwen and the App.
    """

    values = (float(x), float(y), float(yaw_radians))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("non_finite_navigation_goal")
    return {
        "x": -values[0],
        "y": -values[1],
        "yaw_degrees": normalize_degrees(math.degrees(values[2]) - 180.0),
    }


def gateway_pose_to_map(x: float, y: float, yaw_degrees: float) -> dict[str, float]:
    """Forward transform used by Car_real_copy, exposed for round-trip tests."""

    values = (float(x), float(y), float(yaw_degrees))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("non_finite_navigation_goal")
    if not -180.0 <= values[2] <= 180.0:
        raise ValueError("gateway_yaw_out_of_range")
    return {
        "x": -values[0],
        "y": -values[1],
        "yaw": math.atan2(
            math.sin(math.radians(values[2]) + math.pi),
            math.cos(math.radians(values[2]) + math.pi),
        ),
    }


def manager_allows_motion(state: str, sensor_gate_state: str) -> tuple[bool, str | None]:
    state = str(state or "unknown").strip().upper()
    gate = str(sensor_gate_state or "unknown").strip().lower()
    if state == "SAFE_STOP":
        return False, "manager_safe_stop"
    if state != MANAGER_MOTION_STATE:
        return False, f"manager_not_navigation:{state.lower()}"
    if gate != "ready":
        return False, f"sensor_gate_not_ready:{gate}"
    return True, None


def classify_nav_status(status: str) -> dict[str, Any]:
    """Classify the latched String status emitted by motion_controller."""

    value = str(status or "unknown").strip().lower()
    if value.startswith(NAV_SUCCESS_PREFIXES):
        phase = "success"
    elif value.startswith(NAV_FAILURE_PREFIXES):
        phase = "failure"
    elif value.startswith(NAV_ACTIVE_PREFIXES):
        phase = "active"
    elif value == "idle":
        phase = "idle"
    else:
        phase = "unknown"
    return {
        "status": value,
        "phase": phase,
        "terminal": phase in {"success", "failure"},
        "ok": phase == "success",
    }


def head_status_matches(status: str, target: int) -> bool:
    value = str(status or "").strip().lower()
    return value.startswith("succeeded;") and f"target={int(target)}" in value
