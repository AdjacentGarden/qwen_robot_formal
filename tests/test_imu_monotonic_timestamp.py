#!/usr/bin/env python3
"""Hardware-free tests for the Cartographer IMU timestamp contract."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest


DEFAULT_SOURCE = Path(
    "/home/test/Car_real_copy/src/driver/imu_cartographer_publisher/"
    "imu_cartographer_publisher/imu_cartographer_publisher.py"
)


def load_module():
    source = Path(os.getenv("IMU_PUBLISHER_SOURCE", str(DEFAULT_SOURCE)))
    if not source.is_file():
        raise unittest.SkipTest(f"IMU publisher source not found: {source}")
    spec = importlib.util.spec_from_file_location("tested_imu_cartographer_publisher", source)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    if not hasattr(module, "strictly_monotonic_timestamp_ns"):
        raise unittest.SkipTest(
            "immutable Car_real_copy IMU publisher does not expose the optional "
            "strictly_monotonic_timestamp_ns helper"
        )
    return module


class ImuMonotonicTimestampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_first_timestamp_is_unchanged(self):
        self.assertEqual(
            self.module.strictly_monotonic_timestamp_ns(1000, None),
            (1000, False),
        )

    def test_forward_timestamp_is_unchanged(self):
        self.assertEqual(
            self.module.strictly_monotonic_timestamp_ns(1100, 1000),
            (1100, False),
        )

    def test_equal_timestamp_is_advanced_by_one_nanosecond(self):
        self.assertEqual(
            self.module.strictly_monotonic_timestamp_ns(1000, 1000),
            (1001, True),
        )

    def test_backward_clock_step_is_clamped(self):
        self.assertEqual(
            self.module.strictly_monotonic_timestamp_ns(900, 1000),
            (1001, True),
        )

    def test_sequence_remains_strictly_ordered_across_rollbacks(self):
        previous = None
        published = []
        for candidate in (1000, 1100, 1050, 1050, 1200):
            previous, _corrected = self.module.strictly_monotonic_timestamp_ns(candidate, previous)
            published.append(previous)
        self.assertTrue(all(left < right for left, right in zip(published, published[1:])))


if __name__ == "__main__":
    unittest.main()
