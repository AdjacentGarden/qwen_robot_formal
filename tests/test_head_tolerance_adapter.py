from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "robot_skills"))

from resident_runtime_server import ResidentSkills  # noqa: E402
from shared_runtime_server import SharedRuntime  # noqa: E402
from skill_runner import concise_success_summary  # noqa: E402


class FakeRuntime:
    def __init__(
        self,
        actual_angle: float,
        *,
        manager_ok: bool = False,
        fresh: bool = True,
        stable: bool = True,
        initial_angle: float | None = None,
    ) -> None:
        self.actual_angle = actual_angle
        self.initial_angle = actual_angle + 30.0 if initial_angle is None else initial_angle
        self.manager_ok = manager_ok
        self.fresh = fresh
        self.stable = stable
        self.wait_calls: list[tuple[int, dict]] = []
        self.command_calls: list[int] = []
        self.gate_calls: list[bool] = []

    def command_head(self, angle: int, **_kwargs) -> dict:
        self.command_calls.append(angle)
        return {
            "ok": self.manager_ok,
            "subscribers": 1,
            "angle": angle,
            "error": None if self.manager_ok else "head_target_unconfirmed",
        }

    def wait_for_head_target(self, angle: int, **kwargs) -> dict:
        self.wait_calls.append((angle, kwargs))
        observed = self.actual_angle if self.command_calls else self.initial_angle
        error = (observed - angle + 180.0) % 360.0 - 180.0
        ok = (
            self.fresh
            and self.stable
            and abs(error) <= float(kwargs["tolerance_deg"])
        )
        return {
            "ok": ok,
            "available": self.fresh,
            "roll_deg": observed,
            "error_deg": error,
            "at_target": ok,
            "error": None if ok else "head_target_timeout",
        }


def move_head(
    action: str,
    actual_angle: float,
    *,
    manager_ok: bool = False,
    fresh: bool = True,
    stable: bool = True,
    initial_angle: float | None = None,
    gate_ok: bool = True,
) -> tuple[dict, FakeRuntime]:
    owner = ResidentSkills.__new__(ResidentSkills)
    runtime = FakeRuntime(
        actual_angle,
        manager_ok=manager_ok,
        fresh=fresh,
        stable=stable,
        initial_angle=initial_angle,
    )
    owner.runtime = runtime
    owner.head_control_lock = threading.Lock()
    def set_lidar(enabled, timeout=0.0):
        runtime.gate_calls.append(bool(enabled))
        return {"called": True, "ok": gate_ok, "enabled": enabled}

    owner._set_lidar_live = set_lidar
    owner.head_pub = type("FakePublisher", (), {"get_subscription_count": lambda self: 1})()
    return owner._move_head_resident(action, update_supervisor=False), runtime


@pytest.mark.parametrize("actual_angle", [213.14, 217.8])
def test_tilt_accepts_fresh_stable_imu_feedback_within_seven_degrees(actual_angle):
    result, runtime = move_head("up", actual_angle)

    assert result["ok"] is True
    assert result["acceptance"]["source"] == "project_stable_imu_confirmation"
    assert result["acceptance"]["tolerance_deg"] == pytest.approx(7.0)
    assert runtime.wait_calls


@pytest.mark.parametrize(
    ("action", "actual_angle", "fresh", "stable"),
    [
        ("up", 219.0, True, True),
        ("up", 213.14, False, True),
        ("up", 213.14, True, False),
        ("level", 191.0, True, True),
    ],
)
def test_out_of_range_stale_or_unstable_feedback_remains_rejected(
    action, actual_angle, fresh, stable
):
    result, _runtime = move_head(
        action,
        actual_angle,
        fresh=fresh,
        stable=stable,
    )

    assert result["ok"] is False
    assert result["acceptance"]["source"] == "unconfirmed"


def test_level_tolerance_remains_capped_at_five_degrees():
    # Level uses a stricter 2-degree lidar-recovery confirmation while the
    # public action contract remains capped at five degrees.
    result, _runtime = move_head("level", 186.8)

    assert result["ok"] is True
    assert result["acceptance"]["tolerance_deg"] == pytest.approx(5.0)
    assert result["acceptance"]["strict_lidar_level"] is True


