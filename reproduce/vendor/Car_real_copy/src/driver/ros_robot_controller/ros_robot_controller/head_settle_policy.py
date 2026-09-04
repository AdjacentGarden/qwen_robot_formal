from __future__ import annotations

import time
import math


class HeadCommandDeadline:
    """Monotonic, restartable deadline for one externally requested movement."""

    def __init__(self, timeout_sec: float = 5.0) -> None:
        self.active = False
        self.started_at = 0.0
        self.configure(timeout_sec)

    def configure(self, timeout_sec: float) -> None:
        timeout_sec = float(timeout_sec)
        if not math.isfinite(timeout_sec) or timeout_sec <= 0.0:
            raise ValueError("head command timeout must be finite and positive")
        self.timeout_sec = timeout_sec

    def begin(self, now: float | None = None) -> None:
        self.started_at = float(time.monotonic() if now is None else now)
        self.active = True

    def finish(self) -> None:
        self.active = False

    def elapsed(self, now: float | None = None) -> float:
        current = float(time.monotonic() if now is None else now)
        return max(0.0, current - self.started_at)

    def expired(self, now: float | None = None) -> bool:
        return self.active and self.elapsed(now) >= self.timeout_sec


class HeadSpeedProfile:
    """Three-zone desired-rate profile: fast far away, gentle near the target."""

    def __init__(
        self,
        *,
        deadband_deg: float = 2.0,
        medium_error_deg: float = 6.0,
        far_error_deg: float = 15.0,
        near_rate_dps: float = 3.0,
        medium_rate_dps: float = 8.0,
        far_rate_dps: float = 15.0,
    ) -> None:
        self.configure(
            deadband_deg, medium_error_deg, far_error_deg,
            near_rate_dps, medium_rate_dps, far_rate_dps)

    def configure(
        self,
        deadband_deg: float,
        medium_error_deg: float,
        far_error_deg: float,
        near_rate_dps: float,
        medium_rate_dps: float,
        far_rate_dps: float,
    ) -> None:
        values = tuple(float(value) for value in (
            deadband_deg, medium_error_deg, far_error_deg,
            near_rate_dps, medium_rate_dps, far_rate_dps))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("head speed profile values must be finite")
        deadband, medium_error, far_error, near_rate, medium_rate, far_rate = values
        if not 0.0 <= deadband < medium_error < far_error:
            raise ValueError("head error thresholds must be strictly increasing")
        if not 0.0 <= near_rate <= medium_rate <= far_rate:
            raise ValueError("head rate limits must be non-negative and increasing")
        self.deadband_deg = deadband
        self.medium_error_deg = medium_error
        self.far_error_deg = far_error
        self.near_rate_dps = near_rate
        self.medium_rate_dps = medium_rate
        self.far_rate_dps = far_rate

    @staticmethod
    def _interpolate(value, low_x, high_x, low_y, high_y):
        ratio = (value - low_x) / (high_x - low_x)
        return low_y + ratio * (high_y - low_y)

    def desired_rate(self, angle_error: float) -> float:
        angle_error = float(angle_error)
        if not math.isfinite(angle_error):
            return 0.0
        magnitude = abs(angle_error)
        if magnitude <= self.deadband_deg:
            return 0.0
        if magnitude >= self.far_error_deg:
            rate = self.far_rate_dps
        elif magnitude >= self.medium_error_deg:
            rate = self._interpolate(
                magnitude, self.medium_error_deg, self.far_error_deg,
                self.medium_rate_dps, self.far_rate_dps)
        else:
            rate = self._interpolate(
                magnitude, self.deadband_deg, self.medium_error_deg,
                self.near_rate_dps, self.medium_rate_dps)
        return math.copysign(rate, angle_error)


