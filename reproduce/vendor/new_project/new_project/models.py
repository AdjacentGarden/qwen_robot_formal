from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskStatus(str, Enum):
    NEW = "new"
    NEEDS_INFO = "needs_info"
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(str, Enum):
    OPEN = "open"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    INTERRUPTED_CURRENT_TASK = "interrupted_current_task"
    FAILED = "failed"


@dataclass
class WakeupEvent:
    event_id: str = field(default_factory=lambda: new_id("wake"))
    timestamp: float = field(default_factory=now_ts)
    source: str = "manual"
    raw_value: str | None = None
    interrupted_task_group_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandSession:
    session_id: str = field(default_factory=lambda: new_id("session"))
    wakeup_event_id: str | None = None
    session_type: str = "manual_text"
    started_at: float = field(default_factory=now_ts)
    ended_at: float | None = None
    status: str = SessionStatus.OPEN.value
    utterances: list[dict[str, Any]] = field(default_factory=list)
    task_group_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskStep:
    step_id: str = field(default_factory=lambda: new_id("step"))
    order: int = 0
    skill_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    resources: list[str] = field(default_factory=list)
    status: str = TaskStatus.NEW.value
    started_at: float | None = None
    ended_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    interruptible: bool = True
    stop_skill_name: str | None = None
    stop_arguments: dict[str, Any] = field(default_factory=dict)
    depends_on_slots: list[str] = field(default_factory=list)


@dataclass
class TaskGroup:
    task_group_id: str = field(default_factory=lambda: new_id("task"))
    command_session_id: str | None = None
    user_instruction: str = ""
    title: str = ""
    slots: dict[str, Any] = field(default_factory=dict)
    followups: list[dict[str, Any]] = field(default_factory=list)
    steps: list[TaskStep] = field(default_factory=list)
    status: str = TaskStatus.NEW.value
    created_at: float = field(default_factory=now_ts)
    started_at: float | None = None
    ended_at: float | None = None
    interruption_count: int = 0
    interrupted_by_session_id: str | None = None
    resume_context: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    history_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def to_dict(obj: Any) -> Any:
    return _json_safe(obj, set())


def _json_safe(obj: Any, seen: set[int]) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if dataclasses.is_dataclass(obj):
        obj_id = id(obj)
        if obj_id in seen:
            return repr(obj)
        seen.add(obj_id)
        try:
            return {field.name: _json_safe(getattr(obj, field.name), seen) for field in dataclasses.fields(obj)}
        finally:
            seen.discard(obj_id)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        obj_id = id(obj)
        if obj_id in seen:
            return repr(obj)
        seen.add(obj_id)
        try:
            return {str(_json_safe(key, seen)): _json_safe(value, seen) for key, value in obj.items()}
        finally:
            seen.discard(obj_id)
    if isinstance(obj, (list, tuple, set, frozenset)):
        obj_id = id(obj)
        if obj_id in seen:
            return repr(obj)
        seen.add(obj_id)
        try:
            return [_json_safe(item, seen) for item in obj]
        finally:
            seen.discard(obj_id)
    return repr(obj)


def task_step_from_dict(data: dict[str, Any]) -> TaskStep:
    return TaskStep(**data)


def task_group_from_dict(data: dict[str, Any]) -> TaskGroup:
    copied = dict(data)
    copied["steps"] = [task_step_from_dict(item) for item in copied.get("steps", [])]
    return TaskGroup(**copied)


def command_session_from_dict(data: dict[str, Any]) -> CommandSession:
    return CommandSession(**data)


def wakeup_event_from_dict(data: dict[str, Any]) -> WakeupEvent:
    return WakeupEvent(**data)
