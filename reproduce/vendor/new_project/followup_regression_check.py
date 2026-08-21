#!/usr/bin/env python3
from __future__ import annotations

import argparse
from types import SimpleNamespace

from new_project import cli
from new_project.dialogue import RobotOrchestrator
from new_project.models import TaskGroup, TaskStatus, TaskStep


class MemoryStore:
    def __init__(self, task_group: TaskGroup):
        self.task_group = task_group
        self.events = []

    def load_task_group(self, _task_group_id: str) -> TaskGroup:
        return self.task_group

    def save_task_group(self, task_group: TaskGroup) -> None:
        self.task_group = task_group

    def append_event(self, event_type, payload) -> None:
        self.events.append((event_type, payload))


class SilentAudio:
    def __init__(self):
        self.spoken = []

    def speak_text(self, text):
        self.spoken.append(text)
        return True


def ask_result(task_group: TaskGroup, question: str, **extra):
    return {
        "ok": True,
        "task_group_id": task_group.task_group_id,
        "decision": {
            "decision_type": "ask_user",
            "ask_user": {"task_title": task_group.title, "question": question},
        },
        **extra,
    }


def check_state_driven_followups() -> None:
    task = TaskGroup(title="运动", status=TaskStatus.NEEDS_INFO.value)
    task.followups.append({"question": "运动和投影？", "missing_slots": ["exercise_type"], "timestamp": 1})
    store = MemoryStore(task)
    orchestrator = SimpleNamespace(store=store, audio=SilentAudio())
    orchestrator._pending_followup = lambda group: next(
        (item for item in reversed(group.followups) if not item.get("answer") and not item.get("closed_at")),
        None,
    )
    orchestrator._task_group_to_decision_payload = lambda group: {"title": group.title}
    orchestrator.suspend_waiting_followup_safely = lambda *args, **kwargs: {"ok": True}
    calls = []

    def next_followup(_orchestrator, _task_group_id, _args):
        index = len(calls)
        calls.append(index)
        pending = orchestrator._pending_followup(task)
        if index == 0:
            pending["answer"] = "深蹲并打开投影"
            task.slots.update({"exercise_type": "squat", "projector": True})
            task.followups.append({"question": "在哪里做？", "missing_slots": ["where"], "timestamp": 2})
            return ask_result(task, "在哪里做？")
        if index == 1:
            return ask_result(task, "在哪里做？", dialogue_overlay=True, non_slot_turn=True)
        if index == 2:
            pending["answer"] = "去客厅做"
            task.slots["where"] = "living_room"
            task.followups.append({"question": "环境不理想，还继续吗？", "missing_slots": ["environment_override"], "timestamp": 3})
            return ask_result(task, "环境不理想，还继续吗？")
        pending["answer"] = "继续"
        task.status = TaskStatus.COMPLETED.value
        return {"ok": True, "task_group_id": task.task_group_id, "decision": {"decision_type": "task_plan", "ask_user": None}}

    original = cli._followup_voice_with_retries
    cli._followup_voice_with_retries = next_followup
    try:
        args = argparse.Namespace(
            max_followups=3,
            max_followup_total_turns=12,
            max_followup_overlay_turns=4,
            max_followup_elapsed_seconds=240.0,
            execute=False,
        )
        initial = ask_result(task, "运动和投影？")
        result = cli._complete_followup_flow(orchestrator, initial, args)
    finally:
        cli._followup_voice_with_retries = original
    assert len(calls) == 4, calls
    assert result["followup_flow"]["mode"] == "state_driven"
    assert not result["followup_flow"].get("paused")


def build_context_orchestrator(task: TaskGroup):
    orchestrator = RobotOrchestrator.__new__(RobotOrchestrator)
    orchestrator.store = MemoryStore(task)
    orchestrator.audio = SilentAudio()
    orchestrator.realtime_voice = None
    orchestrator.planner = SimpleNamespace(
        _known_navigation_points=lambda: [
            {"name": "living_room", "display_name": "客厅", "aliases": ["客厅"]},
            {"name": "wall", "display_name": "白墙", "aliases": ["白墙"]},
        ]
    )
    return orchestrator


def check_contextual_where_confirmation() -> None:
    task = TaskGroup(title="运动", status=TaskStatus.NEEDS_INFO.value)
    pending = {"question": "想在这里做，还是换个地方？", "missing_slots": ["where"], "timestamp": 1}
    task.followups.append(pending)
    orchestrator = build_context_orchestrator(task)
    text, result = orchestrator._contextualize_followup_answer(task, pending, {}, "星座吧。", execute=False)
    assert text == "星座吧。"
    assert result and result.get("contextual_confirmation") and result.get("non_slot_turn")
    assert "就在这里" in pending["question"]
    text, result = orchestrator._contextualize_followup_answer(task, pending, {}, "去客厅做。", execute=False)
    assert result is None
    assert "living_room" in text
    assert "contextual_confirmation" not in pending

    pending["contextual_confirmation"] = {
        "slot": "where",
        "candidate_value": "here",
        "original_question": "想在这里做，还是换个地方？",
    }
    text, result = orchestrator._contextualize_followup_answer(task, pending, {}, "对，就在这里。", execute=False)
    assert result is None
    assert text == "就在这里做"


def check_overlay_wording_is_not_generic_ack() -> None:
    task = TaskGroup(title="运动", status=TaskStatus.NEEDS_INFO.value)
    pending = {"question": "想在这里做，还是换个地方？", "missing_slots": ["where"], "timestamp": 1}
    task.followups.append(pending)
    orchestrator = build_context_orchestrator(task)
    interaction = SimpleNamespace(interaction_type="conversation", text="聊聊天气", task_operation="none", confidence=0.2)
    result = orchestrator._preserve_followup_after_overlay(task, interaction, pending, "我明白了。", execute=False)
    spoken = result["decision"]["reply"]
    assert "我明白了" not in spoken
    assert "回到刚才的问题" in spoken
    assert result["non_slot_turn"] is True


def check_safe_waiting_invalidates_setup() -> None:
    nav = TaskStep(order=0, skill_name="navigation_goto", status=TaskStatus.COMPLETED.value)
    head = TaskStep(order=1, skill_name="head_control", arguments={"action": "up"}, status=TaskStatus.COMPLETED.value)
    perception = TaskStep(order=2, skill_name="environment_perception", status=TaskStatus.COMPLETED.value)
    task = TaskGroup(title="运动", status=TaskStatus.NEEDS_INFO.value, steps=[nav, head, perception])
    task.followups.append({"question": "还继续吗？", "missing_slots": ["environment_override"]})
    orchestrator = build_context_orchestrator(task)
    orchestrator._execute_maintenance_steps = lambda steps, dry_run: [
        (step, {"ok": True, "dry_run": dry_run}) for step in steps
    ]
    result = orchestrator.suspend_waiting_followup_safely(task.task_group_id, dry_run=True, reason="test")
    assert result["ok"]
    assert nav.status == TaskStatus.COMPLETED.value
    assert head.status == TaskStatus.NEW.value
    assert perception.status == TaskStatus.NEW.value


if __name__ == "__main__":
    check_state_driven_followups()
    check_contextual_where_confirmation()
    check_overlay_wording_is_not_generic_ack()
    check_safe_waiting_invalidates_setup()
    print("FOLLOWUP_REGRESSION_CHECK_OK")
