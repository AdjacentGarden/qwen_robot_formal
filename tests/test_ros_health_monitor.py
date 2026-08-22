from __future__ import annotations

from ros_health_monitor import evaluate_readiness


def test_readiness_requires_fresh_samples_lifecycle_actions_map_and_tf() -> None:
    now = 100.0
    healthy = {
        "topics": {"scan": 99.8, "imu": 99.7, "odom": 99.9},
        "cmd_vel_subscribers": 1,
        "lifecycle": {
            "map_server": "active",
            "planner_server": "active",
            "bt_navigator": "active",
        },
        "actions": {"compute_path_to_pose": True, "navigate_to_pose": True},
        "map_received": True,
        "tf_ready": True,
        "manager": {
            "state": "NAVIGATION",
            "sensor_gate_enabled": True,
            "sensor_gate_state": "ready",
            "control_conflict": False,
        },
    }
    assert evaluate_readiness(healthy, now) == {
        "base": True,
        "odometry": True,
        "navigation": True,
        "manager": True,
    }
    stale_scan = {**healthy, "topics": {**healthy["topics"], "scan": 90.0}}
    assert evaluate_readiness(stale_scan, now)["base"] is False
    no_tf = {**healthy, "tf_ready": False}
    assert evaluate_readiness(no_tf, now)["navigation"] is False
    safe_stop = {**healthy, "manager": {**healthy["manager"], "state": "SAFE_STOP"}}
    assert evaluate_readiness(safe_stop, now)["manager"] is False


def test_monitor_source_has_no_hardware_command_interfaces() -> None:
    from pathlib import Path

    here = Path(__file__).resolve().parent
    monitor = here / "ros_health_monitor.py"
    if not monitor.exists():
        monitor = here.parent / "ros_health_monitor.py"
    source = monitor.read_text(encoding="utf-8")
    forbidden = ("create_publisher(", ".send_goal", "ChangeState")
    assert all(token not in source for token in forbidden)
