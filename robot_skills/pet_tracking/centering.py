"""Pure horizontal centering policy for stationary pet search.

The policy deliberately knows nothing about ROS, cameras, RKNN, or motors.  It
turns a detected bounding-box centre into left/right wheel requests so the
hardware adapter can be tested without publishing a velocity command.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CenteringDecision:
    centered: bool
    inside_center: bool
    normalized_error: float | None
    speed_right: float
    speed_left: float
    consecutive_centered: int
    reason: str

    def public(self) -> dict:
        return asdict(self)


class PetCenteringController:
    """Require several centred frames before declaring alignment complete."""

    def __init__(
        self,
        *,
        center_tolerance_ratio: float = 0.08,
        confirmation_frames: int = 3,
        minimum_turn_speed: float = 0.055,
        maximum_turn_speed: float = 0.10,
        turn_gain: float = 0.18,
        search_speed: float = 0.10,
        missing_hold_frames: int = 4,
    ) -> None:
        self.center_tolerance_ratio = max(
            0.01, min(0.20, float(center_tolerance_ratio))
        )
        self.confirmation_frames = max(1, int(confirmation_frames))
        self.minimum_turn_speed = max(0.01, abs(float(minimum_turn_speed)))
        self.maximum_turn_speed = max(
            self.minimum_turn_speed, abs(float(maximum_turn_speed))
        )
        self.turn_gain = max(0.01, abs(float(turn_gain)))
        self.search_speed = max(
            self.minimum_turn_speed, min(self.maximum_turn_speed, abs(float(search_speed)))
        )
        self.missing_hold_frames = max(0, int(missing_hold_frames))
        self.consecutive_centered = 0
        self.missing_frames = 0
        self.last_speed_right = self.search_speed
        self.last_speed_left = -self.search_speed

    def observe(self, center_x: float, frame_width: float) -> CenteringDecision:
        width = max(1.0, float(frame_width))
        center = max(0.0, min(width, float(center_x)))
        # Positive means the target is left of image centre.  A positive
        # in-place angular command turns the camera towards that side under the
        # existing pet motor convention.
        error = 1.0 - 2.0 * center / width
        inside = abs(center / width - 0.5) <= self.center_tolerance_ratio
        self.missing_frames = 0
        if inside:
            self.consecutive_centered += 1
            self.last_speed_right = 0.0
            self.last_speed_left = 0.0
            return CenteringDecision(
                centered=self.consecutive_centered >= self.confirmation_frames,
                inside_center=True,
                normalized_error=error,
                speed_right=0.0,
                speed_left=0.0,
                consecutive_centered=self.consecutive_centered,
                reason=(
                    "center_confirmed"
                    if self.consecutive_centered >= self.confirmation_frames
                    else "center_verifying"
                ),
            )

        self.consecutive_centered = 0
        magnitude = max(
            self.minimum_turn_speed,
            min(self.maximum_turn_speed, abs(error) * self.turn_gain),
        )
        direction = 1.0 if error > 0.0 else -1.0
        self.last_speed_right = direction * magnitude
        self.last_speed_left = -direction * magnitude
        return CenteringDecision(
            centered=False,
            inside_center=False,
            normalized_error=error,
            speed_right=self.last_speed_right,
            speed_left=self.last_speed_left,
            consecutive_centered=0,
            reason="turn_towards_target",
        )

    def missing(self) -> CenteringDecision:
        """Keep the last heading briefly, then resume the original search turn."""

        self.consecutive_centered = 0
        self.missing_frames += 1
        if self.missing_frames <= self.missing_hold_frames and (
            abs(self.last_speed_right) > 1e-9 or abs(self.last_speed_left) > 1e-9
        ):
            right = self.last_speed_right
            left = self.last_speed_left
            reason = "brief_detection_gap"
        else:
            right = self.search_speed
            left = -self.search_speed
            self.last_speed_right = right
            self.last_speed_left = left
            reason = "resume_search_turn"
        return CenteringDecision(
            centered=False,
            inside_center=False,
            normalized_error=None,
            speed_right=right,
            speed_left=left,
            consecutive_centered=0,
            reason=reason,
        )
