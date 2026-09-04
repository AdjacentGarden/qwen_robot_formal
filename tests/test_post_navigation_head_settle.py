#!/usr/bin/env python3
"""Hardware-free tests for the navigation-to-head quiet window."""

from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROBOT_SKILLS = ROOT / "robot_skills"
if str(ROBOT_SKILLS) not in sys.path:
    sys.path.insert(0, str(ROBOT_SKILLS))

from shared_runtime_server import SharedRuntime  # noqa: E402


class PostNavigationHeadSettleTests(unittest.TestCase):
    def runtime(self, age_sec: float | None) -> SharedRuntime:
        runtime = SharedRuntime.__new__(SharedRuntime)
        runtime.last_navigation_handoff_monotonic = (
            0.0 if age_sec is None else time.monotonic() - age_sec
        )
        return runtime

    def test_waits_only_for_remaining_window(self) -> None:
        runtime = self.runtime(0.03)
        started = time.monotonic()
        result = runtime.wait_for_post_navigation_head_settle(settle_sec=0.08)
        elapsed = time.monotonic() - started
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "post_navigation_settle")
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.12)

    def test_old_navigation_adds_no_delay(self) -> None:
        runtime = self.runtime(1.0)
        started = time.monotonic()
        result = runtime.wait_for_post_navigation_head_settle(settle_sec=0.08)
        self.assertEqual(result["reason"], "already_settled")
        self.assertEqual(result["waited_sec"], 0.0)
        self.assertLess(time.monotonic() - started, 0.03)

    def test_no_navigation_adds_no_delay(self) -> None:
        runtime = self.runtime(None)
        result = runtime.wait_for_post_navigation_head_settle(settle_sec=0.08)
        self.assertEqual(result["reason"], "no_recent_navigation_handoff")
        self.assertEqual(result["waited_sec"], 0.0)


if __name__ == "__main__":
    unittest.main()
