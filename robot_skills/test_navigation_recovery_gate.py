#!/usr/bin/env python3
"""Hardware-free regression tests for post-head-motion navigation recovery."""

from __future__ import annotations

import math
import threading
import unittest
from types import SimpleNamespace

from shared_runtime_server import SharedRuntime


class _HealthFixture:
    def __init__(self, *, advancing: bool, jumping: bool = False):
        self.navigation_health_condition = threading.Condition()
        self.advancing = advancing
        self.jumping = jumping
        self.calls = 0
        self.received = {"scan": 0, "imu": 0, "odom": 0}
        self.advanced = {"scan": 0, "imu": 0, "odom": 0}

    def navigation_sequence_snapshot(self):
        return {
            "received": dict(self.received),
            "stamp_advanced": dict(self.advanced),
        }

    def navigation_health(self, _maximum_age_sec):
        self.calls += 1
        if self.advancing:
            for key in self.received:
                self.received[key] += 1
                self.advanced[key] += 1
        x = 1.0 if self.jumping and self.calls % 2 else 0.0
        return {
            "ok": True,
            "errors": [],
            "navigate_to_pose_ready": True,
            "pose": {"available": True, "x": x, "y": 0.0, "yaw": 0.0, "age_sec": 0.01},
        }


class NavigationRecoveryGateTests(unittest.TestCase):
    def test_rejects_one_old_snapshot_even_if_marked_healthy(self):
        fixture = _HealthFixture(advancing=False)
        result = SharedRuntime.wait_for_navigation_health(
            fixture,
            timeout_sec=0.16,
            stable_sec=0.05,
            minimum_updates=2,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "navigation_inputs_not_ready")
        self.assertEqual(result["stamp_advance_deltas"]["scan"], 0)

    def test_accepts_continuously_advancing_inputs_and_stable_pose(self):
        fixture = _HealthFixture(advancing=True)
        result = SharedRuntime.wait_for_navigation_health(
            fixture,
            timeout_sec=0.50,
            stable_sec=0.08,
            minimum_updates=2,
        )
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["stamp_advance_deltas"]["scan"], 2)
        self.assertGreaterEqual(result["pose_sample_count"], 2)

    def test_rejects_repeated_map_pose_jumps(self):
        fixture = _HealthFixture(advancing=True, jumping=True)
        result = SharedRuntime.wait_for_navigation_health(
            fixture,
            timeout_sec=0.20,
            stable_sec=0.08,
            minimum_updates=1,
            maximum_pose_jump_m=0.20,
        )
        self.assertFalse(result["ok"])

    def test_scan_timestamp_must_advance_and_have_enough_finite_ranges(self):
        runtime = object.__new__(SharedRuntime)
        runtime.navigation_health_condition = threading.Condition()
        runtime.navigation_inputs = {"scan": None, "imu": None, "odom": None}
        runtime.navigation_input_sequences = {"scan": 0, "imu": 0, "odom": 0}
        runtime.navigation_stamp_advance_sequences = {"scan": 0, "imu": 0, "odom": 0}
        runtime.scan_sequence = 0

        def scan(stamp, ranges):
            sec = int(stamp)
            nanosec = int(round((stamp - sec) * 1_000_000_000))
            return SimpleNamespace(
                header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec)),
                ranges=ranges,
                range_min=0.05,
                range_max=12.0,
            )

        SharedRuntime._on_navigation_scan(runtime, scan(10.0, [1.0] * 20))
        SharedRuntime._on_navigation_scan(runtime, scan(10.0, [1.0] * 20))
        sample = runtime.navigation_inputs["scan"]
        self.assertEqual(runtime.navigation_input_sequences["scan"], 2)
        self.assertEqual(runtime.navigation_stamp_advance_sequences["scan"], 1)
        self.assertFalse(sample["stamp_advanced"])
        self.assertTrue(sample["valid"])

        SharedRuntime._on_navigation_scan(runtime, scan(10.1, [math.inf] * 20))
        sample = runtime.navigation_inputs["scan"]
        self.assertEqual(runtime.navigation_stamp_advance_sequences["scan"], 2)
        self.assertFalse(sample["valid"])


if __name__ == "__main__":
    unittest.main()
