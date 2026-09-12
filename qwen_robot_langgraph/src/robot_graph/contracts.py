from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, TypedDict


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Capability:
    resources: tuple[str, ...]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    reversible: bool = True
    cancellable: bool = False
    physical: bool = True


CAPABILITIES = {
    "navigation.goto": Capability(("base", "motion"), ("point",), cancellable=True),
    "base.move": Capability(("base", "motion"), ("direction",), cancellable=True),
    "pet.track": Capability(("base", "motion", "front_camera"), cancellable=True),
    "person.track": Capability(("base", "motion", "front_camera"), cancellable=True),
    "head.move": Capability(("head", "motion"), ("pose",)),
    "sensor.gate": Capability(("motion",), ("enabled",)),
    "projector.start": Capability(("projector",), ("mode",)),
    "projector.off": Capability(("projector",)),
    "projector.pause": Capability(("projector",)),
    "projector.resume": Capability(("projector",)),
    "projector.status": Capability(("projector",), physical=False),
    "camera.capture": Capability(("front_camera",), ("camera",)),
    "pet.observe": Capability(("front_camera",)),
    "exercise.count": Capability(("back_camera", "npu"), ("exercise", "count"), cancellable=True),
    "light.set": Capability(("light",), ("enabled",)),
    "feeder.feed": Capability(("feeder",), ("grams",), reversible=False),
    "speech.say": Capability(("speaker",), ("text",), cancellable=True),
    "speaker.test": Capability(("speaker",), cancellable=True),
    "feeder.status": Capability(("feeder",), physical=False),
    "system.status": Capability((), physical=False),
    "system.time": Capability((), physical=False),
}


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    timeout: float = 30.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {"kind", "args", "timeout"}:
            raise PolicyError("invalid_action")
        return cls(**data)


class StationaryPolicy:
    """No runtime/API switch can lift the current user's wheel prohibition."""
    base_locked = True

    def validate(self, action: Action):
        cap = CAPABILITIES.get(action.kind)
        if cap is None:
            raise PolicyError("unknown_capability:" + action.kind)
        if "base" in cap.resources:
            raise PolicyError("base_motion_forbidden:机器人当前禁止底轮运动")
        if not isinstance(action.args, dict):
            raise PolicyError("arguments_must_be_object")
        keys = set(action.args)
        if keys - set(cap.required + cap.optional) or not set(cap.required) <= keys:
            raise PolicyError("invalid_arguments:" + action.kind)
        if isinstance(action.timeout, bool) or not isinstance(action.timeout, (float, int)) or not math.isfinite(action.timeout) or not 0 < action.timeout <= 3600:
            raise PolicyError("invalid_timeout")
        a = action.args
        enums = {"pose": {"up", "down", "level"}, "mode": {"meeting", "exercise"}, "camera": {"front", "back"}, "exercise": {"push_up", "squat", "pull_up"}}
        for key, values in enums.items():
            if key in a and (not isinstance(a[key], str) or a[key] not in values):
                raise PolicyError("invalid_" + key)
        if "enabled" in a and type(a["enabled"]) is not bool:
            raise PolicyError("invalid_enabled")
        if "grams" in a and (type(a["grams"]) is not int or a["grams"] not in range(10, 101, 10)):
            raise PolicyError("feed_requires_10_to_100_grams_in_10g_steps")
        if "count" in a and (type(a["count"]) is not int or not 1 <= a["count"] <= 200):
            raise PolicyError("invalid_count")
        if "text" in a and (not isinstance(a["text"], str) or not 0 < len(a["text"]) <= 500):
            raise PolicyError("invalid_speech_text")
        return cap


class TaskState(TypedDict, total=False):
    task_id: str
    session_id: str
    plan: dict
    status: str
    index: int
    cleanup_index: int
    operation_id: str
    result: dict
    error: str
    command: str
    epoch: int
    cleanup: bool
    outcome: str
    hold_until: float
    pause_started: float
    results: list[dict]


class DialogueState(TypedDict, total=False):
    session_id: str
    turn_id: str
    text: str
    intent: dict
    reply: str
    task_id: str
    error: str
    # DialogueState ends above; task graph uses the following explicit schema below.


class ExecutionState(TaskState, total=False):
    phase: str
    phase_index: int
    route: str
    started_projection: bool
    resume_operation: bool
    partial_count: int
    cleanup_errors: list[str]
    current_action: dict
