from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

from resident_runtime_server import ResidentSkills


class HeadPublisher:
    def get_subscription_count(self):
        return 1


class Runtime:
    def __init__(self, confirmations, commands=None):
        self.confirmations = [dict(value) for value in confirmations]
        self.commands = [dict(value) for value in (commands or [])]
        self.wait_arguments = []
        self.command_arguments = []

    def wait_for_head_target(self, angle, **kwargs):
        self.wait_arguments.append((angle, dict(kwargs)))
        if not self.confirmations:
            raise AssertionError("unexpected confirmation")
        return self.confirmations.pop(0)

    def command_head(self, angle, **kwargs):
        self.command_arguments.append((angle, dict(kwargs)))
        if not self.commands:
            raise AssertionError("unexpected motor command")
        return self.commands.pop(0)


def server(runtime):
    value = ResidentSkills.__new__(ResidentSkills)
    value.runtime = runtime
    value.head_pub = HeadPublisher()
    value.head_control_lock = threading.RLock()
    value.gate_calls = []

    def gate(enabled, timeout=2.0):
        value.gate_calls.append((bool(enabled), float(timeout)))
        return {
            "ok": True,
            "enabled": bool(enabled),
            "state": "ready" if enabled else "disabled",
        }

    value._set_lidar_live = gate
    return value


class StrictLevelGateTests(unittest.TestCase):
    def test_strict_helper_uses_independent_lidar_thresholds(self):
        runtime = Runtime([{"ok": True}])
        value = server(runtime)
        with patch.dict(os.environ, {
            "HEAD_LIDAR_LEVEL_TOLERANCE_DEG": "1.8",
            "HEAD_LIDAR_LEVEL_STABLE_SEC": "0.9",
            "HEAD_LIDAR_LEVEL_MAX_SPAN_DEG": "0.15",
            "HEAD_LIDAR_LEVEL_MAX_RATE_DPS": "0.25",
            "HEAD_LIDAR_LEVEL_CONFIRM_TIMEOUT_SEC": "3.5",
        }, clear=False):
            result = value._confirm_level_for_lidar(185)
        arguments = runtime.wait_arguments[0][1]
        self.assertEqual(arguments["tolerance_deg"], 1.8)
        self.assertEqual(arguments["stable_sec"], 0.9)
        self.assertEqual(arguments["maximum_roll_span_deg"], 0.15)
        self.assertEqual(arguments["maximum_rate_dps"], 0.25)
        self.assertEqual(arguments["timeout_sec"], 3.5)
        self.assertTrue(result["ok"])

    def test_level_timeout_can_be_accepted_by_strict_project_confirmation(self):
        runtime = Runtime(
            [
                {"ok": False, "available": True, "error_deg": 26.0},
                {"ok": True, "available": True, "error_deg": 1.2},
            ],
            commands=[{"ok": False, "subscribers": 1, "status": "timeout"}],
        )
        value = server(runtime)
        result = value._move_head_resident("level", update_supervisor=False)
        self.assertTrue(result["ok"])
        self.assertEqual(len(runtime.command_arguments), 1)
        self.assertEqual(value.gate_calls[0][0], False)
        self.assertEqual(value.gate_calls[-1][0], True)
        self.assertEqual(result["timing"]["retry_count"], 0)
        self.assertEqual(result["acceptance"]["source"], "strict_project_level_confirmation")

    def test_offset_level_gets_at_most_one_bounded_retry(self):
        runtime = Runtime(
            [
                {"ok": False, "available": True, "error_deg": 26.0},
                {"ok": False, "available": True, "error_deg": 3.1},
                {"ok": True, "available": True, "error_deg": 1.0},
            ],
            commands=[
                {"ok": False, "subscribers": 1, "status": "timeout"},
                {"ok": True, "subscribers": 1, "status": "succeeded"},
            ],
        )
        value = server(runtime)
        result = value._move_head_resident("level", update_supervisor=False)
        self.assertTrue(result["ok"])
        self.assertEqual(len(runtime.command_arguments), 2)
        self.assertEqual(result["timing"]["retry_count"], 1)
        self.assertIsNotNone(result["retry_feedback"])

    def test_unstable_but_already_centered_level_does_not_kick_motor_again(self):
        runtime = Runtime(
            [
                {"ok": False, "available": True, "error_deg": 26.0},
                {"ok": False, "available": True, "error_deg": 1.7},
            ],
            commands=[{"ok": False, "subscribers": 1, "status": "timeout"}],
        )
        value = server(runtime)
        result = value._move_head_resident("level", update_supervisor=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "head_level_not_safe_for_lidar")
        self.assertEqual(len(runtime.command_arguments), 1)
        self.assertEqual(result["timing"]["retry_count"], 0)
        self.assertEqual(value.gate_calls, [(False, 2.0)])

    def test_stable_tilt_remains_idempotent_and_gate_stays_disabled(self):
        runtime = Runtime([{"ok": True, "available": True, "error_deg": 0.2}])
        value = server(runtime)
        result = value._move_head_resident("up", update_supervisor=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["head_motion_skipped"])
        self.assertEqual(runtime.command_arguments, [])
        self.assertEqual(value.gate_calls, [(False, 2.0)])


if __name__ == "__main__":
    unittest.main()
