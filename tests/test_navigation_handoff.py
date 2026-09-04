#!/usr/bin/env python3
"""No-hardware regression tests for navigation-to-head handoff."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROBOT_SKILLS = ROOT / "robot_skills"
if str(ROBOT_SKILLS) not in sys.path:
    sys.path.insert(0, str(ROBOT_SKILLS))

from shared_runtime_server import (  # noqa: E402
    SharedRuntime,
    classify_navigation_handoff,
)


def fake_runtime(controller_status: str = "released_navigation") -> SharedRuntime:
    runtime = SharedRuntime.__new__(SharedRuntime)
    runtime.car_feedback_condition = threading.Condition()
    runtime.car_feedback = {
        "manager_state": "NAVIGATION",
        "controller_status": controller_status,
        "control_conflict": False,
    }
    runtime.car_sequences = {key: 1 for key in runtime.car_feedback}
    runtime.navigation_cancel = threading.Event()
    return runtime


class NavigationHandoffTests(unittest.TestCase):
    def test_classifier_requires_idle(self):
        transition = classify_navigation_handoff({
            "manager_state": "NAVIGATION",
            "controller_status": "switching_zero_hold",
        })
        self.assertFalse(transition["ready"])
        self.assertEqual(transition["error"], "navigation_controller_not_idle")

        idle = classify_navigation_handoff({
            "manager_state": "NAVIGATION",
            "controller_status": "idle",
        })
        self.assertTrue(idle["ready"])
        self.assertIsNone(idle["error"])

    def test_waits_through_release_transition_until_idle(self):
        runtime = fake_runtime()

        def release() -> None:
            time.sleep(0.025)
            with runtime.car_feedback_condition:
                runtime.car_feedback["controller_status"] = "switching_zero_hold"
                runtime.car_feedback_condition.notify_all()
            time.sleep(0.025)
            with runtime.car_feedback_condition:
                runtime.car_feedback["controller_status"] = "idle"
                runtime.car_feedback_condition.notify_all()

        worker = threading.Thread(target=release, daemon=True)
        worker.start()
        result = runtime.wait_for_navigation_handoff(timeout_sec=0.5, stable_sec=0.02)
        worker.join(timeout=0.5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["controller_status"], "idle")
        self.assertEqual(
            [item["controller_status"] for item in result["observed"]],
            ["released_navigation", "switching_zero_hold", "idle"],
        )

    def test_stuck_release_times_out_without_claiming_success(self):
        runtime = fake_runtime("switching_zero_hold")
        result = runtime.wait_for_navigation_handoff(timeout_sec=0.06, stable_sec=0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "navigation_handoff_timeout")

    def test_safe_stop_aborts_handoff_immediately(self):
        runtime = fake_runtime("idle")
        runtime.car_feedback["manager_state"] = "SAFE_STOP"
        result = runtime.wait_for_navigation_handoff(timeout_sec=0.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "navigation_handoff_manager_safe_stop")

    def test_success_is_not_returned_before_handoff(self):
        runtime = fake_runtime("idle")
        runtime.wait_for_navigation_handoff = lambda **_kwargs: {
            "ok": False,
            "error": "navigation_handoff_timeout",
            "manager_state": "NAVIGATION",
        }
        result = runtime.finalize_navigation_terminal_result(
            latest={"ok": True, "status": "succeeded"},
            result={"goal": {"point": "white_wall"}},
            request={},
            started=time.monotonic(),
        )
        self.assertEqual(result["status"], "navigation_handoff_timeout")
        self.assertEqual(result["error"], "navigation_handoff_timeout")

    def test_navigation_failure_does_not_wait_for_handoff(self):
        runtime = fake_runtime("released_navigation")

        def unexpected_wait(**_kwargs):
            raise AssertionError("failed navigation must not wait for successful handoff")

        runtime.wait_for_navigation_handoff = unexpected_wait
        result = runtime.finalize_navigation_terminal_result(
            latest={"ok": False, "status": "aborted"},
            result={"goal": {"point": "white_wall"}},
            request={},
            started=time.monotonic(),
        )
        self.assertEqual(result["status"], "aborted")
        self.assertNotIn("post_navigation_handoff", result)


if __name__ == "__main__":
    unittest.main()
