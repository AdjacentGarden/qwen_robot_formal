from __future__ import annotations

import unittest

from resident_runtime_server import ResidentSkills


class FakeRuntime:
    def __init__(self, snapshot, *, feedback=None):
        self.snapshot = dict(snapshot)
        self.feedback = dict(feedback or {"ok": True, "value": "ready", "sequence": 2})
        self.set_calls = []
        self.wait_calls = []

    def car_feedback_snapshot(self):
        return dict(self.snapshot)

    def set_sensor_gate_enabled(self, enabled, timeout_sec):
        self.set_calls.append((bool(enabled), float(timeout_sec)))
        return {"ok": True, "enabled": bool(enabled), "state": "ready" if enabled else "disabled"}

    def wait_for_car_feedback(self, key, **kwargs):
        self.wait_calls.append((key, dict(kwargs)))
        return dict(self.feedback)


def build_server(runtime):
    server = ResidentSkills.__new__(ResidentSkills)
    server.runtime = runtime
    return server


class SensorGateIdempotenceTests(unittest.TestCase):
    def test_ready_enable_does_not_restart_recovery(self):
        runtime = FakeRuntime({
            "sensor_gate_enabled": True,
            "sensor_gate_state": "ready",
            "sequences": {"sensor_gate_state": 7},
        })
        result = build_server(runtime)._set_lidar_live(True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "already_ready")
        self.assertEqual(runtime.set_calls, [])
        self.assertEqual(runtime.wait_calls, [])

    def test_disabled_disable_is_already_complete(self):
        runtime = FakeRuntime({
            "sensor_gate_enabled": False,
            "sensor_gate_state": "disabled",
            "sequences": {"sensor_gate_state": 4},
        })
        result = build_server(runtime)._set_lidar_live(False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "already_disabled")
        self.assertEqual(runtime.set_calls, [])

    def test_existing_recovery_is_joined_without_reset(self):
        runtime = FakeRuntime({
            "sensor_gate_enabled": True,
            "sensor_gate_state": "recovering",
            "sequences": {"sensor_gate_state": 11},
        })
        result = build_server(runtime)._set_lidar_live(True, timeout=3.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "joined_existing_recovery")
        self.assertEqual(runtime.set_calls, [])
        self.assertEqual(len(runtime.wait_calls), 1)
        self.assertEqual(runtime.wait_calls[0][0], "sensor_gate_state")
        self.assertEqual(runtime.wait_calls[0][1]["after_sequence"], 11)

    def test_real_state_change_still_calls_car_service(self):
        runtime = FakeRuntime({
            "sensor_gate_enabled": False,
            "sensor_gate_state": "disabled",
            "sequences": {"sensor_gate_state": 5},
        })
        result = build_server(runtime)._set_lidar_live(True, timeout=2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(runtime.set_calls, [(True, 16.0)])


if __name__ == "__main__":
    unittest.main()
