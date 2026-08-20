#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from engine import (
    FitnessFramingEventPolicy,
    Candidate,
    ContinuousIdentityTracker,
    PushupPhaseTracker,
    locked_continuity_candidates,
    predict_bbox,
)
from pipeline import Detection, normalized


class SequenceReID:
    def __init__(self, features):
        self.features = [normalized(item) for item in features]

    def feature(self, _crop):
        if not self.features:
            raise AssertionError("feature sequence exhausted")
        return self.features.pop(0)


class IdentityTrackerTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)
        config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
        anchor = normalized([1.0, 0.0, 0.0])
        self.tracker = ContinuousIdentityTracker(
            {"reid_centroid": anchor, "reid_gallery": [anchor]},
            config,
        )

    @staticmethod
    def people(target_box, distractor_box):
        return [Detection(0.95, target_box), Detection(0.94, distractor_box)]

    def update(self, boxes, features):
        return self.tracker.update(self.frame, self.people(*boxes), SequenceReID(features))

    def update_people(self, boxes, features):
        people = [Detection(0.95, box) for box in boxes]
        return self.tracker.update(self.frame, people, SequenceReID(features))

    def test_crossing_freezes_gallery_then_recovers_registered_person(self):
        target_a = [0.96, 0.28, 0.0]
        distractor = [0.18, 0.98, 0.0]
        self.update(((80, 80, 180, 360), (420, 80, 520, 360)), (target_a, distractor))
        selected, _, _ = self.update(
            ((95, 80, 195, 360), (400, 80, 500, 360)),
            ([0.95, 0.30, 0.0], distractor),
        )
        self.assertIsNotNone(selected)
        gallery_size = len(self.tracker.tracklet_gallery)

        selected, _, diagnostics = self.update(
            ((220, 80, 320, 360), (245, 80, 345, 360)),
            ([0.73, 0.68, 0.0], [0.69, 0.72, 0.0]),
        )
        self.assertIsNone(selected)
        self.assertTrue(diagnostics["crossing_ambiguous"])
        self.assertEqual(diagnostics["freeze_reason"], "crossing_ambiguous")
        self.assertEqual(len(self.tracker.tracklet_gallery), gallery_size)

        target_box = (350, 80, 450, 360)
        distractor_box = (120, 80, 220, 360)
        self.update((target_box, distractor_box), ([0.94, 0.32, 0.0], distractor))
        selected, _, diagnostics = self.update(
            ((365, 80, 465, 360), (105, 80, 205, 360)),
            ([0.95, 0.30, 0.0], distractor),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, (365, 80, 465, 360))
        self.assertEqual(diagnostics["selection_mode"], "identity")

    def test_motion_prediction_is_bounded_and_forward(self):
        predicted = predict_bbox(
            (120, 100, 220, 380),
            (100, 100, 200, 380),
            self.frame,
            0.8,
        )
        self.assertIsNotNone(predicted)
        self.assertGreater(predicted[0], 120)
        self.assertLessEqual(predicted[0] - 120, 35)

    def test_face_authenticated_handoff_bridges_immediate_prone_pose(self):
        # This reproduces the real failure: face acquisition authenticated the
        # upright target, then the first counting frame already shows a prone
        # body whose immutable-anchor score is below the normal 0.53 gate.
        self.tracker.bootstrap_authenticated_target((330, 50, 540, 470))
        selected, _, diagnostics = self.update_people(
            ((230, 280, 610, 470),),
            ([0.32, 0.95, 0.0],),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, (230, 280, 610, 470))
        self.assertEqual(diagnostics["selection_mode"], "posture_bridge")
        self.assertTrue(diagnostics["single_posture_bridge_accept"])

    def test_face_authenticated_handoff_rejects_distant_single_bystander(self):
        self.tracker.bootstrap_authenticated_target((420, 40, 620, 470))
        selected, _, diagnostics = self.update_people(
            ((0, 220, 120, 470),),
            ([0.32, 0.95, 0.0],),
        )
        self.assertIsNone(selected)
        self.assertFalse(diagnostics["single_posture_bridge_accept"])

    def test_posture_change_cannot_teleport_lock_to_distant_bystander(self):
        target = [0.96, 0.28, 0.0]
        bystander = [0.40, 0.91, 0.0]
        self.update(((360, 60, 560, 470), (0, 220, 50, 380)), (target, bystander))
        selected, _, _ = self.update(
            ((365, 60, 565, 470), (0, 220, 50, 380)),
            (target, bystander),
        )
        self.assertEqual(selected.person.bbox, (365, 60, 565, 470))

        # Bending/prone appearance is weak against the standing anchor, while
        # the distant bystander now has the larger immutable-anchor score.
        # Physical continuity must keep the authenticated target selected.
        selected, _, diagnostics = self.update(
            ((300, 100, 635, 475), (0, 220, 50, 380)),
            ([0.30, 0.94, 0.0], [0.48, 0.88, 0.0]),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, (300, 100, 635, 475))
        self.assertTrue(diagnostics["motion_gate_active"])
        self.assertEqual(diagnostics["selected_bbox"], [300, 100, 635, 475])

    def test_real_bending_frame_excludes_bystander_before_appearance_ranking(self):
        # Scores and boxes are from recording zhangsan_1786949308 at 1.165 s.
        # The old 0.24 gate admitted both people and association ranking chose
        # the white-shirt bystander (0.6976) over the bending target (0.6482).
        feature = normalized([1.0, 0.0, 0.0])
        bystander = Candidate(
            Detection(0.94, (93, 191, 192, 439)),
            feature,
            0.5701,
            0.6901,
            -1.0,
            0.6901,
            -1.0,
            False,
            0.6801,
            0.2909,
            0.6976,
        )
        target = Candidate(
            Detection(0.95, (241, 182, 429, 450)),
            feature,
            0.4759,
            0.6007,
            -1.0,
            0.6007,
            -1.0,
            False,
            0.5907,
            0.9592,
            0.6482,
        )
        ranked, gate_active, focus_active, max_spatial = locked_continuity_candidates(
            [bystander, target],
            self.tracker.cfg,
        )
        self.assertTrue(gate_active)
        self.assertFalse(focus_active)
        self.assertAlmostEqual(max_spatial, 0.9592)
        self.assertEqual([item.person.bbox for item in ranked], [target.person.bbox])

    def test_spatial_focus_rejects_a_marginally_continuous_bystander(self):
        feature = normalized([1.0, 0.0, 0.0])
        bystander = Candidate(
            Detection(0.94, (93, 191, 192, 439)),
            feature,
            0.70,
            0.80,
            -1.0,
            0.80,
            -1.0,
            False,
            0.79,
            0.50,
            0.83,
        )
        target = Candidate(
            Detection(0.95, (241, 182, 429, 450)),
            feature,
            0.48,
            0.60,
            -1.0,
            0.60,
            -1.0,
            False,
            0.59,
            0.96,
            0.65,
        )
        ranked, gate_active, focus_active, _ = locked_continuity_candidates(
            [bystander, target],
            self.tracker.cfg,
        )
        self.assertTrue(gate_active)
        self.assertTrue(focus_active)
        self.assertEqual([item.person.bbox for item in ranked], [target.person.bbox])

    def test_lost_authenticated_session_cannot_reidentify_moderate_bystander(self):
        target = [0.96, 0.28, 0.0]
        bystander = [0.65, 0.76, 0.0]
        self.tracker.bootstrap_authenticated_target((360, 60, 560, 470))
        selected, _, _ = self.update_people(((365, 60, 565, 470),), (target,))
        self.assertIsNotNone(selected)

        # Keep presenting only a distant, moderately similar body beyond the
        # lost grace window.  The old unlocked path accepted it as the named
        # person after two stable frames and polluted all dynamic galleries.
        for _ in range(int(self.tracker.cfg["lost_grace_frames"]) + 5):
            selected, _, diagnostics = self.update_people(
                ((0, 180, 110, 460),),
                (bystander,),
            )
            self.assertIsNone(selected)
        self.assertTrue(self.tracker.locked)
        self.assertTrue(diagnostics["authenticated_lock_preserved"])

        # The immutable registered anchor can still safely reacquire the real
        # target once it returns.
        for _ in range(int(self.tracker.cfg["stable_accept_frames"])):
            selected, _, diagnostics = self.update_people(
                ((370, 60, 570, 470),),
                (target,),
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, (370, 60, 570, 470))

    def test_trusted_prone_gallery_separates_horizontal_target(self):
        upright = [0.98, 0.20, 0.0]
        bystander = [0.82, 0.57, 0.0]
        self.update(((330, 50, 540, 470), (0, 220, 50, 380)), (upright, bystander))
        self.update(((332, 50, 542, 470), (0, 220, 50, 380)), (upright, bystander))

        prone = [0.20, 0.98, 0.0]
        # A single continuous target safely bridges the first horizontal
        # frames.  After three stable observations the prone gallery starts.
        for index in range(7):
            box = (230 - index, 300, 610 - index, 470)
            selected, _, diagnostics = self.update_people((box,), (prone,))
            self.assertIsNotNone(selected)
        self.assertGreaterEqual(diagnostics["prone_gallery_size"], 5)

        target_box = (220, 300, 600, 470)
        distractor_box = (60, 300, 210, 470)
        selected, candidates, diagnostics = self.update_people(
            (target_box, distractor_box),
            (prone, bystander),
        )
        target = next(item for item in candidates if item.person.bbox == target_box)
        distractor = next(item for item in candidates if item.person.bbox == distractor_box)
        self.assertLess(target.anchor_score, distractor.anchor_score)
        self.assertGreater(target.prone_score, distractor.prone_score)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.person.bbox, target_box)
        self.assertEqual(diagnostics["selected_bbox"], list(target_box))

    def test_ambiguous_crossing_cannot_pollute_prone_gallery(self):
        upright = [0.98, 0.20, 0.0]
        self.update_people(((330, 50, 540, 470),), (upright,))
        self.update_people(((332, 50, 542, 470),), (upright,))
        prone = [0.20, 0.98, 0.0]
        for index in range(7):
            self.update_people(((230 - index, 300, 610 - index, 470),), (prone,))
        gallery_size = len(self.tracker.prone_gallery)
        self.assertGreater(gallery_size, 0)

        selected, _, diagnostics = self.update_people(
            ((220, 300, 600, 470), (240, 300, 620, 470)),
            ([0.65, 0.76, 0.0], [0.64, 0.77, 0.0]),
        )
        self.assertIsNone(selected)
        self.assertTrue(diagnostics["crossing_ambiguous"])
        self.assertEqual(len(self.tracker.prone_gallery), gallery_size)


