from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "robot_skills"
sys.path.insert(0, str(SKILLS))

from shared_runtime_server import SharedRuntime  # noqa: E402
from resident_runtime_server import ResidentSkills  # noqa: E402


class Message:
    def __init__(self, data):
        self.data = data


class Twist:
    class Vector:
        def __init__(self):
            self.x = 0.0
            self.y = 0.0
            self.z = 0.0

    def __init__(self):
        self.linear = self.Vector()
        self.angular = self.Vector()


class UInt16:
    def __init__(self):
        self.data = 0


class Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)

    def get_subscription_count(self):
        return 1


def runtime_without_ros() -> SharedRuntime:
    runtime = object.__new__(SharedRuntime)
    runtime.car_feedback_condition = threading.Condition()
    runtime.car_feedback = {
        "manager_state": "NAVIGATION",
        "manager_event": "",
        "manager_attempt": 1,
        "controller_status": "idle",
        "controller_warning": "",
        "control_conflict": False,
        "nav_status": "idle",
        "sensor_gate_enabled": True,
        "sensor_gate_state": "ready",
        "head_status": "unknown",
        "head_aligned": False,
    }
    runtime.car_sequences = {key: 0 for key in runtime.car_feedback}
    runtime.move_cancel = threading.Event()
    runtime.navigation_cancel = threading.Event()
    runtime.Twist = Twist
    runtime.UInt16 = UInt16
    runtime.cmd_vel_pub = Publisher()
    return runtime


def test_safe_stop_interrupts_both_motion_paths_and_blocks_nonzero_publish():
    runtime = runtime_without_ros()
    runtime._on_car_feedback("manager_state", Message("SAFE_STOP"))
    assert runtime.move_cancel.is_set()
    assert runtime.navigation_cancel.is_set()
    with pytest.raises(RuntimeError, match="manager_safe_stop"):
        runtime._publish_twist(0.1, 0.0)
    assert runtime.cmd_vel_pub.messages == []
    runtime._publish_twist(0.0, 0.0)
    assert len(runtime.cmd_vel_pub.messages) == 1


def test_gate_recovery_and_conflict_both_block_nonzero_motion():
    runtime = runtime_without_ros()
    runtime._on_car_feedback("sensor_gate_state", Message("recovering"))
    with pytest.raises(RuntimeError, match="sensor_gate_not_ready"):
        runtime._publish_twist(0.0, 0.2)
    runtime._on_car_feedback("sensor_gate_state", Message("ready"))
    runtime._on_car_feedback("control_conflict", Message(True))
    with pytest.raises(RuntimeError, match="motion_controller_conflict"):
        runtime._publish_twist(0.1, 0.0)


def test_feedback_wait_rejects_latched_stale_value_and_accepts_new_sequence():
    runtime = runtime_without_ros()
    runtime.car_feedback["nav_status"] = "succeeded"
    runtime.car_sequences["nav_status"] = 4
    stale = runtime.wait_for_car_feedback(
        "nav_status",
        after_sequence=4,
        predicate=lambda value: value == "succeeded",
        timeout_sec=0.05,
    )
    assert stale["ok"] is False
    runtime._on_car_feedback("nav_status", Message("accepted_by_nav2"))
    fresh = runtime.wait_for_car_feedback(
        "nav_status",
        after_sequence=4,
        predicate=lambda value: value == "accepted_by_nav2",
        timeout_sec=0.05,
    )
    assert fresh["ok"] is True


def test_head_completion_requires_fresh_status_and_fresh_aligned_feedback():
    runtime = runtime_without_ros()
    runtime.head_angle_pub = Publisher()
    calls = []

    def wait(key, *, after_sequence, predicate, timeout_sec, cancel_event=None):
        calls.append((key, after_sequence))
        value = "succeeded;target=185;error=+0.2" if key == "head_status" else True
        assert predicate(value)
        return {"ok": True, "key": key, "value": value, "sequence": after_sequence + 1}

    runtime.wait_for_car_feedback = wait
    result = runtime.command_head(185, repeat=1, discovery_timeout=0.0, feedback_timeout=0.2)
    assert result["ok"] is True
    assert calls == [("head_status", 0), ("head_aligned", 0)]
    assert runtime.head_angle_pub.messages[0].data == 185


