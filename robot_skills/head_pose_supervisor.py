#!/usr/bin/env python3
"""Resident head-pose intent and automatic neutral-pose recovery.

This module deliberately contains no ROS imports.  The resident runtime owns
the ROS publishers and supplies small callbacks, which keeps the decision
logic deterministic and hardware-free unit-testable.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


def circular_error_deg(value: float, target: float) -> float:
    return (float(value) - float(target) + 180.0) % 360.0 - 180.0


class HeadPoseSupervisor:
    """Auto-level only when the resident intent says the head should be level.

    A separate enter/exit threshold prevents chatter at the motor deadband.
    Correction is also delayed until the deviation is continuous, the sample
    is fresh, the base/head are idle, and no persistent projection owns the
    tilted pose.
    """

    def __init__(
        self,
        *,
        sample_provider: Callable[[], tuple[float, float] | None],
        resource_provider: Callable[[], dict[str, dict[str, Any]]],
        projection_active_provider: Callable[[], bool],
        level_recovery_needed_provider: Callable[[], bool] | None,
        correction: Callable[[], dict[str, Any]],
        state_path: Path,
        level_angle: float = 185.0,
        enter_error_deg: float = 7.0,
        exit_error_deg: float = 4.0,
        deviation_hold_sec: float = 0.8,
        maximum_sample_age_sec: float = 0.7,
        poll_sec: float = 0.10,
        retry_cooldown_sec: float = 3.0,
        maximum_attempts_per_excursion: int = 3,
        startup_grace_sec: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ):
        self.sample_provider = sample_provider
        self.resource_provider = resource_provider
        self.projection_active_provider = projection_active_provider
        self.level_recovery_needed_provider = level_recovery_needed_provider
        self.correction = correction
        self.state_path = Path(state_path)
        self.level_angle = float(level_angle) % 360.0
        self.enter_error_deg = max(float(enter_error_deg), 0.5)
        self.exit_error_deg = min(max(float(exit_error_deg), 0.1), self.enter_error_deg)
        self.deviation_hold_sec = max(float(deviation_hold_sec), 0.1)
        self.maximum_sample_age_sec = max(float(maximum_sample_age_sec), 0.1)
        self.poll_sec = max(float(poll_sec), 0.03)
        self.retry_cooldown_sec = max(float(retry_cooldown_sec), 0.2)
        self.maximum_attempts_per_excursion = max(int(maximum_attempts_per_excursion), 1)
        self.startup_grace_sec = max(float(startup_grace_sec), 0.0)
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_monotonic = self.monotonic()
        self.desired_mode = "level"
        self.desired_angle = self.level_angle
        self.command_in_progress = False
        self.deviation_since: float | None = None
        self.last_attempt_monotonic: float | None = None
        self.attempts_this_excursion = 0
        self.last_result: dict[str, Any] | None = None
        self.last_sample: dict[str, Any] | None = None
        self.last_level_recovery_needed = False
        self._write_state("initialized")

    def start(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run,
                name="head_pose_supervisor",
                daemon=True,
            )
            self.thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def note_command_started(self, action: str, angle: float) -> None:
        angle = float(angle) % 360.0
        level = abs(circular_error_deg(angle, self.level_angle)) <= self.exit_error_deg
        with self.lock:
            self.desired_mode = "level" if level else str(action or "custom_tilt")
            self.desired_angle = self.level_angle if level else angle
            self.command_in_progress = True
            self.deviation_since = None
            self.attempts_this_excursion = 0
            self._write_state("command_started")

    def note_command_finished(self, *, ok: bool, action: str, angle: float) -> None:
        angle = float(angle) % 360.0
        level = abs(circular_error_deg(angle, self.level_angle)) <= self.exit_error_deg
        with self.lock:
            self.command_in_progress = False
            # A failed tilt may have stopped at an unsafe intermediate angle.
            # Fall back to the neutral intent so the bounded supervisor can
            # recover it.  A failed level command already has that intent.
            if not ok or level:
                self.desired_mode = "level"
                self.desired_angle = self.level_angle
            else:
                self.desired_mode = str(action or "custom_tilt")
                self.desired_angle = angle
            self.deviation_since = None
            self.attempts_this_excursion = 0
            self._write_state("command_finished")

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": bool(self.thread and self.thread.is_alive()),
                "desired_mode": self.desired_mode,
                "desired_angle": self.desired_angle,
                "command_in_progress": self.command_in_progress,
                "deviation_since": self.deviation_since,
                "attempts_this_excursion": self.attempts_this_excursion,
                "last_result": dict(self.last_result) if self.last_result else None,
                "last_sample": dict(self.last_sample) if self.last_sample else None,
                "level_recovery_needed": self.last_level_recovery_needed,
                "thresholds": {
                    "enter_error_deg": self.enter_error_deg,
                    "exit_error_deg": self.exit_error_deg,
                    "deviation_hold_sec": self.deviation_hold_sec,
                    "maximum_sample_age_sec": self.maximum_sample_age_sec,
                },
            }

    def evaluate_once(self) -> dict[str, Any]:
        now = self.monotonic()
        sample = self.sample_provider()
        if sample is None:
            return self._blocked("head_feedback_unavailable", now=now)
        roll, received_monotonic = float(sample[0]), float(sample[1])
        age = max(0.0, now - received_monotonic)
        error = circular_error_deg(roll, self.level_angle)
        try:
            level_recovery_needed = bool(
                self.level_recovery_needed_provider
                and self.level_recovery_needed_provider()
            )
        except Exception:
            # A transient feedback-read error must not move the head.  The
            # next watchdog pass will re-evaluate the gate state.
            level_recovery_needed = False
        with self.lock:
            self.last_level_recovery_needed = level_recovery_needed
            self.last_sample = {
                "roll_deg": roll,
                "error_deg": error,
                "age_sec": age,
                "observed_at": self.wall_time(),
            }
            if now - self.started_monotonic < self.startup_grace_sec:
                return self._blocked_locked("startup_grace")
            if self.desired_mode != "level":
                return self._blocked_locked("intentional_tilt")
            if self.command_in_progress:
                return self._blocked_locked("head_command_in_progress")
            if age > self.maximum_sample_age_sec:
                self.deviation_since = None
                return self._blocked_locked("head_feedback_stale")
            if abs(error) <= self.exit_error_deg and not level_recovery_needed:
                self.deviation_since = None
                self.attempts_this_excursion = 0
                return self._blocked_locked("within_level_deadband")
            gate_recovery_only = (
                abs(error) <= self.exit_error_deg and level_recovery_needed
            )
            if not gate_recovery_only and abs(error) < self.enter_error_deg:
                self.deviation_since = None
                return self._blocked_locked("inside_hysteresis_band")
            resources = self.resource_provider() or {}
            if "base" in resources or "head" in resources:
                self.deviation_since = None
                return self._blocked_locked("motion_or_head_resource_busy")
            if self.projection_active_provider():
                self.deviation_since = None
                return self._blocked_locked("projection_owns_tilt")
            if self.deviation_since is None:
                self.deviation_since = now
                return self._blocked_locked(
                    "level_gate_recovery_dwell_started"
                    if gate_recovery_only else "deviation_dwell_started"
                )
            if now - self.deviation_since < self.deviation_hold_sec:
                return self._blocked_locked(
                    "level_gate_recovery_not_yet_stable"
                    if gate_recovery_only else "deviation_not_yet_stable"
                )
            if self.attempts_this_excursion >= self.maximum_attempts_per_excursion:
                return self._blocked_locked("attempt_limit_reached")
            if (
                self.last_attempt_monotonic is not None
                and now - self.last_attempt_monotonic < self.retry_cooldown_sec
            ):
                return self._blocked_locked("retry_cooldown")
            self.last_attempt_monotonic = now
            self.attempts_this_excursion += 1
            attempt = self.attempts_this_excursion

        try:
            result = dict(self.correction() or {})
        except Exception as exc:  # keep the watchdog alive on transport faults
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result = {
            **result,
            "attempt": attempt,
            "level_gate_recovery_only": gate_recovery_only,
            "trigger_roll_deg": roll,
            "trigger_error_deg": error,
            "completed_at": self.wall_time(),
        }
        with self.lock:
            self.last_result = result
            self.deviation_since = None
            self._write_state("automatic_correction")
        return {"action": "corrected" if result.get("ok") else "correction_failed", **result}

    def _blocked(self, reason: str, *, now: float) -> dict[str, Any]:
        del now
        with self.lock:
            return self._blocked_locked(reason)

    def _blocked_locked(self, reason: str) -> dict[str, Any]:
        return {"action": "none", "reason": reason}

    def _write_state(self, event: str) -> None:
        try:
            payload = {
                "event": event,
                "updated_at": self.wall_time(),
                "desired_mode": self.desired_mode,
                "desired_angle": self.desired_angle,
                "command_in_progress": self.command_in_progress,
                "attempts_this_excursion": self.attempts_this_excursion,
                "last_result": self.last_result,
                "last_sample": self.last_sample,
                "level_recovery_needed": self.last_level_recovery_needed,
            }
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
        except Exception:
            # Telemetry persistence must never terminate the resident runtime.
            pass

    def _run(self) -> None:
        while not self.stop_event.wait(self.poll_sec):
            self.evaluate_once()