class PushupLatencyTests(unittest.TestCase):
    def setUp(self):
        config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
        self.tracker = PushupPhaseTracker(config["pushup"])

    def observe(self, angle, wrist):
        return self.tracker.process_observation(angle, wrist, 10.0, 2.0)

    def establish_down(self):
        # Keep the down phase for more than one processed observation so the
        # subsequent transition represents a real dwell, not a one-frame blip.
        self.observe(80.0, 0.3)
        result = self.observe(80.0, 0.3)
        self.assertEqual(result["phase"], "down")

    def test_strong_up_after_confirmed_down_counts_in_one_frame(self):
        self.establish_down()
        # Raise the filtered angle without declaring up yet.
        self.observe(180.0, 0.6)
        self.observe(180.0, 0.6)
        self.observe(180.0, 0.6)
        self.observe(180.0, 0.6)
        result = self.observe(180.0, 0.9)
        self.assertTrue(result["fast_up_confirmation"])
        self.assertTrue(result["incremented"])
        self.assertEqual(result["count"], 1)

    def test_single_up_sample_counts_after_stable_down_phase(self):
        self.establish_down()
        for _ in range(4):
            self.observe(151.0, 0.6)
        self.tracker.filtered_angle = 151.0
        result = self.observe(151.0, 0.72)
        self.assertFalse(result["fast_up_confirmation"])
        self.assertTrue(result["incremented"])

    def test_short_down_transition_cannot_trigger_fast_count(self):
        self.observe(80.0, 0.3)
        self.tracker.filtered_angle = 151.0
        result = self.observe(180.0, 0.9)
        self.assertFalse(result["fast_up_confirmation"])
        self.assertFalse(result["incremented"])

    def test_horizontal_reacquisition_does_not_need_three_extra_frames(self):
        first = self.observe(80.0, 0.3)
        second = self.observe(80.0, 0.3)
        self.assertTrue(first["horizontal_ready"])
        self.assertEqual(first["phase"], "down")
        self.assertEqual(second["phase"], "down")