class HeadSettlePolicy:
    """Rate-aware Schmitt-trigger arrival latch for the head controller."""

    def __init__(
        self,
        target: float,
        *,
        enter_deg: float = 4.0,
        exit_deg: float = 7.0,
        rate_dps: float = 3.0,
        exit_hold_sec: float = 0.20,
        enter_hold_sec: float = 0.0,
        motion_restart_rate_dps: float | None = None,
        motion_restart_hold_sec: float = 0.0,
    ) -> None:
        self.target = float(target)
        self.settled = False
        self._inside_since: float | None = None
        self._outside_since: float | None = None
        self._motion_since: float | None = None
        self.enter_deg = 0.0
        self.exit_deg = 0.0
        self.rate_dps = 0.0
        self.exit_hold_sec = 0.0
        self.enter_hold_sec = 0.0
        self.motion_restart_rate_dps: float | None = None
        self.motion_restart_hold_sec = 0.0
        self.configure(
            enter_deg,
            exit_deg,
            rate_dps,
            exit_hold_sec,
            enter_hold_sec,
            motion_restart_rate_dps,
            motion_restart_hold_sec,
        )

    def configure(
        self,
        enter_deg: float,
        exit_deg: float,
        rate_dps: float,
        exit_hold_sec: float,
        enter_hold_sec: float = 0.0,
        motion_restart_rate_dps: float | None = None,
        motion_restart_hold_sec: float = 0.0,
    ) -> None:
        values = tuple(float(value) for value in (
            enter_deg,
            exit_deg,
            rate_dps,
            exit_hold_sec,
            enter_hold_sec,
            motion_restart_hold_sec,
        ))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("settle policy values must be finite")
        (
            enter_deg,
            exit_deg,
            rate_dps,
            exit_hold_sec,
            enter_hold_sec,
            motion_restart_hold_sec,
        ) = values
        if motion_restart_rate_dps is not None:
            motion_restart_rate_dps = float(motion_restart_rate_dps)
            if not math.isfinite(motion_restart_rate_dps):
                raise ValueError("motion restart rate must be finite or None")
            motion_restart_rate_dps = max(0.0, motion_restart_rate_dps)
        enter_deg = max(0.0, enter_deg)
        if enter_deg >= exit_deg:
            raise ValueError("enter threshold must be smaller than exit threshold")
        new_entry_config = (
            enter_deg,
            max(0.0, rate_dps),
            max(0.0, enter_hold_sec),
        )
        old_entry_config = (
            self.enter_deg,
            self.rate_dps,
            self.enter_hold_sec,
        )
        new_exit_config = (
            exit_deg,
            max(0.0, exit_hold_sec),
            motion_restart_rate_dps,
            max(0.0, motion_restart_hold_sec),
        )
        old_exit_config = (
            self.exit_deg,
            self.exit_hold_sec,
            self.motion_restart_rate_dps,
            self.motion_restart_hold_sec,
        )
        self.enter_deg = enter_deg
        self.exit_deg = exit_deg
        self.rate_dps = new_entry_config[1]
        self.exit_hold_sec = new_exit_config[1]
        self.enter_hold_sec = new_entry_config[2]
        self.motion_restart_rate_dps = new_exit_config[2]
        self.motion_restart_hold_sec = new_exit_config[3]
        if new_entry_config != old_entry_config:
            self._inside_since = None
        if new_exit_config != old_exit_config:
            self._outside_since = None
            self._motion_since = None

    def set_target(self, target: float) -> bool:
        target = float(target)
        if not math.isfinite(target):
            raise ValueError("target must be finite")
        delta = (target - self.target + 180.0) % 360.0 - 180.0
        changed = abs(delta) > 0.25
        self.target = target
        if changed:
            self.settled = False
            self._inside_since = None
            self._outside_since = None
            self._motion_since = None
        return changed

    def update(
        self,
        angle_error: float,
        angular_rate_dps: float,
        now: float | None = None,
        *,
        entry_allowed: bool = True,
    ) -> bool:
        values = tuple(float(value) for value in (
            angle_error, angular_rate_dps, now if now is not None else time.monotonic()))
        if not all(math.isfinite(value) for value in values):
            self._inside_since = None
            self._outside_since = None
            self._motion_since = None
            self.settled = False
            return False
        error = abs(values[0])
        rate = abs(values[1])
        now = values[2]
        if self.settled:
            self._inside_since = None
            if (
                self.motion_restart_rate_dps is not None
                and rate > self.motion_restart_rate_dps
            ):
                if self._motion_since is None or now < self._motion_since:
                    self._motion_since = now
                    if self.motion_restart_hold_sec == 0.0:
                        self.settled = False
                elif (
                    now - self._motion_since + 1e-12
                    >= self.motion_restart_hold_sec
                ):
                    self.settled = False
                if not self.settled:
                    self._outside_since = None
                    self._motion_since = None
                    return False
            else:
                self._motion_since = None
            if error > self.exit_deg:
                if self._outside_since is None:
                    self._outside_since = now
                    if self.exit_hold_sec == 0.0:
                        self.settled = False
                        self._outside_since = None
                elif now < self._outside_since:
                    self._outside_since = now
                elif now - self._outside_since + 1e-12 >= self.exit_hold_sec:
                    self.settled = False
                    self._outside_since = None
            else:
                self._outside_since = None
            return self.settled

        self._outside_since = None
        self._motion_since = None
        if entry_allowed and error <= self.enter_deg and rate <= self.rate_dps:
            if self.enter_hold_sec == 0.0:
                self.settled = True
                self._inside_since = None
            elif self._inside_since is None or now < self._inside_since:
                self._inside_since = now
            elif now - self._inside_since + 1e-12 >= self.enter_hold_sec:
                self.settled = True
                self._inside_since = None
        else:
            self._inside_since = None
        return self.settled

    def reset_observation_timers(self) -> None:
        """Require both hold intervals to contain continuous valid samples."""
        self._inside_since = None
        self._outside_since = None
        self._motion_since = None

    def reset_entry_timer(self) -> None:
        """Restart the continuous entry qualification interval."""
        self._inside_since = None

    def reset_release_timer(self) -> None:
        """Reset only the exit timer; retained for API compatibility."""
        self._outside_since = None


