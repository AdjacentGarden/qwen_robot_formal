from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import ensure_runtime_dirs, runtime_dir
from .models import CommandSession, SessionStatus, TaskGroup, TaskStatus, command_session_from_dict, task_group_from_dict, to_dict


class JsonStore:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        ensure_runtime_dirs(config)
        self.root = runtime_dir(config)
        self.state_path = self.root / "runtime_state.json"
        if not self.state_path.exists():
            self.save_state(
                {
                    "active_task_group_id": None,
                    "task_queue": [],
                    "interrupted_stack": [],
                    "current_command_session_id": None,
                    "updated_at": time.time(),
                }
            )

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def _write_json(self, path: Path, data: Any) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(to_dict(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)

    def _read_json(self, path: Path) -> Any:
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def load_state(self) -> dict[str, Any]:
        return self._read_json(self.state_path)

    def save_state(self, state: dict[str, Any]) -> None:
        copied = dict(state)
        copied["updated_at"] = time.time()
        self._write_json(self.state_path, copied)

    def _interrupted_retention_seconds(self) -> float:
        value = self.config.get("planner", {}).get("interrupted_task_retention_seconds", 3600)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 3600.0

    def save_session(self, session: CommandSession) -> None:
        self._write_json(self.root / "sessions" / f"{session.session_id}.json", to_dict(session))

    def load_session(self, session_id: str) -> CommandSession:
        return command_session_from_dict(self._read_json(self.root / "sessions" / f"{session_id}.json"))

    def save_task_group(self, task_group: TaskGroup) -> None:
        self._write_json(self.root / "task_groups" / f"{task_group.task_group_id}.json", to_dict(task_group))

    def load_task_group(self, task_group_id: str) -> TaskGroup:
        return task_group_from_dict(self._read_json(self.root / "task_groups" / f"{task_group_id}.json"))

    def update_task_group(
        self,
        task_group_id: str,
        updater: Callable[[TaskGroup], TaskGroup | None],
        fallback: TaskGroup | None = None,
    ) -> TaskGroup:
        """Atomically load, mutate, and save one TaskGroup within this runtime."""
        with self._lock:
            path = self.root / "task_groups" / f"{task_group_id}.json"
            if path.exists():
                current = task_group_from_dict(json.loads(path.read_text(encoding="utf-8")))
            elif fallback is not None:
                current = fallback
            else:
                raise FileNotFoundError(path)
            updated = updater(current) or current
            self._write_json(path, to_dict(updated))
            return updated

    def enqueue_task_group(self, task_group: TaskGroup) -> None:
        if task_group.status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.NEEDS_INFO.value,
            TaskStatus.INTERRUPTED.value,
            TaskStatus.RUNNING.value,
        }:
            self.append_event(
                "non_ready_task_group_enqueue_skipped",
                {"task_group_id": task_group.task_group_id, "status": task_group.status},
            )
            return
        state = self.load_state()
        queue = list(state.get("task_queue", []))
        if task_group.task_group_id not in queue:
            queue.append(task_group.task_group_id)
        state["task_queue"] = queue
        task_group.status = TaskStatus.QUEUED.value
        self.save_task_group(task_group)
        self.save_state(state)

    @staticmethod
    def _terminal_statuses() -> set[str]:
        return {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}

    @staticmethod
    def _non_ready_statuses() -> set[str]:
        return {TaskStatus.NEEDS_INFO.value, TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value}

    @staticmethod
    def _invalid_active_statuses() -> set[str]:
        return {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.NEEDS_INFO.value,
            TaskStatus.INTERRUPTED.value,
        }

    def _reset_queued_task_groups_to_new(self, task_group_ids: list[str], reason: str) -> list[str]:
        reset: list[str] = []
        for item in task_group_ids:
            task_group_id = str(item)
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                self.append_event(
                    "queued_task_group_missing",
                    {"task_group_id": task_group_id, "operation": reason, "error": str(exc)},
                )
                continue
            if task_group.status == TaskStatus.QUEUED.value:
                task_group.status = TaskStatus.NEW.value
                task_group.result_summary = ""
                self.save_task_group(task_group)
                reset.append(task_group.task_group_id)
        return reset

    @staticmethod
    def _task_group_is_waiting_for_user(task_group: TaskGroup) -> bool:
        if task_group.status == TaskStatus.NEEDS_INFO.value:
            return True
        if task_group.status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.INTERRUPTED.value,
        }:
            return False
        last_followup = task_group.followups[-1] if task_group.followups else {}
        return bool(last_followup) and not last_followup.get("answer")

    def _waiting_task_group_orphan_reason(self, task_group: TaskGroup) -> str:
        session_id = str(task_group.command_session_id or "").strip()
        if not session_id:
            return "missing_command_session_id"
        try:
            session = self.load_session(session_id)
        except Exception as exc:
            return f"missing_command_session:{exc}"
        if session.status != SessionStatus.WAITING_USER.value:
            return f"session_not_waiting:{session.status}"
        if task_group.task_group_id not in [str(item) for item in session.task_group_ids]:
            return "not_in_session_task_group_ids"
        return ""

    def _repair_orphan_waiting_task_groups(self, reason: str) -> list[dict[str, Any]]:
        task_dir = self.root / "task_groups"
        cancelled: list[dict[str, Any]] = []
        if not task_dir.exists():
            return cancelled
        now = time.time()
        for path in task_dir.glob("*.json"):
            try:
                task_group = self.load_task_group(path.stem)
            except Exception:
                continue
            if not self._task_group_is_waiting_for_user(task_group):
                continue
            orphan_reason = self._waiting_task_group_orphan_reason(task_group)
            if not orphan_reason:
                continue
            task_group.status = TaskStatus.CANCELLED.value
            task_group.ended_at = now
            task_group.result_summary = "cancelled"
            task_group.metadata["cancel_reason"] = reason
            task_group.metadata["cancelled_orphan_waiting_followup"] = orphan_reason
            self.save_task_group(task_group)
            cancelled.append({"task_group_id": task_group.task_group_id, "reason": orphan_reason})
        return cancelled

    def pop_next_task_group(self) -> TaskGroup | None:
        state = self.load_state()
        queue = list(state.get("task_queue", []))
        changed = False
        while queue:
            task_group_id = str(queue.pop(0))
            changed = True
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                state["task_queue"] = queue
                state["active_task_group_id"] = None
                self.save_state(state)
                self.append_event(
                    "queued_task_group_missing",
                    {"task_group_id": task_group_id, "operation": "pop_next_task_group", "error": str(exc)},
                )
                continue
            if task_group.status in self._terminal_statuses():
                state["task_queue"] = queue
                state["active_task_group_id"] = None
                self.save_state(state)
                self.append_event(
                    "terminal_task_group_queue_skipped",
                    {"task_group_id": task_group_id, "operation": "pop_next_task_group", "status": task_group.status},
                )
                continue
            if task_group.status in self._non_ready_statuses():
                remaining = [str(item) for item in queue]
                reset = self._reset_queued_task_groups_to_new(remaining, "pop_next_task_group_blocked_by_non_ready")
                state["task_queue"] = []
                state["active_task_group_id"] = None
                self.save_state(state)
                self.append_event(
                    "non_ready_task_group_queue_blocked",
                    {
                        "task_group_id": task_group_id,
                        "operation": "pop_next_task_group",
                        "status": task_group.status,
                        "removed_from_queue": remaining,
                        "reset_to_new": reset,
                    },
                )
                return None
            state["task_queue"] = queue
            state["active_task_group_id"] = task_group_id
            self.save_state(state)
            return task_group
        if changed:
            state["task_queue"] = []
            state["active_task_group_id"] = None
            self.save_state(state)
        return None

    def pop_task_group(self, task_group_id: str) -> TaskGroup | None:
        task_group_id = str(task_group_id)
        state = self.load_state()
        queue = list(state.get("task_queue", []))
        if task_group_id not in queue:
            return None
        queue.remove(task_group_id)
        try:
            task_group = self.load_task_group(task_group_id)
        except Exception as exc:
            state["task_queue"] = queue
            if state.get("active_task_group_id") == task_group_id:
                state["active_task_group_id"] = None
            self.save_state(state)
            self.append_event(
                "queued_task_group_missing",
                {"task_group_id": task_group_id, "operation": "pop_task_group", "error": str(exc)},
            )
            return None
        state["task_queue"] = queue
        if task_group.status in self._terminal_statuses() or task_group.status in self._non_ready_statuses():
            state["active_task_group_id"] = None
            self.append_event(
                "non_active_task_group_popped",
                {"task_group_id": task_group_id, "operation": "pop_task_group", "status": task_group.status},
            )
        else:
            state["active_task_group_id"] = task_group_id
        self.save_state(state)
        return task_group

    def repair_runtime_state(self, reason: str, *, clear_ready_queue: bool = False) -> dict[str, Any]:
        state = self.load_state()
        original = {
            "active_task_group_id": state.get("active_task_group_id"),
            "task_queue": list(state.get("task_queue", [])),
            "current_command_session_id": state.get("current_command_session_id"),
        }
        changed = False
        events: list[dict[str, Any]] = []

        active_id = state.get("active_task_group_id")
        if active_id:
            active_id = str(active_id)
            try:
                active = self.load_task_group(active_id)
            except Exception as exc:
                state["active_task_group_id"] = None
                changed = True
                events.append({"kind": "active_missing", "task_group_id": active_id, "error": str(exc)})
            else:
                if active.status in self._invalid_active_statuses():
                    state["active_task_group_id"] = None
                    changed = True
                    events.append({"kind": "active_not_running", "task_group_id": active_id, "status": active.status})

        raw_queue = [str(item) for item in state.get("task_queue", []) if item]
        repaired_queue: list[str] = []
        seen: set[str] = set()
        blocked_by: dict[str, Any] | None = None
        trailing_ids: list[str] = []
        for index, task_group_id in enumerate(raw_queue):
            if task_group_id in seen:
                changed = True
                events.append({"kind": "queue_duplicate_removed", "task_group_id": task_group_id})
                continue
            seen.add(task_group_id)
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                changed = True
                events.append({"kind": "queue_missing_removed", "task_group_id": task_group_id, "error": str(exc)})
                continue
            if task_group.status in self._terminal_statuses():
                changed = True
                events.append({"kind": "queue_terminal_removed", "task_group_id": task_group_id, "status": task_group.status})
                continue
            if task_group.status in self._non_ready_statuses():
                trailing_ids = [str(item) for item in raw_queue[index + 1 :] if item]
                reset = self._reset_queued_task_groups_to_new(trailing_ids, f"{reason}_blocked_by_non_ready")
                blocked_by = {
                    "task_group_id": task_group_id,
                    "status": task_group.status,
                    "removed_after_blocker": trailing_ids,
                    "reset_to_new": reset,
                }
                changed = True
                events.append({"kind": "queue_blocked_by_non_ready", **blocked_by})
                break
            repaired_queue.append(task_group_id)

        if repaired_queue != raw_queue:
            state["task_queue"] = repaired_queue
            changed = True
        if clear_ready_queue and repaired_queue:
            reset = self._reset_queued_task_groups_to_new(repaired_queue, f"{reason}_clear_ready_queue")
            events.append({"kind": "queue_ready_cleared_for_session_boundary", "removed": list(repaired_queue), "reset_to_new": reset})
            repaired_queue = []
            state["task_queue"] = []
            changed = True

        current_session_id = state.get("current_command_session_id")
        if current_session_id:
            current_session_id = str(current_session_id)
            try:
                session = self.load_session(current_session_id)
            except Exception as exc:
                state["current_command_session_id"] = None
                changed = True
                events.append({"kind": "current_session_missing", "session_id": current_session_id, "error": str(exc)})
            else:
                if session.status in {SessionStatus.COMPLETED.value, SessionStatus.FAILED.value}:
                    state["current_command_session_id"] = None
                    changed = True
                    events.append({"kind": "current_session_terminal", "session_id": current_session_id, "status": session.status})

        cancelled_orphans = self._repair_orphan_waiting_task_groups(reason)
        if cancelled_orphans:
            changed = True
            events.append({"kind": "orphan_waiting_task_groups_cancelled", "cancelled": cancelled_orphans})

        if changed:
            self.save_state(state)
        cleanup = self.prune_interrupted_stack()
        final_state = self.load_state()

        result = {
            "ok": True,
            "reason": reason,
            "changed": changed,
            "events": events,
            "blocked_by": blocked_by,
            "before": original,
            "after": {
                "active_task_group_id": final_state.get("active_task_group_id"),
                "task_queue": list(final_state.get("task_queue", [])),
                "current_command_session_id": final_state.get("current_command_session_id"),
            },
            "interrupted_cleanup": cleanup,
        }
        if changed or cleanup.get("removed"):
            self.append_event("runtime_state_repaired", result)
        return result

    def validate_runtime_state(self) -> dict[str, Any]:
        state = self.load_state()
        issues: list[dict[str, Any]] = []

        active_id = state.get("active_task_group_id")
        if active_id:
            active_id = str(active_id)
            try:
                active = self.load_task_group(active_id)
            except Exception as exc:
                issues.append({"kind": "active_missing", "task_group_id": active_id, "error": str(exc)})
            else:
                if active.status in self._invalid_active_statuses():
                    issues.append({"kind": "active_invalid_status", "task_group_id": active_id, "status": active.status})

        seen_queue: set[str] = set()
        queue_blocked = False
        for index, item in enumerate(state.get("task_queue", []) or []):
            task_group_id = str(item)
            if task_group_id in seen_queue:
                issues.append({"kind": "queue_duplicate", "task_group_id": task_group_id, "index": index})
                continue
            seen_queue.add(task_group_id)
            if active_id and task_group_id == str(active_id):
                issues.append({"kind": "queue_contains_active", "task_group_id": task_group_id, "index": index})
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                issues.append({"kind": "queue_missing_task_group", "task_group_id": task_group_id, "index": index, "error": str(exc)})
                continue
            if queue_blocked:
                issues.append({"kind": "queue_has_task_after_blocker", "task_group_id": task_group_id, "status": task_group.status, "index": index})
            if task_group.status in self._terminal_statuses():
                issues.append({"kind": "queue_terminal_task_group", "task_group_id": task_group_id, "status": task_group.status, "index": index})
            elif task_group.status in self._non_ready_statuses():
                issues.append({"kind": "queue_non_ready_task_group", "task_group_id": task_group_id, "status": task_group.status, "index": index})
                queue_blocked = True
            elif task_group.status != TaskStatus.QUEUED.value:
                issues.append({"kind": "queue_unexpected_status", "task_group_id": task_group_id, "status": task_group.status, "index": index})

        seen_stack: set[str] = set()
        for index, item in enumerate(state.get("interrupted_stack", []) or []):
            task_group_id = str(item)
            if task_group_id in seen_stack:
                issues.append({"kind": "interrupted_duplicate", "task_group_id": task_group_id, "index": index})
                continue
            seen_stack.add(task_group_id)
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                issues.append({"kind": "interrupted_missing_task_group", "task_group_id": task_group_id, "index": index, "error": str(exc)})
                continue
            if task_group.status != TaskStatus.INTERRUPTED.value:
                issues.append({"kind": "interrupted_unexpected_status", "task_group_id": task_group_id, "status": task_group.status, "index": index})
            if (task_group.resume_context or {}).get("can_resume") is False:
                issues.append({"kind": "interrupted_can_resume_false", "task_group_id": task_group_id, "index": index})

        current_session_id = state.get("current_command_session_id")
        if current_session_id:
            current_session_id = str(current_session_id)
            try:
                session = self.load_session(current_session_id)
            except Exception as exc:
                issues.append({"kind": "current_session_missing", "session_id": current_session_id, "error": str(exc)})
            else:
                if session.status in {SessionStatus.COMPLETED.value, SessionStatus.FAILED.value}:
                    issues.append({"kind": "current_session_terminal", "session_id": current_session_id, "status": session.status})

        session_dir = self.root / "sessions"
        waiting_session_ids: set[str] = set()
        if session_dir.exists():
            for path in session_dir.glob("*.json"):
                try:
                    session = self.load_session(path.stem)
                except Exception:
                    continue
                if session.status != SessionStatus.WAITING_USER.value:
                    continue
                waiting_session_ids.add(session.session_id)
                has_waiting_task = False
                for task_group_id in session.task_group_ids:
                    try:
                        task_group = self.load_task_group(str(task_group_id))
                    except Exception as exc:
                        issues.append({"kind": "waiting_session_missing_task_group", "session_id": session.session_id, "task_group_id": str(task_group_id), "error": str(exc)})
                        continue
                    if self._task_group_is_waiting_for_user(task_group):
                        has_waiting_task = True
                if not has_waiting_task:
                    issues.append({"kind": "waiting_session_without_waiting_task", "session_id": session.session_id})

        task_dir = self.root / "task_groups"
        if task_dir.exists():
            for path in task_dir.glob("*.json"):
                try:
                    task_group = self.load_task_group(path.stem)
                except Exception:
                    continue
                if not self._task_group_is_waiting_for_user(task_group):
                    continue
                orphan_reason = self._waiting_task_group_orphan_reason(task_group)
                if orphan_reason:
                    issues.append({"kind": "orphan_waiting_task_group", "task_group_id": task_group.task_group_id, "reason": orphan_reason})
                elif task_group.command_session_id not in waiting_session_ids:
                    issues.append({"kind": "waiting_task_group_session_not_indexed", "task_group_id": task_group.task_group_id, "session_id": task_group.command_session_id})

        return {"ok": not issues, "issues": issues, "state": state}

    def clear_task_queue(self, reason: str) -> dict[str, Any]:
        state = self.load_state()
        queue = list(state.get("task_queue", []))
        if not queue:
            return {"ok": True, "reason": reason, "cleared_task_group_ids": []}
        state["task_queue"] = []
        self.save_state(state)
        result = {"ok": True, "reason": reason, "cleared_task_group_ids": queue}
        self.append_event("task_queue_cleared", result)
        return result

    def set_active(self, task_group_id: str | None) -> None:
        state = self.load_state()
        state["active_task_group_id"] = task_group_id
        self.save_state(state)

    def clear_runtime_interrupt_state(
        self,
        reason: str,
        *,
        clear_active: bool = True,
        clear_interrupted: bool = True,
        clear_current_session: bool = True,
    ) -> dict[str, Any]:
        state = self.load_state()
        active_id = state.get("active_task_group_id")
        stack = list(state.get("interrupted_stack") or [])
        current_session_id = state.get("current_command_session_id")
        changed = False

        if clear_active and active_id:
            state["active_task_group_id"] = None
            changed = True
        if clear_interrupted and stack:
            state["interrupted_stack"] = []
            changed = True
        if clear_current_session and current_session_id:
            state["current_command_session_id"] = None
            changed = True

        if changed:
            self.save_state(state)

        result = {
            "ok": True,
            "reason": reason,
            "active_cleared": bool(clear_active and active_id),
            "cleared_task_group_ids": ([active_id] if clear_active and active_id else []) + (stack if clear_interrupted else []),
            "cleared": {
                "active_task_group_id": active_id if clear_active else None,
                "interrupted_stack": stack if clear_interrupted else [],
                "current_command_session_id": current_session_id if clear_current_session else None,
            },
        }
        if changed:
            self.append_event("runtime_interrupt_state_cleared", result)
        return result

    def clear_waiting_followups(self, reason: str) -> dict[str, Any]:
        now = time.time()
        cleared_task_group_ids: list[str] = []
        task_dir = self.root / "task_groups"
        if task_dir.exists():
            for path in task_dir.glob("*.json"):
                try:
                    task_group = self.load_task_group(path.stem)
                except Exception:
                    continue
                last_followup = task_group.followups[-1] if task_group.followups else {}
                waiting_followup = bool(last_followup) and not last_followup.get("answer")
                if task_group.status != TaskStatus.NEEDS_INFO.value and not waiting_followup:
                    continue
                if task_group.status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.INTERRUPTED.value}:
                    continue
                task_group.status = TaskStatus.CANCELLED.value
                task_group.ended_at = now
                task_group.result_summary = "cancelled"
                task_group.metadata["cancel_reason"] = reason
                task_group.metadata["cancelled_waiting_followup"] = True
                self.save_task_group(task_group)
                cleared_task_group_ids.append(task_group.task_group_id)

        cleared_session_ids: list[str] = []
        session_dir = self.root / "sessions"
        if session_dir.exists():
            for path in session_dir.glob("*.json"):
                try:
                    session = self.load_session(path.stem)
                except Exception:
                    continue
                if session.status != SessionStatus.WAITING_USER.value:
                    continue
                session.status = SessionStatus.COMPLETED.value
                session.ended_at = session.ended_at or now
                session.metadata["closed_waiting_followup_reason"] = reason
                self.save_session(session)
                cleared_session_ids.append(session.session_id)

        result = {
            "ok": True,
            "reason": reason,
            "cleared_task_group_ids": cleared_task_group_ids,
            "cleared_session_ids": cleared_session_ids,
        }
        if cleared_task_group_ids or cleared_session_ids:
            self.append_event("waiting_followups_cleared", result)
        return result

    def push_interrupted(self, task_group: TaskGroup) -> None:
        state = self.load_state()
        stack = list(state.get("interrupted_stack", []))
        if task_group.task_group_id not in stack:
            stack.append(task_group.task_group_id)
        state["interrupted_stack"] = stack
        state["active_task_group_id"] = None
        self.save_task_group(task_group)
        self.save_state(state)

    def prune_interrupted_stack(self, max_age_seconds: float | None = None) -> dict[str, Any]:
        state = self.load_state()
        stack = list(state.get("interrupted_stack", []))
        if max_age_seconds is None:
            max_age_seconds = self._interrupted_retention_seconds()
        now = time.time()
        kept: list[str] = []
        removed: list[dict[str, Any]] = []
        seen: set[str] = set()
        terminal = {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }

        for task_group_id in stack:
            if task_group_id in seen:
                removed.append({"task_group_id": task_group_id, "reason": "duplicate"})
                continue
            seen.add(task_group_id)
            try:
                task_group = self.load_task_group(task_group_id)
            except Exception as exc:
                removed.append({"task_group_id": task_group_id, "reason": "missing", "error": str(exc)})
                continue

            context = dict(task_group.resume_context or {})
            reason = ""
            if task_group.status in terminal:
                reason = f"terminal:{task_group.status}"
            elif context.get("can_resume") is False:
                reason = "can_resume_false"
            elif max_age_seconds and max_age_seconds > 0:
                interrupted_at = context.get("interrupted_at") or task_group.ended_at or task_group.created_at
                if isinstance(interrupted_at, (int, float)) and now - float(interrupted_at) > float(max_age_seconds):
                    reason = "expired"

            if not reason:
                kept.append(task_group_id)
                continue

            if reason == "expired" and task_group.status == TaskStatus.INTERRUPTED.value:
                task_group.status = TaskStatus.CANCELLED.value
                task_group.ended_at = now
                task_group.result_summary = "cancelled"
                task_group.resume_context = context
                task_group.resume_context.update(
                    {
                        "can_resume": False,
                        "cancelled_at": now,
                        "cancel_reason": "expired_interrupted_task",
                    }
                )
                self.save_task_group(task_group)
            removed.append({"task_group_id": task_group_id, "reason": reason})

        if kept != stack:
            state["interrupted_stack"] = kept
            self.save_state(state)
            if removed:
                self.append_event("interrupted_stack_pruned", {"removed": removed, "kept": kept})
        return {"ok": True, "kept": kept, "removed": removed}

    def pop_interrupted(self) -> TaskGroup | None:
        self.prune_interrupted_stack()
        state = self.load_state()
        stack = list(state.get("interrupted_stack", []))
        if not stack:
            return None
        task_group_id = stack.pop()
        state["interrupted_stack"] = stack
        self.save_state(state)
        return self.load_task_group(task_group_id)

    def remove_interrupted(self, task_group_id: str) -> TaskGroup | None:
        self.prune_interrupted_stack()
        task_group_id = str(task_group_id)
        state = self.load_state()
        stack = [str(item) for item in state.get("interrupted_stack", [])]
        if task_group_id not in stack:
            return None
        stack = [item for item in stack if item != task_group_id]
        state["interrupted_stack"] = stack
        self.save_state(state)
        return self.load_task_group(task_group_id)

    def peek_interrupted(self) -> TaskGroup | None:
        self.prune_interrupted_stack()
        state = self.load_state()
        stack = list(state.get("interrupted_stack", []))
        if not stack:
            return None
        return self.load_task_group(stack[-1])

    def write_history(self, task_group: TaskGroup) -> str:
        history_id = f"history_{int(time.time() * 1000)}_{task_group.task_group_id}"
        path = self.root / "history" / f"{history_id}.json"
        record = {
            "history_id": history_id,
            "saved_at": time.time(),
            "task_group": to_dict(task_group),
        }
        self._write_json(path, record)
        self._trim_history()
        return history_id

    def recent_history(self, limit: int = 8) -> list[dict[str, Any]]:
        files = sorted((self.root / "history").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        records = []
        for path in files[:limit]:
            try:
                records.append(self._read_json(path))
            except Exception:
                continue
        return records

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        path = self.root / "events" / "events.jsonl"
        record = {"timestamp": time.time(), "event_type": event_type, "payload": payload}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_dict(record), ensure_ascii=False, sort_keys=True) + "\n")

    def _trim_history(self) -> None:
        keep = int(self.config.get("execution", {}).get("history_keep_latest", 200))
        files = sorted((self.root / "history").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[keep:]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _locked_store_method(method):
    @wraps(method)
    def wrapper(self: JsonStore, *args: Any, **kwargs: Any):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


for _method_name in (
    "load_state",
    "save_state",
    "save_session",
    "load_session",
    "save_task_group",
    "load_task_group",
    "enqueue_task_group",
    "_reset_queued_task_groups_to_new",
    "_repair_orphan_waiting_task_groups",
    "pop_next_task_group",
    "pop_task_group",
    "repair_runtime_state",
    "validate_runtime_state",
    "clear_task_queue",
    "set_active",
    "clear_runtime_interrupt_state",
    "clear_waiting_followups",
    "push_interrupted",
    "prune_interrupted_stack",
    "pop_interrupted",
    "remove_interrupted",
    "peek_interrupted",
    "write_history",
    "recent_history",
    "append_event",
    "_trim_history",
):
    setattr(JsonStore, _method_name, _locked_store_method(getattr(JsonStore, _method_name)))
