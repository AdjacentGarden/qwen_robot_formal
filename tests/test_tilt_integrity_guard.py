#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "robot_skills"))

from localization_integrity import (
    compare_localization,
    evaluate_scan_window,
    motion_authorization_error,
)
from scenario_engine import ScenarioCatalog, ScenarioExecutor


def scan(values, sequence=1):
    return {
        "valid": sum(value is not None for value in values) >= 8,
        "ranges": list(values),
        "sequence": sequence,
    }


class LocalizationIntegrityTests(unittest.TestCase):
    def test_base_authorization_is_blocked_until_integrity_is_ready(self):
        self.assertIsNone(motion_authorization_error({"state": "ready"}))
        for state in ("tilted", "recovering", "invalid"):
            with self.subTest(state=state):
                self.assertIn(
                    "localization_integrity_not_ready",
                    motion_authorization_error({"state": state, "reason": "test"}),
                )

    def test_stable_level_scan_matches_baseline(self):
        baseline = scan([1.0 + index * 0.01 for index in range(100)])
        samples = [
            scan([value + (index % 2) * 0.01 for value in baseline["ranges"]], index + 2)
            for index in range(8)
        ]
        result = evaluate_scan_window(samples, baseline)
        self.assertTrue(result["ok"], result)

    def test_floor_or_wrong_angle_profile_is_rejected(self):
        baseline = scan([2.0 + index * 0.01 for index in range(100)])
        wrong = [scan([0.25] * 100, index + 2) for index in range(8)]
        result = evaluate_scan_window(wrong, baseline)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "level_scan_differs_from_baseline")

    def test_sparse_scan_is_rejected(self):
        sparse = [scan([None] * 96 + [1.0] * 4, index) for index in range(10)]
        result = evaluate_scan_window(sparse, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_valid_level_scans")

    def test_stable_pose_and_map_are_accepted(self):
        baseline_pose = {"x": 1.0, "y": 2.0, "yaw": 0.1}
        current_pose = {"x": 1.04, "y": 1.98, "yaw": 0.12}
        baseline_map = {"min_x": -4.0, "min_y": -2.0, "max_x": 5.0, "max_y": 4.0}
        current_map = {"min_x": -4.0, "min_y": -2.0, "max_x": 5.05, "max_y": 4.0}
        self.assertTrue(compare_localization(baseline_pose, current_pose, baseline_map, current_map)["ok"])

    def test_map_expansion_and_pose_teleport_are_rejected(self):
        baseline_pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
        current_pose = {"x": -6.89, "y": -8.57, "yaw": 1.4}
        baseline_map = {"min_x": -4.4, "min_y": -1.6, "max_x": 5.1, "max_y": 4.7}
        current_map = {"min_x": -4.4, "min_y": -1.6, "max_x": 10.9, "max_y": 11.9}
        result = compare_localization(baseline_pose, current_pose, baseline_map, current_map)
        self.assertFalse(result["ok"])
        self.assertIn("post_recovery_pose_shift", result["errors"])
        self.assertIn("post_recovery_map_bounds_jump", result["errors"])


class ScenarioCleanupTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ScenarioCatalog(PROJECT / "scenarios" / "procedure_catalog.json")

    @staticmethod
    def result(ok=True):
        return {
            "ok": ok,
            "validation_ok": ok,
            "executed": True,
            "spoken_summary": "",
            "error": None if ok else "injected_failure",
        }

    def test_workout_failure_still_closes_projector_and_levels(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, arguments.get("action")))
            return self.result(ok=skill != "push_up")

        output = ScenarioExecutor(self.catalog, invoke).execute(
            "push_up_companion", {"duration": 30}, announce=False
        )
        self.assertFalse(output["ok"])
        self.assertIn(("projector_control", "off"), calls)
        self.assertIn(("head_control", "level"), calls)

    def test_meeting_projection_failure_rolls_back_head(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, arguments.get("action")))
            return self.result(ok=not (skill == "projector_control" and arguments.get("action") == "meeting_presentation_on"))

        output = ScenarioExecutor(self.catalog, invoke).execute(
            "meeting_projection", {"stay_put": True, "navigate": False}, announce=False
        )
        self.assertFalse(output["ok"])
        self.assertIn(("projector_control", "off"), calls)
        self.assertEqual(calls[-1], ("head_control", "level"))

    def test_successful_meeting_stays_projecting(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, arguments.get("action")))
            return self.result(ok=True)

        output = ScenarioExecutor(self.catalog, invoke).execute(
            "meeting_projection", {"stay_put": True, "navigate": False}, announce=False
        )
        self.assertTrue(output["ok"])
        self.assertNotIn(("projector_control", "off"), calls)
        self.assertNotEqual(calls[-1], ("head_control", "level"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
