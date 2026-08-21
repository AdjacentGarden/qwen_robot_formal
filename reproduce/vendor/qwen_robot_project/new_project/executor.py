from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import threading
import time
import concurrent.futures
from pathlib import Path
from typing import Any

from .models import TaskGroup, TaskStatus, TaskStep
from .resources import ResourceManager
from .robot_state import RobotStateCollector
from .skill_registry import SkillRegistry
from .speech import SkillSpeechRouter, SpeechEvent
from .speech_policy import SpeechPolicy


START_GATED_SKILLS = {
    "squat",
    "push_up",
    "pull_up",
    "person_tracking",
    "pet_tracking",
}
FITNESS_SKILLS = {"squat", "push_up", "pull_up"}
ROS_RETRY_DEFAULT_SKILLS = {
    "head_control",
    "move_forward",
    "move_backward",
    "move_left",
    "move_right",
    "navigation_goto",
    "projector_control",
}
ROS_RETRY_NONRETRYABLE_ERRORS = {
    "unknown_point",
    "unknown skill",
    "disabled skill",
    "invalid_arguments",
}


class SkillExecutor:
    def __init__(
        self,
        config: dict[str, Any],
        registry: SkillRegistry,
        resources: ResourceManager | None = None,
        speech_callback: Any | None = None,
        task_update_callback: Any | None = None,
    ):
        self.config = config
        self.registry = registry
        self.resources = resources or ResourceManager(config)
        self.current_process: subprocess.Popen[str] | None = None
        self.current_step: TaskStep | None = None
        self._running_processes: dict[int, subprocess.Popen[str]] = {}
        self._running_steps: dict[str, TaskStep] = {}
        self.speech_callback = speech_callback
        self.task_update_callback = task_update_callback
        self.speech_router = SkillSpeechRouter(config)
        self.speech_policy = SpeechPolicy(config)
        self.robot_state = RobotStateCollector(config)
        self._lock = threading.RLock()
        self._interrupt_requested = False
        self._current_speech_events: list[dict[str, Any]] = []
        self._current_last_progress: dict[str, Any] | None = None

    def _notify_task_group_update(self, task_group: TaskGroup, reason: str, step: TaskStep | None = None) -> None:
        if not self.task_update_callback:
            return
        try:
            self.task_update_callback(task_group, step=step, reason=reason)
        except TypeError:
            self.task_update_callback(task_group)
        except Exception:
            pass

    def execute_task_group(self, task_group: TaskGroup, dry_run: bool = False) -> TaskGroup:
        if self.config.get("execution", {}).get("parallel_steps_enabled", False):
            return self.execute_task_group_parallel(task_group, dry_run=dry_run)
        with self._lock:
            self._interrupt_requested = False
            self._current_speech_events = []
            self._current_last_progress = None
            self._running_processes = {}
            self._running_steps = {}
        task_group.status = TaskStatus.RUNNING.value
        task_group.started_at = task_group.started_at or time.time()
        self._notify_task_group_update(task_group, "task_group_started")
        for step in task_group.steps:
            if step.status in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
                continue
            if self._is_interrupt_requested():
                step.status = TaskStatus.INTERRUPTED.value
                step.error = "interrupted"
                result = {"ok": False, "interrupted": True, "error": "interrupted", "last_progress": self.current_snapshot().get("last_progress")}
                step.result = result
                task_group.status = TaskStatus.INTERRUPTED.value
                task_group.ended_at = time.time()
                task_group.result_summary = "interrupted"
                task_group.resume_context = self._build_resume_context(task_group, step, result)
                self._notify_task_group_update(task_group, "task_group_interrupted_before_step", step)
                return task_group
            result = self.execute_step(step, dry_run=dry_run)
            step.result = result
            if result.get("interrupted"):
                step.status = TaskStatus.INTERRUPTED.value
                step.error = result.get("error", "interrupted")
                task_group.status = TaskStatus.INTERRUPTED.value
                task_group.ended_at = time.time()
                task_group.result_summary = "interrupted"
                task_group.resume_context = self._build_resume_context(task_group, step, result)
                self._notify_task_group_update(task_group, "task_group_interrupted", step)
                return task_group
            if result.get("ok"):
                step.status = TaskStatus.COMPLETED.value
                if not dry_run:
                    self.robot_state.record_step_effect(step, result)
                self._notify_task_group_update(task_group, "step_completed", step)
                environment_ask = self._environment_override_prompt(task_group, step, result)
                if environment_ask:
                    task_group.status = TaskStatus.NEEDS_INFO.value
                    task_group.ended_at = None
                    task_group.result_summary = "needs_environment_override"
                    task_group.metadata["waiting_environment_override"] = environment_ask
                    task_group.metadata["runtime_ask_user"] = environment_ask["ask_user"]
                    ask_user = environment_ask["ask_user"]
                    task_group.followups.append(
                        {
                            "question": ask_user.get("question"),
                            "task_title": ask_user.get("task_title"),
                            "missing_slots": ask_user.get("missing_slots", []),
                            "optional_slots": ask_user.get("optional_slots", []),
                            "candidate_skills": ask_user.get("candidate_skills", []),
                            "timestamp": time.time(),
                            "runtime_followup": "environment_override",
                        }
                    )
                    self._notify_task_group_update(task_group, "task_group_needs_info", step)
                    return task_group
            else:
                step.status = TaskStatus.FAILED.value
                step.error = result.get("error", "unknown error")
                task_group.status = TaskStatus.FAILED.value
                task_group.ended_at = time.time()
                task_group.result_summary = step.error or ""
                self._run_completion_finalizers(task_group, dry_run=dry_run)
                self._notify_task_group_update(task_group, "task_group_failed", step)
                return task_group
        task_group.status = TaskStatus.COMPLETED.value
        task_group.ended_at = time.time()
        task_group.result_summary = "completed"
        self._run_completion_finalizers(task_group, dry_run=dry_run)
        self._notify_task_group_update(task_group, "task_group_completed")
        return task_group

    def execute_task_group_parallel(self, task_group: TaskGroup, dry_run: bool = False) -> TaskGroup:
        with self._lock:
            self._interrupt_requested = False
            self._current_speech_events = []
            self._current_last_progress = None
            self._running_processes = {}
            self._running_steps = {}
        task_group.status = TaskStatus.RUNNING.value
        task_group.started_at = task_group.started_at or time.time()
        self._notify_task_group_update(task_group, "task_group_started")

        max_workers = max(1, int(self.config.get("execution", {}).get("parallel_max_workers", 3)))
        while True:
            if self._is_interrupt_requested():
                step = self._first_parallel_pending_step(task_group)
                if step is not None:
                    step.status = TaskStatus.INTERRUPTED.value
                    step.error = "interrupted"
                    result = {"ok": False, "interrupted": True, "error": "interrupted", "last_progress": self.current_snapshot().get("last_progress")}
                    step.result = result
                    task_group.status = TaskStatus.INTERRUPTED.value
                    task_group.ended_at = time.time()
                    task_group.result_summary = "interrupted"
                    task_group.resume_context = self._build_resume_context(task_group, step, result)
                    self._notify_task_group_update(task_group, "task_group_interrupted_before_step", step)
                    return task_group
            ready = self._next_parallel_batch(task_group, max_workers=max_workers)
            if not ready:
                break
            if len(ready) == 1:
                step_results = [(ready[0], self.execute_step(ready[0], dry_run=dry_run))]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(ready)) as pool:
                    futures = {pool.submit(self.execute_step, step, dry_run): step for step in ready}
                    step_results = []
                    for future in concurrent.futures.as_completed(futures):
                        step = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {"ok": False, "error": str(exc)}
                        step_results.append((step, result))
            step_results.sort(key=lambda item: item[0].order)
            for step, result in step_results:
                step.result = result
                if result.get("interrupted"):
                    step.status = TaskStatus.INTERRUPTED.value
                    step.error = result.get("error", "interrupted")
                    task_group.status = TaskStatus.INTERRUPTED.value
                    task_group.ended_at = time.time()
                    task_group.result_summary = "interrupted"
                    task_group.resume_context = self._build_resume_context(task_group, step, result)
                    self._notify_task_group_update(task_group, "task_group_interrupted", step)
                    return task_group
                if result.get("ok"):
                    step.status = TaskStatus.COMPLETED.value
                    if not dry_run:
                        self.robot_state.record_step_effect(step, result)
                    self._notify_task_group_update(task_group, "step_completed", step)
                    environment_ask = self._environment_override_prompt(task_group, step, result)
                    if environment_ask:
                        task_group.status = TaskStatus.NEEDS_INFO.value
                        task_group.ended_at = None
                        task_group.result_summary = "needs_environment_override"
                        task_group.metadata["waiting_environment_override"] = environment_ask
                        task_group.metadata["runtime_ask_user"] = environment_ask["ask_user"]
                        ask_user = environment_ask["ask_user"]
                        task_group.followups.append(
                            {
                                "question": ask_user.get("question"),
                                "task_title": ask_user.get("task_title"),
                                "missing_slots": ask_user.get("missing_slots", []),
                                "optional_slots": ask_user.get("optional_slots", []),
                                "candidate_skills": ask_user.get("candidate_skills", []),
                                "timestamp": time.time(),
                                "runtime_followup": "environment_override",
                            }
                        )
                        self._notify_task_group_update(task_group, "task_group_needs_info", step)
                        return task_group
                else:
                    step.status = TaskStatus.FAILED.value
                    step.error = result.get("error", "unknown error")
                    task_group.status = TaskStatus.FAILED.value
                    task_group.ended_at = time.time()
                    task_group.result_summary = step.error or ""
                    self._run_completion_finalizers(task_group, dry_run=dry_run)
                    self._notify_task_group_update(task_group, "task_group_failed", step)
                    return task_group

        task_group.status = TaskStatus.COMPLETED.value
        task_group.ended_at = time.time()
        task_group.result_summary = "completed"
        self._run_completion_finalizers(task_group, dry_run=dry_run)
        self._notify_task_group_update(task_group, "task_group_completed")
        return task_group

    def _first_parallel_pending_step(self, task_group: TaskGroup) -> TaskStep | None:
        for step in sorted(task_group.steps, key=lambda item: item.order):
            if step.status not in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
                return step
        return None

    def _next_parallel_batch(self, task_group: TaskGroup, max_workers: int) -> list[TaskStep]:
        pending = [
            step
            for step in sorted(task_group.steps, key=lambda item: item.order)
            if step.status not in {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.RUNNING.value}
        ]
        if not pending:
            return []
        first = pending[0]
        first_group = self._parallel_group(first)
        if not first_group:
            return [first]
        ready: list[TaskStep] = []
        used: set[str] = set()
        for step in pending:
            if len(ready) >= max_workers:
                break
            if self._parallel_group(step) != first_group:
                break
            if not self._parallel_dependencies_met(step, task_group):
                break
            resources = set(self.resources.resources_for_skill(step.skill_name, step.arguments))
            if used & resources:
                continue
            if not self._step_allows_parallel(step):
                if ready:
                    break
                return [step]
            step.resources = sorted(resources)
            ready.append(step)
            used.update(resources)
        return ready or [first]

    def _parallel_group(self, step: TaskStep) -> str:
        meta = (step.arguments or {}).get("_scheduler")
        if isinstance(meta, dict):
            return str(meta.get("parallel_group") or "")
        return str((step.arguments or {}).get("parallel_group") or "")

    def _parallel_dependencies_met(self, step: TaskStep, task_group: TaskGroup) -> bool:
        meta = (step.arguments or {}).get("_scheduler")
        depends = []
        if isinstance(meta, dict):
            depends.extend(meta.get("depends_on") or [])
        depends.extend((step.arguments or {}).get("depends_on") or [])
        if not depends:
            return True
        completed_ids = {item.step_id for item in task_group.steps if item.status == TaskStatus.COMPLETED.value}
        completed_skills = {item.skill_name for item in task_group.steps if item.status == TaskStatus.COMPLETED.value}
        return all(str(item) in completed_ids or str(item) in completed_skills for item in depends)

    def _step_allows_parallel(self, step: TaskStep) -> bool:
        meta = (step.arguments or {}).get("_scheduler")
        if isinstance(meta, dict) and meta.get("can_parallel") is False:
            return False
        resources = set(self.resources.resources_for_skill(step.skill_name, step.arguments))
        exclusive = {"base", "navigation", "front_camera", "back_camera", "camera", "npu", "mic", "speaker"}
        if resources & exclusive:
            return False
        return True

    def execute_step(self, step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        ok, reason = self.registry.validate_step(step.skill_name)
        if not ok:
            return {"ok": False, "error": reason}

        self._normalize_step_arguments(step)
        step.resources = self.resources.resources_for_skill(step.skill_name, step.arguments)
        step.status = TaskStatus.RUNNING.value
        step.started_at = time.time()
        with self._lock:
            self.current_step = step
            self._running_steps[step.step_id] = step
            self._current_speech_events = []
            self._current_last_progress = None
        try:
            if dry_run:
                return {"ok": True, "dry_run": True, "skill_name": step.skill_name, "arguments": step.arguments, "resources": step.resources}
            if self._is_interrupt_requested():
                return {"ok": False, "interrupted": True, "error": "interrupted", "last_progress": self.current_snapshot().get("last_progress")}
            if step.skill_name == "pet_tracking" and str((step.arguments or {}).get("action") or "").strip().lower() in {"find_route", "find_route_and_track"}:
                return self._execute_pet_find_route(step, dry_run=dry_run)
            with self.resources.acquire(step.resources):
                if self._is_interrupt_requested():
                    return {"ok": False, "interrupted": True, "error": "interrupted", "last_progress": self.current_snapshot().get("last_progress")}
                if step.skill_name in {"front_camera_capture", "back_camera_capture", "camera_capture"}:
                    path = self._camera_capture(step.skill_name, step.arguments)
                    if self._is_interrupt_requested():
                        return {"ok": False, "interrupted": True, "error": "interrupted", "output_path": str(path), "last_progress": self.current_snapshot().get("last_progress")}
                    return {"ok": True, "output_path": str(path), "speech_events": []}
                if step.skill_name in {"front_camera_record", "back_camera_record", "camera_record"}:
                    path = self._camera_record(step.skill_name, step.arguments)
                    if self._is_interrupt_requested():
                        return {"ok": False, "interrupted": True, "error": "interrupted", "output_path": str(path), "last_progress": self.current_snapshot().get("last_progress")}
                    return {"ok": True, "output_path": str(path), "speech_events": []}
                return self._run_single_function_step(step)
        except Exception as exc:
            interrupted = self._is_interrupt_requested()
            return {"ok": False, "interrupted": interrupted, "error": "interrupted" if interrupted else str(exc), "last_progress": self.current_snapshot().get("last_progress")}
        finally:
            step.ended_at = time.time()
            with self._lock:
                self._running_steps.pop(step.step_id, None)
                self.current_step = None
                if not self._running_processes:
                    self.current_process = None

    def execute_maintenance_step(self, step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        with self._lock:
            self._interrupt_requested = False
            self._current_speech_events = []
            self._current_last_progress = None
            self._running_processes = {}
            self._running_steps = {}
        return self.execute_step(step, dry_run=dry_run)

    def _normalize_step_arguments(self, step: TaskStep) -> None:
        execution_cfg = self.config.get("execution", {})
        if step.skill_name == "head_control":
            step.arguments = dict(step.arguments or {})
            step.arguments.setdefault("wait", execution_cfg.get("head_control_wait_seconds", 0.8))
            step.arguments.setdefault(
                "discovery_timeout",
                execution_cfg.get("head_control_discovery_timeout_seconds", 0.8),
            )
        if step.skill_name in {"move_forward", "move_backward", "move_left", "move_right"}:
            step.arguments = dict(step.arguments or {})
            step.arguments.setdefault(
                "discovery_timeout",
                execution_cfg.get("movement_discovery_timeout_seconds", 0.4),
            )
        if step.skill_name in FITNESS_SKILLS:
            back_camera = self.config.get("cameras", {}).get("back", {}).get("device")
            if back_camera:
                step.arguments = dict(step.arguments or {})
                step.arguments["camera"] = back_camera
        if step.skill_name in {"person_tracking", "pet_tracking"}:
            step.arguments = dict(step.arguments or {})
            tuning = self.config.get("execution", {}).get(step.skill_name, {})
            if isinstance(tuning, dict):
                for key in (
                    "base_speed",
                    "max_linear",
                    "max_angular",
                    "steering_gain",
                    "speed_ema_alpha",
                    "max_speed_step",
                    "search_spin_speed",
                ):
                    if tuning.get(key) is not None:
                        if step.skill_name == "pet_tracking" and key == "base_speed":
                            # Robot-level motion tuning is authoritative. Do not
                            # let an LLM-produced argument silently restore an
                            # obsolete pet tracking speed.
                            step.arguments[key] = tuning[key]
                        else:
                            step.arguments.setdefault(key, tuning[key])
        if step.skill_name == "projector_control":
            step.arguments = dict(step.arguments or {})
            action = str(step.arguments.get("action") or "").strip().lower()
            if action == "meeting_presentation_on":
                step.arguments.setdefault("hold", True)

    def finalize_cancelled_task_group(self, task_group: TaskGroup, dry_run: bool = False) -> list[dict[str, Any]]:
        return self._run_completion_finalizers(task_group, dry_run=dry_run)

    def _run_completion_finalizers(self, task_group: TaskGroup, dry_run: bool = False) -> list[dict[str, Any]]:
        finalizers: list[dict[str, Any]] = []
        finalizer_steps = self._completion_finalizer_steps(task_group)
        failed_finalizers: list[dict[str, Any]] = []
        step_results: dict[str, dict[str, Any]] = {}
        if len(finalizer_steps) > 1 and "execute_maintenance_step" not in vars(self):
            def run_finalizer(step: TaskStep) -> dict[str, Any]:
                executor = SkillExecutor(self.config, self.registry, self.resources)
                return executor.execute_maintenance_step(step, dry_run=dry_run)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(finalizer_steps)) as pool:
                futures = {pool.submit(run_finalizer, step): step for step in finalizer_steps}
                for future in concurrent.futures.as_completed(futures):
                    step = futures[future]
                    try:
                        step_results[step.step_id] = future.result()
                    except Exception as exc:
                        step_results[step.step_id] = {"ok": False, "error": str(exc)}
        else:
            for step in finalizer_steps:
                step_results[step.step_id] = self.execute_maintenance_step(step, dry_run=dry_run)

        for index, step in enumerate(finalizer_steps):
            step.order = len(task_group.steps) + index
            step.resources = self.resources.resources_for_skill(step.skill_name, step.arguments)
            result = step_results[step.step_id]
            step.result = result
            step.status = TaskStatus.COMPLETED.value if result.get("ok") else TaskStatus.FAILED.value
            step.error = "" if result.get("ok") else result.get("error", f"{step.skill_name} finalizer failed")
            if result.get("ok") and not dry_run:
                self.robot_state.record_step_effect(step, result)
            payload = {
                "skill_name": step.skill_name,
                "arguments": step.arguments,
                "status": step.status,
                "error": step.error,
                "result": result,
            }
            finalizers.append(payload)
            task_group.metadata.setdefault("finalizers", []).append(payload)
            if not result.get("ok"):
                task_group.metadata["finalizer_error"] = step.error
                failed_finalizers.append(payload)
        if failed_finalizers:
            task_group.metadata["cleanup_failed"] = True
            task_group.metadata["cleanup_failed_finalizers"] = failed_finalizers
        elif finalizer_steps:
            task_group.metadata.pop("cleanup_failed", None)
            task_group.metadata.pop("cleanup_failed_finalizers", None)
            task_group.metadata.pop("finalizer_error", None)
        return finalizers

    def _completion_finalizer_steps(self, task_group: TaskGroup) -> list[TaskStep]:
        steps: list[TaskStep] = []
        if self._needs_projector_off_finalizer(task_group):
            steps.append(
                TaskStep(
                    skill_name="projector_control",
                    arguments={"action": "off"},
                    reason="task group finalizer: turn off task-owned projector",
                )
            )
        if self._needs_head_level_finalizer(task_group):
            steps.append(
                TaskStep(
                    skill_name="head_control",
                    arguments={"action": "level"},
                    reason="task group finalizer: restore head level",
                )
            )
        return steps

    def _needs_head_level_finalizer(self, task_group: TaskGroup) -> bool:
        for step in task_group.steps:
            if step.skill_name != "head_control" or step.status != TaskStatus.COMPLETED.value:
                continue
            action = str((step.arguments or {}).get("action") or "").strip().lower()
            if action and action not in {"level", "horizontal", "center", "neutral", "stop"}:
                return True
        return False

    def _needs_projector_off_finalizer(self, task_group: TaskGroup) -> bool:
        if not self._task_owns_projector(task_group):
            return False
        last_action = self._last_completed_projector_action(task_group)
        if self._is_projector_off_action(last_action):
            return False
        return True

    def _task_owns_projector(self, task_group: TaskGroup) -> bool:
        if not self._is_fitness_task_group(task_group):
            return False
        if bool((task_group.slots or {}).get("projector")):
            return True
        for step in task_group.steps:
            if step.skill_name != "projector_control":
                continue
            action = str((step.arguments or {}).get("action") or "").strip().lower()
            if self._is_projector_on_action(action):
                return True
            parsed = self._parse_json_from_stdout(str((step.result or {}).get("stdout") or "")) if isinstance(step.result, dict) else None
            if isinstance(parsed, dict) and self._is_projector_on_action(str(parsed.get("action") or "")):
                return True
        return False

    @staticmethod
    def _is_fitness_task_group(task_group: TaskGroup) -> bool:
        if any(step.skill_name in FITNESS_SKILLS for step in task_group.steps):
            return True
        exercise = str((task_group.slots or {}).get("exercise_type") or "").strip()
        return exercise in FITNESS_SKILLS

    @staticmethod
    def _is_projector_on_action(action: Any) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in {"on", "internal_on", "fitness_video_on", "meeting_presentation_on", "external_video_on", "checkerboard", "pattern", "open", "start", "enable"}

    @staticmethod
    def _is_projector_off_action(action: Any) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in {"off", "close", "stop", "disable", "disabled", "external_off"}

    def _cached_projector_action(self) -> str | None:
        try:
            snapshot = self.robot_state.snapshot(fast=True)
        except Exception:
            return None
        projector = None
        peripherals = snapshot.get("peripherals")
        if isinstance(peripherals, dict):
            projector = peripherals.get("projector")
        if not isinstance(projector, dict):
            cache = snapshot.get("cache") if isinstance(snapshot.get("cache"), dict) else {}
            peripherals = cache.get("peripherals") if isinstance(cache.get("peripherals"), dict) else {}
            projector = peripherals.get("projector") if isinstance(peripherals, dict) else None
        if isinstance(projector, dict):
            return str(projector.get("action") or projector.get("state") or projector.get("status") or "")
        return None

    def _last_completed_projector_action(self, task_group: TaskGroup) -> str | None:
        candidates: list[tuple[int, str]] = []
        for step in task_group.steps:
            if step.skill_name != "projector_control" or step.status != TaskStatus.COMPLETED.value:
                continue
            action = str((step.arguments or {}).get("action") or "").strip().lower()
            if not action:
                parsed = self._parse_json_from_stdout(str((step.result or {}).get("stdout") or ""))
                if isinstance(parsed, dict):
                    action = str(parsed.get("action") or "").strip().lower()
            candidates.append((int(step.order or 0), action))
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]

    def _environment_override_prompt(self, task_group: TaskGroup, step: TaskStep, result: dict[str, Any]) -> dict[str, Any] | None:
        if step.skill_name != "environment_perception":
            return None
        if task_group.metadata.get("environment_override_accepted"):
            return None
        arguments = step.arguments or {}
        purpose = str(arguments.get("purpose") or "").strip().lower()
        if purpose not in {"fitness", "fitness_projection", "projection"}:
            return None
        parsed = result.get("parsed_json")
        if not isinstance(parsed, dict):
            return None
        payload = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed
        if not isinstance(payload, dict):
            return None

        blockers: list[str] = []
        exercise_type = str(arguments.get("exercise_type") or task_group.slots.get("exercise_type") or "").strip()
        exercise_payload = payload.get("exercise_suitability") if isinstance(payload.get("exercise_suitability"), dict) else {}
        fitness_space = payload.get("fitness_space") if isinstance(payload.get("fitness_space"), dict) else {}
        if purpose in {"fitness", "fitness_projection"}:
            exercise_ok: bool | None = None
            if exercise_type and isinstance(exercise_payload.get(exercise_type), dict):
                exercise_ok = bool(exercise_payload[exercise_type].get("ok"))
                blockers.extend(str(item) for item in (exercise_payload[exercise_type].get("blockers") or []) if item)
            elif "ok" in fitness_space:
                exercise_ok = bool(fitness_space.get("ok"))
            if exercise_ok is False:
                blockers.append("fitness_not_suitable")

        projection_payload = payload.get("projection_suitability") if isinstance(payload.get("projection_suitability"), dict) else {}
        needs_projection = purpose in {"projection", "fitness_projection"} and self._task_group_has_pending_projector(task_group, after_order=step.order)
        if needs_projection and projection_payload.get("ok") is False:
            blockers.extend(str(item) for item in (projection_payload.get("blockers") or []) if item)
            blockers.append("projection_not_suitable")

        if not blockers:
            return None

        question = self.speech_policy.environment_override_question(task_group, blockers)
        candidate_skills = ["environment_perception", "navigation_goto", "projector_control"]
        if purpose in {"fitness", "fitness_projection"}:
            candidate_skills.extend(["squat", "push_up", "pull_up"])
        ask_user = {
            "task_title": task_group.title,
            "question": question,
            "missing_slots": ["environment_override"],
            "optional_slots": ["where", "projector_control"],
            "candidate_skills": candidate_skills,
        }
        return {
            "step_id": step.step_id,
            "purpose": purpose,
            "exercise_type": exercise_type,
            "blockers": list(dict.fromkeys(blockers)),
            "environment_result": payload,
            "ask_user": ask_user,
        }

    def _task_group_has_pending_projector(self, task_group: TaskGroup, after_order: int | None = None) -> bool:
        for item in task_group.steps:
            if after_order is not None and item.order <= after_order:
                continue
            if item.skill_name == "projector_control" and item.status != TaskStatus.COMPLETED.value:
                return True
        return False

    def _execute_pet_find_route(self, step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"ok": True, "dry_run": True, "skill_name": step.skill_name, "arguments": step.arguments, "resources": step.resources}
        args = dict(step.arguments or {})
        pet = str(args.get("pet") or "all").strip().lower()
        if pet not in {"dog", "cat", "all"}:
            pet = "all"
        search_strategy = str(args.get("search_strategy") or "current_then_known_points").strip() or "current_then_known_points"
        requested_track_after_found = self._pet_track_after_found_enabled(args)
        track_after_found = True
        track_duration = self._pet_track_duration_seconds(args)
        find_timeout = self._pet_find_timeout_seconds(args)
        route_points = self._route_points_from_arguments(args)
        route_state: dict[str, Any] = {
            "pet": pet,
            "search_strategy": search_strategy,
            "per_point_find_timeout_sec": find_timeout,
            "per_point_track_duration_sec": track_duration,
            "track_after_found": track_after_found,
            "requested_track_after_found": requested_track_after_found,
            "track_duration_sec": track_duration,
            "visited_points": [],
            "current_point": None,
            "current_point_index": -1,
            "found": False,
            "found_at_point": None,
            "found_at_point_display_name": None,
            "last_pose": self.robot_state.snapshot(fast=True).get("pose"),
            "last_search_result": None,
            "tracking_started": False,
            "tracking_completed": False,
            "tracking_result": None,
            "video_path": None,
        }
        speech_events: list[dict[str, Any]] = []
        command_log: list[dict[str, Any]] = []

        current_result = self._pet_search_and_track_at_route_point(
            pet,
            point_name=None,
            search_timeout=find_timeout,
            track_duration=track_duration,
        )
        current_compact = self._compact_result(current_result)
        command_log.append({"point": None, "search_and_track_attempt": current_compact})
        route_state["last_search_result"] = current_compact
        self._update_pet_tracking_state_from_result(route_state, current_result)
        if current_result.get("interrupted") or self._is_interrupt_requested():
            return self._pet_route_result(False, True, route_state, speech_events, command_log, "interrupted")
        if self._pet_tracking_confirmed(current_result):
            route_state["tracking_started"] = True
            route_state["tracking_result"] = current_compact
            return self._pet_route_tracking_confirmed_result(
                step,
                pet,
                route_state,
                speech_events,
                command_log,
                found_point=None,
                found_display_name="current_location",
            )
        if not self._pet_tracking_attempt_can_continue(current_result):
            return self._pet_route_result(False, False, route_state, speech_events, command_log, self._result_error_summary(current_result))

        for index, point in enumerate(route_points):
            if self._is_interrupt_requested():
                return self._pet_route_result(False, True, route_state, speech_events, command_log, "interrupted")
            point_name = str(point.get("name") or "")
            if not point_name:
                continue
            point_display_name = str(point.get("display_name") or point_name)
            route_state["current_point"] = point_name
            route_state["current_point_display_name"] = point_display_name
            route_state["current_point_index"] = index
            route_state["visited_points"].append(point_name)
            nav_step = TaskStep(
                skill_name="navigation_goto",
                arguments={"action": "goto", "point": point_name},
                reason=f"pet find_route: navigate to {point_display_name}",
                resources=self.resources.resources_for_skill("navigation_goto", {"action": "goto", "point": point_name}),
            )
            nav_result = self.execute_step(nav_step, dry_run=False)
            nav_step.result = nav_result
            nav_step.status = TaskStatus.COMPLETED.value if nav_result.get("ok") else TaskStatus.FAILED.value
            if nav_result.get("ok"):
                self.robot_state.record_step_effect(nav_step, nav_result)
            command_item: dict[str, Any] = {"point": point_name, "display_name": point_display_name, "navigation": self._compact_result(nav_result)}
            if nav_result.get("interrupted") or self._is_interrupt_requested():
                command_log.append(command_item)
                return self._pet_route_result(False, True, route_state, speech_events, command_log, "interrupted")
            if not nav_result.get("ok"):
                command_log.append(command_item)
                continue

            search_result = self._pet_search_and_track_at_route_point(
                pet,
                point_name=point_name,
                search_timeout=find_timeout,
                track_duration=track_duration,
            )
            search_compact = self._compact_result(search_result)
            command_item["search_and_track_attempt"] = search_compact
            command_log.append(command_item)
            route_state["last_pose"] = self.robot_state.snapshot(fast=True).get("pose")
            route_state["last_search_result"] = search_compact
            self._update_pet_tracking_state_from_result(route_state, search_result)
            if search_result.get("interrupted") or self._is_interrupt_requested():
                return self._pet_route_result(False, True, route_state, speech_events, command_log, "interrupted")
            if self._pet_tracking_confirmed(search_result):
                route_state["tracking_started"] = True
                route_state["tracking_result"] = search_compact
                return self._pet_route_tracking_confirmed_result(
                    step,
                    pet,
                    route_state,
                    speech_events,
                    command_log,
                    found_point=point_name,
                    found_display_name=point_display_name,
                )
            if not self._pet_tracking_attempt_can_continue(search_result):
                return self._pet_route_result(False, False, route_state, speech_events, command_log, self._result_error_summary(search_result))

        message = self._pet_route_not_found_message(pet, command_log)
        event = SpeechEvent(skill_name="pet_tracking", text=message, source="new_project_find_route", kind="complete", payload=route_state)
        speech_events.append(event.as_dict())
        self._emit_speech_event(step, event)
        return self._pet_route_result(True, False, route_state, speech_events, command_log, "")

    def _pet_track_at_route_point(self, pet: str, point_name: str | None, duration: float) -> dict[str, Any]:
        child_args = {"action": "track", "pet": pet, "duration": duration, "json": True, "suppress_speech": True}
        child = TaskStep(
            skill_name="pet_tracking",
            arguments=child_args,
            reason=f"pet find_route: continuous search and track at {point_name or 'current_location'}",
            resources=self.resources.resources_for_skill("pet_tracking", child_args),
        )
        result = self.execute_step(child, dry_run=False)
        result = self._normalize_pet_track_result(result)
        child.result = result
        child.status = TaskStatus.COMPLETED.value if result.get("ok") else TaskStatus.FAILED.value
        if result.get("ok"):
            self.robot_state.record_step_effect(child, result)
        return result

    def _pet_search_and_track_at_route_point(
        self,
        pet: str,
        point_name: str | None,
        search_timeout: float,
        track_duration: float,
    ) -> dict[str, Any]:
        child_args: dict[str, Any] = {
            "action": "track",
            "pet": pet,
            "duration": track_duration,
            "search_timeout": search_timeout,
            "track_duration": track_duration,
            "json": True,
            "suppress_speech": True,
        }
        tuning = self.config.get("execution", {}).get("pet_tracking", {})
        if isinstance(tuning, dict):
            for key in (
                "base_speed",
                "max_linear",
                "max_angular",
                "steering_gain",
                "speed_ema_alpha",
                "max_speed_step",
                "search_spin_speed",
            ):
                if tuning.get(key) is not None:
                    child_args[key] = tuning[key]
        child = TaskStep(
            skill_name="pet_tracking",
            arguments=child_args,
            reason=f"pet find_route: search and track continuously at {point_name or 'current_location'}",
            resources=self.resources.resources_for_skill("pet_tracking", child_args),
        )
        result = self.execute_step(child, dry_run=False)
        result = self._normalize_pet_track_result(result)
        child.result = result
        child.status = TaskStatus.COMPLETED.value if result.get("ok") else TaskStatus.FAILED.value
        if result.get("ok"):
            self.robot_state.record_step_effect(child, result)
        return result

    def _pet_find_at_current_location(self, pet: str, point_name: str | None, timeout_sec: float) -> dict[str, Any]:
        child_args = {"action": "find", "pet": pet, "timeout": timeout_sec, "json": True}
        child = TaskStep(
            skill_name="pet_tracking",
            arguments=child_args,
            reason=f"pet find_route: search at {point_name or 'current_location'}",
            resources=self.resources.resources_for_skill("pet_tracking", child_args),
        )
        started_at = time.time()
        result = self.execute_step(child, dry_run=False)
        result = self._augment_pet_find_result_from_result_file(result, started_at)
        child.result = result
        child.status = TaskStatus.COMPLETED.value if result.get("ok") else TaskStatus.FAILED.value
        if result.get("ok"):
            self.robot_state.record_step_effect(child, result)
        return result

    def _pet_route_tracking_confirmed_result(
        self,
        step: TaskStep,
        pet: str,
        route_state: dict[str, Any],
        speech_events: list[dict[str, Any]],
        command_log: list[dict[str, Any]],
        found_point: str | None,
        found_display_name: str | None,
    ) -> dict[str, Any]:
        route_state["found"] = True
        route_state["found_at_point"] = found_point or "current_location"
        route_state["found_at_point_display_name"] = found_display_name or "current_location"
        route_state["last_pose"] = self.robot_state.snapshot(fast=True).get("pose")
        route_state["tracking_started"] = True
        route_state["tracking_completed"] = True
        self._append_pet_route_speech(step, speech_events, self._pet_tracking_completed_message(pet, route_state.get("video_path")), "complete", route_state)
        return self._pet_route_result(True, False, route_state, speech_events, command_log, "")

    def _normalize_pet_track_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("interrupted"):
            return result
        payload = result.get("parsed_json")
        if not isinstance(payload, dict) or str(payload.get("mode") or "") != "track":
            return result
        track_result = payload.get("track_result")
        if str(track_result or "").strip().lower() == "success":
            return result
        merged = dict(result)
        merged["ok"] = False
        merged["error"] = str(payload.get("error") or "pet_tracking_track_not_success")
        return merged

    def _pet_tracking_confirmed(self, result: dict[str, Any]) -> bool:
        if not result.get("ok"):
            return False
        payload = result.get("parsed_json")
        if not isinstance(payload, dict) or str(payload.get("mode") or "") != "track":
            return False
        return str(payload.get("track_result") or "").strip().lower() == "success"

    def _pet_find_confirmed(self, result: dict[str, Any]) -> bool:
        if not result.get("ok"):
            return False
        payload = result.get("parsed_json")
        if isinstance(payload, dict):
            if str(payload.get("mode") or "") == "find":
                return bool(payload.get("found"))
            find_result = payload.get("find_result")
            if isinstance(find_result, dict):
                return bool(find_result.get("found"))
        return False

    def _pet_find_attempt_can_continue(self, result: dict[str, Any]) -> bool:
        if result.get("interrupted"):
            return False
        payload = result.get("parsed_json")
        if isinstance(payload, dict) and str(payload.get("mode") or "") == "find":
            state = str(payload.get("state") or "").strip().lower()
            if state in {"not_found", "none", ""} and not payload.get("error"):
                return True
            find_result = payload.get("find_result")
            if isinstance(find_result, dict):
                state = str(find_result.get("state") or "").strip().lower()
                if state in {"not_found", "none", ""} and not find_result.get("error"):
                    return True
        return False

    def _pet_tracking_attempt_can_continue(self, result: dict[str, Any]) -> bool:
        if result.get("interrupted"):
            return False
        payload = result.get("parsed_json")
        if isinstance(payload, dict) and str(payload.get("mode") or "") == "track":
            track_result = str(payload.get("track_result") or "").strip().lower()
            if track_result in {"", "failure", "failed", "not_found", "none"}:
                return True
        return str(result.get("error") or "") == "pet_tracking_track_not_success"

    def _update_pet_tracking_state_from_result(self, route_state: dict[str, Any], track_result: dict[str, Any]) -> None:
        payload = track_result.get("parsed_json")
        if isinstance(payload, dict):
            if payload.get("result_path"):
                route_state["tracking_result_path"] = payload.get("result_path")
            track_text = str(payload.get("track_result") or "").strip().lower()
            if track_text == "success":
                video_path = self._pet_tracking_video_path()
                if video_path.exists():
                    route_state["video_path"] = str(video_path)
                route_state["tracking_completed"] = True

    def _pet_track_after_found_enabled(self, args: dict[str, Any] | None = None) -> bool:
        args = args or {}
        value = args.get("track_after_found")
        if value is None:
            value = args.get("follow_after_found")
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text not in {"0", "false", "no", "off", "disable", "disabled", "否", "不", "不要", "不用", "只看", "只检测"}

    def _pet_track_duration_seconds(self, args: dict[str, Any] | None = None) -> float:
        args = args or {}
        value = args.get("track_duration") or args.get("track_duration_seconds") or args.get("follow_duration")
        if value is None:
            value = self.config.get("execution", {}).get("pet_track_duration_seconds", 15)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 15.0

    def _pet_tracking_video_path(self) -> Path:
        return Path(self.config.get("paths", {}).get("single_function_dir", "/home/test/qwen_single_function")) / "pet_tracking" / "runtime" / "pet_tracking_record.mp4"

    def _append_pet_route_speech(
        self,
        step: TaskStep,
        speech_events: list[dict[str, Any]],
        text: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        event = SpeechEvent(skill_name="pet_tracking", text=text, source="new_project_find_route", kind=kind, payload=dict(payload))
        speech_events.append(event.as_dict())
        self._record_speech_event(event)
        self._emit_speech_event(step, event)

    def _pet_target_name(self, pet: str) -> str:
        return "小狗" if pet == "dog" else "小猫" if pet == "cat" else "宠物"

    def _pet_tracking_completed_message(self, pet: str, video_path: Any | None = None) -> str:
        if video_path:
            return f"{self._pet_target_name(pet)}跟随完成，视频已保存"
        return f"{self._pet_target_name(pet)}跟随完成"

    def _augment_pet_find_result_from_result_file(self, result: dict[str, Any], started_at: float) -> dict[str, Any]:
        if isinstance(result.get("parsed_json"), dict):
            return result
        result_path = Path(self.config.get("paths", {}).get("single_function_dir", "/home/test/qwen_single_function")) / "pet_tracking" / "runtime" / "pet_tracking_result.txt"
        try:
            stat = result_path.stat()
        except OSError:
            return result
        if stat.st_mtime < started_at - 2.0:
            return result
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return result
        if not isinstance(payload, dict) or str(payload.get("mode") or "") != "find":
            return result

        parsed_json: dict[str, Any] = {
            "ok": bool(payload.get("ok", True)),
            "skill": "pet_tracking",
            "mode": "find",
            "source": payload.get("source"),
            "pet": payload.get("pet"),
            "found": bool(payload.get("found", False)),
            "state": str(payload.get("state") or ""),
            "elapsed_sec": payload.get("elapsed_sec"),
            "timeout_sec": payload.get("timeout_sec"),
            "find_result": payload,
        }
        if payload.get("error"):
            parsed_json["error"] = payload.get("error")

        merged = dict(result)
        merged["parsed_json"] = parsed_json
        if payload.get("ok") is False:
            merged["ok"] = False
            merged["error"] = str(payload.get("error") or merged.get("error") or "pet_find_failed")
        return merged

    def _pet_route_not_found_message(self, pet: str, command_log: list[dict[str, Any]]) -> str:
        target = "小狗" if pet == "dog" else "小猫" if pet == "cat" else "宠物"
        routed_items = [item for item in command_log if item.get("point")]
        nav_failed = [
            item for item in routed_items
            if isinstance(item.get("navigation"), dict) and not item["navigation"].get("ok")
        ]
        if routed_items and len(nav_failed) == len(routed_items):
            return f"当前位置没有找到{target}，后续地点导航失败，暂时无法继续寻找"
        if nav_failed:
            return f"没有找到{target}，部分地点导航失败"
        return f"没有找到{target}"

    def _pet_find_timeout_seconds(self, args: dict[str, Any] | None = None) -> float:
        args = args or {}
        value = args.get("timeout") or args.get("duration")
        if value is None:
            value = self.config.get("execution", {}).get("pet_find_timeout_seconds", 15)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return 15.0

    def _route_points_from_arguments(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        strategy = str((args or {}).get("search_strategy") or "").strip().lower()
        if strategy in {"current_only", "current", "here_only", "当前位置", "只在当前位置"}:
            return []
        requested = args.get("points") or args.get("route_points")
        known = self._known_navigation_points()
        if not requested:
            return known
        if isinstance(requested, str):
            names = [item.strip() for item in requested.split(",") if item.strip()]
        elif isinstance(requested, list):
            names = [str(item).strip() for item in requested if str(item).strip()]
        else:
            names = []
        return [self._resolve_navigation_point(name, known) or {"name": name} for name in names]

    def _known_navigation_points(self) -> list[dict[str, Any]]:
        path = self.config.get("robot_state", {}).get("navigation_points_path")
        if not path:
            path = str(Path(self.config.get("paths", {}).get("single_function_dir", "/home/test/qwen_single_function")) / "points" / "named_points.json")
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return []
        points = payload.get("points") if isinstance(payload, dict) and isinstance(payload.get("points"), dict) else payload
        if not isinstance(points, dict):
            return []
        result: list[dict[str, Any]] = []
        for name, value in points.items():
            item = {"id": name, "name": name, "display_name": name, "aliases": []}
            if isinstance(value, dict):
                item.update(value)
            canonical = str(item.get("name") or item.get("id") or name).strip() or str(name)
            item["id"] = canonical
            item["name"] = canonical
            item.setdefault("display_name", canonical)
            aliases = item.get("aliases")
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                aliases = []
            normalized_aliases: list[str] = []
            for token in [name, item.get("display_name"), *aliases]:
                text = str(token or "").strip()
                if text and text != canonical and text not in normalized_aliases:
                    normalized_aliases.append(text)
            item["aliases"] = normalized_aliases
            result.append(item)
        return result

    def _navigation_point_tokens(self, point: dict[str, Any]) -> list[str]:
        tokens: list[str] = []
        for item in [point.get("id"), point.get("name"), point.get("display_name")]:
            text = str(item or "").strip()
            if text and text not in tokens:
                tokens.append(text)
        aliases = point.get("aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for item in aliases:
                text = str(item or "").strip()
                if text and text not in tokens:
                    tokens.append(text)
        return tokens

    def _resolve_navigation_point(self, value: Any, points: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        text = str(value or "").strip()
        if not text:
            return None
        points = points if points is not None else self._known_navigation_points()
        text_lower = text.lower()
        for point in points:
            for token in self._navigation_point_tokens(point):
                if text == token or text_lower == token.lower():
                    return point
        return None

    def _pet_result_found(self, result: dict[str, Any]) -> bool:
        payload = result.get("parsed_json")
        if isinstance(payload, dict):
            if payload.get("found") is True:
                return True
            if str(payload.get("state") or "").lower() in {"found", "found_tracking", "tracking"}:
                return True
        for event in result.get("speech_events") or []:
            if isinstance(event, dict):
                payload = event.get("payload")
                if isinstance(payload, dict) and payload.get("found") is True:
                    return True
        return False

    def _result_error_summary(self, result: dict[str, Any]) -> str:
        payload = result.get("parsed_json")
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload.get("error"))
        if result.get("error"):
            return str(result.get("error"))
        return "skill_failed"

    def _compact_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": result.get("ok"),
            "interrupted": result.get("interrupted", False),
            "error": result.get("error", ""),
            "parsed_json": result.get("parsed_json"),
            "last_progress": result.get("last_progress"),
        }

    def _pet_route_result(
        self,
        ok: bool,
        interrupted: bool,
        route_state: dict[str, Any],
        speech_events: list[dict[str, Any]],
        command_log: list[dict[str, Any]],
        error: str,
    ) -> dict[str, Any]:
        return {
            "ok": ok and not interrupted,
            "interrupted": interrupted,
            "error": error,
            "parsed_json": route_state,
            "speech_events": speech_events,
            "route_log": command_log,
            "last_progress": self.current_snapshot().get("last_progress"),
        }

    def interrupt_current(self) -> dict[str, Any]:
        requested = self.request_interrupt(signal_process=True)
        stop_result = self._try_stop_current_skill()
        processes = requested.get("processes") or []
        for process in processes:
            if not process or process.poll() is not None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_process_group(process, signal.SIGKILL)
        requested.pop("process", None)
        requested.pop("processes", None)
        requested["stop_result"] = stop_result
        return requested

    def request_interrupt(self, signal_process: bool = True) -> dict[str, Any]:
        with self._lock:
            self._interrupt_requested = True
            processes = list(self._running_processes.values())
            process = self.current_process
            step = self.current_step or next(iter(self._running_steps.values()), None)
            snapshot = self.current_snapshot()
        if signal_process:
            for item in processes or ([process] if process else []):
                if item and item.poll() is None:
                    self._kill_process_group(item, signal.SIGTERM)
        return {"ok": True, "interrupted": bool(step), "snapshot": snapshot, "process": process, "processes": processes}

    def _kill_process_group(self, process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except Exception:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    def current_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current_step": self.current_step.__dict__.copy() if self.current_step else None,
                "running_steps": [step.__dict__.copy() for step in self._running_steps.values()],
                "running_process_count": len(self._running_processes),
                "interrupt_requested": self._interrupt_requested,
                "last_progress": dict(self._current_last_progress) if isinstance(self._current_last_progress, dict) else None,
                "speech_events": list(self._current_speech_events[-20:]),
            }

    def _is_interrupt_requested(self) -> bool:
        with self._lock:
            return self._interrupt_requested

    def _record_speech_event(self, event: SpeechEvent) -> None:
        event_dict = event.as_dict()
        effect_step: TaskStep | None = None
        state_effect = event.payload.get("state_effect") if isinstance(event.payload, dict) else None
        with self._lock:
            self._current_speech_events.append(event_dict)
            if event.kind in {"ready", "start", "progress", "count", "status"}:
                self._current_last_progress = event_dict
            if isinstance(state_effect, dict):
                candidates = [step for step in self._running_steps.values() if step.skill_name == event.skill_name]
                if self.current_step is not None and self.current_step.skill_name == event.skill_name:
                    effect_step = self.current_step
                elif len(candidates) == 1:
                    effect_step = candidates[0]
        if effect_step is not None and isinstance(state_effect, dict):
            parsed = {"skill": effect_step.skill_name, **state_effect}
            self.robot_state.record_step_effect(
                effect_step,
                {"ok": True, "returncode": None, "parsed_json": parsed, "runtime_effect": True},
            )

    def _build_resume_context(self, task_group: TaskGroup, interrupted_step: TaskStep, result: dict[str, Any]) -> dict[str, Any]:
        completed_steps = [step.step_id for step in task_group.steps if step.status == TaskStatus.COMPLETED.value]
        pending_steps = [
            step.step_id
            for step in task_group.steps
            if step.step_id != interrupted_step.step_id and step.status != TaskStatus.COMPLETED.value
        ]
        spec = self.registry.get(interrupted_step.skill_name) or {}
        recovery = spec.get("recovery") if isinstance(spec.get("recovery"), dict) else {}
        return {
            "interrupted_at": time.time(),
            "active_step_id": interrupted_step.step_id,
            "active_skill": interrupted_step.skill_name,
            "completed_steps": completed_steps,
            "pending_steps": pending_steps,
            "last_progress": result.get("last_progress"),
            "speech_events": result.get("speech_events", [])[-20:],
            "robot_state_before_interrupt": self.robot_state.snapshot(active_step=interrupted_step, fast=True),
            "can_resume": True,
            "resume_strategy": self._resume_strategy_for_skill(interrupted_step.skill_name),
            "recovery": recovery,
            "scene_restore_requirements": recovery.get("scene_restore_requirements", []),
        }

    def _resume_strategy_for_skill(self, skill_name: str) -> str:
        spec = self.registry.get(skill_name) or {}
        recovery = spec.get("recovery") if isinstance(spec.get("recovery"), dict) else {}
        if recovery.get("resumable") is True:
            return str(recovery.get("resume_strategy") or "resume_with_arguments")
        if skill_name in {"squat", "push_up", "pull_up"}:
            return "resume_with_arguments"
        if skill_name in {"person_tracking", "pet_tracking", "navigation_goto", "face_registration"}:
            return "restart_with_context"
        return "restart_step"

    def _run_single_function_step(self, step: TaskStep) -> dict[str, Any]:
        timeout = self._timeout_for_step(step)
        max_attempts = self._ros_retry_attempts(step.skill_name)
        delay = self._ros_retry_delay_seconds()
        backoff = self._ros_retry_backoff()
        attempts_log: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None

        for attempt in range(1, max_attempts + 1):
            start_gate_path = self._prepare_start_gate(step)
            command = self._single_function_command(step.skill_name, step.arguments, start_gate_path=start_gate_path)
            completed, speech_events, command_timing = self._run_command(
                step,
                command,
                timeout=timeout,
                start_gate_path=start_gate_path,
            )
            result = self._single_function_result_from_completed(
                step,
                command,
                completed,
                speech_events,
                command_timing=command_timing,
            )
            retryable, retry_reason = self._should_retry_single_function_result(step.skill_name, result)
            if retry_reason:
                result["retry_reason"] = retry_reason
            attempts_log.append(self._retry_attempt_summary(attempt, result, retryable, retry_reason))

            if result.get("interrupted"):
                break
            if not retryable or attempt >= max_attempts:
                break
            sleep_for = max(0.0, delay * (backoff ** max(0, attempt - 1)))
            if sleep_for > 0:
                time.sleep(sleep_for)

        if result is None:
            return {"ok": False, "error": "single_function_not_executed", "attempts": attempts_log}
        if len(attempts_log) > 1 or (attempts_log and attempts_log[0].get("retryable")):
            result["attempts"] = attempts_log
        return result

    def _single_function_result_from_completed(
        self,
        step: TaskStep,
        command: list[str],
        completed: subprocess.CompletedProcess[str],
        speech_events: list[SpeechEvent],
        command_timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed_json = self._parse_json_from_stdout(completed.stdout or "")
        interrupted = self._is_interrupt_requested()
        if interrupted:
            parsed_json = self._interrupted_payload(step, parsed_json)
        payload_failed = isinstance(parsed_json, dict) and parsed_json.get("ok") is False
        ok = completed.returncode == 0 and not interrupted and not payload_failed
        parsed_error = ""
        if isinstance(parsed_json, dict) and parsed_json.get("error"):
            parsed_error = str(parsed_json.get("error"))
        if payload_failed and not parsed_error:
            nested = parsed_json.get("result") if isinstance(parsed_json.get("result"), dict) else {}
            parsed_error = str(
                parsed_json.get("message")
                or nested.get("error")
                or nested.get("reason")
                or parsed_json.get("status")
                or nested.get("status")
                or "single_function_reported_failure"
            )
        error = "interrupted" if interrupted else ("" if ok else (parsed_error or (completed.stdout or "")[-1000:]))
        result = {
            "ok": ok,
            "interrupted": interrupted,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:] if completed.stdout else "",
            "parsed_json": parsed_json,
            "speech_events": [event.as_dict() for event in speech_events],
            "last_progress": self.current_snapshot().get("last_progress"),
            "command": command,
            "error": error,
            "timing": dict(command_timing or {}),
        }
        soft_failure = self._soft_failure_reason(step.skill_name, parsed_json, completed.stdout or "")
        if soft_failure and not interrupted:
            result["ok"] = False
            result["soft_failure"] = soft_failure
            result["error"] = soft_failure
        return result

    def _ros_retry_config(self) -> dict[str, Any]:
        value = self.config.get("execution", {}).get("ros_retry", {})
        return value if isinstance(value, dict) else {}

    def _ros_retry_skills(self) -> set[str]:
        configured = self._ros_retry_config().get("skills")
        if isinstance(configured, list):
            return {str(item) for item in configured}
        return set(ROS_RETRY_DEFAULT_SKILLS)

    def _ros_retry_attempts(self, skill_name: str) -> int:
        if skill_name not in self._ros_retry_skills():
            return 1
        try:
            attempts = int(self._ros_retry_config().get("attempts", 3))
        except (TypeError, ValueError):
            attempts = 3
        return max(1, min(5, attempts))

    def _ros_retry_delay_seconds(self) -> float:
        try:
            return max(0.0, float(self._ros_retry_config().get("delay_seconds", 0.3)))
        except (TypeError, ValueError):
            return 0.3

    def _ros_retry_backoff(self) -> float:
        try:
            return max(1.0, float(self._ros_retry_config().get("backoff", 1.5)))
        except (TypeError, ValueError):
            return 1.5

    def _soft_failure_reason(self, skill_name: str, parsed_json: dict[str, Any] | None, stdout: str) -> str:
        payload = parsed_json if isinstance(parsed_json, dict) else {}
        if skill_name == "head_control" and payload.get("ok") is True:
            try:
                if int(payload.get("subscribers", 0)) <= 0:
                    return "head_control_subscribers_0"
            except (TypeError, ValueError):
                return "head_control_subscribers_unknown"
        if skill_name == "navigation_goto":
            error = str(payload.get("error") or "")
            if error == "action_server_unavailable" or "Action servers: 0" in stdout:
                return "navigation_action_server_unavailable"
        return ""

    def _should_retry_single_function_result(self, skill_name: str, result: dict[str, Any]) -> tuple[bool, str]:
        if skill_name not in self._ros_retry_skills():
            return False, ""
        if result.get("interrupted"):
            return False, ""
        soft_failure = str(result.get("soft_failure") or "")
        if soft_failure:
            return True, soft_failure
        if result.get("ok"):
            return False, ""
        payload = result.get("parsed_json")
        payload_error = ""
        if isinstance(payload, dict):
            payload_error = str(payload.get("error") or "")
        error_text = payload_error or str(result.get("error") or "")
        if any(marker in error_text for marker in ROS_RETRY_NONRETRYABLE_ERRORS):
            return False, error_text
        retry_markers = (
            "action_server_unavailable",
            "Action servers: 0",
            "subscribers: 0",
            "subscribers_0",
            "timeout",
            "temporarily unavailable",
            "open_and_lock_file failed",
            "RTPS_TRANSPORT_SHM",
            "DDS",
        )
        if any(marker in error_text for marker in retry_markers):
            return True, payload_error or "retryable_ros_error"
        if result.get("returncode") not in {None, 0}:
            return True, payload_error or f"returncode_{result.get('returncode')}"
        return False, ""

    def _retry_attempt_summary(self, attempt: int, result: dict[str, Any], retryable: bool, reason: str) -> dict[str, Any]:
        payload = result.get("parsed_json")
        summary: dict[str, Any] = {
            "attempt": attempt,
            "ok": bool(result.get("ok")),
            "retryable": bool(retryable),
            "reason": reason or "",
            "returncode": result.get("returncode"),
            "timing": dict(result.get("timing") or {}) if isinstance(result.get("timing"), dict) else {},
        }
        if isinstance(payload, dict):
            for key in ("skill", "action", "error", "status", "subscribers", "topic"):
                if key in payload:
                    summary[key] = payload.get(key)
        if not summary.get("error") and result.get("error"):
            summary["error"] = str(result.get("error"))[:500]
        return summary

    def _run_command(
        self,
        step: TaskStep,
        command: list[str],
        timeout: float | None,
        start_gate_path: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[SpeechEvent], dict[str, Any]]:
        command_started_at = time.monotonic()
        first_stdout_at: float | None = None
        first_speech_event_at: float | None = None
        complete_event_at: float | None = None
        speech_callback_seconds = 0.0
        speech_callback_count = 0
        stdout_line_count = 0
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("SINGLE_FUNCTION_JSON", "1")
        env.setdefault("SINGLE_FUNCTION_SPEECH_EVENTS", "1")
        gate_opened = False
        emitted_complete = False
        if self._is_interrupt_requested():
            return (
                subprocess.CompletedProcess(command, -15, stdout=""),
                [],
                {"total_ms": 0.0, "interrupted_before_spawn": True},
            )

        def handle_event(event: SpeechEvent) -> None:
            nonlocal gate_opened, emitted_complete, first_speech_event_at, complete_event_at
            nonlocal speech_callback_seconds, speech_callback_count
            event_received_at = time.monotonic()
            if first_speech_event_at is None:
                first_speech_event_at = event_received_at
            if event.kind == "complete" and complete_event_at is None:
                complete_event_at = event_received_at
            speech_events.append(event)
            if self._is_interrupt_requested() and event.kind == "complete":
                return
            if event.kind == "ready" and start_gate_path is not None:
                if not gate_opened:
                    self._record_speech_event(event)
                    callback_started_at = time.monotonic()
                    spoken = self._emit_speech_event(step, event)
                    speech_callback_seconds += time.monotonic() - callback_started_at
                    speech_callback_count += 1
                    if spoken is False:
                        print(f"speech_event_failed:{step.skill_name}:{event.kind}", flush=True)
                    self._open_start_gate(start_gate_path)
                    gate_opened = True
                return
            if event.kind == "complete":
                if emitted_complete:
                    return
                emitted_complete = True
            self._record_speech_event(event)
            callback_started_at = time.monotonic()
            self._emit_speech_event(step, event)
            speech_callback_seconds += time.monotonic() - callback_started_at
            speech_callback_count += 1

        process_spawn_started_at = time.monotonic()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            env=env,
            bufsize=1,
        )
        process_spawned_at = time.monotonic()
        with self._lock:
            self.current_process = process
            self._running_processes[process.pid] = process
        lines: list[str] = []
        speech_events: list[SpeechEvent] = []
        started = time.monotonic()
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        output_poll_seconds = max(
            0.01,
            float(self.config.get("execution", {}).get("process_output_poll_seconds", 0.05)),
        )
        try:
            while True:
                if self._is_interrupt_requested():
                    break
                if timeout is not None and time.monotonic() - started > timeout:
                    self.interrupt_current()
                    raise subprocess.TimeoutExpired(command, timeout)
                ready = selector.select(timeout=output_poll_seconds)
                for key, _mask in ready:
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    if first_stdout_at is None:
                        first_stdout_at = time.monotonic()
                    stdout_line_count += 1
                    lines.append(line)
                    event = self.speech_router.line_to_event(step.skill_name, line)
                    if event is not None:
                        handle_event(event)
                if process.poll() is not None:
                    break
            if self._is_interrupt_requested() and process.poll() is None:
                try:
                    grace = float(self.config.get("execution", {}).get("interrupt_process_grace_seconds", 0.5))
                    process.wait(timeout=max(0.1, grace))
                except subprocess.TimeoutExpired:
                    self._kill_process_group(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            if process.poll() is not None:
                for line in process.stdout:
                    if first_stdout_at is None:
                        first_stdout_at = time.monotonic()
                    stdout_line_count += 1
                    lines.append(line)
                    event = self.speech_router.line_to_event(step.skill_name, line)
                    if event is not None:
                        handle_event(event)
        finally:
            selector.close()
            with self._lock:
                self._running_processes.pop(process.pid, None)
                if self.current_process is process:
                    self.current_process = next(iter(self._running_processes.values()), None)
            if start_gate_path is not None:
                try:
                    start_gate_path.unlink(missing_ok=True)
                except Exception:
                    pass
        command_finished_at = time.monotonic()

        def offset_ms(value: float | None) -> float | None:
            if value is None:
                return None
            return round((value - command_started_at) * 1000.0, 3)

        command_timing = {
            "clock": "monotonic",
            "total_ms": offset_ms(command_finished_at),
            "process_spawn_ms": round((process_spawned_at - process_spawn_started_at) * 1000.0, 3),
            "process_spawned_offset_ms": offset_ms(process_spawned_at),
            "first_stdout_offset_ms": offset_ms(first_stdout_at),
            "first_speech_event_offset_ms": offset_ms(first_speech_event_at),
            "complete_event_offset_ms": offset_ms(complete_event_at),
            "speech_callback_total_ms": round(speech_callback_seconds * 1000.0, 3),
            "speech_callback_count": speech_callback_count,
            "stdout_line_count": stdout_line_count,
            "returncode": process.returncode,
        }
        parsed_payload = self._parse_json_from_stdout("".join(lines))
        if isinstance(parsed_payload, dict):
            metrics = parsed_payload.get("metrics")
            if isinstance(metrics, dict) and isinstance(metrics.get("elapsed_sec"), (int, float)):
                command_timing["skill_reported_elapsed_ms"] = round(float(metrics["elapsed_sec"]) * 1000.0, 3)
        return subprocess.CompletedProcess(command, process.returncode, stdout="".join(lines)), speech_events, command_timing

    def _emit_speech_event(self, step: TaskStep, event: SpeechEvent) -> bool:
        if self.speech_callback is None:
            return True
        if self._suppress_speech_for_step(step):
            return True
        try:
            result = self.speech_callback(step, event)
            return False if result is False else True
        except Exception as exc:
            print(f"speech_callback_failed:{exc}", flush=True)
            return False

    def _suppress_speech_for_step(self, step: TaskStep) -> bool:
        speech_cfg = self.config.get("speech", {})
        if speech_cfg.get("suppress_restore_step_events", True) and str(step.reason or "").startswith("restore interrupted task "):
            return True
        if str(step.reason or "").startswith("task group finalizer:"):
            return True
        if bool((step.arguments or {}).get("suppress_speech")):
            return True
        return False

    def _prepare_start_gate(self, step: TaskStep) -> Path | None:
        if not self._needs_start_gate(step):
            return None
        runtime_dir = Path(self.config.get("paths", {}).get("runtime_dir", "/tmp"))
        gate_dir = runtime_dir / "start_gates"
        gate_dir.mkdir(parents=True, exist_ok=True)
        gate_path = gate_dir / f"{step.step_id}.start"
        gate_path.unlink(missing_ok=True)
        return gate_path

    def _needs_start_gate(self, step: TaskStep) -> bool:
        if step.skill_name not in START_GATED_SKILLS:
            return False
        action = str((step.arguments or {}).get("action") or "").strip().lower()
        mode = str((step.arguments or {}).get("mode") or "").strip().lower()
        requested = mode or action
        if requested in {"query", "status", "stop", "cancel", "find"}:
            return False
        return True

    def _open_start_gate(self, gate_path: Path) -> None:
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(str(time.time()), encoding="utf-8")

    def _single_function_command(self, skill_name: str, arguments: dict[str, Any], start_gate_path: Path | None = None) -> list[str]:
        single_function_dir = Path(self.config["paths"]["single_function_dir"])
        actual_skill = "navigation_goto" if skill_name == "navigation_list" else skill_name
        run_sh = single_function_dir / actual_skill / "run.sh"
        args = self._arguments_to_cli(skill_name, arguments)
        if start_gate_path is not None:
            args.extend(["--start-gate", str(start_gate_path)])
        return ["bash", str(run_sh), *args]

    def _arguments_to_cli(self, skill_name: str, arguments: dict[str, Any]) -> list[str]:
        args = dict(arguments or {})
        args.pop("suppress_speech", None)
        args.pop("_scheduler", None)
        args.pop("parallel_group", None)
        args.pop("depends_on", None)
        if skill_name == "navigation_list":
            return ["list"]
        if skill_name in {
            "feeder_control",
            "head_control",
            "light_control",
            "person_tracking",
            "pet_tracking",
            "projector_control",
            "pull_up",
            "push_up",
            "squat",
        }:
            action = args.pop("action", None)
            cli = [str(action)] if action else []
            cli += self._options(args)
            if skill_name == "pet_tracking":
                cli.append("--json")
            return cli
        if skill_name == "navigation_goto":
            cli = ["--action", str(args.pop("action", "goto"))]
            point = (
                args.pop("point", None)
                or args.pop("point_or_coordinates", None)
                or args.pop("destination", None)
                or args.pop("name", None)
            )
            if point is not None:
                cli.extend(["--point", str(point)])
            return cli + self._options(args)
        if skill_name in {"reminder_schedule", "reminder_cancel"}:
            action = "schedule" if skill_name == "reminder_schedule" else "cancel"
            params = dict(args)
            return [action, "--json-params", json.dumps(params, ensure_ascii=False)]
        if skill_name == "reminder_query":
            return ["query"] + self._options(args)
        return self._options(args)

    def _options(self, arguments: dict[str, Any]) -> list[str]:
        cli: list[str] = []
        for key, value in arguments.items():
            if value is None:
                continue
            name = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    cli.append(name)
                continue
            cli.extend([name, str(value)])
        return cli

    def _timeout_for_step(self, step: TaskStep) -> float | None:
        action = str((step.arguments or {}).get("action") or "").strip()
        action_key = f"{step.skill_name}.{action}" if action else ""
        action_timeouts = self.config.get("execution", {}).get("action_timeout_seconds", {})
        if action_key and isinstance(action_timeouts, dict) and action_key in action_timeouts:
            return float(action_timeouts[action_key])
        long_tags = {"long_running", "navigation", "tracking"}
        spec = self.registry.get(step.skill_name) or {}
        tags = set(spec.get("domain_tags") or []) | set(spec.get("side_effects") or [])
        key = "long_running_timeout_seconds" if tags & long_tags else "default_timeout_seconds"
        return float(self.config.get("execution", {}).get(key, 120))

    def _parse_json_from_stdout(self, stdout: str) -> dict[str, Any] | None:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{") or not line.endswith("}"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def _interrupted_payload(self, step: TaskStep, parsed_json: dict[str, Any] | None) -> dict[str, Any]:
        progress = self.current_snapshot().get("last_progress") or {}
        payload = progress.get("payload") if isinstance(progress.get("payload"), dict) else {}
        data = dict(parsed_json or {})
        data.update(
            {
                "event": "skill_interrupted",
                "kind": "interrupted",
                "skill_name": step.skill_name,
                "interrupted": True,
            }
        )
        for key in ("current_count", "count", "session_count", "elapsed_seconds", "initial_count", "resume_from_interrupt"):
            if key in payload and key not in data:
                data[key] = payload[key]
        return data

    def _try_stop_current_skill(self) -> dict[str, Any]:
        with self._lock:
            steps = list(self._running_steps.values()) or ([self.current_step] if self.current_step else [])
        steps = [step for step in steps if step is not None]
        if not steps:
            return {"ok": True, "skipped": True}
        stop_capable = {
            "person_tracking",
            "pet_tracking",
            "pull_up",
            "push_up",
            "squat",
        }
        targets = []
        seen: set[str] = set()
        for step in steps:
            if step.skill_name in stop_capable and step.skill_name not in seen:
                targets.append(step.skill_name)
                seen.add(step.skill_name)
        if not targets:
            return {"ok": True, "skipped": True}
        results = []
        try:
            for skill_name in targets:
                command = self._single_function_command(skill_name, {"action": "stop"})
                completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                results.append({"skill_name": skill_name, "ok": completed.returncode == 0, "stdout": completed.stdout[-1000:] if completed.stdout else ""})
            return {"ok": all(item.get("ok") for item in results), "results": results}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "results": results}

    def _camera_name(self, skill_name: str, arguments: dict[str, Any]) -> str:
        if skill_name.startswith("back_"):
            return "back"
        if skill_name.startswith("front_"):
            return "front"
        return str(arguments.get("camera_name") or arguments.get("camera") or "front")

    def _camera_capture(self, skill_name: str, arguments: dict[str, Any]) -> Path:
        import cv2

        camera_name = self._camera_name(skill_name, arguments)
        cfg = self.config["cameras"][camera_name]
        path = Path(arguments.get("output_path") or cfg["image_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        cap = cv2.VideoCapture(arguments.get("device") or cfg["device"])
        if not cap.isOpened():
            raise RuntimeError(f"camera open failed: {camera_name}")
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(arguments.get("width") or cfg["width"]))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(arguments.get("height") or cfg["height"]))
            cap.set(cv2.CAP_PROP_FPS, float(arguments.get("fps") or cfg["fps"]))
            for _ in range(int(arguments.get("warmup_frames") or cfg["warmup_frames"])):
                cap.read()
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"camera read failed: {camera_name}")
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, int(cfg["jpeg_quality"])])
            return path
        finally:
            cap.release()

    def _camera_record(self, skill_name: str, arguments: dict[str, Any]) -> Path:
        import cv2

        camera_name = self._camera_name(skill_name, arguments)
        cfg = self.config["cameras"][camera_name]
        path = Path(arguments.get("output_path") or cfg["video_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fps = float(arguments.get("fps") or cfg["fps"])
        seconds = float(arguments.get("seconds") or arguments.get("duration") or cfg["record_seconds"])
        cap = cv2.VideoCapture(arguments.get("device") or cfg["device"])
        writer = None
        if not cap.isOpened():
            raise RuntimeError(f"camera open failed: {camera_name}")
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(arguments.get("width") or cfg["width"]))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(arguments.get("height") or cfg["height"]))
            cap.set(cv2.CAP_PROP_FPS, fps)
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"camera read failed: {camera_name}")
            height, width = frame.shape[:2]
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*cfg["fourcc"]), fps, (width, height))
            if not writer.isOpened():
                raise RuntimeError(f"video writer open failed: {path}")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if self._is_interrupt_requested():
                    break
                ok, frame = cap.read()
                if ok and frame is not None:
                    writer.write(frame)
                time.sleep(max(0.0, 1.0 / fps / 2.0))
            return path
        finally:
            if writer is not None:
                writer.release()
            cap.release()
