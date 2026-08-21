#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from head_pose_supervisor import HeadPoseSupervisor


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class Harness:
    def __init__(self):
        self.clock = Clock()
        self.roll = 185.0
        self.sample_time = self.clock()
        self.resources = {}
        self.projection_active = False
        self.corrections = 0
        self.correction_ok = True
        self.temp = tempfile.TemporaryDirectory()
        self.supervisor = HeadPoseSupervisor(
            sample_provider=lambda: (self.roll, self.sample_time),
            resource_provider=lambda: dict(self.resources),
            projection_active_provider=lambda: self.projection_active,
            correction=self.correct,
            state_path=Path(self.temp.name) / "state.json",
            deviation_hold_sec=0.8,
            retry_cooldown_sec=3.0,
            startup_grace_sec=0.0,
            monotonic=self.clock,
            wall_time=self.clock,
        )

    def fresh(self, roll=None):
        if roll is not None:
            self.roll = float(roll)
        self.sample_time = self.clock()

    def correct(self):
        self.corrections += 1
        if self.correction_ok:
            self.roll = 185.0
            self.sample_time = self.clock()
        return {"ok": self.correction_ok}

    def close(self):
        self.temp.cleanup()


class HeadPoseSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_level_deviation_is_corrected_after_continuous_dwell(self):
        self.h.fresh(195.0)
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "deviation_dwell_started")
        self.h.clock.advance(0.7)
        self.h.fresh()
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "deviation_not_yet_stable")
        self.h.clock.advance(0.2)
        self.h.fresh()
        result = self.h.supervisor.evaluate_once()
        self.assertEqual(result["action"], "corrected")
        self.assertEqual(self.h.corrections, 1)

    def test_intentional_up_and_down_are_never_auto_levelled(self):
        for action, angle in (("up", 211.0), ("down", 163.0)):
            self.h.supervisor.note_command_started(action, angle)
            self.h.supervisor.note_command_finished(ok=True, action=action, angle=angle)
            self.h.fresh(angle)
            self.h.clock.advance(5.0)
            result = self.h.supervisor.evaluate_once()
            self.assertEqual(result["reason"], "intentional_tilt")
        self.assertEqual(self.h.corrections, 0)

    def test_explicit_level_failure_remains_recoverable(self):
        self.h.supervisor.note_command_started("level", 185.0)
        self.h.supervisor.note_command_finished(ok=False, action="level", angle=185.0)
        self.h.fresh(197.0)
        self.h.supervisor.evaluate_once()
        self.h.clock.advance(0.9)
        self.h.fresh()
        self.assertEqual(self.h.supervisor.evaluate_once()["action"], "corrected")

    def test_failed_tilt_falls_back_to_level_intent(self):
        self.h.supervisor.note_command_started("up", 211.0)
        self.h.supervisor.note_command_finished(ok=False, action="up", angle=211.0)
        self.assertEqual(self.h.supervisor.status()["desired_mode"], "level")

    def test_busy_base_suppresses_correction_and_restarts_dwell(self):
        self.h.resources = {"base": {"skill": "navigation_goto"}}
        self.h.fresh(198.0)
        self.h.clock.advance(2.0)
        self.h.fresh()
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "motion_or_head_resource_busy")
        self.h.resources = {}
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "deviation_dwell_started")
        self.assertEqual(self.h.corrections, 0)

    def test_projection_session_suppresses_startup_recovery(self):
        self.h.projection_active = True
        self.h.fresh(211.0)
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "projection_owns_tilt")
        self.h.clock.advance(2.0)
        self.h.fresh()
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "projection_owns_tilt")
        self.assertEqual(self.h.corrections, 0)

    def test_stale_feedback_never_moves_head(self):
        self.h.roll = 200.0
        self.h.clock.advance(2.0)
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "head_feedback_stale")
        self.assertEqual(self.h.corrections, 0)

    def test_hysteresis_avoids_chatter(self):
        for roll in (190.0, 191.5, 189.2, 191.9):
            self.h.fresh(roll)
            result = self.h.supervisor.evaluate_once()
            self.assertIn(result["reason"], {"inside_hysteresis_band", "within_level_deadband"})
            self.h.clock.advance(1.0)
        self.assertEqual(self.h.corrections, 0)

    def test_failures_are_bounded_per_excursion(self):
        self.h.correction_ok = False
        self.h.fresh(200.0)
        for expected_attempt in (1, 2, 3):
            self.h.supervisor.evaluate_once()
            self.h.clock.advance(0.9)
            self.h.fresh()
            result = self.h.supervisor.evaluate_once()
            self.assertEqual(result["action"], "correction_failed")
            self.assertEqual(result["attempt"], expected_attempt)
            self.h.clock.advance(3.1)
            self.h.fresh()
        self.h.supervisor.evaluate_once()
        self.h.clock.advance(1.0)
        self.h.fresh()
        self.assertEqual(self.h.supervisor.evaluate_once()["reason"], "attempt_limit_reached")
        self.assertEqual(self.h.corrections, 3)


if __name__ == "__main__":
    unittest.main()
