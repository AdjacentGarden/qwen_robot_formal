from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import ensure_runtime_dirs
from .dialogue import RobotOrchestrator
from .doubao_realtime import DoubaoRealtimeSession
from .executor import SkillExecutor
from .models import CommandSession, SessionStatus, TaskGroup, TaskStatus, TaskStep, WakeupEvent, to_dict
from .resources import ResourceManager
from .robot_state import RobotStateRestorer
from .skill_registry import SkillRegistry
from .speech import SpeechEvent


REQUIRED_SELF_CHECK_SKILLS = {
    "camera_capture",
    "environment_perception",
    "face_recognition",
    "head_control",
    "move_backward",
    "move_forward",
    "move_right",
    "navigation_goto",
    "pet_tracking",
    "projector_control",
    "squat",
}


class _FakePlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, _text: str, history: list[dict[str, Any]] | None = None, session_context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "decision_type": "ask_user",
                "reply": "你想做什么运动？",
                "ask_user": {
                    "task_title": "运动任务",
                    "question": "你想做什么运动？",
                    "missing_slots": ["exercise_type"],
                    "optional_slots": [],
                    "candidate_skills": ["squat"],
                },
                "confidence": 0.9,
                "task_groups": [
                    {
                        "title": "后退",
                        "user_instruction": "先后退",
                        "slots": {},
                        "followups": [],
                        "steps": [{"skill_name": "move_backward", "arguments": {}, "reason": "前置动作"}],
                    },
                    {
                        "title": "运动任务",
                        "user_instruction": "做运动",
                        "slots": {},
                        "followups": [],
                        "steps": [],
                    },
                    {
                        "title": "后置动作",
                        "user_instruction": "运动后再后退",
                        "slots": {},
                        "followups": [],
                        "steps": [{"skill_name": "move_backward", "arguments": {}, "reason": "后置动作"}],
                    },
                ],
            }
        return {
            "decision_type": "task_plan",
            "reply": "开始深蹲",
            "ask_user": None,
            "confidence": 0.9,
            "task_groups": [
                {
                    "title": "运动任务",
                    "user_instruction": "做深蹲",
                    "slots": {"exercise_type": "squat"},
                    "followups": [],
                    "steps": [{"skill_name": "squat", "arguments": {"action": "run"}, "reason": "补齐运动类型"}],
                }
            ],
        }