def test_manager_success_keeps_original_fast_path():
    result, runtime = move_head("up", 230.0, manager_ok=True)

    assert result["ok"] is True
    assert result["acceptance"]["source"] == "car_real_copy"
    assert len(runtime.wait_calls) == 1  # Idempotence probe before motion.
    assert runtime.command_calls == [211]


@pytest.mark.parametrize(
    ("action", "target", "expected_gate"),
    [
        ("up", 211.0, False),
        ("down", 163.0, False),
        ("level", 185.0, True),
    ],
)
def test_same_pose_skips_motor_but_preserves_lidar_contract(action, target, expected_gate):
    result, runtime = move_head(action, target, initial_angle=target)

    assert result["ok"] is True
    assert result["already_at_target"] is True
    assert result["head_motion_skipped"] is True
    assert result["acceptance"]["source"] == "already_at_target"
    assert runtime.command_calls == []
    assert runtime.gate_calls == [expected_gate]


@pytest.mark.parametrize(
    ("action", "initial_angle", "final_angle", "target"),
    [
        ("down", 211.0, 163.0, 163),
        ("up", 163.0, 211.0, 211),
    ],
)
def test_up_and_down_transitions_still_issue_one_real_command(
    action, initial_angle, final_angle, target
):
    result, runtime = move_head(
        action,
        final_angle,
        initial_angle=initial_angle,
    )

    assert result["ok"] is True
    assert result.get("already_at_target") is not True
    assert runtime.command_calls == [target]
    assert runtime.gate_calls == [False]


@pytest.mark.parametrize(
    ("action", "initial_angle", "final_angle", "target", "expected_gate"),
    [
        ("up", 185.0, 211.0, 211, False),
        ("down", 185.0, 163.0, 163, False),
        ("level", 211.0, 185.0, 185, True),
        ("level", 163.0, 185.0, 185, True),
    ],
)
def test_remaining_cross_pose_transitions_issue_exactly_one_command(
    action, initial_angle, final_angle, target, expected_gate
):
    result, runtime = move_head(
        action,
        final_angle,
        initial_angle=initial_angle,
    )

    assert result["ok"] is True
    assert result.get("already_at_target") is not True
    assert runtime.command_calls == [target]
    assert runtime.gate_calls == ([False, True] if action == "level" else [expected_gate])


def test_same_pose_does_not_hide_lidar_gate_failure():
    result, runtime = move_head("up", 211.0, initial_angle=211.0, gate_ok=False)

    assert result["ok"] is False
    assert result["head_motion_skipped"] is True
    assert result["error"] == "lidar_guard_not_ready"
    assert runtime.command_calls == []


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("up", {"已经是抬头状态了。", "头已经抬好了，不用再调整。"}),
        ("down", {"已经是低头状态了。", "头已经低下来了，不用再调整。"}),
        ("level", {"现在已经是平视状态了。", "头部已经回正，不用再调整。"}),
    ],
)
def test_atomic_speech_explains_that_the_pose_is_already_satisfied(action, expected):
    spoken = concise_success_summary(
        "head_control",
        action,
        {"action": action},
        {"ok": True, "already_at_target": True},
        "",
    )
    assert spoken in expected


def test_stable_confirmation_works_with_ten_hz_feedback():
    runtime = SharedRuntime.__new__(SharedRuntime)
    runtime.head_feedback_condition = threading.Condition()
    runtime.latest_head_roll = None
    runtime.latest_head_roll_rate = None

    def publish_feedback():
        time.sleep(0.02)
        for angle in (185.30, 185.25, 185.26):
            with runtime.head_feedback_condition:
                runtime.latest_head_roll = (angle, time.monotonic())
                runtime.head_feedback_condition.notify_all()
            time.sleep(0.10)

    publisher = threading.Thread(target=publish_feedback)
    publisher.start()
    result = runtime.wait_for_head_target(
        185.0,
        tolerance_deg=5.0,
        maximum_rate_dps=4.0,
        maximum_roll_span_deg=1.5,
        stable_sec=0.15,
        timeout_sec=0.6,
        maximum_feedback_age_sec=0.5,
    )
    publisher.join(timeout=1.0)

    assert result["ok"] is True
    assert result["stable_sec"] >= 0.15
    assert result["stable_roll_span_deg"] <= 1.5
