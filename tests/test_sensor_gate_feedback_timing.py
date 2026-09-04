from __future__ import annotations

import threading

from resident_runtime_server import ResidentSkills
from shared_runtime_server import SharedRuntime


class _Request:
    def __init__(self):
        self.data = False


class _SetBool:
    Request = _Request


class _Response:
    success = True
    message = "sensor gate disabled"


class _Future:
    def result(self):
        return _Response()


class _Client:
    def wait_for_service(self, *, timeout_sec):
        assert timeout_sec > 0.0
        return True

    def call_async(self, request):
        assert request.data is False
        return _Future()


def _runtime(state="ready"):
    runtime = object.__new__(SharedRuntime)
    runtime.SetBool = _SetBool
    runtime.sensor_gate_client = _Client()
    runtime.car_feedback_condition = threading.Condition()
    runtime.car_feedback = {
        "sensor_gate_state": state,
        "sensor_gate_enabled": state != "disabled",
    }
    runtime.car_sequences = {"sensor_gate_state": 7}
    runtime.car_feedback_snapshot = lambda: {
        **runtime.car_feedback,
        "sequences": dict(runtime.car_sequences),
    }
    return runtime


def test_disable_keeps_real_remaining_budget_for_late_status_feedback():
    runtime = _runtime("ready")
    runtime._wait_future = lambda future, timeout_sec: "done"
    observed = {}

    def wait(key, *, after_sequence, predicate, timeout_sec, cancel_event=None):
        observed.update(
            key=key,
            after_sequence=after_sequence,
            timeout_sec=timeout_sec,
        )
        assert predicate("disabled")
        return {"ok": True, "value": "disabled", "sequence": after_sequence + 1}

    runtime.wait_for_car_feedback = wait
    result = runtime.set_sensor_gate_enabled(False, timeout_sec=3.0)

    assert result["ok"] is True
    assert observed["key"] == "sensor_gate_state"
    assert observed["after_sequence"] == 7
    # Regression: the old fixed `timeout_sec - 3.0` calculation yielded 0.1s.
    assert observed["timeout_sec"] >= 2.5


def test_disable_accepts_status_that_arrives_before_service_response():
    runtime = _runtime("ready")

    def finish_service(future, timeout_sec):
        runtime.car_feedback["sensor_gate_state"] = "disabled"
        runtime.car_feedback["sensor_gate_enabled"] = False
        runtime.car_sequences["sensor_gate_state"] += 1
        return "done"

    runtime._wait_future = finish_service
    runtime.wait_for_car_feedback = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("fresh snapshot should avoid a second feedback wait")
    )

    result = runtime.set_sensor_gate_enabled(False, timeout_sec=3.0)

    assert result["ok"] is True
    assert result["state"] == "disabled"


class _Publisher:
    @staticmethod
    def get_subscription_count():
        return 1


class _HeadRuntime:
    def __init__(self):
        self.commands = 0
        self.confirmations = 0
        self.sessions = 0

    @staticmethod
    def wait_for_post_navigation_head_settle():
        return {"ok": True, "waited_sec": 0.0}

    def wait_for_head_target(self, *args, **kwargs):
        self.confirmations += 1
        return {
            "ok": False,
            "available": True,
            "error_deg": -26.0,
        }

    def begin_head_tilt_session(self, **kwargs):
        self.sessions += 1

    def command_head(self, *args, **kwargs):
        self.commands += 1
        return {
            "ok": self.commands == 2,
            "subscribers": 1,
            "status": "succeeded" if self.commands == 2 else "timeout",
        }


def test_tilt_retries_once_after_fresh_feedback_confirms_first_miss(monkeypatch):
    monkeypatch.setenv("QWEN_PROJECT_LOCALIZATION_GUARD", "1")
    owner = object.__new__(ResidentSkills)
    owner.runtime = _HeadRuntime()
    owner.head_pub = _Publisher()
    owner.head_control_lock = threading.Lock()
    owner.head_pose_supervisor = None
    owner._set_lidar_live = lambda enabled: {
        "ok": not enabled,
        "state": "disabled",
    }

    result = owner._move_head_resident("up", update_supervisor=False)

    assert result["ok"] is True
    assert result["timing"]["retry_count"] == 1
    assert result["retry_feedback"]["ok"] is True
    assert owner.runtime.commands == 2
    assert owner.runtime.sessions == 1
