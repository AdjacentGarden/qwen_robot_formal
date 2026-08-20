#!/usr/bin/env python3
from __future__ import annotations

import unittest

from autonomy_exploration import AutonomyEngine


class FakeRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.wait_calls = []

    def wait_for_head_target(self, target, **kwargs):
        self.wait_calls.append((target, dict(kwargs)))
        if self.responses:
            return dict(self.responses.pop(0))
        return {"ok": False, "available": False, "error": "head_feedback_not_received"}


class FakeOwner:
    def __init__(self, responses):
        self.runtime = FakeRuntime(responses)
        self.head_calls = []

    def _head(self, argv):
        self.head_calls.append(list(argv))
        return 0


def make_engine(responses):
    engine = AutonomyEngine.__new__(AutonomyEngine)
    engine.owner = FakeOwner(responses)
    engine._last_head_feedback = {}
    return engine


CFG = {
    "head_raise_command_offset_deg": 5.0,
    "head_level_command_offset_deg": 5.0,
    "head_raise_target_tolerance_deg": 2.0,
    "head_level_target_tolerance_deg": 3.0,
    "head_closed_loop_max_attempts": 4,
    "head_closed_loop_attempt_timeout_sec": 0.01,
    "head_closed_loop_correction_gain": 1.0,
    "head_closed_loop_max_correction_deg": 7.0,
    "head_closed_loop_min_correction_deg": 1.0,
}


class ClosedLoopHeadTests(unittest.TestCase):
    @staticmethod
    def commanded_angles(engine):
        return [int(call[call.index("--angle") + 1]) for call in engine.owner.head_calls]

    def test_adapts_command_from_physical_error(self):
        engine = make_engine([
            {"ok": False, "available": True, "roll_deg": 206.0, "error_deg": -5.0},
            {"ok": True, "available": True, "roll_deg": 211.2, "error_deg": 0.2},
        ])
        self.assertTrue(engine._raise_head_and_wait(dict(CFG), 211))
        self.assertEqual(self.commanded_angles(engine), [216, 221])
        self.assertEqual(engine._last_head_feedback["final"]["roll_deg"], 211.2)

    def test_correction_is_clamped_for_bad_feedback(self):
        engine = make_engine([
            {"ok": False, "available": True, "roll_deg": 180.0, "error_deg": -31.0},
            {"ok": True, "available": True, "roll_deg": 211.0, "error_deg": 0.0},
        ])
        self.assertTrue(engine._raise_head_and_wait(dict(CFG), 211))
        self.assertEqual(self.commanded_angles(engine), [216, 223])

    def test_never_claims_arrival_without_feedback(self):
        engine = make_engine([
            {"ok": False, "available": False, "error": "head_feedback_not_received"}
            for _ in range(4)
        ])
        self.assertFalse(engine._raise_head_and_wait(dict(CFG), 211))
        self.assertEqual(self.commanded_angles(engine), [216, 216, 216, 216])
        self.assertFalse(engine._last_head_feedback["ok"])

    def test_level_uses_calibrated_angle_command(self):
        engine = make_engine([
            {"ok": True, "available": True, "roll_deg": 185.0, "error_deg": 0.0}
        ])
        self.assertTrue(engine._level_head_and_wait(dict(CFG)))
        self.assertEqual(self.commanded_angles(engine), [190])

    def test_capture_gate_requires_fresh_success(self):
        engine = make_engine([
            {"ok": True, "available": True, "roll_deg": 211.0, "error_deg": 0.0}
        ])
        self.assertTrue(engine._verify_head_before_capture(dict(CFG), 211))
        self.assertTrue(engine._last_head_feedback["capture_gate"]["ok"])


if __name__ == "__main__":
    unittest.main()
