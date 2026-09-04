import math

import pytest

from robot_skills.car_real_contract import (
    classify_nav_status,
    gateway_pose_to_map,
    head_status_matches,
    manager_allows_motion,
    map_pose_to_gateway,
    normalize_degrees,
)


@pytest.mark.parametrize(
    ("pose", "gateway"),
    [
        ((0.0, 0.0, -math.pi), (-0.0, -0.0, 0.0)),
        ((-2.2, 0.1, -math.pi / 2), (2.2, -0.1, 90.0)),
        ((0.0, 3.0, math.pi / 2), (-0.0, -3.0, -90.0)),
    ],
)
def test_known_points_convert_without_changing_physical_goal(pose, gateway):
    converted = map_pose_to_gateway(*pose)
    assert converted["x"] == pytest.approx(gateway[0])
    assert converted["y"] == pytest.approx(gateway[1])
    assert converted["yaw_degrees"] == pytest.approx(gateway[2])
    restored = gateway_pose_to_map(
        converted["x"], converted["y"], converted["yaw_degrees"]
    )
    assert restored["x"] == pytest.approx(pose[0])
    assert restored["y"] == pytest.approx(pose[1])
    assert math.sin(restored["yaw"] - pose[2]) == pytest.approx(0.0, abs=1e-7)


def test_degree_normalization_and_invalid_values():
    assert normalize_degrees(270.0) == pytest.approx(-90.0)
    assert normalize_degrees(180.0) == pytest.approx(180.0)
    with pytest.raises(ValueError):
        normalize_degrees(math.nan)


@pytest.mark.parametrize(
    ("state", "gate", "allowed", "reason"),
    [
        ("NAVIGATION", "ready", True, None),
        ("WAIT_BASE", "ready", False, "manager_not_navigation:wait_base"),
        ("NAVIGATION", "recovering", False, "sensor_gate_not_ready:recovering"),
        ("SAFE_STOP", "ready", False, "manager_safe_stop"),
    ],
)
def test_manager_motion_gate(state, gate, allowed, reason):
    assert manager_allows_motion(state, gate) == (allowed, reason)


def test_navigation_status_classifier_and_stale_head_status():
    assert classify_nav_status("accepted_by_nav2")["phase"] == "active"
    assert classify_nav_status("navigating;elapsed_s=1.0")["phase"] == "active"
    assert classify_nav_status("succeeded")["ok"] is True
    assert classify_nav_status("rejected_goal_outside_current_map")["phase"] == "failure"
    assert classify_nav_status("waiting_nav2_accept")["phase"] == "active"
    assert classify_nav_status("wall_alignment_rejected:busy")["phase"] == "failure"
    assert classify_nav_status("wall_alignment_cancelled")["phase"] == "failure"
    assert classify_nav_status("wall_alignment_service_error")["phase"] == "failure"
    assert head_status_matches("succeeded;target=185;error=+0.3", 185)
    assert not head_status_matches("succeeded;target=211;error=+0.3", 185)
