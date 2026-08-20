"""Conflict-free supervision for proactive robot events and task recovery.

This module deliberately owns no ROS node and opens no device.  Runtime code
feeds it telemetry already published by the existing controller (or decoded by
the existing resident owner).  That rule prevents a second reader from racing
the controller for ``/dev/ttyS0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import IntEnum
import math
import time
from typing import Any, Callable, Iterable, Mapping, Sequence


class EventPriority(IntEnum):
    BATTERY = 10
    STATUS = 20
    ATTENTION = 40
    USER_TASK = 60
    SAFETY = 100


@dataclass(frozen=True)
class Notification:
    kind: str
    priority: int
    speech_intent: str
    fallback_text: str
    created_at: float
    dedupe_key: str
    requires_idle: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


class NotificationArbiter:
    """One speech lane with safety priority and deferred low-priority events."""

    def __init__(self, *, idle_grace_seconds: float = 1.5) -> None:
        self.idle_grace_seconds = max(0.0, float(idle_grace_seconds))
        self._events: list[Notification] = []
        self._dedupe: set[str] = set()
        self.last_user_input_at = float("-inf")

    def note_user_input(self, now: float | None = None) -> None:
        self.last_user_input_at = time.monotonic() if now is None else float(now)

    def submit(self, event: Notification) -> bool:
        if event.dedupe_key in self._dedupe:
            return False
        # A long task can cross several battery thresholds.  At idle, saying
        # every stale level would be noisy and misleading; retain the newest
        # (lowest) threshold while preserving all crossed levels as metadata.
        if event.kind == "battery_threshold":
            prior = [item for item in self._events if item.kind == event.kind]
            crossed: list[int] = []
            for item in prior:
                crossed.extend(int(v) for v in item.metadata.get("crossed_thresholds", []))
                self._dedupe.discard(item.dedupe_key)
            crossed.extend(int(v) for v in event.metadata.get("crossed_thresholds", []))
            if crossed:
                metadata = dict(event.metadata)
                metadata["crossed_thresholds"] = sorted(set(crossed), reverse=True)
                event = replace(event, metadata=metadata)
            self._events = [item for item in self._events if item.kind != event.kind]
        self._events.append(event)
        self._dedupe.add(event.dedupe_key)
        return True

    def pop_ready(
        self,
        *,
        now: float | None = None,
        task_active: bool,
        user_input_pending: bool,
        speaking: bool,
    ) -> Notification | None:
        current = time.monotonic() if now is None else float(now)
        if speaking:
            return None
        candidates = []
        for event in self._events:
            if event.requires_idle and (
                task_active
                or user_input_pending
                or current - self.last_user_input_at < self.idle_grace_seconds
            ):
                continue
            candidates.append(event)
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: (int(item.priority), -item.created_at))
        self._events.remove(selected)
        return selected

    @property
    def pending(self) -> tuple[Notification, ...]:
        return tuple(self._events)


@dataclass(frozen=True)
class BatteryTelemetry:
    voltage_v: float | None = None
    current_a: float | None = None
    percentage: float | None = None
    charging: bool | None = None
    source: str = "unknown"
    timestamp: float = 0.0

    def normalized(self) -> "BatteryTelemetry":
        voltage = _finite_or_none(self.voltage_v)
        current = _finite_or_none(self.current_a)
        percentage = _finite_or_none(self.percentage)
        if percentage is not None:
            percentage = min(100.0, max(0.0, percentage))
        return replace(self, voltage_v=voltage, current_a=current, percentage=percentage)


def battery_from_ros_message(message: Any, *, source: str = "ros_battery_state") -> BatteryTelemetry:
    """Adapt a ROS-like BatteryState without importing ROS or opening serial."""

    raw_percentage = _finite_or_none(getattr(message, "percentage", None))
    if raw_percentage is not None and 0.0 <= raw_percentage <= 1.0:
        raw_percentage *= 100.0
    status = getattr(message, "power_supply_status", None)
    charging_values = {
        getattr(message, "POWER_SUPPLY_STATUS_CHARGING", object()),
        getattr(message, "POWER_SUPPLY_STATUS_FULL", object()),
    }
    not_charging = getattr(message, "POWER_SUPPLY_STATUS_NOT_CHARGING", object())
    charging = True if status in charging_values else False if status == not_charging else None
    return BatteryTelemetry(
        voltage_v=_finite_or_none(getattr(message, "voltage", None)),
        current_a=_finite_or_none(getattr(message, "current", None)),
        percentage=raw_percentage,
        charging=charging,
        source=source,
        timestamp=time.time(),
    ).normalized()


def battery_from_millivolts(value: Any, *, source: str = "ros_robot_controller/battery") -> BatteryTelemetry:
    """Adapt the existing UInt16 millivolt report; percent/current stay unknown."""

    raw = getattr(value, "data", value)
    millivolts = _finite_or_none(raw)
    voltage = millivolts / 1000.0 if millivolts is not None and millivolts > 0 else None
    return BatteryTelemetry(voltage_v=voltage, source=source, timestamp=time.time())


class BatteryThresholdMonitor:
    def __init__(
        self,
        *,
        thresholds: Sequence[int] = (80, 70, 60, 50, 40, 30, 20, 10),
        rearm_margin: float = 3.0,
    ) -> None:
        values = sorted({int(value) for value in thresholds}, reverse=True)
        if not values or values[-1] <= 0 or values[0] > 100:
            raise ValueError("invalid_battery_thresholds")
        self.thresholds = tuple(values)
        self.rearm_margin = max(0.0, float(rearm_margin))
        self.last_percentage: float | None = None
        self.announced: set[int] = set()
        self.latest: BatteryTelemetry | None = None

    def observe(self, sample: BatteryTelemetry, *, now: float | None = None) -> Notification | None:
        sample = sample.normalized()
        self.latest = sample
        percentage = sample.percentage
        if percentage is None:
            return None
        if sample.charging is True:
            for threshold in self.thresholds:
                if percentage >= threshold + self.rearm_margin:
                    self.announced.discard(threshold)
        previous = self.last_percentage
        self.last_percentage = percentage
        # Do not announce every threshold below the first startup sample.
        if previous is None:
            self.announced.update(value for value in self.thresholds if percentage <= value)
            return None
        crossed = [
            value
            for value in self.thresholds
            if value not in self.announced and previous > value >= percentage
        ]
        if not crossed:
            return None
        self.announced.update(crossed)
        level = min(crossed)
        created = time.monotonic() if now is None else float(now)
        details = []
        if sample.voltage_v is not None:
            details.append(f"电压{sample.voltage_v:.2f}伏")
        if sample.current_a is not None:
            details.append(f"电流{sample.current_a:.2f}安")
        suffix = "，" + "，".join(details) if details else ""
        return Notification(
            kind="battery_threshold",
            priority=EventPriority.BATTERY,
            speech_intent="在机器人空闲时简短提醒当前剩余电量，不打断用户或任务",
            fallback_text=f"提醒一下，当前剩余电量约{round(percentage):d}%{suffix}。",
            created_at=created,
            dedupe_key=f"battery:{level}",
            requires_idle=True,
            metadata={
                "percentage": percentage,
                "voltage_v": sample.voltage_v,
                "current_a": sample.current_a,
                "crossed_thresholds": crossed,
                "source": sample.source,
            },
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "last_percentage": self.last_percentage,
            "announced": sorted(self.announced, reverse=True),
        }

    def import_state(self, value: Mapping[str, Any]) -> None:
        raw = _finite_or_none(value.get("last_percentage"))
        self.last_percentage = min(100.0, max(0.0, raw)) if raw is not None else None
        self.announced = {
            int(item) for item in value.get("announced", []) if int(item) in self.thresholds
        }


@dataclass(frozen=True)
class CliffTelemetry:
    distances: tuple[float, ...]
    source: str
    timestamp: float


def gp2y_from_existing_frame(data: Iterable[Any], *, source: str = "resident_gp2y") -> CliffTelemetry:
    """Decode the existing four-byte GP2Y frame without owning its serial port."""

    values = tuple(float(value) for value in data)
    if len(values) != 4 or any(not math.isfinite(value) for value in values):
        raise ValueError("invalid_gp2y_frame")
    return CliffTelemetry(values, source, time.time())


class CliffDetector:
    """Debounced cliff detector whose calibration must be explicitly supplied."""

    def __init__(
        self,
        *,
        front_sensor_indices: Sequence[int],
        unsafe_threshold: float,
        unsafe_when: str,
        confirm_samples: int = 3,
        clear_samples: int = 5,
        clear_margin: float = 2.0,
        emergency_stop: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not front_sensor_indices:
            raise ValueError("front_sensor_indices_required")
        if unsafe_when not in {"above", "below"}:
            raise ValueError("unsafe_when_must_be_above_or_below")
        self.front_sensor_indices = tuple(int(value) for value in front_sensor_indices)
        self.unsafe_threshold = float(unsafe_threshold)
        self.unsafe_when = unsafe_when
        self.confirm_samples = max(1, int(confirm_samples))
        self.clear_samples = max(1, int(clear_samples))
        self.clear_margin = max(0.0, float(clear_margin))
        self.emergency_stop = emergency_stop
        self._unsafe_count = 0
        self._clear_count = 0
        self.active = False
        self.episode = 0

    def _unsafe(self, value: float) -> bool:
        return value >= self.unsafe_threshold if self.unsafe_when == "above" else value <= self.unsafe_threshold

    def _clear(self, value: float) -> bool:
        if self.unsafe_when == "above":
            return value <= self.unsafe_threshold - self.clear_margin
        return value >= self.unsafe_threshold + self.clear_margin

    def observe(self, sample: CliffTelemetry, *, now: float | None = None) -> Notification | None:
        try:
            front = [sample.distances[index] for index in self.front_sensor_indices]
        except IndexError as exc:
            raise ValueError("front_sensor_index_out_of_range") from exc
        unsafe_values = [value for value in front if self._unsafe(value)]
        if unsafe_values:
            self._unsafe_count += 1
            self._clear_count = 0
        else:
            self._unsafe_count = 0
            if all(self._clear(value) for value in front):
                self._clear_count += 1
            else:
                self._clear_count = 0
        if self.active and self._clear_count >= self.clear_samples:
            self.active = False
            self._clear_count = 0
        if self.active or self._unsafe_count < self.confirm_samples:
            return None
        self.active = True
        self.episode += 1
        details = {
            "front_values": tuple(front),
            "threshold": self.unsafe_threshold,
            "unsafe_when": self.unsafe_when,
            "source": sample.source,
            "episode": self.episode,
        }
        # Motion cancellation is independent of speech and happens first.
        if self.emergency_stop is not None:
            self.emergency_stop(details)
        created = time.monotonic() if now is None else float(now)
        return Notification(
            kind="cliff_detected",
            priority=EventPriority.SAFETY,
            speech_intent="立即说明检测到前方跌落风险且已停止移动",
            fallback_text="前方检测到跌落风险，我已经停止移动。",
            created_at=created,
            dedupe_key=f"cliff:{self.episode}",
            requires_idle=False,
            metadata=details,
        )


@dataclass(frozen=True)
class PoseCoverage:
    visible_points: int
    required_points: int
    reason: str = ""
    bbox: tuple[float, float, float, float] | None = None
    timestamp: float = 0.0


class FitnessFramingMonitor:
    def __init__(
        self,
        *,
        minimum_ratio: float = 0.70,
        hold_seconds: float = 1.2,
        repeat_seconds: float = 10.0,
    ) -> None:
        self.minimum_ratio = min(1.0, max(0.1, float(minimum_ratio)))
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.repeat_seconds = max(1.0, float(repeat_seconds))
        self.issue_started_at: float | None = None
        self.last_prompt_at = float("-inf")
        self.episode = 0

    def observe(self, coverage: PoseCoverage, *, now: float | None = None) -> Notification | None:
        current = time.monotonic() if now is None else float(now)
        ratio = coverage.visible_points / max(1, coverage.required_points)
        problematic = ratio < self.minimum_ratio or coverage.reason in {
            "no_pose", "torso_not_visible", "elbows_not_visible", "identity_not_locked"
        }
        if not problematic:
            self.issue_started_at = None
            return None
        if self.issue_started_at is None:
            self.issue_started_at = current
            self.episode += 1
            return None
        if current - self.issue_started_at < self.hold_seconds:
            return None
        if current - self.last_prompt_at < self.repeat_seconds:
            return None
        self.last_prompt_at = current
        direction, fallback = self._guidance(coverage.bbox, ratio)
        return Notification(
            kind="fitness_framing",
            priority=EventPriority.ATTENTION,
            speech_intent="自然提醒用户调整站位，确保全身关节点进入画面，不责怪用户",
            fallback_text=fallback,
            created_at=current,
            dedupe_key=f"fitness_framing:{self.episode}:{int(current // self.repeat_seconds)}",
            requires_idle=False,
            metadata={
                "visible_points": coverage.visible_points,
                "required_points": coverage.required_points,
                "visible_ratio": ratio,
                "reason": coverage.reason,
                "guidance": direction,
            },
        )

    @staticmethod
    def _guidance(
        bbox: tuple[float, float, float, float] | None,
        ratio: float,
    ) -> tuple[str, str]:
        if bbox is not None:
            left, top, right, bottom = bbox
            width, height = right - left, bottom - top
            if left <= 0.02:
                return "move_image_right", "你的身体有一部分出了画面左侧，往右挪一点我就能继续数了。"
            if right >= 0.98:
                return "move_image_left", "你的身体有一部分出了画面右侧，往左挪一点我就能继续数了。"
            if top <= 0.02 or bottom >= 0.98 or width >= 0.94 or height >= 0.94:
                return "move_farther", "目前全身还没有完整入镜，稍微离摄像头远一点。"
            if width <= 0.18 and height <= 0.30:
                return "move_closer", "你在画面里有点小，往摄像头方向靠近一点。"
        if ratio < 0.4:
            return "enter_and_center", "我暂时没看全你的动作，请站到画面中间，让全身都出现在镜头里。"
        return "adjust_position", "还有几个关键关节点没拍全，请前后左右挪一点，保持全身在画面里。"


@dataclass(frozen=True)
class TaskAction:
    kind: str
    name: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    speech_intent: str = ""
    fallback_text: str = ""


@dataclass(frozen=True)
class TaskSnapshot:
    task_name: str
    arguments: Mapping[str, Any]
    location: str | None = None
    count: int = 0
    elapsed_seconds: float = 0.0
    resume_prefix: tuple[TaskAction, ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)


class InterruptibleTaskCoordinator:
    """Deterministic lifecycle; Qwen renders speech from semantic briefs."""

    def __init__(self) -> None:
        self.state = "idle"
        self.active: TaskSnapshot | None = None
        self.suspended: TaskSnapshot | None = None
        self.interruption_name = ""
        self.interruption_arguments: dict[str, Any] = {}

    def start(self, snapshot: TaskSnapshot) -> None:
        if self.state != "idle":
            raise RuntimeError(f"task_coordinator_busy:{self.state}")
        self.active = snapshot
        self.state = "running"

    def checkpoint(self, *, count: int | None = None, elapsed_seconds: float | None = None) -> None:
        if self.active is None:
            return
        self.active = replace(
            self.active,
            count=self.active.count if count is None else max(0, int(count)),
            elapsed_seconds=(
                self.active.elapsed_seconds
                if elapsed_seconds is None
                else max(0.0, float(elapsed_seconds))
            ),
        )

    def interrupt(self, name: str, arguments: Mapping[str, Any]) -> tuple[TaskAction, ...]:
        if self.state != "running" or self.active is None:
            raise RuntimeError("no_interruptible_task")
        self.suspended = self.active
        self.active = None
        self.interruption_name = str(name)
        self.interruption_arguments = dict(arguments)
        self.state = "interrupting"
        return (
            TaskAction("cancel_active", self.suspended.task_name),
            TaskAction("head_level", "head_control", {"direction": "level"}),
            TaskAction(
                "execute_interruption",
                self.interruption_name,
                self.interruption_arguments,
                "立即承接用户的新任务，并说明已暂停之前的任务",
                "好，我先暂停刚才的任务，马上处理这件事。",
            ),
        )

    def interruption_completed(self, *, session_remains_active: bool = False) -> tuple[TaskAction, ...]:
        if self.state != "interrupting":
            raise RuntimeError(f"not_interrupting:{self.state}")
        if session_remains_active:
            self.state = "interruption_session_active"
            return ()
        return self._ask_resume()

    def interruption_session_ended(self) -> tuple[TaskAction, ...]:
        if self.state != "interruption_session_active":
            raise RuntimeError(f"no_active_interruption_session:{self.state}")
        return self._ask_resume()

    def _ask_resume(self) -> tuple[TaskAction, ...]:
        self.state = "awaiting_resume"
        snapshot = self.suspended
        if snapshot is None:
            self.state = "idle"
            return ()
        return (
            TaskAction(
                "ask_resume",
                snapshot.task_name,
                {"count": snapshot.count, "elapsed_seconds": snapshot.elapsed_seconds},
                "结合刚才暂停的任务和当前进度，自然询问用户是否继续；一次只问这一件事",
                "刚才的运动暂停了，你还想从刚才的进度继续吗？",
            ),
        )

    def resume_decision(self, accepted: bool) -> tuple[TaskAction, ...]:
        if self.state != "awaiting_resume" or self.suspended is None:
            raise RuntimeError(f"not_awaiting_resume:{self.state}")
        snapshot = self.suspended
        if not accepted:
            self._clear()
            return (
                TaskAction(
                    "resume_declined",
                    snapshot.task_name,
                    speech_intent="自然确认不再恢复刚才的任务，不追加设备动作",
                    fallback_text="好，那刚才的任务就先不继续了。",
                ),
            )
        arguments = dict(snapshot.arguments)
        arguments.update(
            {
                "initial_count": snapshot.count,
                "initial_elapsed_seconds": snapshot.elapsed_seconds,
                "resume_from_interrupt": True,
            }
        )
        actions: list[TaskAction] = []
        if snapshot.location:
            actions.append(TaskAction("navigate_back", "navigation_goto", {"point": snapshot.location}))
        actions.extend(snapshot.resume_prefix)
        actions.append(
            TaskAction(
                "resume_task",
                snapshot.task_name,
                arguments,
                "说明将从保存的进度继续，不要声称重新计数",
                f"好，我回到刚才的位置，从第{snapshot.count}个之后继续。",
            )
        )
        self.active = replace(snapshot, arguments=arguments)
        self.suspended = None
        self.state = "running"
        return tuple(actions)

    def _clear(self) -> None:
        self.state = "idle"
        self.active = None
        self.suspended = None
        self.interruption_name = ""
        self.interruption_arguments = {}


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