def test_active_sources_do_not_reference_old_workspace_or_direct_drive_topics():
    active = [
        ROOT / "run.sh",
        ROOT / "robot_stack.sh",
        ROOT / "ros_health_monitor.py",
        ROOT / "robot_skills/car_real_contract.py",
        ROOT / "robot_skills/shared_runtime_server.py",
        ROOT / "robot_skills/resident_runtime_server.py",
        ROOT / "robot_skills/resident_pet_worker.py",
        ROOT / "android_app/robot_bridge/bridge.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in active)
    assert "/home/test/car_real_copy_zhenghang" not in combined
    assert 'create_publisher(Twist, "/cmd_vel",' not in combined
    assert "send_goal_async(message" not in combined
    assert "/home/test/Car_real_copy" in combined
    assert "/cmd_vel_external" in combined
    assert "/motion_controller/nav_goal_with_options" in combined


def test_app_manual_and_navigation_fast_paths_cannot_bypass_adapter():
    source = (ROOT / "android_app/robot_bridge/bridge.py").read_text(encoding="utf-8")
    assert "direct_motion_disabled_use_qwen_manager_adapter" in source
    assert "direct_navigation_disabled_use_qwen_manager_adapter" in source
    assert 'result = self.execute_app_plan(command)' in source


def test_external_pet_worker_uses_retained_manager_gate_and_conflict_status():
    source = (ROOT / "robot_skills/resident_pet_worker.py").read_text(encoding="utf-8")
    assert "DurabilityPolicy.TRANSIENT_LOCAL" in source
    assert '"/mapping_manager/state"' in source
    assert '"/motion_controller/sensor_gate_state"' in source
    assert '"/motion_controller/control_conflict"' in source
    assert 'raise RuntimeError("motion_controller_conflict")' in source


def test_head_supervisor_uses_car_real_copy_feedback_topics_and_angle_convention():
    source = (ROOT / "robot_skills/shared_runtime_server.py").read_text(encoding="utf-8")
    assert '"/head/current_angle_deg"' in source
    assert '"/head/angular_rate_dps"' in source
    assert '"/head/roll_deg"' not in source
    runtime = runtime_without_ros()
    runtime.head_feedback_condition = threading.Condition()
    runtime.head_level_angle = 185.0
    runtime.latest_head_roll = None
    runtime._on_head_roll(Message(10.5))
    assert runtime.latest_head_roll[0] == pytest.approx(195.5)


def test_navigation_dry_run_describes_the_same_manager_gateway_as_execute():
    points = json.loads((SKILLS / "points/named_points.json").read_text(encoding="utf-8"))
    study = points["study_projection"]
    env = dict(os.environ)
    env["V8_NAVIGATION_POINTS_DB"] = str(SKILLS / "points/named_points.json")
    completed = subprocess.run(
        [
            sys.executable,
            str(SKILLS / "navigation_goto/run.py"),
            "goto",
            "study_projection",
            "--dry-run",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=5.0,
        check=True,
    )
    result = json.loads(completed.stdout)
    transport = result["transport"]
    assert transport["backend"] == "Car_real_copy/mapping_navigation_manager"
    assert transport["topic"] == "/motion_controller/nav_goal_with_options"
    assert transport["yaw_unit"] == "degrees"
    assert transport["gateway_goal"]["x"] == pytest.approx(-float(study["x"]))
    assert transport["gateway_goal"]["y"] == pytest.approx(-float(study["y"]))
    assert transport["gateway_goal"]["yaw_degrees"] == pytest.approx(-90.0, abs=0.001)
    assert "NavigateToPose" not in completed.stdout


@pytest.mark.parametrize(
    ("skill", "argv"),
    [
        ("move_forward", []),
        ("navigation_goto", ["goto", "origin"]),
        ("head_control", ["up"]),
        ("person_tracking", ["start", "--execute"]),
        ("pet_tracking", ["find", "--backend", "ros2"]),
    ],
)
def test_every_head_or_base_skill_shares_the_motion_domain(skill, argv):
    resident = object.__new__(ResidentSkills)
    resources = resident._resources_for(skill, argv)
    assert "motion_domain" in resources
