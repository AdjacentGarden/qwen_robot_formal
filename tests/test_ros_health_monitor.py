from __future__ import annotations

import threading
from types import SimpleNamespace

from ros_health_monitor import HealthMonitor, evaluate_readiness


def _snapshot(*, manager_state: str = "NAVIGATION") -> dict:
    return {
        "topics": {},
        "cmd_vel_subscribers": 0,
        "lifecycle": {
            "map_server": "unknown",
            "planner_server": "unknown",
            "bt_navigator": "unknown",
        },
        "actions": {
            "compute_path_to_pose": False,
            "navigate_to_pose": False,
        },
        "map_received": False,
        "tf_ready": False,
        "manager": {
            "state": manager_state,
            "sensor_gate_enabled": True,
            "sensor_gate_state": "ready",
            "control_conflict": False,
        },
    }


def test_authoritative_manager_ready_is_not_vetoed_by_stale_lifecycle_cache() -> None:
    readiness = evaluate_readiness(_snapshot(), now=100.0)
    assert readiness == {
        "base": False,
        "odometry": False,
        "navigation": False,
        "manager": True,
    }


def test_manager_still_requires_navigation_state_ready_gate_and_no_conflict() -> None:
    not_navigation = _snapshot(manager_state="WAIT_NAVIGATION_READY")
    assert evaluate_readiness(not_navigation, 100.0)["manager"] is False

    gate_disabled = _snapshot()
    gate_disabled["manager"]["sensor_gate_enabled"] = False
    assert evaluate_readiness(gate_disabled, 100.0)["manager"] is False

    conflict = _snapshot()
    conflict["manager"]["control_conflict"] = True
    assert evaluate_readiness(conflict, 100.0)["manager"] is False


class FakeFuture:
    def __init__(self) -> None:
        self._done = False
        self._cancelled = False
        self._callbacks = []
        self._label = "unknown"

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self._cancelled = True
        self._done = True
        for callback in tuple(self._callbacks):
            callback(self)

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)
        if self._done:
            callback(self)

    def result(self):
        if self._cancelled:
            raise RuntimeError("cancelled")
        return SimpleNamespace(current_state=SimpleNamespace(label=self._label))

    def complete(self, label: str) -> None:
        self._label = label
        self._done = True
        for callback in tuple(self._callbacks):
            callback(self)


class FakeClient:
    def __init__(self, futures) -> None:
        self.futures = list(futures)

    def service_is_ready(self) -> bool:
        return True

    def call_async(self, _request):
        return self.futures.pop(0)


def test_timed_out_lifecycle_request_is_retired_and_retried() -> None:
    old_future = FakeFuture()
    new_future = FakeFuture()
    monitor = object.__new__(HealthMonitor)
    monitor.lock = threading.RLock()
    monitor.GetState = SimpleNamespace(Request=lambda: object())
    monitor.snapshot = _snapshot()
    monitor.pending_lifecycle = {}
    monitor.lifecycle_generations = {"planner_server": 0}
    monitor.last_lifecycle_request = 0.0
    monitor.lifecycle_clients = {
        "planner_server": FakeClient([old_future, new_future]),
    }

    monitor.request_lifecycle_states(1.0)
    assert monitor.pending_lifecycle["planner_server"][0] is old_future

    monitor.request_lifecycle_states(3.1)
    assert old_future._cancelled is True
    assert monitor.pending_lifecycle["planner_server"][0] is new_future
    assert monitor.snapshot["lifecycle"]["planner_server"] == "unknown"

    new_future.complete("active")
    assert "planner_server" not in monitor.pending_lifecycle
    assert monitor.snapshot["lifecycle"]["planner_server"] == "active"


def test_manager_restart_invalidates_inflight_lifecycle_requests() -> None:
    pending = FakeFuture()
    monitor = object.__new__(HealthMonitor)
    monitor.lock = threading.RLock()
    monitor.snapshot = _snapshot()
    monitor.pending_lifecycle = {"planner_server": (pending, 1.0, 1)}
    monitor.lifecycle_generations = {"planner_server": 1}

    monitor.mark_manager("state", SimpleNamespace(data="WAIT_BASE"))

    assert pending._cancelled is True
    assert monitor.pending_lifecycle == {}
    assert monitor.snapshot["lifecycle"]["planner_server"] == "unknown"