def run_resume_self_check(config: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    cfg["paths"]["runtime_dir"] = tempfile.mkdtemp(prefix="robot_resume_self_check_")
    local_spec_dir = Path(__file__).resolve().parents[1] / "self_program" / "skill_function_specs"
    if local_spec_dir.exists():
        cfg["paths"]["skill_spec_dir"] = str(local_spec_dir)
    _ensure_self_check_skill_specs(cfg)
    ensure_runtime_dirs(cfg)

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, sort_keys=True)})

    def require(name: str, condition: bool, detail: Any = "") -> None:
        add(name, condition, detail)
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    restorer = RobotStateRestorer(cfg)
    saved = {"peripherals": {"projector": {"action": "internal_on", "timestamp": 1}}}
    current_same = {"peripherals": {"projector": {"action": "internal_on", "timestamp": 999}}}
    current_diff = {"peripherals": {"projector": {"action": "off", "timestamp": 999}}}
    same_diff = restorer.diff(saved, current_same, requirements=["projector"])
    require("peripheral_timestamp_ignored", same_diff["items"] == [], same_diff)
    changed_diff = restorer.diff(saved, current_diff, requirements=["projector"])
    require("peripheral_state_diff_detected", len(changed_diff["items"]) == 1 and changed_diff["items"][0]["saved_state"] == "internal_on", changed_diff)
    restore_steps = restorer.build_restore_steps(changed_diff)
    require(
        "peripheral_restore_step",
        bool(restore_steps) and restore_steps[0].skill_name == "projector_control" and restore_steps[0].arguments.get("action") == "internal_on",
        [step.__dict__ for step in restore_steps],
    )

    spoken: list[str] = []
    executor = SkillExecutor(cfg, SkillRegistry(cfg), ResourceManager(cfg), speech_callback=lambda _step, event: spoken.append(event.text))
    event = SpeechEvent(skill_name="projector_control", text="投影操作完成", source="self_check")
    executor._emit_speech_event(TaskStep(skill_name="projector_control", reason="restore interrupted task projector state"), event)
    executor._emit_speech_event(TaskStep(skill_name="projector_control", reason="user requested projector"), event)
    require("restore_step_speech_suppressed", spoken == ["投影操作完成"], spoken)

    executor.robot_state._save_cache({"peripherals": {"projector": {"action": "internal_on", "timestamp": 1}}})
    finalizer_group = TaskGroup(
        title="projector squat",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.COMPLETED.value, order=2),
        ],
    )
    finalizer_steps = executor._completion_finalizer_steps(finalizer_group)
    require(
        "fitness_projector_and_head_finalizers",
        [(step.skill_name, step.arguments.get("action")) for step in finalizer_steps] == [("projector_control", "off"), ("head_control", "level")],
        [step.__dict__ for step in finalizer_steps],
    )
    completed_fitness = TaskGroup(
        title="completed fitness with projector",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, order=2),
        ],
    )
    completed_fitness = executor.execute_task_group(completed_fitness, dry_run=True)
    completed_finalizers = [(item.get("skill_name"), (item.get("arguments") or {}).get("action")) for item in completed_fitness.metadata.get("finalizers", [])]
    require(
        "fitness_completion_executes_projector_off_then_head_level",
        completed_fitness.status == TaskStatus.COMPLETED.value and completed_finalizers == [("projector_control", "off"), ("head_control", "level")],
        {"status": completed_fitness.status, "finalizers": completed_finalizers},
    )
    executor.robot_state._save_cache({"peripherals": {"projector": {"action": "off", "timestamp": 999}}})
    cached_off_projector_task = TaskGroup(
        title="cached off but task owns projector",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.COMPLETED.value, order=2),
        ],
    )
    cached_off_finalizers = [(step.skill_name, step.arguments.get("action")) for step in executor._completion_finalizer_steps(cached_off_projector_task)]
    require(
        "fitness_projector_off_not_skipped_by_stale_cache",
        cached_off_finalizers == [("projector_control", "off"), ("head_control", "level")],
        cached_off_finalizers,
    )
    cleanup_failure_executor = SkillExecutor(cfg, SkillRegistry(cfg), ResourceManager(cfg))
    cleanup_failure_task = TaskGroup(
        title="cleanup failure task",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.COMPLETED.value, order=2),
        ],
    )

    def fake_cleanup_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        if step.skill_name == "projector_control":
            return {"ok": False, "error": "projector_off_failed"}
        return {"ok": True, "dry_run": dry_run}

    cleanup_failure_executor.execute_maintenance_step = fake_cleanup_step
    cleanup_failure_executor._run_completion_finalizers(cleanup_failure_task, dry_run=False)
    require(
        "cleanup_failure_is_marked",
        cleanup_failure_task.metadata.get("cleanup_failed") is True
        and (cleanup_failure_task.metadata.get("cleanup_failed_finalizers") or [])[0].get("skill_name") == "projector_control",
        cleanup_failure_task.metadata,
    )
    standalone_projector = TaskGroup(
        title="standalone projector",
        steps=[TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=0)],
    )
    require("standalone_projector_not_auto_off", executor._completion_finalizer_steps(standalone_projector) == [], [step.__dict__ for step in executor._completion_finalizer_steps(standalone_projector)])

    queue_orchestrator = RobotOrchestrator(cfg)
    queue_orchestrator.planner = _FakePlanner()
    queue_orchestrator.audio.speak_text = lambda _text: None
    first = queue_orchestrator.handle_text("先后退然后做运动", execute=True, dry_run=True, enqueue=True)
    state = queue_orchestrator.store.load_state()
    require("followup_queue_empty_before_answer", state.get("task_queue") == [], state)
    ids = first.get("task_group_ids") or []
    require("followup_three_task_groups", len(ids) == 3, ids)
    pre_task = queue_orchestrator.store.load_task_group(ids[0])
    pending_task = queue_orchestrator.store.load_task_group(ids[1])
    after_task = queue_orchestrator.store.load_task_group(ids[2])
    require("pre_task_executed_before_followup", pre_task.status == TaskStatus.COMPLETED.value, pre_task.status)
    require("pending_task_needs_info", pending_task.status == TaskStatus.NEEDS_INFO.value, pending_task.status)
    require("post_task_waits_behind_followup", after_task.status == TaskStatus.NEW.value, after_task.status)
    followup = queue_orchestrator.answer_followup(pending_task.task_group_id, "深蹲就在这里", execute=True, dry_run=True, enqueue=True)
    executed_ids = [item["task_group_id"] for item in followup.get("execution", {}).get("executed", [])]
    require("followup_executes_pending_then_post_task", executed_ids == [pending_task.task_group_id, after_task.task_group_id], executed_ids)

    gated_steps_orchestrator = RobotOrchestrator(cfg)
    gated_steps_orchestrator.audio.speak_text = lambda _text: None
    gated_question = "需要打开投影吗？"
    gated_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": gated_question,
        "ask_user": {
            "task_title": "深蹲",
            "question": gated_question,
            "missing_slots": [],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["projector_control"],
        },
        "confidence": 1.0,
        "user_text": "做深蹲",
        "task_groups": [
            {
                "title": "深蹲",
                "user_instruction": "做深蹲",
                "slots": {"exercise_type": "squat", "where": "here"},
                "followups": [],
                "steps": [
                    {"skill_name": "head_control", "arguments": {"action": "up"}, "reason": "precondition"},
                    {
                        "skill_name": "environment_perception",
                        "arguments": {"camera": "both", "purpose": "fitness_projection", "exercise_type": "squat"},
                        "reason": "check first",
                    },
                ],
            }
        ],
    }
    gated = gated_steps_orchestrator.handle_voice_decision(gated_decision, execute=True, dry_run=True, enqueue=True)
    gated_id = (gated.get("task_group_ids") or [None])[0]
    gated_task = gated_steps_orchestrator.store.load_task_group(gated_id)
    require(
        "ask_user_task_group_with_steps_waits_before_execution",
        gated_task.status == TaskStatus.NEEDS_INFO.value
        and all(step.status == TaskStatus.NEW.value for step in gated_task.steps)
        and gated_steps_orchestrator.store.load_state().get("task_queue") == [],
        {"result": gated, "status": gated_task.status, "steps": [(step.skill_name, step.status) for step in gated_task.steps], "state": gated_steps_orchestrator.store.load_state()},
    )
    gated_answer = gated_steps_orchestrator.answer_followup(gated_task.task_group_id, "打开投影", execute=True, dry_run=True, enqueue=True)
    gated_after = gated_steps_orchestrator.store.load_task_group(gated_task.task_group_id)
    require(
        "ask_user_task_group_with_steps_executes_after_answer",
        gated_after.status in {TaskStatus.COMPLETED.value, TaskStatus.NEEDS_INFO.value}
        and any(step.skill_name == "squat" for step in gated_after.steps)
        and gated_after.slots.get("projector") is True,
        {"answer": gated_answer, "status": gated_after.status, "slots": gated_after.slots, "steps": [(step.skill_name, step.status) for step in gated_after.steps]},
    )
    direct_enqueue_orchestrator = RobotOrchestrator(cfg)
    direct_needs_info = TaskGroup(
        title="direct needs info",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    direct_enqueue_orchestrator.store.save_task_group(direct_needs_info)
    direct_enqueue_orchestrator.store.enqueue_task_group(direct_needs_info)
    require(
        "store_refuses_needs_info_task_enqueue",
        direct_enqueue_orchestrator.store.load_state().get("task_queue") == [],
        direct_enqueue_orchestrator.store.load_state(),
    )
    stale_queue_orchestrator = RobotOrchestrator(cfg)
    stale_waiting = TaskGroup(
        title="stale queued waiting",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    stale_after = TaskGroup(title="after stale waiting", status=TaskStatus.NEW.value, steps=[TaskStep(skill_name="move_backward")])
    stale_queue_orchestrator.store.save_task_group(stale_waiting)
    stale_queue_orchestrator.store.save_task_group(stale_after)
    stale_state = stale_queue_orchestrator.store.load_state()
    stale_state["task_queue"] = [stale_waiting.task_group_id, stale_after.task_group_id]
    stale_queue_orchestrator.store.save_state(stale_state)
    stale_exec = stale_queue_orchestrator.drain_task_group_ids([stale_waiting.task_group_id, stale_after.task_group_id], dry_run=True)
    stale_waiting_loaded = stale_queue_orchestrator.store.load_task_group(stale_waiting.task_group_id)
    stale_after_loaded = stale_queue_orchestrator.store.load_task_group(stale_after.task_group_id)
    require(
        "executor_refuses_stale_queued_needs_info_task",
        [item.get("task_group_id") for item in stale_exec.get("executed", [])] == [stale_waiting.task_group_id]
        and stale_exec["executed"][0].get("skipped") is True
        and stale_exec["executed"][0].get("reason") == "non_ready_task_group"
        and stale_waiting_loaded.status == TaskStatus.NEEDS_INFO.value
        and stale_after_loaded.status == TaskStatus.NEW.value
        and stale_queue_orchestrator.store.load_state().get("task_queue") == [],
        {"execution": stale_exec, "waiting": stale_waiting_loaded.status, "after": stale_after_loaded.status, "state": stale_queue_orchestrator.store.load_state()},
    )
    pop_next_orchestrator = RobotOrchestrator(cfg)
    pop_next_waiting = TaskGroup(
        title="pop next waiting",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    pop_next_after = TaskGroup(title="pop next after", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_backward")])
    pop_next_orchestrator.store.save_task_group(pop_next_waiting)
    pop_next_orchestrator.store.save_task_group(pop_next_after)
    pop_next_state = pop_next_orchestrator.store.load_state()
    pop_next_state["task_queue"] = [pop_next_waiting.task_group_id, pop_next_after.task_group_id]
    pop_next_state["active_task_group_id"] = None
    pop_next_orchestrator.store.save_state(pop_next_state)
    pop_next_result = pop_next_orchestrator.store.pop_next_task_group()
    pop_next_after_loaded = pop_next_orchestrator.store.load_task_group(pop_next_after.task_group_id)
    pop_next_state_after = pop_next_orchestrator.store.load_state()
    require(
        "store_pop_next_blocks_on_non_ready_task_without_activating_or_running_later_tasks",
        pop_next_result is None
        and pop_next_state_after.get("active_task_group_id") is None
        and pop_next_state_after.get("task_queue") == []
        and pop_next_after_loaded.status == TaskStatus.NEW.value,
        {
            "result": pop_next_result,
            "state": pop_next_state_after,
            "after_status": pop_next_after_loaded.status,
        },
    )
    runtime_invariant_cfg = copy.deepcopy(cfg)
    runtime_invariant_cfg["paths"]["runtime_dir"] = tempfile.mkdtemp(prefix="robot_runtime_invariant_self_check_")
    ensure_runtime_dirs(runtime_invariant_cfg)

    valid_wait_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    valid_wait_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    valid_wait_task = TaskGroup(
        command_session_id=valid_wait_session.session_id,
        title="valid wait",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    valid_wait_session.task_group_ids = [valid_wait_task.task_group_id]
    valid_wait_orchestrator.store.save_session(valid_wait_session)
    valid_wait_orchestrator.store.save_task_group(valid_wait_task)
    valid_wait_report = valid_wait_orchestrator.store.validate_runtime_state()
    valid_wait_repair = valid_wait_orchestrator.store.repair_runtime_state("self_check_valid_waiting_task")
    valid_wait_loaded = valid_wait_orchestrator.store.load_task_group(valid_wait_task.task_group_id)
    require(
        "runtime_state_validation_preserves_valid_waiting_followup",
        valid_wait_report.get("ok") is True
        and valid_wait_repair.get("changed") is False
        and valid_wait_loaded.status == TaskStatus.NEEDS_INFO.value,
        {"report": valid_wait_report, "repair": valid_wait_repair, "status": valid_wait_loaded.status},
    )
    valid_wait_loaded.status = TaskStatus.CANCELLED.value
    valid_wait_loaded.ended_at = 1
    valid_wait_orchestrator.store.save_task_group(valid_wait_loaded)
    valid_wait_session.status = SessionStatus.COMPLETED.value
    valid_wait_session.ended_at = 1
    valid_wait_orchestrator.store.save_session(valid_wait_session)

    repair_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    repair_waiting = TaskGroup(
        title="repair waiting",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    repair_after = TaskGroup(title="repair after", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_backward")])
    repair_terminal = TaskGroup(title="repair terminal", status=TaskStatus.COMPLETED.value, steps=[TaskStep(skill_name="move_forward")])
    repair_completed_session = CommandSession(session_type="voice", status=SessionStatus.COMPLETED.value, ended_at=1)
    repair_orchestrator.store.save_task_group(repair_waiting)
    repair_orchestrator.store.save_task_group(repair_after)
    repair_orchestrator.store.save_task_group(repair_terminal)
    repair_orchestrator.store.save_session(repair_completed_session)
    repair_state = repair_orchestrator.store.load_state()
    repair_state["active_task_group_id"] = repair_waiting.task_group_id
    repair_state["task_queue"] = [
        repair_terminal.task_group_id,
        repair_waiting.task_group_id,
        repair_after.task_group_id,
        repair_after.task_group_id,
        "task_missing_repair",
    ]
    repair_state["current_command_session_id"] = repair_completed_session.session_id
    repair_orchestrator.store.save_state(repair_state)
    repair_report_before = repair_orchestrator.store.validate_runtime_state()
    repair_result = repair_orchestrator.store.repair_runtime_state("self_check_repair_runtime_state")
    repair_state_after = repair_orchestrator.store.load_state()
    repair_after_loaded = repair_orchestrator.store.load_task_group(repair_after.task_group_id)
    repair_waiting_loaded = repair_orchestrator.store.load_task_group(repair_waiting.task_group_id)
    repair_report_after = repair_orchestrator.store.validate_runtime_state()
    require(
        "runtime_state_repair_clears_invalid_pointers_and_cancels_orphan_waiting_task",
        repair_report_before.get("ok") is False
        and any(item.get("kind") == "orphan_waiting_task_group" for item in repair_report_before.get("issues") or [])
        and repair_result.get("changed") is True
        and repair_state_after.get("active_task_group_id") is None
        and repair_state_after.get("task_queue") == []
        and repair_state_after.get("current_command_session_id") is None
        and repair_waiting_loaded.status == TaskStatus.CANCELLED.value
        and repair_after_loaded.status == TaskStatus.NEW.value,
        {
            "result": repair_result,
            "before": repair_report_before,
            "after": repair_report_after,
            "state": repair_state_after,
            "waiting_status": repair_waiting_loaded.status,
            "after_status": repair_after_loaded.status,
        },
    )
    active_running_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    active_running = TaskGroup(
        title="active running",
        status=TaskStatus.RUNNING.value,
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.RUNNING.value)],
    )
    active_running_orchestrator.store.save_task_group(active_running)
    active_running_state = active_running_orchestrator.store.load_state()
    active_running_state["active_task_group_id"] = active_running.task_group_id
    active_running_state["task_queue"] = []
    active_running_orchestrator.store.save_state(active_running_state)
    active_running_result = active_running_orchestrator.store.repair_runtime_state("self_check_running_active_is_valid")
    require(
        "runtime_state_repair_preserves_running_active_task",
        active_running_orchestrator.store.load_state().get("active_task_group_id") == active_running.task_group_id
        and active_running_result.get("changed") is False,
        {"result": active_running_result, "state": active_running_orchestrator.store.load_state()},
    )
    active_running.status = TaskStatus.CANCELLED.value
    active_running.ended_at = 1
    active_running_orchestrator.store.save_task_group(active_running)
    active_running_state = active_running_orchestrator.store.load_state()
    active_running_state["active_task_group_id"] = None
    active_running_orchestrator.store.save_state(active_running_state)
    ready_queue_repair_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    ready_queue_task = TaskGroup(title="ready queue keep", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_forward")])
    ready_queue_repair_orchestrator.store.save_task_group(ready_queue_task)
    ready_queue_state = ready_queue_repair_orchestrator.store.load_state()
    ready_queue_state["task_queue"] = [ready_queue_task.task_group_id]
    ready_queue_repair_orchestrator.store.save_state(ready_queue_state)
    ready_queue_keep_result = ready_queue_repair_orchestrator.store.repair_runtime_state("self_check_keep_ready_queue")
    require(
        "runtime_state_repair_keeps_ready_queue_without_session_boundary",
        ready_queue_keep_result.get("changed") is False
        and ready_queue_repair_orchestrator.store.load_state().get("task_queue") == [ready_queue_task.task_group_id],
        {"result": ready_queue_keep_result, "state": ready_queue_repair_orchestrator.store.load_state()},
    )
    ready_queue_clear_result = ready_queue_repair_orchestrator.store.repair_runtime_state("self_check_clear_ready_queue", clear_ready_queue=True)
    ready_queue_loaded = ready_queue_repair_orchestrator.store.load_task_group(ready_queue_task.task_group_id)
    require(
        "runtime_state_repair_clears_ready_queue_at_session_boundary",
        ready_queue_clear_result.get("changed") is True
        and ready_queue_repair_orchestrator.store.load_state().get("task_queue") == []
        and ready_queue_loaded.status == TaskStatus.NEW.value,
        {"result": ready_queue_clear_result, "state": ready_queue_repair_orchestrator.store.load_state(), "status": ready_queue_loaded.status},
    )
    concurrent_store_cfg = copy.deepcopy(cfg)
    concurrent_store_cfg["paths"]["runtime_dir"] = tempfile.mkdtemp(prefix="robot_store_concurrent_self_check_")
    ensure_runtime_dirs(concurrent_store_cfg)
    concurrent_store = RobotOrchestrator(concurrent_store_cfg).store
    concurrent_tasks = [
        TaskGroup(title=f"concurrent enqueue {index}", status=TaskStatus.NEW.value, steps=[TaskStep(skill_name="move_forward")])
        for index in range(24)
    ]
    for task in concurrent_tasks:
        concurrent_store.save_task_group(task)
    concurrent_start = threading.Barrier(12)

    def enqueue_task_concurrently(task_group_id: str) -> str:
        if concurrent_start is not None:
            concurrent_start.wait(timeout=5)
        task = concurrent_store.load_task_group(task_group_id)
        concurrent_store.enqueue_task_group(task)
        return task_group_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(enqueue_task_concurrently, task.task_group_id) for task in concurrent_tasks]
        completed_ids = [future.result(timeout=10) for future in futures]
    concurrent_state = concurrent_store.load_state()
    concurrent_queue = list(concurrent_state.get("task_queue") or [])
    concurrent_report = concurrent_store.validate_runtime_state()
    require(
        "json_store_serializes_concurrent_queue_updates",
        set(completed_ids) == {task.task_group_id for task in concurrent_tasks}
        and len(concurrent_queue) == len(concurrent_tasks)
        and set(concurrent_queue) == {task.task_group_id for task in concurrent_tasks}
        and len(concurrent_queue) == len(set(concurrent_queue))
        and concurrent_report.get("ok") is True
        and all(concurrent_store.load_task_group(task.task_group_id).status == TaskStatus.QUEUED.value for task in concurrent_tasks),
        {"queue": concurrent_queue, "report": concurrent_report},
    )
    entry_repair_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    entry_repair_orchestrator.audio.speak_text = lambda _text: True
    entry_waiting = TaskGroup(
        title="entry stale waiting",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "need answer", "timestamp": 1, "missing_slots": ["environment_override"]}],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    entry_after = TaskGroup(title="entry stale after", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_backward")])
    entry_repair_orchestrator.store.save_task_group(entry_waiting)
    entry_repair_orchestrator.store.save_task_group(entry_after)
    entry_state = entry_repair_orchestrator.store.load_state()
    entry_state["active_task_group_id"] = entry_waiting.task_group_id
    entry_state["task_queue"] = [entry_waiting.task_group_id, entry_after.task_group_id]
    entry_repair_orchestrator.store.save_state(entry_state)

    def complete_task_group(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        task_group.status = TaskStatus.COMPLETED.value
        task_group.ended_at = 1
        for step in task_group.steps:
            step.status = TaskStatus.COMPLETED.value
            step.result = {"ok": True, "dry_run": dry_run}
        return task_group

    entry_repair_orchestrator.executor.execute_task_group = complete_task_group  # type: ignore[method-assign]
    entry_decision = {
        "ok": True,
        "decision_type": "task_plan",
        "reply": "",
        "ask_user": None,
        "confidence": 1.0,
        "user_text": "move now",
        "task_groups": [
            {
                "title": "new move",
                "user_instruction": "move now",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "move_forward", "arguments": {}, "reason": "new command"}],
            }
        ],
    }
    entry_result = entry_repair_orchestrator.handle_voice_decision(entry_decision, execute=True, dry_run=True, enqueue=True)
    entry_waiting_loaded = entry_repair_orchestrator.store.load_task_group(entry_waiting.task_group_id)
    entry_after_loaded = entry_repair_orchestrator.store.load_task_group(entry_after.task_group_id)
    entry_state_after = entry_repair_orchestrator.store.load_state()
    require(
        "voice_command_entry_repairs_stale_runtime_queue_before_executing_new_task",
        entry_waiting_loaded.status == TaskStatus.CANCELLED.value
        and entry_after_loaded.status == TaskStatus.NEW.value
        and entry_state_after.get("active_task_group_id") is None
        and entry_state_after.get("task_queue") == []
        and len(entry_result.get("execution", {}).get("executed", [])) == 1
        and entry_result["execution"]["executed"][0].get("status") == TaskStatus.COMPLETED.value,
        {
            "result": entry_result,
            "state": entry_state_after,
            "waiting_status": entry_waiting_loaded.status,
            "after_status": entry_after_loaded.status,
        },
    )
    entry_ready_orchestrator = RobotOrchestrator(runtime_invariant_cfg)
    entry_ready_orchestrator.audio.speak_text = lambda _text: True
    stale_ready = TaskGroup(title="stale ready", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_backward")])
    entry_ready_orchestrator.store.save_task_group(stale_ready)
    entry_ready_state = entry_ready_orchestrator.store.load_state()
    entry_ready_state["task_queue"] = [stale_ready.task_group_id]
    entry_ready_orchestrator.store.save_state(entry_ready_state)
    entry_ready_calls: list[str] = []

    def complete_and_record(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        entry_ready_calls.append(task_group.task_group_id)
        task_group.status = TaskStatus.COMPLETED.value
        task_group.ended_at = 1
        for step in task_group.steps:
            step.status = TaskStatus.COMPLETED.value
            step.result = {"ok": True, "dry_run": dry_run}
        return task_group

    entry_ready_orchestrator.executor.execute_task_group = complete_and_record  # type: ignore[method-assign]
    entry_ready_result = entry_ready_orchestrator.handle_voice_decision(entry_decision, execute=True, dry_run=True, enqueue=True)
    stale_ready_loaded = entry_ready_orchestrator.store.load_task_group(stale_ready.task_group_id)
    require(
        "voice_command_entry_clears_old_ready_queue_and_executes_only_new_session",
        stale_ready_loaded.status == TaskStatus.NEW.value
        and entry_ready_orchestrator.store.load_state().get("task_queue") == []
        and len(entry_ready_calls) == 1
        and entry_ready_calls[0] != stale_ready.task_group_id
        and entry_ready_result.get("execution", {}).get("executed", [{}])[0].get("status") == TaskStatus.COMPLETED.value,
        {
            "calls": entry_ready_calls,
            "stale_status": stale_ready_loaded.status,
            "state": entry_ready_orchestrator.store.load_state(),
            "result": entry_ready_result,
        },
    )

    fail_orchestrator = RobotOrchestrator(cfg)
    fail_orchestrator.audio.speak_text = lambda _text: None
    failed_decision = {
        "ok": True,
        "decision_type": "task_plan",
        "reply": "",
        "ask_user": None,
        "confidence": 1.0,
        "user_text": "bad then after",
        "task_groups": [
            {
                "title": "bad",
                "user_instruction": "bad",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "not_a_skill", "arguments": {}, "reason": "force failure"}],
            },
            {
                "title": "after",
                "user_instruction": "after",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "move_backward", "arguments": {}, "reason": "must not run after failure"}],
            },
        ],
    }
    failed = fail_orchestrator.handle_voice_decision(failed_decision, execute=True, dry_run=True)
    failed_ids = failed.get("task_group_ids") or []
    failed_exec_ids = [item["task_group_id"] for item in failed.get("execution", {}).get("executed", [])]
    failed_first = fail_orchestrator.store.load_task_group(failed_ids[0])
    failed_after = fail_orchestrator.store.load_task_group(failed_ids[1])
    require("failed_group_stops_later_groups", failed_exec_ids == [failed_ids[0]] and failed_first.status == TaskStatus.FAILED.value and failed_after.status == TaskStatus.CANCELLED.value, {"executed": failed_exec_ids, "first": failed_first.status, "after": failed_after.status})
    require("failed_group_clears_queue", fail_orchestrator.store.load_state().get("task_queue") == [], fail_orchestrator.store.load_state())

    terminal_orchestrator = RobotOrchestrator(cfg)
    terminal_task = TaskGroup(
        title="terminal task",
        status=TaskStatus.COMPLETED.value,
        result_summary="already done",
        steps=[TaskStep(skill_name="move_forward", status=TaskStatus.COMPLETED.value)],
    )
    terminal_orchestrator.store.save_task_group(terminal_task)
    terminal_orchestrator.store.enqueue_task_group(terminal_task)
    require(
        "terminal_task_group_not_requeued",
        terminal_orchestrator.store.load_state().get("task_queue") == [],
        terminal_orchestrator.store.load_state(),
    )
    terminal_execute_calls = {"count": 0}

    def terminal_execute_should_not_run(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        terminal_execute_calls["count"] += 1
        raise AssertionError(f"terminal task should not execute: {task_group.task_group_id}")

    terminal_orchestrator.executor.execute_task_group = terminal_execute_should_not_run  # type: ignore[method-assign]
    terminal_item, terminal_stop = terminal_orchestrator._execute_queued_task_group(terminal_task, dry_run=True)
    require(
        "terminal_task_group_execution_skipped",
        terminal_execute_calls["count"] == 0
        and terminal_item.get("skipped") is True
        and terminal_item.get("reason") == "terminal_task_group"
        and terminal_stop is False,
        {"item": terminal_item, "calls": terminal_execute_calls, "stop": terminal_stop},
    )

    stale_terminal_orchestrator = RobotOrchestrator(cfg)
    stale_terminal_orchestrator.audio.speak_text = lambda _text: True
    stale_failed = TaskGroup(title="stale failed", status=TaskStatus.FAILED.value, result_summary="old failure")
    fresh_ready = TaskGroup(title="fresh ready", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_forward")])
    stale_terminal_orchestrator.store.save_task_group(stale_failed)
    stale_terminal_orchestrator.store.save_task_group(fresh_ready)
    stale_state = stale_terminal_orchestrator.store.load_state()
    stale_state["task_queue"] = [stale_failed.task_group_id, fresh_ready.task_group_id]
    stale_terminal_orchestrator.store.save_state(stale_state)
    stale_run_calls: list[str] = []

    def stale_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        stale_run_calls.append(task_group.task_group_id)
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "fresh completed"
        return task_group

    stale_terminal_orchestrator.executor.execute_task_group = stale_execute  # type: ignore[method-assign]
    stale_drain = stale_terminal_orchestrator.drain_task_group_ids([stale_failed.task_group_id, fresh_ready.task_group_id], dry_run=True)
    stale_fresh_loaded = stale_terminal_orchestrator.store.load_task_group(fresh_ready.task_group_id)
    require(
        "stale_failed_task_group_does_not_block_following_ready_task",
        stale_run_calls == [fresh_ready.task_group_id]
        and stale_fresh_loaded.status == TaskStatus.COMPLETED.value
        and [item.get("task_group_id") for item in stale_drain.get("executed", [])] == [stale_failed.task_group_id, fresh_ready.task_group_id],
        {"drain": stale_drain, "calls": stale_run_calls, "fresh_status": stale_fresh_loaded.status},
    )

    missing_queue_orchestrator = RobotOrchestrator(cfg)
    missing_queue_orchestrator.audio.speak_text = lambda _text: True
    missing_next_ready = TaskGroup(title="ready after missing", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_forward")])
    missing_queue_orchestrator.store.save_task_group(missing_next_ready)
    missing_state = missing_queue_orchestrator.store.load_state()
    missing_state["task_queue"] = ["task_missing_from_queue", missing_next_ready.task_group_id]
    missing_state["active_task_group_id"] = None
    missing_queue_orchestrator.store.save_state(missing_state)
    missing_queue_calls: list[str] = []

    def missing_queue_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        missing_queue_calls.append(task_group.task_group_id)
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "completed after missing"
        return task_group

    missing_queue_orchestrator.executor.execute_task_group = missing_queue_execute  # type: ignore[method-assign]
    missing_queue_drain = missing_queue_orchestrator.drain_queue(dry_run=True)
    missing_queue_state = missing_queue_orchestrator.store.load_state()
    require(
        "missing_queued_task_group_skipped_without_active_pollution",
        missing_queue_calls == [missing_next_ready.task_group_id]
        and missing_queue_state.get("task_queue") == []
        and missing_queue_state.get("active_task_group_id") is None
        and [item.get("task_group_id") for item in missing_queue_drain.get("executed", [])] == [missing_next_ready.task_group_id],
        {"drain": missing_queue_drain, "calls": missing_queue_calls, "state": missing_queue_state},
    )

    missing_order_orchestrator = RobotOrchestrator(cfg)
    missing_order_orchestrator.audio.speak_text = lambda _text: True
    missing_order_ready = TaskGroup(title="ordered ready after missing", status=TaskStatus.QUEUED.value, steps=[TaskStep(skill_name="move_backward")])
    missing_order_orchestrator.store.save_task_group(missing_order_ready)
    missing_order_state = missing_order_orchestrator.store.load_state()
    missing_order_state["task_queue"] = ["task_missing_ordered", missing_order_ready.task_group_id]
    missing_order_orchestrator.store.save_state(missing_order_state)
    missing_order_calls: list[str] = []

    def missing_order_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        missing_order_calls.append(task_group.task_group_id)
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "ordered completed after missing"
        return task_group

    missing_order_orchestrator.executor.execute_task_group = missing_order_execute  # type: ignore[method-assign]
    missing_order_drain = missing_order_orchestrator.drain_task_group_ids(["task_missing_ordered", missing_order_ready.task_group_id], dry_run=True)
    missing_order_state_after = missing_order_orchestrator.store.load_state()
    require(
        "missing_ordered_task_group_skipped_and_following_task_runs",
        missing_order_calls == [missing_order_ready.task_group_id]
        and missing_order_state_after.get("task_queue") == []
        and missing_order_state_after.get("active_task_group_id") is None
        and [item.get("task_group_id") for item in missing_order_drain.get("executed", [])] == [missing_order_ready.task_group_id],
        {"drain": missing_order_drain, "calls": missing_order_calls, "state": missing_order_state_after},
    )

    executor_exception_orchestrator = RobotOrchestrator(cfg)
    executor_exception_orchestrator.audio.speak_text = lambda _text: True
    exception_first = TaskGroup(title="executor exception", status=TaskStatus.NEW.value, steps=[TaskStep(skill_name="move_forward")])
    exception_after = TaskGroup(title="after executor exception", status=TaskStatus.NEW.value, steps=[TaskStep(skill_name="move_backward")])
    exception_session = CommandSession(session_type="self_check", task_group_ids=[exception_first.task_group_id, exception_after.task_group_id])
    executor_exception_orchestrator.store.save_session(exception_session)
    executor_exception_orchestrator.store.save_task_group(exception_first)
    executor_exception_orchestrator.store.save_task_group(exception_after)
    executor_exception_orchestrator.store.enqueue_task_group(exception_first)
    executor_exception_orchestrator.store.enqueue_task_group(exception_after)

    def raising_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        raise RuntimeError("self_check_executor_crash")

    executor_exception_orchestrator.executor.execute_task_group = raising_execute  # type: ignore[method-assign]
    exception_drain = executor_exception_orchestrator.drain_task_group_ids(exception_session.task_group_ids, dry_run=True)
    exception_first_loaded = executor_exception_orchestrator.store.load_task_group(exception_first.task_group_id)
    exception_after_loaded = executor_exception_orchestrator.store.load_task_group(exception_after.task_group_id)
    exception_state = executor_exception_orchestrator.store.load_state()
    require(
        "executor_exception_marks_failed_and_clears_active",
        exception_first_loaded.status == TaskStatus.FAILED.value
        and exception_first_loaded.metadata.get("executor_exception") == "self_check_executor_crash"
        and exception_after_loaded.status == TaskStatus.CANCELLED.value
        and exception_state.get("active_task_group_id") is None
        and [item.get("task_group_id") for item in exception_drain.get("executed", [])] == [exception_first.task_group_id],
        {
            "drain": exception_drain,
            "first_status": exception_first_loaded.status,
            "first_metadata": exception_first_loaded.metadata,
            "after_status": exception_after_loaded.status,
            "state": exception_state,
        },
    )

    history_orchestrator = RobotOrchestrator(cfg)
    history_orchestrator.audio.speak_text = lambda _text: True
    history_task = TaskGroup(
        title="completed task with existing history",
        status=TaskStatus.QUEUED.value,
        result_summary="existing summary",
        history_refs=["history_existing"],
        steps=[TaskStep(skill_name="move_forward", status=TaskStatus.COMPLETED.value)],
    )

    def fake_history_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        task_group.status = TaskStatus.COMPLETED.value
        task_group.ended_at = task_group.ended_at or 1.0
        return task_group

    def history_write_should_not_run(task_group: TaskGroup) -> str:
        raise AssertionError(f"history should not be written twice: {task_group.task_group_id}")

    history_orchestrator.executor.execute_task_group = fake_history_execute  # type: ignore[method-assign]
    history_orchestrator.store.write_history = history_write_should_not_run  # type: ignore[method-assign]
    history_item, history_stop = history_orchestrator._execute_queued_task_group(history_task, dry_run=True)
    history_loaded = history_orchestrator.store.load_task_group(history_task.task_group_id)
    require(
        "completed_task_group_history_write_idempotent",
        history_item.get("status") == TaskStatus.COMPLETED.value
        and history_stop is False
        and history_loaded.history_refs == ["history_existing"],
        {"item": history_item, "history_refs": history_loaded.history_refs},
    )

    doubao_parser = DoubaoRealtimeSession(cfg, SkillRegistry(cfg), ResourceManager(cfg))
    non_json_multi = doubao_parser._parse_decision("好的，我先往前走五秒，再往右走四秒，然后看看这是谁。", "command")
    non_json_multi_groups = non_json_multi.get("task_groups") or []
    non_json_multi_skills = [
        ((group.get("steps") or [{}])[0].get("skill_name") if isinstance(group, dict) else None)
        for group in non_json_multi_groups
    ]
    non_json_multi_durations = [
        ((group.get("steps") or [{}])[0].get("arguments") or {}).get("duration") if isinstance(group, dict) else None
        for group in non_json_multi_groups
    ]
    require(
        "doubao_non_json_multi_instruction_split_taskgroups",
        non_json_multi.get("decision_type") == "task_plan"
        and non_json_multi_skills == ["move_forward", "move_right", "face_recognition"]
        and non_json_multi_durations[:2] == [5.0, 4.0]
        and len(non_json_multi_groups) == 3,
        {"decision": non_json_multi, "skills": non_json_multi_skills, "durations": non_json_multi_durations},
    )
    multi_execute_orchestrator = RobotOrchestrator(cfg)
    multi_execute_orchestrator.audio.speak_text = lambda _text: True
    multi_execute = multi_execute_orchestrator.handle_voice_decision(non_json_multi, execute=True, dry_run=True, enqueue=True)
    multi_execute_groups = [
        multi_execute_orchestrator.store.load_task_group(task_group_id)
        for task_group_id in multi_execute.get("task_group_ids", [])
    ]
    multi_execute_step_order = [
        [step.skill_name for step in task_group.steps]
        for task_group in multi_execute_groups
    ]
    multi_execute_statuses = [task_group.status for task_group in multi_execute_groups]
    require(
        "multi_instruction_executes_all_taskgroups_in_order",
        [item.get("task_group_id") for item in multi_execute.get("execution", {}).get("executed", [])] == [task_group.task_group_id for task_group in multi_execute_groups]
        and multi_execute_statuses == [TaskStatus.COMPLETED.value, TaskStatus.COMPLETED.value, TaskStatus.COMPLETED.value]
        and multi_execute_step_order == [["move_forward"], ["move_right"], ["head_control", "face_recognition"]]
        and all(task_group.history_refs for task_group in multi_execute_groups),
        {
            "result": multi_execute,
            "statuses": multi_execute_statuses,
            "steps": multi_execute_step_order,
            "history_refs": [task_group.history_refs for task_group in multi_execute_groups],
        },
    )

    split_wait_orchestrator = RobotOrchestrator(cfg)
    split_wait_orchestrator.audio.speak_text = lambda _text: True
    split_wait_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": "好的，我先往前走五秒，然后开始运动。你想做什么运动？是在这里做还是去某个已保存地点做？需要打开投影吗？",
        "confidence": 0.95,
        "user_text": "请你先往前走五秒，然后我想做运动，最后拍一张照片",
        "ask_user": {
            "task_title": "运动",
            "question": "你想做什么运动？是在这里做还是去某个已保存地点做？需要打开投影吗？",
            "missing_slots": ["exercise_type", "where"],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
        },
        "task_groups": [
            {
                "title": "前进五秒",
                "user_instruction": "请你先往前走五秒",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "move_forward", "arguments": {"duration": 5.0}, "reason": "前置独立移动任务"}],
            },
            {
                "title": "运动",
                "user_instruction": "我想做运动",
                "slots": {},
                "followups": [],
                "steps": [],
            },
            {
                "title": "拍照",
                "user_instruction": "最后拍一张照片",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "camera_capture", "arguments": {"camera_name": "front"}, "reason": "后置独立任务"}],
            },
        ],
    }
    split_wait_initial = split_wait_orchestrator.handle_voice_decision(split_wait_decision, execute=True, dry_run=True, enqueue=True)
    split_wait_ids = split_wait_initial.get("task_group_ids", [])
    split_wait_groups = [split_wait_orchestrator.store.load_task_group(task_group_id) for task_group_id in split_wait_ids]
    split_wait_session = split_wait_orchestrator.store.load_session(split_wait_initial.get("session"))
    split_wait_state = split_wait_orchestrator.store.load_state()
    require(
        "top_level_ask_user_executes_ready_group_and_waits_later_group",
        len(split_wait_groups) == 3
        and [item.get("task_group_id") for item in split_wait_initial.get("execution", {}).get("executed", [])] == [split_wait_groups[0].task_group_id]
        and split_wait_groups[0].status == TaskStatus.COMPLETED.value
        and split_wait_groups[1].status == TaskStatus.NEEDS_INFO.value
        and split_wait_groups[2].status == TaskStatus.NEW.value
        and split_wait_groups[1].followups
        and not split_wait_groups[1].followups[-1].get("answer")
        and split_wait_session.status == SessionStatus.WAITING_USER.value
        and split_wait_state.get("active_task_group_id") is None
        and not split_wait_state.get("task_queue"),
        {
            "result": split_wait_initial,
            "statuses": [group.status for group in split_wait_groups],
            "followups": split_wait_groups[1].followups if len(split_wait_groups) > 1 else [],
            "state": split_wait_state,
        },
    )
    split_wait_followup = split_wait_orchestrator.answer_followup_decision(
        split_wait_groups[1].task_group_id,
        {
            "ok": True,
            "decision_type": "followup_text",
            "followup_text": "做深蹲，就在这里，不用投影",
            "user_text": "做深蹲，就在这里，不用投影",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.9,
        },
        execute=True,
        dry_run=True,
        enqueue=True,
    )
    split_wait_done = [split_wait_orchestrator.store.load_task_group(task_group_id) for task_group_id in split_wait_ids]
    split_wait_done_steps = [[step.skill_name for step in task_group.steps] for task_group in split_wait_done]
    split_wait_done_session = split_wait_orchestrator.store.load_session(split_wait_initial.get("session"))
    require(
        "top_level_ask_user_followup_completes_waiting_group_and_continues_later_group",
        split_wait_followup.get("ok") is True
        and split_wait_followup.get("task_group_id") == split_wait_groups[1].task_group_id
        and [item.get("task_group_id") for item in split_wait_followup.get("execution", {}).get("executed", [])]
        == [split_wait_groups[1].task_group_id, split_wait_groups[2].task_group_id]
        and [group.status for group in split_wait_done] == [TaskStatus.COMPLETED.value, TaskStatus.COMPLETED.value, TaskStatus.COMPLETED.value]
        and split_wait_done_steps == [["move_forward"], ["head_control", "environment_perception", "squat"], ["head_control", "camera_capture"]]
        and split_wait_done[1].slots.get("exercise_type") == "squat"
        and split_wait_done[1].slots.get("where") == "here"
        and split_wait_done[1].slots.get("projector") is False
        and split_wait_done_session.status == SessionStatus.COMPLETED.value
        and all(task_group.history_refs for task_group in split_wait_done),
        {
            "result": split_wait_followup,
            "statuses": [group.status for group in split_wait_done],
            "steps": split_wait_done_steps,
            "slots": split_wait_done[1].slots if len(split_wait_done) > 1 else {},
            "session_status": split_wait_done_session.status,
            "history_refs": [task_group.history_refs for task_group in split_wait_done],
        },
    )

    orphan_ask_orchestrator = RobotOrchestrator(cfg)
    orphan_ask_orchestrator.audio.speak_text = lambda _text: True
    orphan_ask_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": "你想做什么运动？",
        "confidence": 0.9,
        "user_text": "请你先往前走五秒，然后我想做运动",
        "ask_user": {
            "task_title": "运动",
            "question": "你想做什么运动？深蹲、俯卧撑还是引体向上？",
            "missing_slots": ["exercise_type"],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
        },
        "task_groups": [
            {
                "title": "前进五秒",
                "user_instruction": "请你先往前走五秒",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "move_forward", "arguments": {"duration": 5.0}, "reason": "前置独立移动任务"}],
            }
        ],
    }
    orphan_ask_initial = orphan_ask_orchestrator.handle_voice_decision(orphan_ask_decision, execute=True, dry_run=True, enqueue=True)
    orphan_ask_groups = [orphan_ask_orchestrator.store.load_task_group(task_group_id) for task_group_id in orphan_ask_initial.get("task_group_ids", [])]
    orphan_ask_session = orphan_ask_orchestrator.store.load_session(orphan_ask_initial.get("session"))
    orphan_ask_waiting = [group for group in orphan_ask_groups if group.status == TaskStatus.NEEDS_INFO.value]
    require(
        "ask_user_without_matching_group_creates_waiting_taskgroup",
        len(orphan_ask_groups) == 2
        and [item.get("task_group_id") for item in orphan_ask_initial.get("execution", {}).get("executed", [])] == [orphan_ask_groups[0].task_group_id]
        and orphan_ask_groups[0].status == TaskStatus.COMPLETED.value
        and len(orphan_ask_waiting) == 1
        and orphan_ask_waiting[0].title == "运动"
        and orphan_ask_waiting[0].followups
        and not orphan_ask_waiting[0].followups[-1].get("answer")
        and orphan_ask_session.status == SessionStatus.WAITING_USER.value,
        {"result": orphan_ask_initial, "statuses": [group.status for group in orphan_ask_groups], "groups": [to_dict(group) for group in orphan_ask_groups], "session": to_dict(orphan_ask_session)},
    )
    orphan_followup = orphan_ask_orchestrator.answer_followup(orphan_ask_waiting[0].task_group_id, "做深蹲，就在这里，不用投影", execute=True, dry_run=True, enqueue=True)
    orphan_done_groups = [orphan_ask_orchestrator.store.load_task_group(task_group_id) for task_group_id in orphan_ask_initial.get("task_group_ids", [])]
    require(
        "synthetic_waiting_taskgroup_accepts_followup_and_completes",
        orphan_followup.get("ok") is True
        and orphan_done_groups[0].status == TaskStatus.COMPLETED.value
        and orphan_done_groups[1].status == TaskStatus.COMPLETED.value
        and [step.skill_name for step in orphan_done_groups[1].steps] == ["head_control", "environment_perception", "squat"]
        and orphan_done_groups[1].slots.get("exercise_type") == "squat"
        and orphan_done_groups[1].slots.get("where") == "here"
        and orphan_done_groups[1].slots.get("projector") is False,
        {"followup": orphan_followup, "groups": [to_dict(group) for group in orphan_done_groups]},
    )

    retarget_ask_orchestrator = RobotOrchestrator(cfg)
    retarget_ask_orchestrator.audio.speak_text = lambda _text: True
    retarget_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": "你想做什么运动？",
        "confidence": 0.9,
        "user_text": "请你先往前走五秒，然后我想做运动",
        "ask_user": {
            "task_title": "运动",
            "question": "你想做什么运动？深蹲、俯卧撑还是引体向上？",
            "missing_slots": ["exercise_type"],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
        },
        "task_groups": [
            {
                "title": "前进五秒",
                "user_instruction": "请你先往前走五秒",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "move_forward", "arguments": {"duration": 5.0}, "reason": "前置独立移动任务"}],
            },
            {
                "title": "开始锻炼",
                "user_instruction": "我想做运动",
                "slots": {},
                "followups": [],
                "steps": [],
            },
        ],
    }
    retarget_initial = retarget_ask_orchestrator.handle_voice_decision(retarget_decision, execute=True, dry_run=True, enqueue=True)
    retarget_groups = [retarget_ask_orchestrator.store.load_task_group(task_group_id) for task_group_id in retarget_initial.get("task_group_ids", [])]
    retarget_waiting = [group for group in retarget_groups if group.status == TaskStatus.NEEDS_INFO.value]
    require(
        "ask_user_mismatched_title_retargets_existing_fitness_group",
        len(retarget_groups) == 2
        and len(retarget_waiting) == 1
        and retarget_waiting[0].title == "开始锻炼"
        and retarget_waiting[0].followups[-1].get("task_title") == "开始锻炼"
        and retarget_initial.get("decision", {}).get("ask_user", {}).get("task_title") == "开始锻炼"
        and retarget_groups[0].status == TaskStatus.COMPLETED.value,
        {"result": retarget_initial, "groups": [to_dict(group) for group in retarget_groups]},
    )
    retarget_ask_orchestrator.store.clear_waiting_followups("self_check_after_retarget_ask_user")

    failed_prefix_ask_orchestrator = RobotOrchestrator(cfg)
    failed_prefix_spoken: list[str] = []
    failed_prefix_ask_orchestrator.audio.speak_text = lambda text: failed_prefix_spoken.append(str(text)) or True
    failed_prefix_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": "你想做什么运动？",
        "confidence": 0.9,
        "user_text": "请你先执行一个不可用动作，然后我想做运动",
        "ask_user": {
            "task_title": "运动",
            "question": "你想做什么运动？深蹲、俯卧撑还是引体向上？",
            "missing_slots": ["exercise_type"],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
        },
        "task_groups": [
            {
                "title": "不可用动作",
                "user_instruction": "先执行一个不可用动作",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "self_check_unknown_skill", "arguments": {}, "reason": "force prefix failure"}],
            },
            {
                "title": "运动",
                "user_instruction": "我想做运动",
                "slots": {},
                "followups": [],
                "steps": [],
            },
        ],
    }
    failed_prefix_result = failed_prefix_ask_orchestrator.handle_voice_decision(failed_prefix_decision, execute=True, dry_run=True, enqueue=True)
    failed_prefix_groups = [failed_prefix_ask_orchestrator.store.load_task_group(task_group_id) for task_group_id in failed_prefix_result.get("task_group_ids", [])]
    failed_prefix_session = failed_prefix_ask_orchestrator.store.load_session(failed_prefix_result.get("session"))
    failed_prefix_runtime = failed_prefix_ask_orchestrator.store.validate_runtime_state()
    require(
        "ask_user_prefix_failure_drops_unbound_followup",
        failed_prefix_result.get("decision", {}).get("ask_user") is None
        and failed_prefix_result.get("decision", {}).get("dropped_ask_user", {}).get("reason") == "no_waiting_task_group_after_execution"
        and [item.get("status") for item in failed_prefix_result.get("execution", {}).get("executed", [])] == [TaskStatus.FAILED.value]
        and [group.status for group in failed_prefix_groups] == [TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]
        and failed_prefix_session.status == SessionStatus.COMPLETED.value
        and failed_prefix_runtime.get("ok") is True
        and "你想做什么运动？深蹲、俯卧撑还是引体向上？" not in failed_prefix_spoken,
        {
            "result": failed_prefix_result,
            "groups": [to_dict(group) for group in failed_prefix_groups],
            "session": to_dict(failed_prefix_session),
            "runtime": failed_prefix_runtime,
            "spoken": failed_prefix_spoken,
        },
    )

    invariant_probe_failures: list[dict[str, Any]] = []
    invariant_probe_cases = [
        ("move_forward", "运动", "运动"),
        ("move_forward", "运动", "开始锻炼"),
        ("move_forward", "不存在", "开始锻炼"),
        ("move_forward", "不存在", "拍照"),
        ("self_check_unknown_skill", "运动", "运动"),
        ("self_check_unknown_skill", "不存在", "拍照"),
    ]
    for prefix_skill, ask_title, target_title in invariant_probe_cases:
        probe_orchestrator = RobotOrchestrator(cfg)
        probe_orchestrator.audio.speak_text = lambda _text: True
        probe_result = probe_orchestrator.handle_voice_decision(
            {
                "ok": True,
                "decision_type": "ask_user",
                "reply": "请补充信息",
                "confidence": 0.9,
                "user_text": "复合任务状态一致性探针",
                "ask_user": {
                    "task_title": ask_title,
                    "question": "你想做什么运动？",
                    "missing_slots": ["exercise_type"],
                    "optional_slots": ["projector_control"],
                    "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
                },
                "task_groups": [
                    {
                        "title": "前置任务",
                        "user_instruction": "先执行前置任务",
                        "slots": {},
                        "followups": [],
                        "steps": [{"skill_name": prefix_skill, "arguments": {}, "reason": "state invariant probe"}],
                    },
                    {
                        "title": target_title,
                        "user_instruction": "我想做运动" if target_title != "拍照" else "拍一张照片",
                        "slots": {},
                        "followups": [],
                        "steps": [],
                    },
                ],
            },
            execute=True,
            dry_run=True,
            enqueue=True,
        )
        probe_runtime = probe_orchestrator.store.validate_runtime_state()
        if probe_runtime.get("ok") is not True:
            invariant_probe_failures.append(
                {
                    "prefix_skill": prefix_skill,
                    "ask_title": ask_title,
                    "target_title": target_title,
                    "result": probe_result,
                    "runtime": probe_runtime,
                }
            )
        probe_orchestrator.store.clear_waiting_followups("self_check_after_invariant_probe")
    require(
        "top_level_ask_user_runtime_invariants_hold_across_prefix_outcomes",
        invariant_probe_failures == [],
        invariant_probe_failures,
    )

    non_json_pet = doubao_parser._parse_decision("好的，我来找小狗。", "command")
    non_json_pet_groups = non_json_pet.get("task_groups") or []
    non_json_pet_steps = [
        step
        for group in non_json_pet_groups
        if isinstance(group, dict)
        for step in (group.get("steps") or [])
        if isinstance(step, dict)
    ]
    require(
        "doubao_non_json_pet_find_route_without_head",
        len(non_json_pet_groups) == 1
        and [step.get("skill_name") for step in non_json_pet_steps] == ["pet_tracking"]
        and (non_json_pet_steps[0].get("arguments") or {}).get("action") == "find_route"
        and (non_json_pet_steps[0].get("arguments") or {}).get("pet") == "dog",
        {"decision": non_json_pet, "steps": non_json_pet_steps},
    )

    pet_plan_orchestrator = RobotOrchestrator(cfg)
    pet_decision = {
        "decision_type": "task_plan",
        "reply": "",
        "ask_user": None,
        "confidence": 1.0,
        "user_text": "请你看看小狗在哪",
        "task_groups": [
            {
                "title": "找小狗",
                "user_instruction": "请你看看小狗在哪",
                "slots": {},
                "followups": [],
                "steps": [
                    {
                        "skill_name": "pet_tracking",
                        "arguments": {"action": "find", "pet": "dog"},
                        "reason": "find dog",
                    }
                ],
            }
        ],
    }
    pet_processed = pet_plan_orchestrator.planner._postprocess_decision(pet_decision, "请你看看小狗在哪")
    pet_steps = (pet_processed.get("task_groups") or [{}])[0].get("steps") or []
    pet_skills = [step.get("skill_name") for step in pet_steps]
    pet_step = next((step for step in pet_steps if step.get("skill_name") == "pet_tracking"), {})
    require(
        "pet_find_route_does_not_raise_head",
        "head_control" not in pet_skills
        and pet_step.get("arguments", {}).get("action") == "find_route"
        and pet_step.get("arguments", {}).get("track_after_found") is True,
        {"steps": pet_steps},
    )

    neutralize_orchestrator = RobotOrchestrator(cfg)
    neutralize_calls: list[dict[str, Any]] = []

    def fake_neutralize_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        neutralize_calls.append({"skill_name": step.skill_name, "arguments": dict(step.arguments or {}), "dry_run": dry_run})
        return {"ok": True, "skill_name": step.skill_name, "arguments": dict(step.arguments or {})}

    neutralize_orchestrator.executor.execute_maintenance_step = fake_neutralize_step  # type: ignore[method-assign]
    neutralize_task = TaskGroup(
        title="interrupted fitness",
        slots={"exercise_type": "squat", "projector": True},
        status=TaskStatus.INTERRUPTED.value,
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
        resume_context={
            "active_skill": "squat",
            "active_step_id": "",
            "can_resume": True,
            "robot_state_before_interrupt": {
                "head": {"valid": True, "action": "up", "angle": 208},
                "peripherals": {"projector": {"action": "internal_on"}},
                "pose": {"valid": False},
            },
            "interrupt_ownership": {
                "head": True,
                "head_action": "up",
                "projector": True,
                "projector_action": "internal_on",
                "active_skill": "squat",
            },
        },
    )
    neutralize_task.resume_context["active_step_id"] = neutralize_task.steps[2].step_id
    neutralize_orchestrator.store.save_task_group(neutralize_task)
    neutralized = neutralize_orchestrator.neutralize_interrupted_task_for_new_session(
        {"interrupted": True, "task_group_id": neutralize_task.task_group_id},
        dry_run=False,
        fast_snapshot=True,
    )
    require(
        "fitness_interrupt_neutralizes_head_and_projector_before_new_task",
        neutralized.get("ok") is True
        and [(item["skill_name"], item["arguments"].get("action")) for item in neutralize_calls]
        == [("head_control", "level"), ("projector_control", "off")],
        {"neutralized": neutralized, "calls": neutralize_calls},
    )

    pause_orchestrator = RobotOrchestrator(cfg)
    pause_session = CommandSession(session_type="self_check")
    pause_first = TaskGroup(command_session_id=pause_session.session_id, title="runtime ask", user_instruction="runtime ask", steps=[TaskStep(skill_name="environment_perception", arguments={}, reason="runtime ask")])
    pause_after = TaskGroup(command_session_id=pause_session.session_id, title="after runtime ask", user_instruction="after", steps=[TaskStep(skill_name="move_backward", arguments={}, reason="after")])
    pause_session.task_group_ids = [pause_first.task_group_id, pause_after.task_group_id]
    pause_orchestrator.store.save_session(pause_session)
    pause_orchestrator.store.save_task_group(pause_first)
    pause_orchestrator.store.save_task_group(pause_after)
    pause_orchestrator._enqueue_ready_session_task_groups(pause_session)

    def fake_needs_info(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        if task_group.task_group_id == pause_first.task_group_id:
            task_group.status = TaskStatus.NEEDS_INFO.value
            task_group.result_summary = "needs_environment_override"
            task_group.metadata["runtime_ask_user"] = {
                "task_title": task_group.title,
                "question": "runtime question",
                "missing_slots": ["environment_override"],
                "optional_slots": [],
                "candidate_skills": [],
            }
        return task_group

    pause_orchestrator.executor.execute_task_group = fake_needs_info
    paused = pause_orchestrator.drain_session_queue(pause_session, dry_run=True)
    pause_after_loaded = pause_orchestrator.store.load_task_group(pause_after.task_group_id)
    require("runtime_followup_pauses_later_groups", [item["task_group_id"] for item in paused.get("executed", [])] == [pause_first.task_group_id] and pause_after_loaded.status == TaskStatus.NEW.value, {"execution": paused, "after": pause_after_loaded.status})
    require("runtime_followup_clears_queue", pause_orchestrator.store.load_state().get("task_queue") == [], pause_orchestrator.store.load_state())

    cleanup_stop_orchestrator = RobotOrchestrator(cfg)
    cleanup_stop_orchestrator.audio.speak_text = lambda _text: None
    cleanup_stop_session = CommandSession(session_type="self_check")
    cleanup_stop_first = TaskGroup(command_session_id=cleanup_stop_session.session_id, title="cleanup failed", user_instruction="cleanup failed", steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, reason="cleanup failed")])
    cleanup_stop_after = TaskGroup(command_session_id=cleanup_stop_session.session_id, title="must pause", user_instruction="after", steps=[TaskStep(skill_name="move_backward", arguments={}, reason="must pause")])
    cleanup_stop_session.task_group_ids = [cleanup_stop_first.task_group_id, cleanup_stop_after.task_group_id]
    cleanup_stop_orchestrator.store.save_session(cleanup_stop_session)
    cleanup_stop_orchestrator.store.save_task_group(cleanup_stop_first)
    cleanup_stop_orchestrator.store.save_task_group(cleanup_stop_after)
    cleanup_stop_orchestrator._enqueue_ready_session_task_groups(cleanup_stop_session)

    def fake_cleanup_failed_group(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "completed"
        if task_group.task_group_id == cleanup_stop_first.task_group_id:
            task_group.metadata["cleanup_failed"] = True
            task_group.metadata["cleanup_failed_finalizers"] = [{"skill_name": "projector_control", "error": "projector_off_failed"}]
        return task_group

    cleanup_stop_orchestrator.executor.execute_task_group = fake_cleanup_failed_group
    cleanup_stopped = cleanup_stop_orchestrator.drain_session_queue(cleanup_stop_session, dry_run=True)
    cleanup_after_loaded = cleanup_stop_orchestrator.store.load_task_group(cleanup_stop_after.task_group_id)
    require(
        "cleanup_failure_stops_later_groups",
        [item["task_group_id"] for item in cleanup_stopped.get("executed", [])] == [cleanup_stop_first.task_group_id]
        and cleanup_stopped["executed"][0].get("cleanup_failed") is True
        and cleanup_after_loaded.status == TaskStatus.NEW.value
        and cleanup_stop_orchestrator.store.load_state().get("task_queue") == [],
        {"execution": cleanup_stopped, "after": cleanup_after_loaded.status, "state": cleanup_stop_orchestrator.store.load_state()},
    )

    route_orchestrator = RobotOrchestrator(cfg)
    route_orchestrator.audio.speak_text = lambda _text: True
    route_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    route_question = "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。"
    route_task = TaskGroup(
        command_session_id=route_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        slots={"exercise_type": "squat", "where": "here", "projector": True},
        followups=[
            {
                "question": route_question,
                "task_title": "运动",
                "missing_slots": ["environment_override"],
                "optional_slots": ["where", "projector_control"],
                "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="environment_perception", arguments={"camera": "both", "purpose": "fitness_projection", "exercise_type": "squat"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.NEW.value, order=2),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value, order=3),
        ],
        metadata={
            "waiting_environment_override": {
                "ask_user": {
                    "question": route_question,
                    "task_title": "运动",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                }
            }
        },
    )
    route_session.task_group_ids = [route_task.task_group_id]
    route_orchestrator.store.save_session(route_session)
    route_orchestrator.store.save_task_group(route_task)
    routed = route_orchestrator.handle_voice_decision(
        {"ok": True, "decision_type": "answer", "user_text": "就在这里继续吧", "reply": "", "task_groups": []},
        execute=True,
        dry_run=True,
    )
    routed_loaded = route_orchestrator.store.load_task_group(route_task.task_group_id)
    routed_session = route_orchestrator.store.load_session(routed["session"])
    routed_finalizers = [(item.get("skill_name"), (item.get("arguments") or {}).get("action")) for item in routed_loaded.metadata.get("finalizers", [])]
    require(
        "command_session_routes_pending_environment_followup",
        bool(routed.get("routed_pending_followup"))
        and routed_loaded.status == TaskStatus.COMPLETED.value
        and routed_finalizers == [("projector_control", "off"), ("head_control", "level")],
        {"routed": routed.get("routed_pending_followup"), "status": routed_loaded.status, "finalizers": routed_finalizers},
    )
    require(
        "routed_followup_keeps_current_command_session",
        routed.get("task_group_session") == route_session.session_id
        and routed_session.session_id != route_session.session_id
        and routed_session.task_group_ids == [route_task.task_group_id]
        and routed_session.metadata.get("routed_to_existing_task_group") is True
        and routed_session.metadata.get("routed_task_group_id") == route_task.task_group_id
        and any(item.get("kind") == "routed_followup_answer" for item in routed_session.utterances),
        {
            "returned_session": routed.get("session"),
            "task_group_session": routed.get("task_group_session"),
            "session_metadata": routed_session.metadata,
            "session_task_groups": routed_session.task_group_ids,
            "utterances": routed_session.utterances,
        },
    )
    manual_route_orchestrator = RobotOrchestrator(cfg)
    manual_route_orchestrator.audio.speak_text = lambda _text: True
    manual_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    manual_task = TaskGroup(
        command_session_id=manual_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        slots={"exercise_type": "squat", "where": "here", "projector": False},
        followups=[
            {
                "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                "task_title": "运动",
                "missing_slots": ["environment_override"],
                "optional_slots": ["where", "projector_control"],
                "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        metadata={
            "waiting_environment_override": {
                "ask_user": {
                    "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                    "task_title": "运动",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                }
            }
        },
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="environment_perception", arguments={"camera": "both", "purpose": "fitness_projection", "exercise_type": "squat"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value, order=2),
        ],
    )
    manual_session.task_group_ids = [manual_task.task_group_id]
    manual_route_orchestrator.store.save_session(manual_session)
    manual_route_orchestrator.store.save_task_group(manual_task)
    manual_routed = manual_route_orchestrator.handle_text("就在这里继续吧", execute=True, dry_run=True)
    manual_loaded = manual_route_orchestrator.store.load_task_group(manual_task.task_group_id)
    require(
        "manual_text_routes_pending_environment_followup",
        bool(manual_routed.get("routed_pending_followup"))
        and manual_loaded.status == TaskStatus.COMPLETED.value
        and manual_routed.get("task_group_session") == manual_session.session_id,
        {"routed": manual_routed.get("routed_pending_followup"), "status": manual_loaded.status, "session": manual_routed.get("session")},
    )
    assistant_prose_orchestrator = RobotOrchestrator(cfg)
    assistant_prose_orchestrator.audio.speak_text = lambda _text: True

    class _FakeFollowupVoice:
        def decide_once(self, seconds: float | None = None, mode: str = "followup", context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"ok": True, "text": "好嘞，那咱们就在这里继续"}

    assistant_prose_orchestrator.realtime_voice = _FakeFollowupVoice()
    assistant_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    assistant_task = TaskGroup(
        command_session_id=assistant_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        slots={"exercise_type": "squat", "where": "here", "projector": False},
        followups=[
            {
                "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                "task_title": "运动",
                "missing_slots": ["environment_override"],
                "optional_slots": ["where", "projector_control"],
                "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        metadata={
            "waiting_environment_override": {
                "ask_user": {
                    "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                    "task_title": "运动",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                }
            }
        },
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="environment_perception", arguments={"camera": "both", "purpose": "fitness_projection", "exercise_type": "squat"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value, order=2),
        ],
    )
    assistant_session.task_group_ids = [assistant_task.task_group_id]
    assistant_prose_orchestrator.store.save_session(assistant_session)
    assistant_prose_orchestrator.store.save_task_group(assistant_task)
    assistant_result = assistant_prose_orchestrator.handle_followup_voice(assistant_task.task_group_id, execute=True, dry_run=True)
    assistant_loaded = assistant_prose_orchestrator.store.load_task_group(assistant_task.task_group_id)
    require(
        "assistant_prose_continue_is_rejected_without_polluting_environment_followup",
        assistant_result.get("ok") is False
        and assistant_result.get("error") == "assistant_followup_prose"
        and assistant_loaded.status == TaskStatus.NEEDS_INFO.value
        and not any(item.get("answer") for item in assistant_loaded.followups)
        and assistant_prose_orchestrator._classify_environment_override_reply("好嘞，那咱们就在这里继续") == "planner",
        {"result": assistant_result, "status": assistant_loaded.status, "followups": assistant_loaded.followups},
    )
    assistant_loaded.status = TaskStatus.CANCELLED.value
    assistant_prose_orchestrator.store.save_task_group(assistant_loaded)
    assistant_session.status = SessionStatus.COMPLETED.value
    assistant_session.ended_at = time.time()
    assistant_prose_orchestrator.store.save_session(assistant_session)

    robot_prompt_orchestrator = RobotOrchestrator(cfg)
    robot_prompt_orchestrator.audio.speak_text = lambda _text: True

    class _RobotPromptFollowupVoice:
        def decide_once(self, seconds: float | None = None, mode: str = "followup", context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "ok": True,
                "decision_type": "followup_text",
                "text": "我得先看看这里的环境是否适合运动和投影，我这就调用前摄和后摄进行感知。",
                "user_text": "我得先看看这里的环境是否适合运动和投影，我这就调用前摄和后摄进行感知。",
                "followup_text": "我得先看看这里的环境是否适合运动和投影，我这就调用前摄和后摄进行感知。",
                "task_groups": [],
                "ask_user": None,
            }

    robot_prompt_orchestrator.realtime_voice = _RobotPromptFollowupVoice()
    robot_prompt_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    robot_prompt_task = TaskGroup(
        command_session_id=robot_prompt_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        slots={"exercise_type": "squat", "where": "here", "projector": True},
        followups=[
            {
                "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                "task_title": "运动",
                "missing_slots": ["environment_override"],
                "optional_slots": ["where", "projector_control"],
                "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        metadata={
            "waiting_environment_override": {
                "ask_user": {
                    "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                    "task_title": "运动",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat", "push_up", "pull_up"],
                }
            }
        },
    )
    robot_prompt_session.task_group_ids = [robot_prompt_task.task_group_id]
    robot_prompt_orchestrator.store.save_session(robot_prompt_session)
    robot_prompt_orchestrator.store.save_task_group(robot_prompt_task)
    rejected_robot_prompt = robot_prompt_orchestrator.handle_followup_voice(robot_prompt_task.task_group_id, execute=True, dry_run=True)
    rejected_robot_prompt_loaded = robot_prompt_orchestrator.store.load_task_group(robot_prompt_task.task_group_id)
    require(
        "robot_followup_prompt_is_rejected_without_polluting_task",
        rejected_robot_prompt.get("ok") is False
        and rejected_robot_prompt.get("error") == "assistant_followup_prose"
        and rejected_robot_prompt_loaded.status == TaskStatus.NEEDS_INFO.value
        and not any(item.get("answer") for item in rejected_robot_prompt_loaded.followups),
        {"result": rejected_robot_prompt, "status": rejected_robot_prompt_loaded.status, "followups": rejected_robot_prompt_loaded.followups},
    )
    rejected_robot_prompt_loaded.status = TaskStatus.CANCELLED.value
    robot_prompt_orchestrator.store.save_task_group(rejected_robot_prompt_loaded)
    robot_prompt_session.status = SessionStatus.COMPLETED.value
    robot_prompt_session.ended_at = time.time()
    robot_prompt_orchestrator.store.save_session(robot_prompt_session)
    projector_followup = {
        "question": "需要我打开投影辅助训练吗？",
        "optional_slots": ["projector_control"],
        "candidate_skills": ["projector_control"],
    }
    robot_projector_prompt = "请问你需要我用投影仪显示动作指导吗？"
    require(
        "robot_projector_question_is_not_user_projector_consent",
        robot_prompt_orchestrator._classify_projector_followup_reply(projector_followup, robot_projector_prompt) is None
        and robot_prompt_orchestrator._classify_environment_override_reply("好的，那我们先确认一下运动环境是否安全。") == "planner",
        {
            "projector": robot_prompt_orchestrator._classify_projector_followup_reply(projector_followup, robot_projector_prompt),
            "environment": robot_prompt_orchestrator._classify_environment_override_reply("好的，那我们先确认一下运动环境是否安全。"),
        },
    )

    route_fitness_orchestrator = RobotOrchestrator(cfg)
    route_fitness_orchestrator.audio.speak_text = lambda _text: True
    fitness_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    fitness_question = "你想做什么运动？深蹲、俯卧撑还是引体向上？另外，是在这里做，还是去某个已保存的地点做？需要我打开投影辅助训练吗？"
    fitness_task = TaskGroup(
        command_session_id=fitness_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": fitness_question,
                "task_title": "运动",
                "missing_slots": ["exercise_type", "where"],
                "optional_slots": ["projector_control"],
                "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
                "timestamp": 1,
            }
        ],
    )
    fitness_session.task_group_ids = [fitness_task.task_group_id]
    route_fitness_orchestrator.store.save_session(fitness_session)
    route_fitness_orchestrator.store.save_task_group(fitness_task)
    routed_fitness = route_fitness_orchestrator.handle_voice_decision(
        {"ok": True, "decision_type": "answer", "user_text": "我想做深蹲，就在这里做，并且打开投影", "reply": "", "task_groups": []},
        execute=True,
        dry_run=True,
    )
    routed_fitness_loaded = route_fitness_orchestrator.store.load_task_group(fitness_task.task_group_id)
    require(
        "command_session_routes_pending_fitness_followup",
        bool(routed_fitness.get("routed_pending_followup"))
        and routed_fitness_loaded.status in {TaskStatus.COMPLETED.value, TaskStatus.NEEDS_INFO.value}
        and routed_fitness_loaded.slots.get("exercise_type") == "squat"
        and routed_fitness_loaded.slots.get("where") == "here"
        and routed_fitness_loaded.slots.get("projector") is True
        and any(step.skill_name == "squat" for step in routed_fitness_loaded.steps),
        {"routed": routed_fitness.get("routed_pending_followup"), "status": routed_fitness_loaded.status, "slots": routed_fitness_loaded.slots, "steps": [step.skill_name for step in routed_fitness_loaded.steps]},
    )

    negative_orchestrator = RobotOrchestrator(cfg)
    negative_orchestrator.audio.speak_text = lambda _text: True
    negative_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    negative_task = TaskGroup(
        command_session_id=negative_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": fitness_question,
                "task_title": "运动",
                "missing_slots": ["exercise_type", "where"],
                "optional_slots": ["projector_control"],
                "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
                "timestamp": 1,
            }
        ],
    )
    negative_session.task_group_ids = [negative_task.task_group_id]
    negative_orchestrator.store.save_session(negative_session)
    negative_orchestrator.store.save_task_group(negative_task)
    pet_decision = {
        "ok": True,
        "decision_type": "task_plan",
        "user_text": "请你去找一下小狗在哪里",
        "reply": "好的，我去找小狗。",
        "ask_user": None,
        "confidence": 0.95,
        "task_groups": [
            {
                "title": "找小狗",
                "user_instruction": "请你去找一下小狗在哪里",
                "slots": {"pet": "dog"},
                "followups": [],
                "steps": [
                    {
                        "skill_name": "pet_tracking",
                        "arguments": {"action": "find_route", "pet": "dog", "track_after_found": True},
                        "reason": "用户要求找小狗，这是新的突发任务，不是运动追问回答。",
                    }
                ],
            }
        ],
    }
    pet_result = negative_orchestrator.handle_voice_decision(pet_decision, execute=False, dry_run=True)
    negative_loaded = negative_orchestrator.store.load_task_group(negative_task.task_group_id)
    pet_group = negative_orchestrator.store.load_task_group(pet_result["task_group_ids"][0])
    require(
        "new_pet_command_not_routed_to_pending_fitness_followup",
        not pet_result.get("routed_pending_followup")
        and negative_loaded.status == TaskStatus.NEEDS_INFO.value
        and pet_group.task_group_id != negative_task.task_group_id
        and any(step.skill_name == "pet_tracking" for step in pet_group.steps),
        {
            "routed": pet_result.get("routed_pending_followup"),
            "pending_status": negative_loaded.status,
            "new_task_group_ids": pet_result.get("task_group_ids"),
            "pet_steps": [step.skill_name for step in pet_group.steps],
        },
    )

    barge_pet_orchestrator = RobotOrchestrator(cfg)
    barge_pet_orchestrator.audio.speak_text = lambda _text: True
    barge_neutralize_calls: list[dict[str, Any]] = []
    barge_execute_calls: list[list[str]] = []

    def fake_barge_neutralize_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        barge_neutralize_calls.append({"skill_name": step.skill_name, "arguments": dict(step.arguments or {}), "dry_run": dry_run})
        return {"ok": True, "skill_name": step.skill_name, "arguments": dict(step.arguments or {})}

    def fake_barge_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        barge_execute_calls.append([step.skill_name for step in task_group.steps])
        for step in task_group.steps:
            if step.status not in {TaskStatus.CANCELLED.value, TaskStatus.COMPLETED.value}:
                step.status = TaskStatus.COMPLETED.value
                step.result = {"ok": True, "dry_run": dry_run}
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "self_check_completed"
        task_group.ended_at = 1
        return task_group

    barge_pet_orchestrator.executor.execute_maintenance_step = fake_barge_neutralize_step  # type: ignore[method-assign]
    barge_pet_orchestrator.executor.execute_task_group = fake_barge_execute  # type: ignore[method-assign]
    barge_fitness = TaskGroup(
        title="barge interrupted fitness",
        slots={"exercise_type": "squat", "projector": True},
        status=TaskStatus.INTERRUPTED.value,
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
    )
    barge_fitness.resume_context = {
        "active_step_id": barge_fitness.steps[-1].step_id,
        "can_resume": True,
        "interrupt_ownership": {"head": True, "head_action": "up", "projector": True, "projector_action": "internal_on"},
        "robot_state_before_interrupt": {
            "pose": {"valid": True, "x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 208, "action": "up"},
            "peripherals": {"projector": {"action": "internal_on"}},
        },
    }
    barge_pet_orchestrator.store.push_interrupted(barge_fitness)
    barge_neutralized = barge_pet_orchestrator.neutralize_interrupted_task_for_new_session(
        {"interrupted": True, "task_group_id": barge_fitness.task_group_id},
        dry_run=False,
        fast_snapshot=True,
    )
    barge_pet_result = barge_pet_orchestrator.handle_voice_decision(pet_decision, execute=True, dry_run=True, enqueue=True)
    barge_pet_loaded = barge_pet_orchestrator.store.load_task_group(barge_pet_result["task_group_ids"][0])
    barge_pet_steps = [(step.skill_name, dict(step.arguments or {})) for step in barge_pet_loaded.steps]
    require(
        "barge_in_fitness_neutralizes_then_executes_pet_find_route",
        barge_neutralized.get("ok") is True
        and [(item["skill_name"], item["arguments"].get("action")) for item in barge_neutralize_calls] == [("head_control", "level"), ("projector_control", "off")]
        and not barge_pet_result.get("routed_pending_followup")
        and barge_pet_loaded.status == TaskStatus.COMPLETED.value
        and barge_execute_calls == [["pet_tracking"]]
        and barge_pet_steps == [("pet_tracking", {"action": "find_route", "pet": "dog", "track_after_found": True})],
        {
            "neutralized": barge_neutralized,
            "neutralize_calls": barge_neutralize_calls,
            "result": barge_pet_result,
            "pet_steps": barge_pet_steps,
            "execute_calls": barge_execute_calls,
        },
    )
    barge_pet_orchestrator.store.clear_runtime_interrupt_state("self_check_barge_pet_cleanup")

    active_barge_cfg = copy.deepcopy(cfg)
    active_barge_cfg["paths"]["runtime_dir"] = tempfile.mkdtemp(prefix="robot_active_barge_self_check_")
    ensure_runtime_dirs(active_barge_cfg)
    active_barge_orchestrator = RobotOrchestrator(active_barge_cfg)
    active_barge_orchestrator.audio.speak_text = lambda _text: True
    active_barge_orchestrator._start_interrupt_context_finalizer = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    active_barge_neutralize_calls: list[dict[str, Any]] = []
    active_barge_execute_calls: list[list[str]] = []

    def active_barge_snapshot(active_task_group_id: str | None = None, active_step: TaskStep | None = None, fast: bool = False) -> dict[str, Any]:
        return {
            "snapshot_mode": "fast" if fast else "full",
            "pose": {"valid": True, "x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 208, "action": "up"},
            "peripherals": {"projector": {"action": "internal_on"}},
        }

    def active_barge_maintenance_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        active_barge_neutralize_calls.append({"skill_name": step.skill_name, "arguments": dict(step.arguments or {}), "dry_run": dry_run})
        return {"ok": True, "skill_name": step.skill_name, "arguments": dict(step.arguments or {})}

    def active_barge_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        active_barge_execute_calls.append([step.skill_name for step in task_group.steps])
        for step in task_group.steps:
            if step.status != TaskStatus.CANCELLED.value:
                step.status = TaskStatus.COMPLETED.value
                step.result = {"ok": True, "dry_run": dry_run}
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "self_check_completed"
        task_group.ended_at = 1
        return task_group

    active_barge_orchestrator.robot_state.snapshot = active_barge_snapshot
    active_barge_orchestrator.executor.execute_maintenance_step = active_barge_maintenance_step  # type: ignore[method-assign]
    active_barge_orchestrator.executor.execute_task_group = active_barge_execute  # type: ignore[method-assign]
    active_barge_task = TaskGroup(
        title="active running fitness",
        slots={"exercise_type": "squat", "where": "here", "projector": True},
        status=TaskStatus.RUNNING.value,
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.RUNNING.value, order=2),
        ],
    )
    active_barge_orchestrator.store.save_task_group(active_barge_task)
    active_barge_state = active_barge_orchestrator.store.load_state()
    active_barge_state["active_task_group_id"] = active_barge_task.task_group_id
    active_barge_state["task_queue"] = []
    active_barge_orchestrator.store.save_state(active_barge_state)
    active_barge_orchestrator.executor.current_step = active_barge_task.steps[-1]
    active_barge_orchestrator.executor._current_last_progress = {"current_count": 3, "elapsed_seconds": 12}  # type: ignore[attr-defined]
    active_barge_interrupt = active_barge_orchestrator.begin_interrupt_active_task_fast(WakeupEvent(source="self_check", raw_value="1"))
    active_barge_interrupted = active_barge_orchestrator.store.load_task_group(active_barge_task.task_group_id)
    active_barge_neutralized = active_barge_orchestrator.neutralize_interrupted_task_for_new_session(active_barge_interrupt, dry_run=False, fast_snapshot=True)
    active_barge_pet_result = active_barge_orchestrator.handle_voice_decision(pet_decision, execute=True, dry_run=True, enqueue=True)
    active_barge_pet = active_barge_orchestrator.store.load_task_group(active_barge_pet_result["task_group_ids"][0])
    active_barge_runtime = active_barge_orchestrator.store.validate_runtime_state()
    require(
        "active_running_fitness_barge_in_pet_starts_after_neutralize",
        active_barge_interrupt.get("ok") is True
        and active_barge_interrupt.get("interrupted") is True
        and active_barge_interrupted.status == TaskStatus.INTERRUPTED.value
        and active_barge_interrupted.resume_context.get("active_skill") == "squat"
        and ((active_barge_interrupted.resume_context.get("last_progress") or {}).get("current_count") == 3)
        and active_barge_neutralized.get("ok") is True
        and [(item["skill_name"], item["arguments"].get("action")) for item in active_barge_neutralize_calls]
        == [("head_control", "level"), ("projector_control", "off")]
        and active_barge_pet.status == TaskStatus.COMPLETED.value
        and active_barge_execute_calls == [["pet_tracking"]]
        and [step.skill_name for step in active_barge_pet.steps] == ["pet_tracking"]
        and active_barge_runtime.get("ok") is True,
        {
            "interrupt": active_barge_interrupt,
            "interrupted": to_dict(active_barge_interrupted),
            "neutralized": active_barge_neutralized,
            "neutralize_calls": active_barge_neutralize_calls,
            "pet_result": active_barge_pet_result,
            "pet": to_dict(active_barge_pet),
            "execute_calls": active_barge_execute_calls,
            "runtime": active_barge_runtime,
        },
    )
    active_barge_orchestrator.store.clear_runtime_interrupt_state("self_check_active_barge_cleanup")

    resume_orchestrator = RobotOrchestrator(cfg)
    resume_orchestrator.audio.speak_text = lambda _text: None
    text = "继续但是不用回去"
    require(
        "resume_reply_skip_scene_restore",
        resume_orchestrator.classify_resume_reply(text) == "resume" and resume_orchestrator.classify_scene_restore_reply(text) == "skip_restore",
        {"resume": resume_orchestrator.classify_resume_reply(text), "scene": resume_orchestrator.classify_scene_restore_reply(text)},
    )
    projector_step = TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=0)
    squat_step = TaskStep(skill_name="squat", arguments={"action": "run", "duration": 30}, status=TaskStatus.INTERRUPTED.value, order=1)
    group = TaskGroup(title="projector squat", steps=[projector_step, squat_step], status=TaskStatus.INTERRUPTED.value)
    group.slots = {"exercise_type": "squat", "projector": True}
    group.resume_context = {
        "active_step_id": squat_step.step_id,
        "completed_steps": [projector_step.step_id],
        "last_progress": {"payload": {"current_count": 3, "elapsed_seconds": 9}},
        "robot_state_before_interrupt": {
            "pose": {"valid": True, "x": 1, "y": 2, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 185},
            "peripherals": {"projector": {"action": "internal_on", "timestamp": 1}},
        },
    }
    resume_orchestrator.robot_state._read_pose_from_ros = lambda: {"valid": False, "source": "self_check_no_ros"}
    resume_orchestrator.robot_state._save_cache(
        {
            "pose": {"valid": True, "x": 5, "y": 2, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 205},
            "peripherals": {"projector": {"action": "off", "timestamp": 999}},
        }
    )
    resume_orchestrator.store.push_interrupted(group)
    stale_active_step = TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.INTERRUPTED.value, order=0)
    stale_group = TaskGroup(
        title="stale active projector squat",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            stale_active_step,
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
        status=TaskStatus.INTERRUPTED.value,
    )
    resume_orchestrator.robot_state._save_cache({"peripherals": {"projector": {"action": "internal_on", "timestamp": 3}}, "head": {"valid": True, "angle": 205, "action": "up"}})
    ownership = resume_orchestrator._interrupt_task_ownership(stale_group, stale_active_step)
    neutral_steps = resume_orchestrator._neutralization_steps_for_interrupt(stale_group, stale_active_step, ownership)
    require(
        "neutralize_projector_even_with_stale_active_step",
        ownership.get("head") is True and ownership.get("projector") is True and [(step.skill_name, step.arguments.get("action")) for step in neutral_steps] == [("head_control", "level"), ("projector_control", "off")],
        {"ownership": ownership, "steps": [step.__dict__ for step in neutral_steps]},
    )
    neutralized_context_group = TaskGroup(
        title="neutralized resume state priority",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
        status=TaskStatus.INTERRUPTED.value,
    )
    neutralized_context_group.resume_context = {
        "neutralized_for_new_session": True,
        "saved_interrupt_state_for_resume": {
            "head": {"valid": True, "angle": 205},
            "peripherals": {"projector": {"action": "internal_on", "timestamp": 1}},
        },
        "robot_state_full_snapshot": {
            "head": {"valid": True, "angle": 185},
            "peripherals": {"projector": {"action": "off", "timestamp": 2}},
        },
    }
    saved_for_resume = resume_orchestrator._resume_saved_robot_state(neutralized_context_group)
    require(
        "resume_uses_saved_interrupt_state_before_neutralized_snapshot",
        (saved_for_resume.get("head") or {}).get("angle") == 205 and ((saved_for_resume.get("peripherals") or {}).get("projector") or {}).get("action") == "internal_on",
        saved_for_resume,
    )
    inferred_context_group = TaskGroup(
        title="inferred owned resume state",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
        status=TaskStatus.INTERRUPTED.value,
    )
    inferred_context_group.resume_context = {
        "active_step_id": inferred_context_group.steps[-1].step_id,
        "interrupt_ownership": {"head": True, "head_action": "up", "projector": True, "projector_action": "internal_on"},
    }
    inferred_saved = resume_orchestrator._saved_state_for_resume_before_neutralize(inferred_context_group, {"pose": {"valid": True, "x": 0, "y": 0, "yaw": 0}})
    require(
        "resume_state_infers_owned_head_and_projector",
        (inferred_saved.get("head") or {}).get("valid") is True
        and ((inferred_saved.get("peripherals") or {}).get("projector") or {}).get("action") == "internal_on",
        inferred_saved,
    )
    fast_neutralize_orchestrator = RobotOrchestrator(cfg)
    fast_neutralize_task = TaskGroup(
        title="fast neutralize fitness",
        slots={"exercise_type": "squat", "projector": True},
        steps=[
            TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
            TaskStep(skill_name="projector_control", arguments={"action": "internal_on"}, status=TaskStatus.COMPLETED.value, order=1),
            TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=2),
        ],
        status=TaskStatus.INTERRUPTED.value,
    )
    fast_neutralize_task.resume_context = {
        "active_step_id": fast_neutralize_task.steps[-1].step_id,
        "interrupt_ownership": {"head": True, "head_action": "up", "projector": True, "projector_action": "internal_on"},
        "robot_state_before_interrupt": {
            "pose": {"valid": True, "x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 205},
            "peripherals": {"projector": {"action": "internal_on"}},
        },
    }
    fast_neutralize_orchestrator.store.save_task_group(fast_neutralize_task)
    snapshot_fast_args: list[bool] = []

    def fake_snapshot(active_task_group_id: str | None = None, active_step: TaskStep | None = None, fast: bool = False) -> dict[str, Any]:
        snapshot_fast_args.append(bool(fast))
        return {
            "snapshot_mode": "fast" if fast else "full",
            "pose": {"valid": True, "x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 205},
            "peripherals": {"projector": {"action": "internal_on"}},
        }

    fast_neutralize_orchestrator.robot_state.snapshot = fake_snapshot
    fast_neutralized = fast_neutralize_orchestrator.neutralize_interrupted_task_for_new_session(
        {"interrupted": True, "task_group_id": fast_neutralize_task.task_group_id},
        dry_run=True,
        fast_snapshot=True,
    )
    require(
        "barge_in_neutralize_uses_fast_snapshot",
        fast_neutralized.get("ok") is True
        and fast_neutralized.get("fast_snapshot") is True
        and snapshot_fast_args[:1] == [True]
        and [(item.get("skill_name"), (item.get("arguments") or {}).get("action")) for item in fast_neutralized.get("steps", [])]
        == [("head_control", "level"), ("projector_control", "off")],
        {"result": fast_neutralized, "snapshot_fast_args": snapshot_fast_args},
    )
    resume_orchestrator.robot_state._save_cache(
        {
            "pose": {"valid": True, "x": 5, "y": 2, "yaw": 0, "frame_id": "map"},
            "head": {"valid": True, "angle": 205},
            "peripherals": {"projector": {"action": "off", "timestamp": 999}},
        }
    )
    preview = resume_orchestrator.preview_resume_scene_restore()
    preview_steps = [step["skill_name"] for step in preview.get("restore_steps", [])]
    require("resume_preview_restore_chain", preview_steps == ["navigation_goto", "head_control", "projector_control"], preview)
    resume_orchestrator.resume_last_interrupted(execute=True, dry_run=True, restore_scene=True)
    loaded = resume_orchestrator.store.load_task_group(group.task_group_id)
    require("resume_summary_skips_restore_steps", loaded.result_summary == "运动计数完成", loaded.result_summary)
    require("resume_initial_count_kept", loaded.steps[-1].arguments.get("initial_count") == 3, loaded.steps[-1].arguments)
    resume_state_after_success = resume_orchestrator.store.load_state()
    require(
        "resume_success_clears_interrupted_stack_and_active",
        resume_state_after_success.get("interrupted_stack") == []
        and resume_state_after_success.get("active_task_group_id") is None,
        resume_state_after_success,
    )

    resume_prepare_failure_orchestrator = RobotOrchestrator(cfg)
    failing_resume_step = TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.INTERRUPTED.value, order=0)
    failing_resume_group = TaskGroup(
        title="resume prepare failure",
        slots={"exercise_type": "squat"},
        steps=[failing_resume_step],
        status=TaskStatus.INTERRUPTED.value,
        resume_context={"active_step_id": failing_resume_step.step_id, "can_resume": True},
    )
    resume_prepare_failure_orchestrator.store.push_interrupted(failing_resume_group)

    def failing_resume_snapshot(active_task_group_id: str | None = None, active_step: TaskStep | None = None, fast: bool = False) -> dict[str, Any]:
        raise RuntimeError("self_check_resume_snapshot_failed")

    resume_prepare_failure_orchestrator.robot_state.snapshot = failing_resume_snapshot
    resume_prepare_failure = resume_prepare_failure_orchestrator.resume_last_interrupted(execute=True, dry_run=True, restore_scene=True)
    resume_prepare_failure_state = resume_prepare_failure_orchestrator.store.load_state()
    require(
        "resume_prepare_failure_keeps_interrupted_task",
        resume_prepare_failure.get("ok") is False
        and resume_prepare_failure.get("error") == "self_check_resume_snapshot_failed"
        and resume_prepare_failure_state.get("interrupted_stack") == [failing_resume_group.task_group_id]
        and resume_prepare_failure_orchestrator.store.peek_interrupted().task_group_id == failing_resume_group.task_group_id,
        {"result": resume_prepare_failure, "state": resume_prepare_failure_state},
    )

    multi_stack_orchestrator = RobotOrchestrator(cfg)
    multi_stack_orchestrator.audio.speak_text = lambda _text: True
    multi_stack_orchestrator.store.clear_runtime_interrupt_state("self_check_multi_stack_isolation")
    multi_stack_orchestrator.store.clear_task_queue("self_check_multi_stack_isolation")
    older_step = TaskStep(skill_name="move_forward", status=TaskStatus.INTERRUPTED.value, order=0)
    older_interrupted = TaskGroup(
        title="older interrupted",
        steps=[older_step],
        status=TaskStatus.INTERRUPTED.value,
        resume_context={"active_step_id": older_step.step_id, "can_resume": True},
    )
    newer_step = TaskStep(skill_name="move_backward", status=TaskStatus.INTERRUPTED.value, order=0)
    newer_interrupted = TaskGroup(
        title="newer interrupted",
        steps=[newer_step],
        status=TaskStatus.INTERRUPTED.value,
        resume_context={"active_step_id": newer_step.step_id, "can_resume": True},
    )
    multi_stack_orchestrator.store.push_interrupted(older_interrupted)
    multi_stack_orchestrator.store.push_interrupted(newer_interrupted)
    multi_stack_orchestrator.robot_state.snapshot = lambda active_task_group_id=None, active_step=None, fast=False: {
        "pose": {"valid": False, "source": "self_check"},
        "head": {"valid": False},
        "peripherals": {},
    }

    def multi_stack_execute(task_group: TaskGroup, dry_run: bool = True) -> TaskGroup:
        task_group.status = TaskStatus.COMPLETED.value
        task_group.result_summary = "multi stack completed"
        for step in task_group.steps:
            if step.status != TaskStatus.COMPLETED.value:
                step.status = TaskStatus.COMPLETED.value
        return task_group

    multi_stack_orchestrator.executor.execute_task_group = multi_stack_execute  # type: ignore[method-assign]
    multi_stack_resume = multi_stack_orchestrator.resume_last_interrupted(execute=True, dry_run=True, restore_scene=False)
    multi_stack_state = multi_stack_orchestrator.store.load_state()
    require(
        "resume_removes_only_resumed_task_from_interrupted_stack",
        multi_stack_resume.get("ok") is True
        and multi_stack_resume.get("task_group_id") == newer_interrupted.task_group_id
        and multi_stack_state.get("interrupted_stack") == [older_interrupted.task_group_id]
        and multi_stack_orchestrator.store.peek_interrupted().task_group_id == older_interrupted.task_group_id,
        {"result": multi_stack_resume, "state": multi_stack_state},
    )

    cleanup_orchestrator = RobotOrchestrator(cfg)
    cleanup_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    cleanup_task = TaskGroup(
        command_session_id=cleanup_session.session_id,
        title="stale waiting followup",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[{"question": "stale question", "timestamp": 1}],
    )
    cleanup_session.task_group_ids = [cleanup_task.task_group_id]
    cleanup_orchestrator.store.save_session(cleanup_session)
    cleanup_orchestrator.store.save_task_group(cleanup_task)
    cleanup = cleanup_orchestrator.store.clear_waiting_followups("self_check_startup_clear")
    cleanup_loaded = cleanup_orchestrator.store.load_task_group(cleanup_task.task_group_id)
    cleanup_session_loaded = cleanup_orchestrator.store.load_session(cleanup_session.session_id)
    require(
        "startup_clear_waiting_followups",
        cleanup_task.task_group_id in cleanup.get("cleared_task_group_ids", [])
        and cleanup_loaded.status == TaskStatus.CANCELLED.value
        and cleanup_session_loaded.status == SessionStatus.COMPLETED.value,
        {"cleanup": cleanup, "task_status": cleanup_loaded.status, "session_status": cleanup_session_loaded.status},
    )

    from .cli import _complete_followup_flow, _empty_wakeup_voice_result, _followup_voice_with_retries, _heard_is_empty_or_noop_command, _pending_followup_wait_result, _result_is_empty_or_noop_command, _run_wakeup_session, _wait_previous_session_or_force_stop

    class _FollowupVoiceTexts:
        def __init__(self, texts: list[str]) -> None:
            self.texts = list(texts)
            self.calls: list[dict[str, Any]] = []

        def prepare_once(self, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"ok": True, "mode": mode, "prepared": True, "context": context or {}}

        def decide_once(self, seconds: float | None = None, mode: str = "followup", context: dict[str, Any] | None = None) -> dict[str, Any]:
            text = self.texts.pop(0) if self.texts else ""
            self.calls.append({"mode": mode, "text": text, "context": context or {}})
            return {
                "ok": bool(text),
                "decision_type": "followup_text",
                "reply": "",
                "task_groups": [],
                "ask_user": None,
                "confidence": 0.9 if text else 0.0,
                "user_text": text,
                "followup_text": text,
                "text": text,
            }

    def make_environment_waiting_task(orchestrator: RobotOrchestrator) -> tuple[CommandSession, TaskGroup]:
        session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
        task = TaskGroup(
            command_session_id=session.session_id,
            title="运动",
            user_instruction="我想做运动",
            status=TaskStatus.NEEDS_INFO.value,
            slots={"exercise_type": "squat", "where": "here", "projector": True},
            metadata={
                "waiting_environment_override": {
                    "ask_user": {
                        "task_title": "运动",
                        "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                        "missing_slots": ["environment_override"],
                        "optional_slots": ["where", "projector_control"],
                        "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat"],
                    }
                }
            },
            followups=[
                {
                    "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                    "task_title": "运动",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat"],
                    "timestamp": 1,
                    "runtime_followup": "environment_override",
                }
            ],
            steps=[
                TaskStep(skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value, order=0),
                TaskStep(skill_name="environment_perception", arguments={"camera": "both", "purpose": "fitness_projection", "exercise_type": "squat"}, status=TaskStatus.COMPLETED.value, order=1),
                TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value, order=2),
            ],
        )
        session.task_group_ids = [task.task_group_id]
        orchestrator.store.save_session(session)
        orchestrator.store.save_task_group(task)
        return session, task

    retry_followup_orchestrator = RobotOrchestrator(cfg)
    retry_spoken: list[str] = []
    retry_followup_orchestrator.audio.speak_text = lambda text: retry_spoken.append(str(text)) or True
    retry_followup_orchestrator.audio.wait_for_speech_settle = lambda _delay=None: 0.0  # type: ignore[method-assign]
    retry_voice = _FollowupVoiceTexts(["好嘞，那咱们就在这里继续", "就在这里继续"])
    retry_followup_orchestrator.realtime_voice = retry_voice
    _, retry_task = make_environment_waiting_task(retry_followup_orchestrator)
    retry_args = argparse.Namespace(
        seconds=8.0,
        execute=False,
        asr_retries=1,
        followup_echo_retries=0,
        followup_post_speech_listen_delay_seconds=0.0,
        post_speech_listen_delay_seconds=0.0,
    )
    retry_result = _followup_voice_with_retries(retry_followup_orchestrator, retry_task.task_group_id, retry_args)
    retry_loaded = retry_followup_orchestrator.store.load_task_group(retry_task.task_group_id)
    retry_answers = [item.get("answer") for item in retry_loaded.followups if item.get("answer")]
    require(
        "followup_retry_discards_assistant_prose_then_accepts_user_answer",
        retry_result.get("ok") is True
        and len(retry_voice.calls) == 2
        and retry_loaded.status in {TaskStatus.NEW.value, TaskStatus.QUEUED.value}
        and retry_answers == ["就在这里继续"]
        and bool(retry_loaded.metadata.get("environment_override_accepted")),
        {"result": retry_result, "answers": retry_answers, "status": retry_loaded.status, "calls": retry_voice.calls, "spoken": retry_spoken},
    )

    hold_followup_orchestrator = RobotOrchestrator(cfg)
    hold_followup_orchestrator.audio.speak_text = lambda _text: True
    hold_followup_orchestrator.audio.wait_for_speech_settle = lambda _delay=None: 0.0  # type: ignore[method-assign]
    hold_voice = _FollowupVoiceTexts(["好嘞，那咱们就在这里继续"])
    hold_followup_orchestrator.realtime_voice = hold_voice
    _, hold_task = make_environment_waiting_task(hold_followup_orchestrator)
    hold_args = argparse.Namespace(
        seconds=8.0,
        execute=False,
        asr_retries=0,
        followup_echo_retries=0,
        followup_post_speech_listen_delay_seconds=0.0,
        post_speech_listen_delay_seconds=0.0,
    )
    hold_result = _followup_voice_with_retries(hold_followup_orchestrator, hold_task.task_group_id, hold_args)
    hold_loaded = hold_followup_orchestrator.store.load_task_group(hold_task.task_group_id)
    require(
        "followup_assistant_prose_without_retry_keeps_task_waiting",
        hold_result.get("ok") is True
        and hold_result.get("keep_waiting") is True
        and hold_loaded.status == TaskStatus.NEEDS_INFO.value
        and not any(item.get("answer") for item in hold_loaded.followups),
        {"result": hold_result, "status": hold_loaded.status, "followups": hold_loaded.followups, "calls": hold_voice.calls},
    )

    class _FollowupQuestionEchoVoice:
        def __init__(self, question: str) -> None:
            self.question = question
            self.calls: list[dict[str, Any]] = []

        def decide_once(self, seconds: float | None = None, mode: str = "followup", context: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append({"mode": mode, "context": context or {}})
            return {
                "ok": True,
                "decision_type": "ask_user",
                "reply": self.question,
                "user_text": self.question,
                "task_groups": [],
                "ask_user": {
                    "task_title": "运动",
                    "question": self.question,
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat"],
                },
                "confidence": 0.9,
            }

    echo_followup_orchestrator = RobotOrchestrator(cfg)
    echo_followup_orchestrator.audio.speak_text = lambda _text: True
    echo_followup_orchestrator.audio.wait_for_speech_settle = lambda _delay=None: 0.0  # type: ignore[method-assign]
    _, echo_task = make_environment_waiting_task(echo_followup_orchestrator)
    echo_question = echo_task.followups[-1]["question"]
    echo_voice = _FollowupQuestionEchoVoice(echo_question)
    echo_followup_orchestrator.realtime_voice = echo_voice
    echo_args = argparse.Namespace(
        seconds=8.0,
        execute=False,
        asr_retries=0,
        followup_echo_retries=0,
        followup_post_speech_listen_delay_seconds=0.0,
        post_speech_listen_delay_seconds=0.0,
    )
    echo_result = _followup_voice_with_retries(echo_followup_orchestrator, echo_task.task_group_id, echo_args)
    echo_loaded = echo_followup_orchestrator.store.load_task_group(echo_task.task_group_id)
    require(
        "followup_question_echo_keeps_task_waiting",
        echo_result.get("ok") is True
        and echo_result.get("keep_waiting") is True
        and echo_loaded.status == TaskStatus.NEEDS_INFO.value
        and not any(item.get("answer") for item in echo_loaded.followups)
        and len(echo_voice.calls) == 1,
        {"result": echo_result, "status": echo_loaded.status, "followups": echo_loaded.followups, "calls": echo_voice.calls},
    )

    require(
        "barge_in_empty_realtime_decision_detected",
        _heard_is_empty_or_noop_command(
            {
                "decision": {
                    "ok": True,
                    "decision_type": "noop",
                    "task_groups": [],
                    "ask_user": None,
                    "user_text": "",
                    "reason": "empty_model_text",
                }
            }
        )
        is True,
        "",
    )
    require(
        "barge_in_pet_command_not_empty",
        _heard_is_empty_or_noop_command(
            {
                "decision": {
                    "ok": True,
                    "decision_type": "task_plan",
                    "task_groups": [
                        {
                            "title": "find dog",
                            "user_instruction": "find dog",
                            "steps": [{"skill_name": "pet_tracking", "arguments": {"action": "find_route"}}],
                        }
                    ],
                    "ask_user": None,
                    "user_text": "请你找一下小狗在哪儿",
                }
            }
        )
        is False,
        "",
    )
    require(
        "resume_prompt_skips_noop_voice_result",
        _result_is_empty_or_noop_command({"ok": True, "noop": True, "decision": {"decision_type": "noop", "task_groups": [], "ask_user": None, "user_text": ""}})
        is True,
        "",
    )

    class _EmptyCommandStore:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def append_event(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append({"name": name, "payload": payload})

    class _EmptyCommandAudio:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def speak_text(self, text: str) -> bool:
            self.spoken.append(text)
            return True

    class _EmptyCommandOrchestrator:
        def __init__(self) -> None:
            self.store = _EmptyCommandStore()
            self.audio = _EmptyCommandAudio()

    empty_command_orchestrator = _EmptyCommandOrchestrator()
    empty_barge_result = _empty_wakeup_voice_result(
        empty_command_orchestrator,  # type: ignore[arg-type]
        {
            "decision": {
                "ok": True,
                "decision_type": "noop",
                "task_groups": [],
                "ask_user": None,
                "user_text": "",
                "reason": "empty_model_text",
            }
        },
        {"ok": True, "interrupted": True, "task_group_id": "task_interrupted"},
        type("Event", (), {"event_id": "wake_empty"})(),
    )
    require(
        "barge_in_empty_command_keeps_interrupted_without_resume_prompt",
        empty_barge_result.get("ok") is False
        and empty_barge_result.get("error") == "barge_in_command_empty"
        and _result_is_empty_or_noop_command(empty_barge_result) is True
        and [event["name"] for event in empty_command_orchestrator.store.events] == ["barge_in_command_empty_kept_interrupted"]
        and empty_command_orchestrator.audio.spoken == ["我没有听清楚新的任务，刚才的任务已经暂停保存，请重新唤醒后再说一遍"],
        {"result": empty_barge_result, "events": empty_command_orchestrator.store.events, "spoken": empty_command_orchestrator.audio.spoken},
    )

    realtime_parser = DoubaoRealtimeSession(cfg, SkillRegistry(cfg), ResourceManager(cfg))
    wake_echo_question = realtime_parser._parse_decision("我在，你有什么事吗？", "command")
    require(
        "doubao_wake_reply_echo_is_noop",
        wake_echo_question.get("decision_type") == "noop"
        and wake_echo_question.get("reason") == "robot_prompt_echo"
        and not wake_echo_question.get("task_groups")
        and not wake_echo_question.get("ask_user"),
        wake_echo_question,
    )
    movement_question = realtime_parser._parse_decision("是要我动一下吗？你想让我往哪个方向动呢？", "command")
    movement_groups = movement_question.get("task_groups") or []
    require(
        "doubao_non_json_movement_question_becomes_taskgroup_followup",
        movement_question.get("decision_type") == "ask_user"
        and isinstance(movement_question.get("ask_user"), dict)
        and bool(movement_groups)
        and movement_groups[0].get("title") == "移动任务"
        and movement_question["ask_user"].get("missing_slots") == ["direction"],
        movement_question,
    )
    fitness_setup_question = realtime_parser._parse_decision(
        "好的，那我们先确认一下运动环境是否安全，再看看是否需要用投影仪辅助。请问你想在哪里做深蹲，需要我用投影仪显示动作指导吗？",
        "command",
    )
    fitness_groups = fitness_setup_question.get("task_groups") or []
    require(
        "doubao_non_json_fitness_setup_question_becomes_taskgroup_followup",
        fitness_setup_question.get("decision_type") == "ask_user"
        and isinstance(fitness_setup_question.get("ask_user"), dict)
        and bool(fitness_groups)
        and fitness_groups[0].get("slots", {}).get("exercise_type") is None
        and fitness_setup_question["ask_user"].get("missing_slots") == ["exercise_type", "where"]
        and "projector_control" in fitness_setup_question["ask_user"].get("optional_slots", []),
        fitness_setup_question,
    )
    json_answer_question = realtime_parser._parse_decision(
        '{"decision_type":"answer","reply":"是要我动一下吗？你想让我往哪个方向动呢？","task_groups":[],"ask_user":null,"confidence":0.5,"user_text":"是要我动一下吗？你想让我往哪个方向动呢？"}',
        "command",
    )
    require(
        "doubao_json_answer_question_is_not_stateless_answer",
        json_answer_question.get("decision_type") == "ask_user"
        and isinstance(json_answer_question.get("ask_user"), dict)
        and bool(json_answer_question.get("task_groups")),
        json_answer_question,
    )
    json_fitness_question = realtime_parser._parse_decision(
        json.dumps(
            {
                "decision_type": "answer",
                "reply": "好的，那我们先确认一下运动环境是否安全，再看看是否需要用投影仪辅助。请问你想在哪里做深蹲，需要我用投影仪显示动作指导吗？",
                "task_groups": [],
                "ask_user": None,
                "confidence": 0.5,
                "user_text": "好的，那我们先确认一下运动环境是否安全，再看看是否需要用投影仪辅助。请问你想在哪里做深蹲，需要我用投影仪显示动作指导吗？",
            },
            ensure_ascii=False,
        ),
        "command",
    )
    json_fitness_groups = json_fitness_question.get("task_groups") or []
    require(
        "doubao_json_answer_fitness_question_is_not_stateless_answer",
        json_fitness_question.get("decision_type") == "ask_user"
        and isinstance(json_fitness_question.get("ask_user"), dict)
        and bool(json_fitness_groups)
        and json_fitness_groups[0].get("slots", {}).get("exercise_type") is None
        and json_fitness_question["ask_user"].get("missing_slots") == ["exercise_type", "where"]
        and "projector_control" in json_fitness_question["ask_user"].get("optional_slots", []),
        json_fitness_question,
    )

    bare_answer_movement_orchestrator = RobotOrchestrator(cfg)
    bare_answer_movement_orchestrator.audio.speak_text = lambda _text: True
    bare_answer_movement = bare_answer_movement_orchestrator.handle_voice_decision(
        {
            "ok": True,
            "decision_type": "answer",
            "reply": "是要我动一下吗？你想让我往哪个方向动呢？",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.5,
            "user_text": "是要我动一下吗？你想让我往哪个方向动呢？",
        },
        execute=False,
        dry_run=True,
        enqueue=True,
    )
    bare_answer_movement_groups = [
        bare_answer_movement_orchestrator.store.load_task_group(task_group_id)
        for task_group_id in bare_answer_movement.get("task_group_ids", [])
    ]
    bare_answer_movement_session = bare_answer_movement_orchestrator.store.load_session(str(bare_answer_movement.get("session")))
    require(
        "voice_entry_recovers_bare_answer_movement_question",
        bare_answer_movement.get("ok") is True
        and bare_answer_movement.get("decision", {}).get("decision_type") == "ask_user"
        and isinstance(bare_answer_movement.get("decision", {}).get("ask_user"), dict)
        and bool(bare_answer_movement_groups)
        and bare_answer_movement_groups[0].status == TaskStatus.NEEDS_INFO.value
        and bare_answer_movement_groups[0].followups
        and bare_answer_movement.get("decision", {}).get("recovered_from_bare_answer_decision"),
        {"result": bare_answer_movement, "groups": [to_dict(group) for group in bare_answer_movement_groups], "session": to_dict(bare_answer_movement_session)},
    )
    require(
        "voice_entry_updates_user_text_after_bare_answer_recovery",
        bare_answer_movement_session.utterances
        and bare_answer_movement_session.utterances[0].get("text") == bare_answer_movement.get("decision", {}).get("user_text")
        and bare_answer_movement_session.utterances[0].get("text") != "是要我动一下吗？你想让我往哪个方向动呢？",
        {"session": to_dict(bare_answer_movement_session), "decision": bare_answer_movement.get("decision")},
    )

    bare_answer_echo_while_waiting = bare_answer_movement_orchestrator.handle_voice_decision(
        {
            "ok": True,
            "decision_type": "noop",
            "reply": "",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.0,
            "user_text": "",
            "reason": "robot_prompt_echo",
            "raw_text": "我在，你有什么事吗？",
        },
        execute=False,
        dry_run=True,
        enqueue=True,
    )
    bare_answer_echo_waiting_group = bare_answer_movement_orchestrator.store.load_task_group(bare_answer_movement_groups[0].task_group_id)
    require(
        "voice_noop_echo_does_not_route_to_pending_followup",
        bare_answer_echo_while_waiting.get("noop") is True
        and bare_answer_echo_while_waiting.get("task_group_ids") == []
        and bare_answer_echo_waiting_group.status == TaskStatus.NEEDS_INFO.value
        and not any(followup.get("answer") for followup in bare_answer_echo_waiting_group.followups),
        {"result": bare_answer_echo_while_waiting, "waiting_group": to_dict(bare_answer_echo_waiting_group)},
    )
    bare_answer_movement_orchestrator.store.clear_waiting_followups("self_check_after_bare_answer_movement")

    bare_answer_pet_orchestrator = RobotOrchestrator(cfg)
    bare_answer_pet_orchestrator.audio.speak_text = lambda _text: True
    bare_answer_pet = bare_answer_pet_orchestrator.handle_voice_decision(
        {
            "ok": True,
            "decision_type": "answer",
            "reply": "好的，我来看看小狗在哪里。",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.5,
            "user_text": "好的，我来看看小狗在哪里。",
        },
        execute=True,
        dry_run=True,
        enqueue=True,
    )
    bare_answer_pet_groups = [
        bare_answer_pet_orchestrator.store.load_task_group(task_group_id)
        for task_group_id in bare_answer_pet.get("task_group_ids", [])
    ]
    bare_answer_pet_steps = [step for group in bare_answer_pet_groups for step in group.steps]
    require(
        "voice_entry_recovers_bare_answer_pet_intent",
        bare_answer_pet.get("ok") is True
        and bare_answer_pet.get("decision", {}).get("decision_type") == "task_plan"
        and len(bare_answer_pet_groups) == 1
        and bare_answer_pet_groups[0].status == TaskStatus.COMPLETED.value
        and [step.skill_name for step in bare_answer_pet_steps] == ["pet_tracking"]
        and bare_answer_pet_steps[0].arguments.get("action") == "find_route"
        and bare_answer_pet_steps[0].arguments.get("track_after_found") is True
        and bare_answer_pet.get("decision", {}).get("recovered_from_bare_answer_decision"),
        {"result": bare_answer_pet, "steps": [to_dict(step) for step in bare_answer_pet_steps]},
    )

    pet_intent = realtime_parser._parse_decision("好的，我来看看小狗在哪里。", "command")
    pet_groups = pet_intent.get("task_groups") or []
    pet_steps = pet_groups[0].get("steps") if pet_groups else []
    require(
        "doubao_non_json_pet_intent_becomes_task_plan",
        pet_intent.get("decision_type") == "task_plan"
        and bool(pet_steps)
        and pet_steps[0].get("skill_name") == "pet_tracking"
        and pet_steps[0].get("arguments", {}).get("action") == "find_route"
        and pet_steps[0].get("arguments", {}).get("pet") == "dog",
        pet_intent,
    )
    pet_json_answer = realtime_parser._parse_decision(
        '{"decision_type":"answer","reply":"好的，我来看看小狗在哪里。","task_groups":[],"ask_user":null,"confidence":0.5,"user_text":"好的，我来看看小狗在哪里。"}',
        "command",
    )
    pet_json_groups = pet_json_answer.get("task_groups") or []
    pet_json_steps = pet_json_groups[0].get("steps") if pet_json_groups else []
    require(
        "doubao_json_answer_pet_intent_becomes_task_plan",
        pet_json_answer.get("decision_type") == "task_plan"
        and bool(pet_json_steps)
        and pet_json_steps[0].get("skill_name") == "pet_tracking"
        and pet_json_steps[0].get("arguments", {}).get("action") == "find_route",
        pet_json_answer,
    )
    movement_intent = realtime_parser._parse_decision("好的，我往后走五秒。", "command")
    movement_intent_groups = movement_intent.get("task_groups") or []
    movement_intent_steps = movement_intent_groups[0].get("steps") if movement_intent_groups else []
    require(
        "doubao_non_json_movement_intent_becomes_task_plan",
        movement_intent.get("decision_type") == "task_plan"
        and bool(movement_intent_steps)
        and movement_intent_steps[0].get("skill_name") == "move_backward"
        and movement_intent_steps[0].get("arguments", {}).get("duration") == 5.0,
        movement_intent,
    )

    movement_followup_orchestrator = RobotOrchestrator(cfg)
    movement_followup_orchestrator.audio.speak_text = lambda _text: True
    movement_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    movement_task = TaskGroup(
        command_session_id=movement_session.session_id,
        title="移动任务",
        user_instruction="用户想让机器人移动，但方向还不明确",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": "你想让我往前、往后、往左还是往右移动？",
                "task_title": "移动任务",
                "missing_slots": ["direction"],
                "optional_slots": ["duration"],
                "candidate_skills": ["move_forward", "move_backward", "move_left", "move_right"],
                "timestamp": 1,
            }
        ],
    )
    movement_session.task_group_ids = [movement_task.task_group_id]
    movement_followup_orchestrator.store.save_session(movement_session)
    movement_followup_orchestrator.store.save_task_group(movement_task)
    movement_answer = movement_followup_orchestrator.answer_followup(movement_task.task_group_id, "往前走五秒", execute=False, dry_run=True, enqueue=False)
    movement_loaded = movement_followup_orchestrator.store.load_task_group(movement_task.task_group_id)
    require(
        "movement_followup_answer_generates_move_step",
        movement_answer.get("ok") is True
        and movement_loaded.status == TaskStatus.NEW.value
        and len(movement_loaded.steps) == 1
        and movement_loaded.steps[0].skill_name == "move_forward"
        and movement_loaded.steps[0].arguments.get("duration") == 5.0,
        {"result": movement_answer, "steps": [step.__dict__ for step in movement_loaded.steps]},
    )

    movement_route_orchestrator = RobotOrchestrator(cfg)
    movement_route_orchestrator.audio.speak_text = lambda _text: True
    movement_route_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    movement_route_task = TaskGroup(
        command_session_id=movement_route_session.session_id,
        title="移动任务",
        user_instruction="用户想让机器人移动，但方向还不明确",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": "你想让我往前、往后、往左还是往右移动？",
                "task_title": "移动任务",
                "missing_slots": ["direction"],
                "optional_slots": ["duration"],
                "candidate_skills": ["move_forward", "move_backward", "move_left", "move_right"],
                "timestamp": 1,
            }
        ],
    )
    movement_route_session.task_group_ids = [movement_route_task.task_group_id]
    movement_route_orchestrator.store.save_session(movement_route_session)
    movement_route_orchestrator.store.save_task_group(movement_route_task)
    routed_movement = movement_route_orchestrator.handle_text("往右走三秒", execute=False, dry_run=True, enqueue=False)
    routed_movement_loaded = movement_route_orchestrator.store.load_task_group(movement_route_task.task_group_id)
    require(
        "command_session_routes_pending_movement_followup",
        bool(routed_movement.get("routed_pending_followup"))
        and len(routed_movement_loaded.steps) == 1
        and routed_movement_loaded.steps[0].skill_name == "move_right"
        and routed_movement_loaded.steps[0].arguments.get("duration") == 3.0,
        {"routed": routed_movement.get("routed_pending_followup"), "steps": [step.__dict__ for step in routed_movement_loaded.steps]},
    )

    fitness_projector_orchestrator = RobotOrchestrator(cfg)
    fitness_projector_orchestrator.audio.speak_text = lambda _text: True
    fitness_projector_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    fitness_projector_task = TaskGroup(
        command_session_id=fitness_projector_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": "你想做什么运动？是在这里做，还是去某个已保存的地点做？需要我打开投影辅助训练吗？",
                "task_title": "运动",
                "missing_slots": ["exercise_type", "where"],
                "optional_slots": ["projector_control"],
                "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
                "timestamp": 1,
            }
        ],
    )
    fitness_projector_session.task_group_ids = [fitness_projector_task.task_group_id]
    fitness_projector_orchestrator.store.save_session(fitness_projector_session)
    fitness_projector_orchestrator.store.save_task_group(fitness_projector_task)
    fitness_projector_partial = fitness_projector_orchestrator.answer_followup_decision(
        fitness_projector_task.task_group_id,
        {
            "ok": True,
            "decision_type": "followup_text",
            "followup_text": "做深蹲，就在这里",
            "user_text": "做深蹲，就在这里",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.9,
        },
        execute=False,
        dry_run=True,
        enqueue=False,
    )
    fitness_projector_loaded = fitness_projector_orchestrator.store.load_task_group(fitness_projector_task.task_group_id)
    require(
        "fitness_followup_requires_projector_answer_when_omitted",
        fitness_projector_partial.get("decision", {}).get("ask_user", {}).get("optional_slots") == ["projector_control"]
        and fitness_projector_loaded.status == TaskStatus.NEEDS_INFO.value
        and fitness_projector_loaded.slots.get("exercise_type") == "squat"
        and fitness_projector_loaded.slots.get("where") == "here"
        and fitness_projector_loaded.slots.get("projector") is None
        and len([item for item in fitness_projector_loaded.followups if not item.get("answer")]) == 1,
        {"result": fitness_projector_partial, "slots": fitness_projector_loaded.slots, "followups": fitness_projector_loaded.followups},
    )
    fitness_projector_final = fitness_projector_orchestrator.answer_followup_decision(
        fitness_projector_task.task_group_id,
        {
            "ok": True,
            "decision_type": "followup_text",
            "followup_text": "不用投影",
            "user_text": "不用投影",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.9,
        },
        execute=False,
        dry_run=True,
        enqueue=False,
    )
    fitness_projector_done = fitness_projector_orchestrator.store.load_task_group(fitness_projector_task.task_group_id)
    fitness_projector_skills = [step.skill_name for step in fitness_projector_done.steps]
    require(
        "fitness_projector_second_answer_completes_plan",
        fitness_projector_final.get("decision", {}).get("ask_user") is None
        and fitness_projector_done.status == TaskStatus.NEW.value
        and fitness_projector_done.slots.get("projector") is False
        and fitness_projector_skills == ["head_control", "environment_perception", "squat"],
        {"result": fitness_projector_final, "slots": fitness_projector_done.slots, "skills": fitness_projector_skills},
    )

    projector_followup = {
        "question": "你想做什么运动？是在这里做还是去某个已保存的地点做？需要我打开投影辅助训练吗？",
        "missing_slots": ["exercise_type", "where"],
        "optional_slots": ["projector_control"],
        "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
    }
    require(
        "projector_followup_does_not_capture_open_light",
        fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开灯") is None
        and fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开投影") is True
        and fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开") is True,
        {
            "open_light": fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开灯"),
            "open_projector": fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开投影"),
            "open_short": fitness_projector_orchestrator._classify_projector_followup_reply(projector_followup, "打开"),
        },
    )

    class _FollowupRealtimeSequence:
        def __init__(self, texts: list[str]) -> None:
            self.texts = list(texts)
            self.calls: list[dict[str, Any]] = []

        def prepare_once(self, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"ok": True, "mode": mode, "prepared": True}

        def decide_once(self, seconds: float | None = None, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
            text = self.texts.pop(0) if self.texts else ""
            self.calls.append({"mode": mode, "text": text, "context": context or {}})
            return {
                "ok": bool(text),
                "decision_type": "followup_text",
                "reply": "",
                "task_groups": [],
                "ask_user": None,
                "confidence": 0.9 if text else 0.0,
                "user_text": text,
                "followup_text": text,
                "text": text,
            }

    full_flow_orchestrator = RobotOrchestrator(cfg)
    full_flow_orchestrator.audio.speak_text = lambda _text: True
    full_flow_orchestrator.realtime_voice = _FollowupRealtimeSequence(["我想就在这里做深蹲，并且打开投影", "就在这里继续"])
    fake_step_calls: list[dict[str, Any]] = []

    def fake_execute_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        fake_step_calls.append({"skill": step.skill_name, "arguments": dict(step.arguments or {}), "dry_run": dry_run})
        if step.skill_name == "environment_perception":
            return {
                "ok": True,
                "parsed_json": {
                    "result": {
                        "exercise_suitability": {"squat": {"ok": False, "blockers": ["self_check_space_blocked"]}},
                        "projection_suitability": {"ok": False, "blockers": ["self_check_projection_blocked"]},
                    }
                },
            }
        return {"ok": True, "skill_name": step.skill_name, "arguments": step.arguments}

    full_flow_orchestrator.executor.execute_step = fake_execute_step  # type: ignore[method-assign]
    initial_fitness_decision = {
        "ok": True,
        "decision_type": "ask_user",
        "reply": "你想做什么运动？",
        "task_groups": [
            {
                "title": "运动",
                "user_instruction": "我想做运动",
                "slots": {},
                "followups": [],
                "steps": [],
            }
        ],
        "ask_user": {
            "task_title": "运动",
            "question": "你想做什么运动？深蹲、俯卧撑还是引体向上？另外，是在这里做还是去某个已保存地点做？需要打开投影吗？",
            "missing_slots": ["exercise_type", "where"],
            "optional_slots": ["projector_control"],
            "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
        },
        "confidence": 0.9,
        "user_text": "我想做运动",
    }
    first_result = full_flow_orchestrator.handle_voice_decision(initial_fitness_decision, execute=True, dry_run=False, enqueue=True)
    full_flow_args = argparse.Namespace(seconds=8.0, execute=True, asr_retries=0, max_followups=3, followup_post_speech_listen_delay_seconds=0.0, followup_echo_retries=0, post_speech_listen_delay_seconds=0.0)
    full_flow_result = _complete_followup_flow(full_flow_orchestrator, first_result, full_flow_args)
    full_flow_task_id = (full_flow_result.get("task_group_ids") or [""])[0]
    full_flow_task = full_flow_orchestrator.store.load_task_group(full_flow_task_id)
    full_flow_skills = [step.skill_name for step in full_flow_task.steps]
    full_flow_statuses = [step.status for step in full_flow_task.steps]
    full_flow_history = full_flow_orchestrator.store.recent_history(limit=10)
    full_flow_history_records = [
        item
        for item in full_flow_history
        if isinstance(item.get("task_group"), dict) and item["task_group"].get("task_group_id") == full_flow_task.task_group_id
    ]
    full_flow_history_task = full_flow_history_records[0].get("task_group") if full_flow_history_records else {}
    require(
        "full_followup_flow_fitness_environment_continue_completes_one_taskgroup",
        full_flow_task.status == TaskStatus.COMPLETED.value
        and full_flow_skills == ["head_control", "environment_perception", "projector_control", "squat"]
        and all(status == TaskStatus.COMPLETED.value for status in full_flow_statuses)
        and full_flow_task.slots.get("exercise_type") == "squat"
        and full_flow_task.slots.get("where") == "here"
        and full_flow_task.slots.get("projector") is True
        and bool(full_flow_task.metadata.get("environment_override_accepted"))
        and len(full_flow_history_records) == 1
        and len(full_flow_history_task.get("followups") or []) == 2
        and len(full_flow_task.history_refs) == 1
        and any(call["skill"] == "projector_control" and call["arguments"].get("action") == "off" for call in fake_step_calls)
        and any(call["skill"] == "head_control" and call["arguments"].get("action") == "level" for call in fake_step_calls),
        {
            "result": full_flow_result,
            "status": full_flow_task.status,
            "skills": full_flow_skills,
            "statuses": full_flow_statuses,
            "slots": full_flow_task.slots,
            "metadata": full_flow_task.metadata,
            "history_refs": full_flow_task.history_refs,
            "history_records": full_flow_history_records,
            "calls": fake_step_calls,
        },
    )

    class _DaemonRealtimeFlow:
        def __init__(self, command_decision: dict[str, Any], followups: list[str]) -> None:
            self.command_decision = copy.deepcopy(command_decision)
            self.followups = list(followups)
            self.calls: list[dict[str, Any]] = []

        def prepare_once(self, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append({"mode": f"prepare:{mode}", "seconds": None, "context": context or {}})
            return {"ok": True, "mode": mode, "prepared": True}

        def decide_once(self, seconds: float | None = None, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append({"mode": mode, "seconds": seconds, "context": context or {}})
            if mode == "command":
                return copy.deepcopy(self.command_decision)
            if mode == "followup":
                text = self.followups.pop(0) if self.followups else ""
                return {
                    "ok": bool(text),
                    "decision_type": "followup_text",
                    "reply": "",
                    "task_groups": [],
                    "ask_user": None,
                    "confidence": 0.9 if text else 0.0,
                    "user_text": text,
                    "followup_text": text,
                    "text": text,
                }
            return {"ok": True, "reply_type": "resume_confirmation", "text": "", "resume_action": "none"}

    class _DaemonWakeEvent:
        def __init__(self) -> None:
            self.event_id = "wake_self_check_daemon"
            self.raw_value = "1"
            self.source = "self_check"
            self.timestamp = 1.0
            self.metadata = {}
            self.interrupted_task_group_id = None

    daemon_flow_orchestrator = RobotOrchestrator(cfg)
    daemon_spoken: list[str] = []
    daemon_flow_orchestrator.audio.speak_text = lambda text: daemon_spoken.append(str(text)) or True
    daemon_flow_orchestrator.audio.speak_wake_reply = lambda text: daemon_spoken.append(str(text)) or True
    daemon_flow_orchestrator.audio.wait_for_speech_settle = lambda _delay=None: 0.0  # type: ignore[method-assign]
    daemon_realtime = _DaemonRealtimeFlow(initial_fitness_decision, ["我想就在这里做深蹲，并且打开投影", "就在这里继续"])
    daemon_flow_orchestrator.realtime_voice = daemon_realtime
    daemon_step_calls: list[dict[str, Any]] = []

    def daemon_execute_step(step: TaskStep, dry_run: bool = False) -> dict[str, Any]:
        daemon_step_calls.append({"skill": step.skill_name, "arguments": dict(step.arguments or {}), "dry_run": dry_run})
        if step.skill_name == "environment_perception":
            return {
                "ok": True,
                "parsed_json": {
                    "result": {
                        "exercise_suitability": {"squat": {"ok": False, "blockers": ["self_check_daemon_space_blocked"]}},
                        "projection_suitability": {"ok": False, "blockers": ["self_check_daemon_projection_blocked"]},
                    }
                },
            }
        return {"ok": True, "skill_name": step.skill_name, "arguments": dict(step.arguments or {})}

    daemon_flow_orchestrator.executor.execute_step = daemon_execute_step  # type: ignore[method-assign]
    daemon_args = argparse.Namespace(
        seconds=8.0,
        execute=True,
        asr_retries=0,
        max_followups=3,
        followup_post_speech_listen_delay_seconds=0.0,
        followup_echo_retries=0,
        post_speech_listen_delay_seconds=0.0,
        wake_reply="我在",
        post_wake_listen_delay_seconds=0.0,
        max_resume_rounds=0,
        resume_confirm_seconds=4.0,
        resume_confirm_retries=0,
        interrupt_wait_seconds=0.01,
    )
    daemon_result = _run_wakeup_session(
        daemon_flow_orchestrator,
        _DaemonWakeEvent(),
        {"ok": True, "interrupted": False, "mode": "fast"},
        daemon_args,
        speak_wake_reply=True,
    )
    daemon_voice = daemon_result.get("voice_result") if isinstance(daemon_result.get("voice_result"), dict) else {}
    daemon_task_id = (daemon_voice.get("task_group_ids") or [""])[0]
    require(
        "daemon_wakeup_session_returns_task_group_id",
        bool(daemon_task_id),
        {"result": daemon_result, "calls": daemon_realtime.calls, "spoken": daemon_spoken, "step_calls": daemon_step_calls},
    )
    daemon_task = daemon_flow_orchestrator.store.load_task_group(daemon_task_id)
    daemon_skills = [step.skill_name for step in daemon_task.steps]
    require(
        "daemon_wakeup_session_completes_fitness_followup_taskgroup",
        daemon_voice.get("ok") is True
        and daemon_task.status == TaskStatus.COMPLETED.value
        and daemon_skills == ["head_control", "environment_perception", "projector_control", "squat"]
        and [call["mode"] for call in daemon_realtime.calls if not str(call["mode"]).startswith("prepare:")] == ["command", "followup", "followup"]
        and [call["mode"] for call in daemon_realtime.calls if str(call["mode"]).startswith("prepare:")] == ["prepare:followup", "prepare:followup"]
        and any(call["skill"] == "projector_control" and call["arguments"].get("action") == "off" for call in daemon_step_calls)
        and any(call["skill"] == "head_control" and call["arguments"].get("action") == "level" for call in daemon_step_calls)
        and daemon_spoken[:1] == ["我在"],
        {
            "result": daemon_result,
            "status": daemon_task.status,
            "skills": daemon_skills,
            "calls": daemon_realtime.calls,
            "step_calls": daemon_step_calls,
            "spoken": daemon_spoken,
        },
    )

    wait_followup_orchestrator = RobotOrchestrator(cfg)
    wait_followup_orchestrator.audio.speak_text = lambda _text: True
    wait_followup_task = TaskGroup(
        title="fitness wait followup",
        status=TaskStatus.NEEDS_INFO.value,
        followups=[
            {
                "question": "continue here?",
                "task_title": "fitness wait followup",
                "missing_slots": ["environment_override"],
                "optional_slots": ["projector_control"],
                "candidate_skills": ["environment_perception", "projector_control", "squat"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    wait_followup_orchestrator.store.save_task_group(wait_followup_task)
    wait_result = _pending_followup_wait_result(
        wait_followup_orchestrator,
        wait_followup_task.task_group_id,
        {"ok": False, "error": "no_valid_speech_detected", "heard": {"text": ""}},
    )
    wait_loaded = wait_followup_orchestrator.store.load_task_group(wait_followup_task.task_group_id)
    require(
        "followup_listen_failure_keeps_task_waiting",
        wait_result.get("ok") is True
        and wait_result.get("keep_waiting") is True
        and wait_loaded.status == TaskStatus.NEEDS_INFO.value
        and bool(wait_loaded.followups)
        and not wait_loaded.followups[-1].get("answer"),
        {"wait_result": wait_result, "task_status": wait_loaded.status, "followups": wait_loaded.followups},
    )

    unclear_env_orchestrator = RobotOrchestrator(cfg)
    unclear_env_orchestrator.audio.speak_text = lambda _text: True
    unclear_env_session = CommandSession(session_type="voice", status=SessionStatus.WAITING_USER.value)
    unclear_env_task = TaskGroup(
        command_session_id=unclear_env_session.session_id,
        title="运动",
        user_instruction="我想做运动",
        status=TaskStatus.NEEDS_INFO.value,
        metadata={
            "waiting_environment_override": {
                "ask_user": {
                    "task_title": "运动",
                    "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                    "missing_slots": ["environment_override"],
                    "optional_slots": ["where", "projector_control"],
                    "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat"],
                }
            }
        },
        followups=[
            {
                "question": "这里可能不适合当前运动或投影，需要换到其他已保存地点再做吗？你也可以说就在这里继续。",
                "task_title": "运动",
                "missing_slots": ["environment_override"],
                "optional_slots": ["where", "projector_control"],
                "candidate_skills": ["environment_perception", "navigation_goto", "projector_control", "squat"],
                "timestamp": 1,
                "runtime_followup": "environment_override",
            }
        ],
        steps=[TaskStep(skill_name="squat", arguments={"action": "run"}, status=TaskStatus.NEW.value)],
    )
    unclear_env_session.task_group_ids = [unclear_env_task.task_group_id]
    unclear_env_orchestrator.store.save_session(unclear_env_session)
    unclear_env_orchestrator.store.save_task_group(unclear_env_task)
    unclear_env_again = unclear_env_orchestrator.answer_followup_decision(
        unclear_env_task.task_group_id,
        {
            "ok": True,
            "decision_type": "followup_text",
            "followup_text": "我还没想好",
            "user_text": "我还没想好",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.9,
        },
        execute=False,
        dry_run=True,
        enqueue=False,
    )
    unclear_env_loaded = unclear_env_orchestrator.store.load_task_group(unclear_env_task.task_group_id)
    unanswered_env = [item for item in unclear_env_loaded.followups if not item.get("answer")]
    require(
        "environment_override_unclear_answer_reopens_pending_followup",
        unclear_env_again.get("decision", {}).get("ask_user", {}).get("missing_slots") == ["environment_override"]
        and unclear_env_loaded.status == TaskStatus.NEEDS_INFO.value
        and len(unanswered_env) == 1
        and unanswered_env[0].get("runtime_followup") == "environment_override"
        and unclear_env_orchestrator._is_task_group_waiting_for_followup(unclear_env_loaded),
        {"result": unclear_env_again, "followups": unclear_env_loaded.followups, "waiting": unclear_env_orchestrator._is_task_group_waiting_for_followup(unclear_env_loaded)},
    )
    unclear_env_route = unclear_env_orchestrator.handle_text("就在这里继续", execute=False, dry_run=True, enqueue=False)
    unclear_env_after_route = unclear_env_orchestrator.store.load_task_group(unclear_env_task.task_group_id)
    require(
        "environment_override_reopened_followup_routes_next_wakeup_answer",
        bool(unclear_env_route.get("routed_pending_followup"))
        and unclear_env_after_route.status == TaskStatus.NEW.value
        and bool(unclear_env_after_route.metadata.get("environment_override_accepted")),
        {"route": unclear_env_route, "status": unclear_env_after_route.status, "metadata": unclear_env_after_route.metadata, "followups": unclear_env_after_route.followups},
    )

    class _SelfCheckEvent:
        event_id = "wake_self_check"

    class _SelfCheckStore:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def append_event(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append({"name": name, "payload": payload})

    class _SelfCheckExecutor:
        def __init__(self) -> None:
            self.interrupt_count = 0

        def interrupt_current(self) -> dict[str, Any]:
            self.interrupt_count += 1
            return {"ok": True, "interrupted": True, "source": "self_check_force_stop"}

    class _SelfCheckOrchestrator:
        def __init__(self) -> None:
            self.store = _SelfCheckStore()
            self.executor = _SelfCheckExecutor()

    class _TimeoutThenResultFuture:
        def __init__(self) -> None:
            self.calls = 0

        def result(self, timeout: float | None = None) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise concurrent.futures.TimeoutError()
            return {"ok": True, "stopped_after_force": True}

    class _AlwaysTimeoutFuture:
        def __init__(self) -> None:
            self.calls = 0

        def result(self, timeout: float | None = None) -> dict[str, Any]:
            self.calls += 1
            raise concurrent.futures.TimeoutError()

    force_args = argparse.Namespace(interrupt_wait_seconds=0.01)
    force_orchestrator = _SelfCheckOrchestrator()
    previous_result, previous_error, force_stop_result = _wait_previous_session_or_force_stop(
        force_orchestrator,
        _TimeoutThenResultFuture(),  # type: ignore[arg-type]
        force_args,
        _SelfCheckEvent(),
    )
    require(
        "barge_in_force_stop_allows_new_command_to_continue",
        previous_error is None
        and previous_result == {"ok": True, "stopped_after_force": True}
        and force_stop_result is not None
        and force_orchestrator.executor.interrupt_count == 1
        and [event["name"] for event in force_orchestrator.store.events] == ["previous_task_stopped", "previous_task_force_stop_attempted"],
        {"previous_result": previous_result, "previous_error": previous_error, "events": force_orchestrator.store.events},
    )
    timeout_orchestrator = _SelfCheckOrchestrator()
    timeout_result, timeout_error, timeout_force_stop = _wait_previous_session_or_force_stop(
        timeout_orchestrator,
        _AlwaysTimeoutFuture(),  # type: ignore[arg-type]
        force_args,
        _SelfCheckEvent(),
    )
    require(
        "barge_in_force_stop_reports_still_stopping",
        timeout_result is None
        and timeout_error == "previous_session_still_stopping_after_force_stop"
        and timeout_force_stop is not None
        and timeout_orchestrator.executor.interrupt_count == 1,
        {"timeout_result": timeout_result, "timeout_error": timeout_error, "events": timeout_orchestrator.store.events},
    )

    compact = json.dumps(SkillRegistry(cfg).compact_specs_for_prompt(), ensure_ascii=False)
    require("prompt_specs_clean", not any(token in compact for token in ["???", "? skill", "????", "�"]))

    final_runtime = RobotOrchestrator(cfg).store.validate_runtime_state()
    require("self_check_runtime_state_clean", final_runtime.get("ok") is True, final_runtime)

    return {"ok": all(item["ok"] for item in checks), "runtime_dir": cfg["paths"]["runtime_dir"], "checks": checks}


def _ensure_self_check_skill_specs(cfg: dict[str, Any]) -> None:
    spec_dir = Path(cfg["paths"]["skill_spec_dir"])
    existing = {path.stem for path in spec_dir.glob("*.json")} if spec_dir.exists() else set()
    if REQUIRED_SELF_CHECK_SKILLS.issubset(existing):
        return
    temp_spec_dir = Path(cfg["paths"]["runtime_dir"]) / "self_check_skill_specs"
    temp_spec_dir.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_SELF_CHECK_SKILLS:
        spec = {
            "name": name,
            "description_zh": f"self-check spec for {name}",
            "domain_tags": [],
            "side_effects": [],
            "resources": [],
            "allowed_actions": [],
            "recovery": {},
        }
        if name == "navigation_goto":
            spec["side_effects"] = ["navigation"]
            spec["resources"] = ["navigation", "base"]
        elif name == "head_control":
            spec["resources"] = ["head"]
        elif name == "projector_control":
            spec["resources"] = ["projector"]
        elif name == "environment_perception":
            spec["resources"] = ["front_camera", "back_camera", "npu"]
        elif name == "squat":
            spec["domain_tags"] = ["fitness", "long_running"]
            spec["resources"] = ["back_camera", "npu"]
        elif name == "pet_tracking":
            spec["domain_tags"] = ["pet", "tracking", "long_running"]
            spec["resources"] = ["front_camera", "base", "npu"]
        elif name in {"move_backward", "move_forward", "move_right"}:
            spec["resources"] = ["base"]
        elif name in {"face_recognition", "camera_capture"}:
            spec["resources"] = ["front_camera"]
        (temp_spec_dir / f"{name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cfg["paths"]["skill_spec_dir"] = str(temp_spec_dir)
