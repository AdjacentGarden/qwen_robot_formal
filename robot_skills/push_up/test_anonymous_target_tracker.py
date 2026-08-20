#!/usr/bin/env python3
from __future__ import annotations

import unittest

import numpy as np

from engine import AnonymousTargetTracker
from pipeline import Detection


class AnonymousTargetTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)

    def test_no_person_returns_missing_without_identity_models(self) -> None:
        selected, candidates, state = AnonymousTargetTracker().update(
            self.frame, [], None
        )
        self.assertIsNone(selected)
        self.assertEqual(candidates, [])
        self.assertEqual(state["state"], "missing")
        self.assertIs(state["face_recognition"], False)
        self.assertIs(state["reid"], False)

    def test_person_present_selects_large_central_subject(self) -> None:
        tracker = AnonymousTargetTracker()
        edge = Detection(0.95, (0, 40, 120, 300))
        central = Detection(0.90, (210, 50, 470, 440))
        selected, candidates, state = tracker.update(
            self.frame, [edge, central], None
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, central.bbox)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(state["state"], "anonymous_locked")
        self.assertIs(state["face_recognition"], False)
        self.assertIs(state["reid"], False)

    def test_subsequent_frame_prefers_box_continuity(self) -> None:
        tracker = AnonymousTargetTracker()
        first = Detection(0.90, (210, 50, 470, 440))
        tracker.update(self.frame, [first], None)
        continuous = Detection(0.80, (215, 55, 475, 445))
        intruder = Detection(0.99, (0, 0, 360, 470))
        selected, _, _ = tracker.update(
            self.frame, [intruder, continuous], None
        )
        self.assertEqual(selected.person.bbox, continuous.bbox)


if __name__ == "__main__":
    unittest.main()
