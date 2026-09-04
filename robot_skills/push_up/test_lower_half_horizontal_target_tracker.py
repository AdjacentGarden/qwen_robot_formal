#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from engine import LowerHalfHorizontalTargetTracker, PushupPhaseTracker
from pipeline import Detection


ROOT = Path(__file__).resolve().parent


class LowerHalfHorizontalTargetTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)

    def test_upright_person_is_not_selected(self) -> None:
        tracker = LowerHalfHorizontalTargetTracker(self.config)
        upright = Detection(0.95, (230, 70, 390, 460))
        selected, candidates, state = tracker.update(self.frame, [upright], None)
        self.assertIsNone(selected)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].wide_body)
        self.assertEqual(state["freeze_reason"], "waiting_for_horizontal_target")

    def test_single_lower_horizontal_person_locks_after_three_frames(self) -> None:
        tracker = LowerHalfHorizontalTargetTracker(self.config)
        boxes = [
            Detection(0.95, (150, 300, 530, 450)),
            Detection(0.95, (152, 301, 532, 451)),
            Detection(0.95, (154, 302, 534, 452)),
        ]
        for index, person in enumerate(boxes, start=1):
            selected, _, state = tracker.update(self.frame, [person], None)
            if index < 3:
                self.assertIsNone(selected)
                self.assertEqual(state["freeze_reason"], "geometry_target_stabilizing")
            else:
                self.assertIsNotNone(selected)
                self.assertEqual(selected.person.bbox, person.bbox)
                self.assertEqual(state["state"], "geometry_locked")
                self.assertFalse(state["reid"])

    def test_two_eligible_people_pause_instead_of_guessing(self) -> None:
        tracker = LowerHalfHorizontalTargetTracker(self.config)
        first = Detection(0.95, (20, 300, 300, 455))
        second = Detection(0.93, (330, 300, 625, 455))
        selected, _, state = tracker.update(self.frame, [first, second], None)
        self.assertIsNone(selected)
        self.assertEqual(state["freeze_reason"], "multiple_horizontal_targets")
        self.assertEqual(state["eligible_people"], 2)

    def test_small_lower_box_is_rejected(self) -> None:
        tracker = LowerHalfHorizontalTargetTracker(self.config)
        small = Detection(0.95, (250, 390, 390, 450))
        selected, candidates, state = tracker.update(self.frame, [small], None)
        self.assertIsNone(selected)
        self.assertFalse(candidates[0].wide_body)
        self.assertEqual(state["freeze_reason"], "waiting_for_horizontal_target")

    def test_initial_down_then_up_counts_first_real_repetition(self) -> None:
        tracker = PushupPhaseTracker(self.config["pushup"])
        for _ in range(5):
            result = tracker.process_observation(75.0, 0.35, 12.0, 2.0)
        self.assertTrue(result["armed"])
        self.assertTrue(result["initial_down_seeded"])
        self.assertEqual(result["phase"], "down")
        for _ in range(5):
            result = tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        self.assertEqual(result["count"], 1)

    def test_initial_up_does_not_create_a_false_repetition(self) -> None:
        tracker = PushupPhaseTracker(self.config["pushup"])
        result = tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        self.assertTrue(result["armed"])
        self.assertEqual(result["phase"], "up")
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