class FitnessFramingEventPolicyTests(unittest.TestCase):
    def test_centered_person_with_pose_backend_miss_is_not_told_to_reframe(self):
        policy = FitnessFramingEventPolicy(hold_seconds=0.0, repeat_seconds=10.0)
        pose = {
            "valid": False,
            "visible_points": 0,
            "required_points": 8,
            "reason": "no_pose",
        }
        bbox = (120.0, 80.0, 520.0, 470.0)
        self.assertIsNone(policy.observe(pose, bbox, (640, 480), 1.0))
        event = policy.observe(pose, bbox, (640, 480), 1.1)
        self.assertIsNotNone(event)
        self.assertEqual(event["guidance"], "hold_pose")
        self.assertNotIn("没有完整", event["text"])
        self.assertNotIn("挪", event["text"])

    def test_bottom_edge_does_not_tell_pushup_user_to_move_farther(self):
        policy = FitnessFramingEventPolicy(hold_seconds=0.0, repeat_seconds=10.0)
        pose = {
            "valid": False,
            "visible_points": 5,
            "required_points": 8,
            "reason": "elbows_not_visible",
        }
        bbox = (120.0, 180.0, 520.0, 480.0)
        self.assertIsNone(policy.observe(pose, bbox, (640, 480), 1.0))
        event = policy.observe(pose, bbox, (640, 480), 1.1)
        self.assertIsNotNone(event)
        self.assertEqual(event["guidance"], "adjust_position")
        self.assertNotIn("离摄像头远", event["text"])

    def test_top_edge_still_reports_distance_guidance(self):
        policy = FitnessFramingEventPolicy(hold_seconds=0.0, repeat_seconds=10.0)
        pose = {
            "valid": False,
            "visible_points": 5,
            "required_points": 8,
            "reason": "elbows_not_visible",
        }
        bbox = (120.0, 0.0, 520.0, 400.0)
        self.assertIsNone(policy.observe(pose, bbox, (640, 480), 1.0))
        event = policy.observe(pose, bbox, (640, 480), 1.1)
        self.assertIsNotNone(event)
        self.assertEqual(event["guidance"], "move_farther")


if __name__ == "__main__":
    unittest.main()
