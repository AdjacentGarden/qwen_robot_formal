#!/usr/bin/env python3
"""Ensure navigation diagnostics are truthful and speakable."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skill_runner import build_spoken_summary  # noqa: E402


class NavigationFailureSpeechTests(unittest.TestCase):
    def summary(self, error: str) -> str:
        step = SimpleNamespace(skill_name="navigation_goto", arguments={"point": "study_projection"})
        return build_spoken_summary(SimpleNamespace(), step, {"ok": False, "error": error})

    def test_cartographer_failure_is_not_reported_as_generic_navigation_failure(self):
        spoken = self.summary("localization_node_unavailable")
        self.assertIn("定位节点", spoken)
        self.assertIn("没有启动导航", spoken)

    def test_duplicate_imu_has_explicit_conflict_message(self):
        spoken = self.summary("imu_publisher_conflict")
        self.assertIn("重复", spoken)
        self.assertIn("惯性传感器", spoken)

    def test_real_no_path_is_not_misreported_as_localization_failure(self):
        spoken = self.summary("navigation_no_valid_path")
        self.assertIn("规划不出安全路径", spoken)
        self.assertNotIn("定位节点", spoken)


if __name__ == "__main__":
    unittest.main()