class HeadArrivalBrakePolicy:
    """Hysteretic minimum braking used only while moving inside the target window."""

    def __init__(
        self,
        *,
        engage_rate_dps: float = 1.0,
        release_rate_dps: float = 0.3,
    ) -> None:
        self.engage_rate_dps = 0.0
        self.release_rate_dps = 0.0
        self.active = False
        self._direction = 0
        self.configure(engage_rate_dps, release_rate_dps)

    def configure(
        self,
        engage_rate_dps: float,
        release_rate_dps: float,
    ) -> None:
        engage_rate_dps = float(engage_rate_dps)
        release_rate_dps = float(release_rate_dps)
        if not all(math.isfinite(value) for value in (
                engage_rate_dps, release_rate_dps)):
            raise ValueError("arrival brake rates must be finite")
        engage_rate_dps = max(0.0, engage_rate_dps)
        release_rate_dps = max(0.0, release_rate_dps)
        disabled = engage_rate_dps == 0.0
        if disabled:
            release_rate_dps = 0.0
        elif release_rate_dps >= engage_rate_dps:
            raise ValueError(
                "arrival brake release rate must be smaller than engage rate")
        changed = (
            engage_rate_dps != self.engage_rate_dps
            or release_rate_dps != self.release_rate_dps
        )
        self.engage_rate_dps = engage_rate_dps
        self.release_rate_dps = release_rate_dps
        if changed:
            self.reset()

    def update(
        self,
        angle_error: float,
        angular_rate_dps: float,
        enter_deg: float,
        settled: bool,
    ) -> bool:
        values = tuple(float(value) for value in (
            angle_error, angular_rate_dps, enter_deg))
        if not all(math.isfinite(value) for value in values):
            self.reset()
            return False
        error = abs(values[0])
        rate = abs(values[1])
        enter_deg = max(0.0, values[2])
        direction = 1 if values[1] > 0.0 else -1 if values[1] < 0.0 else 0
        if self.engage_rate_dps == 0.0 or settled or error > enter_deg:
            self.reset()
        elif self.active:
            if rate <= self.release_rate_dps or direction != self._direction:
                self.reset()
        elif rate >= self.engage_rate_dps:
            self.active = True
            self._direction = direction
        return self.active

    def apply_minimum_command(
        self,
        calculated_command: float,
        angular_rate_dps: float,
        minimum_command: float,
    ) -> float:
        values = tuple(float(value) for value in (
            calculated_command, angular_rate_dps, minimum_command))
        if not all(math.isfinite(value) for value in values):
            self.reset()
            return 0.0
        command, _rate, minimum = values
        minimum = max(0.0, minimum)
        if not self.active or minimum == 0.0 or self._direction == 0:
            return command
        return math.copysign(max(abs(command), minimum), self._direction)

    def reset(self) -> None:
        self.active = False
        self._direction = 0
