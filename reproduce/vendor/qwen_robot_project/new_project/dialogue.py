from __future__ import annotations

import concurrent.futures
import difflib
import contextlib
import json
import re
import threading
import time
from typing import Any

from .audio import AudioManager
from .doubao_realtime import DoubaoRealtimeSession
from .qwen_voice import QwenVoiceSession
from .executor import SkillExecutor
from .interaction import (
    AMBIGUOUS,
    CONVERSATION,
    FOLLOWUP_ANSWER,
    TASK_CANCEL,
    TASK_MODIFICATION,
    TASK_PAUSE,
    TASK_QUERY,
    TASK_REPLACEMENT,
    TASK_RESTART,
    TASK_RESUME,
    TEMPORARY_TASK,
    Interaction,
    InteractionRouter,
)
from .models import CommandSession, SessionStatus, TaskGroup, TaskStatus, TaskStep, WakeupEvent, to_dict
from .planner import Planner
from .resources import ResourceManager
from .robot_state import FITNESS_SKILLS, RobotStateCollector, RobotStateRestorer
from .skill_registry import SkillRegistry
from .speech import SpeechEvent
from .speech_policy import SpeechPolicy
from .storage import JsonStore
from .user_memory import UserMemoryStore


class RobotOrchestrator:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.store = JsonStore(config)
        self.registry = SkillRegistry(config)
        self.resources = ResourceManager(config)
        self.voice_backend = str(config.get("audio", {}).get("voice_io_backend") or "")
        if self.voice_backend == "qwen_modelscope_hybrid":
            self.realtime_voice = QwenVoiceSession(config, self.registry, self.resources)
        elif self.voice_backend == "doubao_realtime":
            self.realtime_voice = DoubaoRealtimeSession(config, self.registry, self.resources)
        else:
            self.realtime_voice = None
        self.audio = AudioManager(config, self.resources, realtime_voice=self.realtime_voice)
        self.planner = Planner(config, self.registry)
        self.interactions = InteractionRouter()
        self.speech_policy = SpeechPolicy(config)
        self.user_memory = UserMemoryStore(config)
        self.robot_state = RobotStateCollector(config)
        self.robot_restorer = RobotStateRestorer(config)
        self.executor = SkillExecutor(
            config,
            self.registry,
            self.resources,
            speech_callback=self._speak_skill_event,
            task_update_callback=self._persist_task_group_progress,
        )

    def close(self) -> None:
        if self.realtime_voice is not None:
            self.realtime_voice.close()

    def _persist_task_group_progress(self, task_group: TaskGroup, step: TaskStep | None = None, reason: str = "") -> None:
        def merge(existing: TaskGroup) -> TaskGroup:
            # The fast interrupt path and the executor finish on different
            # threads. Preserve interrupt metadata captured by either side
            # instead of allowing the last writer to erase resume progress.
            if existing.status == TaskStatus.INTERRUPTED.value and task_group.status == TaskStatus.INTERRUPTED.value:
                merged_context = dict(existing.resume_context or {})
                merged_context.update(dict(task_group.resume_context or {}))
                task_group.resume_context = merged_context
                merged_metadata = dict(existing.metadata or {})
                merged_metadata.update(dict(task_group.metadata or {}))
                task_group.metadata = merged_metadata
                task_group.interruption_count = max(existing.interruption_count, task_group.interruption_count)
            return task_group

        self.store.update_task_group(task_group.task_group_id, merge, fallback=task_group)
        self.store.append_event(
            "task_group_progress_saved",
            {
                "task_group_id": task_group.task_group_id,
                "reason": reason,
                "status": task_group.status,
                "step_id": step.step_id if step else None,
                "skill_name": step.skill_name if step else None,
                "step_status": step.status if step else None,
                "step_timing": (
                    dict(step.result.get("timing") or {})
                    if step and isinstance(step.result, dict) and isinstance(step.result.get("timing"), dict)
                    else None
                ),
            },
        )

    def recover_persisted_runtime_state(self) -> dict[str, Any]:
        cleanup = self.store.prune_interrupted_stack()
        state = self.store.load_state()
        active_id = state.get("active_task_group_id")
        if not active_id:
            return {"ok": True, "active_recovered": False, "interrupted_cleanup": cleanup}

        try:
            task_group = self.store.load_task_group(active_id)
        except Exception as exc:
            state["active_task_group_id"] = None
            self.store.save_state(state)
            self.store.append_event("stale_active_task_cleared", {"task_group_id": active_id, "error": str(exc)})
            return {"ok": True, "active_recovered": False, "stale_active_cleared": active_id, "interrupted_cleanup": cleanup}

        if task_group.status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            state["active_task_group_id"] = None
            self.store.save_state(state)
            self.store.append_event("stale_active_task_cleared", {"task_group_id": active_id, "status": task_group.status})
            return {"ok": True, "active_recovered": False, "stale_active_cleared": active_id, "interrupted_cleanup": cleanup}

        active_step = self._first_running_step(task_group) or self._first_interrupted_or_pending_step(task_group)
        for step in task_group.steps:
            if step is active_step or step.status == TaskStatus.RUNNING.value:
                step.status = TaskStatus.INTERRUPTED.value
                step.error = step.error or "daemon_restarted"
        task_group.status = TaskStatus.INTERRUPTED.value
        task_group.interruption_count += 1
        context = dict(task_group.resume_context or {})
        context.update(
            {
                "reason": "daemon_startup_stale_active",
                "interrupted_at": time.time(),
                "active_step_id": active_step.step_id if active_step else None,
                "active_skill": active_step.skill_name if active_step else None,
                "completed_steps": [step.step_id for step in task_group.steps if step.status == TaskStatus.COMPLETED.value],
                "pending_steps": [step.step_id for step in task_group.steps if step.status != TaskStatus.COMPLETED.value],
                "robot_state_before_interrupt": self.robot_state.snapshot(active_task_group_id=active_id, active_step=active_step),
                "can_resume": True,
                "resume_strategy": self._resume_strategy_for_step(active_step),
                "recovery": self._recovery_for_step(active_step),
            }
        )
        task_group.resume_context = context
        self.store.push_interrupted(task_group)
        self.store.append_event("stale_active_task_recovered", {"task_group_id": active_id, "active_skill": context.get("active_skill")})
        cleanup = self.store.prune_interrupted_stack()
        return {"ok": True, "active_recovered": True, "task_group_id": active_id, "interrupted_cleanup": cleanup}

    def handle_text(
        self,
        text: str,
        session_type: str = "manual_text",
        execute: bool = False,
        dry_run: bool = True,
        enqueue: bool = True,
        wakeup_event: WakeupEvent | None = None,
    ) -> dict[str, Any]:
        self.store.repair_runtime_state("handle_text_start", clear_ready_queue=True)
        session = CommandSession(wakeup_event_id=wakeup_event.event_id if wakeup_event else None, session_type=session_type)
        session.utterances.append({"role": "user", "text": text, "timestamp": time.time()})
        self.store.save_session(session)
        self.store.append_event("command_session_started", {"session_id": session.session_id, "text": text})

        memory_result = self._handle_memory_turn(text, {}, session, execute=execute)
        if memory_result is not None:
            return memory_result

        routed_followup = self._route_voice_decision_to_pending_followup(
            {"ok": True, "decision_type": "answer", "user_text": text, "reply": "", "task_groups": []},
            text,
            execute=execute,
            dry_run=dry_run,
            enqueue=enqueue,
            wakeup_event=wakeup_event,
            command_session=session,
            decision_backend=session_type,
        )
        if routed_followup is not None:
            return routed_followup

        history = self.store.recent_history(limit=int(self.config.get("planner", {}).get("history_context_items", 8)))
        decision = self.planner.plan(text, history=history, session_context={"session_id": session.session_id})
        task_groups = self._task_groups_from_decision(session, decision)
        decision = self._apply_task_start_ack_policy(decision, task_groups, session.session_id)

        for task_group in task_groups:
            session.task_group_ids.append(task_group.task_group_id)
            self.store.save_task_group(task_group)

        if decision.get("ask_user"):
            if enqueue:
                self._enqueue_ready_session_task_groups(session)
            execution = self.drain_session_queue(session, dry_run=dry_run) if execute else {"executed": []}
            decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
            if has_runtime_ask:
                return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}
            waiting_task_group = self._find_pending_followup_task_group(session)
            if waiting_task_group is None:
                decision = self._drop_unbound_ask_user(decision, session, execution, "no_waiting_task_group_after_execution")
                session.status = SessionStatus.COMPLETED.value
                session.ended_at = time.time()
                self.store.save_session(session)
                return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}
            session.status = SessionStatus.WAITING_USER.value
            self.store.save_session(session)
            if execute:
                self._prepare_followup_voice([waiting_task_group], decision["ask_user"])
                self.audio.speak_text(decision["ask_user"]["question"])
            return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}

        if enqueue:
            self._enqueue_ready_session_task_groups(session)

        if execute:
            execution = self.drain_session_queue(session, dry_run=dry_run)
        else:
            execution = {"executed": []}

        decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
        if not has_runtime_ask:
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            self.store.save_session(session)
        return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}

    def _handle_memory_turn(
        self,
        user_text: str,
        decision: dict[str, Any],
        session: CommandSession,
        *,
        execute: bool,
    ) -> dict[str, Any] | None:
        operation = self.user_memory.interpret(user_text, decision)
        if operation.get("operation") == "none":
            return None
        result = self.user_memory.apply(operation)
        if not result.get("handled"):
            return None
        if not any(item.get("role") == "user" and item.get("text") == user_text for item in session.utterances):
            session.utterances.append(
                {
                    "role": "user",
                    "text": user_text,
                    "kind": "memory_operation",
                    "memory_operation": operation.get("operation"),
                    "timestamp": time.time(),
                }
            )
        session.status = SessionStatus.COMPLETED.value if result.get("ok") else SessionStatus.FAILED.value
        session.ended_at = time.time()
        session.metadata.update(
            {
                "decision_backend": self.voice_backend if decision else "local_memory",
                "memory_operation": operation.get("operation"),
                "memory_result": result,
            }
        )
        self.store.save_session(session)
        reply = str(result.get("reply") or "")
        if execute and reply:
            self.audio.speak_text(reply)
        self.store.append_event(
            "user_memory_updated" if operation.get("operation") in {"remember", "forget"} else "user_memory_queried",
            {
                "session_id": session.session_id,
                "operation": operation.get("operation"),
                "ok": result.get("ok"),
                "fact_id": (result.get("fact") or {}).get("fact_id") if isinstance(result.get("fact"), dict) else None,
                "removed_fact_ids": [item.get("fact_id") for item in result.get("removed", []) if isinstance(item, dict)],
            },
        )
        memory_decision = {
            "decision_type": "answer",
            "interaction_type": "conversation",
            "memory_operation": operation.get("operation"),
            "reply": reply,
            "task_groups": [],
            "ask_user": None,
            "confidence": 1.0 if operation.get("source") == "local" else decision.get("confidence", 0.8),
            "user_text": user_text,
        }
        return {
            "ok": bool(result.get("ok")),
            "session": session.session_id,
            "decision": memory_decision,
            "task_group_ids": [],
            "memory": result,
        }

    def handle_voice_once(
        self,
        seconds: float | None = None,
        execute: bool = False,
        dry_run: bool = True,
        wakeup_event: WakeupEvent | None = None,
    ) -> dict[str, Any]:
        if self.realtime_voice is not None:
            decision = self.realtime_voice.decide_once(seconds=seconds, mode="command")
            return self.handle_voice_decision(decision, execute=execute, dry_run=dry_run, wakeup_event=wakeup_event)
        return {
            "ok": False,
            "error": "realtime_voice_not_configured",
            "message": "voice command path requires a configured realtime voice backend",
        }

    def handle_voice_decision(
        self,
        decision: dict[str, Any],
        execute: bool = False,
        dry_run: bool = True,
        wakeup_event: WakeupEvent | None = None,
        enqueue: bool = True,
    ) -> dict[str, Any]:
        pipeline_started_at = time.monotonic()
        pipeline_marks: dict[str, float] = {"pipeline_started": pipeline_started_at}
        trace_id = str(decision.get("trace_id") or "")
        if decision.get("ok", True):
            self.store.repair_runtime_state("handle_voice_decision_start", clear_ready_queue=True)
        pipeline_marks["runtime_repaired"] = time.monotonic()
        if not decision.get("ok", True):
            reply = str(decision.get("reply") or self._voice_decision_error_reply(str(decision.get("error") or "")))
            session = CommandSession(wakeup_event_id=wakeup_event.event_id if wakeup_event else None, session_type="voice")
            session.status = SessionStatus.FAILED.value
            session.ended_at = time.time()
            session.utterances.append({"role": "user", "text": "", "timestamp": time.time(), "decision_error": decision.get("error"), "decision_backend": self.voice_backend})
            self.store.save_session(session)
            self.store.append_event(
                "doubao_decision_empty",
                {
                    "session_id": session.session_id,
                    "error": decision.get("error"),
                    "invalid_skill": decision.get("invalid_skill"),
                    "reason": decision.get("reason"),
                    "raw_text": decision.get("raw_text"),
                },
            )
            self.audio.speak_text(reply)
            return {"ok": False, "session": session.session_id, "error": decision.get("error") or "doubao_decision_empty", "decision": decision}

        session = CommandSession(wakeup_event_id=wakeup_event.event_id if wakeup_event else None, session_type="voice")
        user_text = str(decision.get("user_text") or "")
        if not user_text:
            user_text = self._voice_decision_fallback_user_text(decision)
        decision = self._postprocess_voice_decision(decision, user_text)
        decision = self._recover_bare_voice_answer_decision(decision)
        pipeline_marks["decision_postprocessed"] = time.monotonic()
        user_text = str(decision.get("user_text") or user_text or self._voice_decision_fallback_user_text(decision))
        memory_result = self._handle_memory_turn(user_text, decision, session, execute=execute)
        if memory_result is not None:
            return memory_result
        terminal_result = self._terminalize_interrupted_task_if_requested(
            decision,
            user_text,
            execute=execute,
            session=session,
        )
        if terminal_result is not None:
            return terminal_result
        interrupted_operation = self._route_interrupted_task_operation(
            decision,
            user_text,
            execute=execute,
            dry_run=dry_run,
            session=session,
        )
        if interrupted_operation is not None:
            return interrupted_operation
        if decision.get("decision_type") == "noop" and not decision.get("task_groups") and not decision.get("ask_user"):
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            session.utterances.append(
                {
                    "role": "user",
                    "text": user_text,
                    "timestamp": time.time(),
                    "decision_backend": self.voice_backend,
                    "noop_reason": decision.get("reason"),
                }
            )
            session.metadata["decision_backend"] = self.voice_backend
            session.metadata["noop_reason"] = decision.get("reason")
            self.store.save_session(session)
            self.store.append_event(
                "voice_noop_ignored",
                {
                    "session_id": session.session_id,
                    "reason": decision.get("reason"),
                    "raw_text": decision.get("raw_text"),
                    "wakeup_event_id": wakeup_event.event_id if wakeup_event else None,
                },
            )
            return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": [], "noop": True}
        routed_followup = self._route_voice_decision_to_pending_followup(
            decision,
            user_text,
            execute=execute,
            dry_run=dry_run,
            enqueue=enqueue,
            wakeup_event=wakeup_event,
            command_session=session,
        )
        if routed_followup is not None:
            return routed_followup
        session.utterances.append({"role": "user", "text": user_text, "timestamp": time.time(), "decision_backend": self.voice_backend})
        session.metadata["decision_backend"] = self.voice_backend
        if decision.get("trace_id"):
            session.metadata["voice_trace_id"] = str(decision.get("trace_id"))
        if isinstance(decision.get("timing"), dict):
            session.metadata["voice_timing_summary_ms"] = dict(decision["timing"].get("durations_ms") or {})
        if isinstance(decision.get("intent_analysis"), dict):
            session.metadata["intent_analysis"] = dict(decision["intent_analysis"])
            session.metadata["semantic_adjudication_completed"] = bool(decision.get("semantic_adjudication_completed"))
        self.store.save_session(session)
        self.store.append_event(
            "command_session_started",
            {
                "session_id": session.session_id,
                "text": user_text,
                "decision_backend": self.voice_backend,
                "trace_id": decision.get("trace_id"),
            },
        )
        if isinstance(decision.get("intent_analysis"), dict):
            analysis = decision["intent_analysis"]
            self.store.append_event(
                "voice_intent_analysis",
                {
                    "session_id": session.session_id,
                    "speech_act": analysis.get("speech_act"),
                    "implied_goal": analysis.get("implied_goal"),
                    "actionable": bool(analysis.get("actionable")),
                    "authorization": analysis.get("authorization"),
                    "negated": bool(analysis.get("negated")),
                    "uncertain": bool(analysis.get("uncertain")),
                    "target_skill": analysis.get("target_skill"),
                    "target_action": analysis.get("target_action"),
                    "confidence": analysis.get("confidence"),
                    "materialized": bool(decision.get("intent_analysis_materialized")),
                    "adjudicated": bool(decision.get("semantic_adjudication_completed")),
                },
            )

        task_groups = self._task_groups_from_decision(session, decision)
        decision = self._apply_task_start_ack_policy(decision, task_groups, session.session_id)
        pipeline_marks["task_groups_built"] = time.monotonic()
        for task_group in task_groups:
            session.task_group_ids.append(task_group.task_group_id)
            self.store.save_task_group(task_group)
        pipeline_marks["task_groups_persisted"] = time.monotonic()

        if decision.get("ask_user"):
            if enqueue:
                self._enqueue_ready_session_task_groups(session)
            execution = self.drain_session_queue(session, dry_run=dry_run) if execute else {"executed": []}
            decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
            if has_runtime_ask:
                return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}
            waiting_task_group = self._find_pending_followup_task_group(session)
            if waiting_task_group is None:
                decision = self._drop_unbound_ask_user(decision, session, execution, "no_waiting_task_group_after_execution")
                session.status = SessionStatus.COMPLETED.value
                session.ended_at = time.time()
                self.store.save_session(session)
                return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}
            session.status = SessionStatus.WAITING_USER.value
            self.store.save_session(session)
            if execute:
                self._prepare_followup_voice([waiting_task_group], decision["ask_user"])
                self.audio.speak_text(decision["ask_user"]["question"])
            return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}

        reply = str(decision.get("reply") or "")
        if reply and not task_groups:
            self.audio.speak_text(reply)
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            self.store.save_session(session)
            return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": []}

        ack_pool: concurrent.futures.ThreadPoolExecutor | None = None
        ack_future: concurrent.futures.Future | None = None
        if reply and execute:
            if self._can_overlap_ack_with_startup(task_groups):
                ack_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                ack_future = ack_pool.submit(self.audio.speak_text, reply)
            else:
                self.audio.speak_text(reply)

        if enqueue:
            self._enqueue_ready_session_task_groups(session)

        try:
            pipeline_marks["execution_started"] = time.monotonic()
            execution = self.drain_session_queue(session, dry_run=dry_run) if execute else {"executed": []}
            pipeline_marks["execution_finished"] = time.monotonic()
        finally:
            if ack_future is not None:
                try:
                    ack_future.result()
                finally:
                    assert ack_pool is not None
                    ack_pool.shutdown(wait=True)
        decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
        if not has_runtime_ask:
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            self.store.save_session(session)
        pipeline_marks["pipeline_finished"] = time.monotonic()
        self.store.append_event(
            "voice_decision_pipeline_timing",
            {
                "trace_id": trace_id or decision.get("trace_id"),
                "session_id": session.session_id,
                "task_group_ids": list(session.task_group_ids),
                "marks_ms_from_pipeline_start": {
                    key: round((value - pipeline_started_at) * 1000.0, 3)
                    for key, value in pipeline_marks.items()
                },
                "durations_ms": {
                    "runtime_repair": round((pipeline_marks["runtime_repaired"] - pipeline_started_at) * 1000.0, 3),
                    "decision_postprocess": round(
                        (pipeline_marks["decision_postprocessed"] - pipeline_marks["runtime_repaired"]) * 1000.0,
                        3,
                    ),
                    "task_group_build": round(
                        (pipeline_marks["task_groups_built"] - pipeline_marks["decision_postprocessed"]) * 1000.0,
                        3,
                    ),
                    "task_group_persist": round(
                        (pipeline_marks["task_groups_persisted"] - pipeline_marks["task_groups_built"]) * 1000.0,
                        3,
                    ),
                    "execution_and_skill_speech": round(
                        (pipeline_marks["execution_finished"] - pipeline_marks["execution_started"]) * 1000.0,
                        3,
                    ),
                    "pipeline_total": round((pipeline_marks["pipeline_finished"] - pipeline_started_at) * 1000.0, 3),
                },
            },
        )
        return {"ok": True, "session": session.session_id, "decision": decision, "task_group_ids": session.task_group_ids, "execution": execution}

    def _route_interrupted_task_operation(
        self,
        decision: dict[str, Any],
        user_text: str,
        *,
        execute: bool,
        dry_run: bool,
        session: CommandSession,
    ) -> dict[str, Any] | None:
        interrupted = self.store.peek_interrupted()
        if interrupted is None:
            return None
        interaction = self.interactions.classify(decision, user_text, has_pending_followup=False)
        if interaction.interaction_type == TASK_REPLACEMENT:
            self.cancel_last_interrupted(reason="replaced_by_user_command")
            return None
        if interaction.interaction_type == TASK_CANCEL and not decision.get("task_groups"):
            result = self.cancel_last_interrupted(reason="cancelled_by_user_command")
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            session.utterances.append({"role": "user", "text": user_text, "kind": "task_cancel", "timestamp": time.time()})
            self.store.save_session(session)
            return {
                "ok": True,
                "session": session.session_id,
                "decision": {"decision_type": "answer", "interaction_type": TASK_CANCEL, "reply": "好的，已取消刚才的任务", "task_groups": [], "ask_user": None},
                "task_group_ids": [],
                "cancel": result,
            }
        if interaction.interaction_type == TASK_PAUSE and not decision.get("task_groups"):
            interrupted.metadata.update({"suspend_reason": "user_pause", "paused_at": time.time()})
            self.store.save_task_group(interrupted)
            reply = interaction.reply or self.speech_policy.paused(interrupted)
            if execute:
                self.audio.speak_text(reply)
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            session.utterances.append({"role": "user", "text": user_text, "kind": "task_pause", "timestamp": time.time()})
            self.store.save_session(session)
            return {
                "ok": True,
                "session": session.session_id,
                "decision": {"decision_type": "answer", "interaction_type": TASK_PAUSE, "reply": reply, "task_groups": [], "ask_user": None},
                "task_group_ids": [],
                "paused_task_group_id": interrupted.task_group_id,
            }
        if interaction.interaction_type in {TASK_RESUME, TASK_RESTART} and not decision.get("task_groups"):
            if interaction.interaction_type == TASK_RESTART:
                self._restart_interrupted_task_group(interrupted, interaction)
            reply = self.speech_policy.resume_ack(interrupted, restart=interaction.interaction_type == TASK_RESTART)
            if execute:
                self.audio.speak_text(reply)
            result = self.resume_last_interrupted(execute=execute, dry_run=dry_run, restore_scene=True)
            session.status = SessionStatus.COMPLETED.value if result.get("ok") else SessionStatus.FAILED.value
            session.ended_at = time.time()
            session.utterances.append(
                {
                    "role": "user",
                    "text": user_text,
                    "kind": interaction.interaction_type,
                    "task_group_id": interrupted.task_group_id,
                    "timestamp": time.time(),
                }
            )
            self.store.save_session(session)
            return {
                "ok": bool(result.get("ok")),
                "session": session.session_id,
                "decision": {"decision_type": "answer", "interaction_type": interaction.interaction_type, "reply": reply, "task_groups": [], "ask_user": None},
                "task_group_ids": [interrupted.task_group_id],
                "resume": result,
            }
        return None

    def _terminalize_interrupted_task_if_requested(
        self,
        decision: dict[str, Any],
        user_text: str,
        *,
        execute: bool,
        session: CommandSession,
    ) -> dict[str, Any] | None:
        interrupted = self.store.peek_interrupted()
        if interrupted is None:
            return None
        active_skill = str((interrupted.resume_context or {}).get("active_skill") or "")
        if not active_skill:
            return None
        spec = self.registry.get(active_skill) or {}
        terminal_actions = {str(item).strip().lower() for item in spec.get("terminal_actions") or [] if str(item).strip()}
        terminal_markers = [str(item).strip() for item in spec.get("terminal_utterance_markers_zh") or [] if str(item).strip()]
        requested_by_step = False
        for group in decision.get("task_groups") or []:
            for step in group.get("steps") or []:
                if str(step.get("skill_name") or "") != active_skill:
                    continue
                action = str((step.get("arguments") or {}).get("action") or "").strip().lower()
                if action in terminal_actions:
                    requested_by_step = True
                    break
        requested_by_text = any(marker in user_text for marker in terminal_markers)
        if not requested_by_step and not requested_by_text:
            return None
        result = self.cancel_last_interrupted(reason="terminal_action_for_interrupted_task", speak=False)
        reply = str(spec.get("terminal_reply_zh") or self.speech_policy.cancelled(interrupted))
        if execute and reply:
            self.audio.speak_text(reply)
        session.status = SessionStatus.COMPLETED.value
        session.ended_at = time.time()
        session.utterances.append({"role": "user", "text": user_text, "kind": "task_terminal_action", "timestamp": time.time()})
        session.metadata.update({"decision_backend": "doubao_realtime", "terminalized_task_group_id": interrupted.task_group_id})
        self.store.save_session(session)
        self.store.append_event(
            "interrupted_task_terminalized_by_command",
            {"task_group_id": interrupted.task_group_id, "skill_name": active_skill, "user_text": user_text},
        )
        return {
            "ok": bool(result.get("ok", True)),
            "session": session.session_id,
            "decision": {"decision_type": "answer", "interaction_type": TASK_CANCEL, "reply": reply, "task_groups": [], "ask_user": None},
            "task_group_ids": [],
            "terminalized": result,
        }

    def _restart_interrupted_task_group(self, task_group: TaskGroup, interaction: Interaction) -> None:
        for step in task_group.steps:
            step.status = TaskStatus.NEW.value
            step.started_at = None
            step.ended_at = None
            step.result = None
            step.error = None
            for key in ("initial_count", "current_count", "resume_count", "visited_points", "current_point_index"):
                step.arguments.pop(key, None)
        task_group.resume_context = {}
        task_group.result_summary = ""
        task_group.ended_at = None
        previous_revision = int(task_group.metadata.get("revision", 1))
        task_group.metadata.update(
            {
                "revision": previous_revision + 1,
                "supersedes_revision": previous_revision,
                "restart_requested_at": time.time(),
                "restart_user_text": interaction.text,
            }
        )
        self.store.save_task_group(task_group)

    @staticmethod
    def _can_overlap_ack_with_startup(task_groups: list[TaskGroup]) -> bool:
        startup_heavy = {
            "environment_perception",
            "face_recognition",
            "face_registration",
            "head_control",
            "person_tracking",
            "pet_tracking",
            "squat",
            "push_up",
            "pull_up",
        }
        for task_group in task_groups:
            for step in sorted(task_group.steps, key=lambda item: item.order):
                if step.status == TaskStatus.COMPLETED.value:
                    continue
                return step.skill_name in startup_heavy
        return False

    def _apply_task_start_ack_policy(
        self,
        decision: dict[str, Any],
        task_groups: list[TaskGroup],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if decision.get("ask_user") or not task_groups:
            return decision
        skill_names = [
            step.skill_name
            for task_group in task_groups
            for step in task_group.steps
            if step.skill_name and step.status != TaskStatus.COMPLETED.value
        ]
        intent_analysis = decision.get("intent_analysis")
        semantic_action_authorized = bool(
            isinstance(intent_analysis, dict)
            and intent_analysis.get("actionable")
            and not intent_analysis.get("negated")
            and not intent_analysis.get("uncertain")
            and str(intent_analysis.get("authorization") or "") in {"explicit", "pragmatically_implied"}
        )
        if not skill_names:
            return decision
        if not semantic_action_authorized and any(self.registry.should_speak_start_ack(name) for name in skill_names):
            return decision
        updated = dict(decision)
        original_reply = str(updated.get("reply") or "").strip()
        updated["reply"] = ""
        updated["task_start_ack_policy"] = "final_summary_only"
        if original_reply:
            updated["suppressed_start_ack"] = original_reply
        self.store.append_event(
            "task_start_ack_suppressed",
            {
                "session_id": session_id,
                "skill_names": skill_names,
                "suppressed_text": original_reply,
                "policy": "final_summary_only",
                "semantic_action_authorized": semantic_action_authorized,
            },
        )
        return updated

    def _postprocess_voice_decision(self, decision: dict[str, Any], user_text: str) -> dict[str, Any]:
        if not isinstance(decision, dict):
            return decision
        if not decision.get("ok", True):
            return decision
        if decision.get("decision_type") not in {"task_plan", "ask_user", "noop", "answer", None}:
            return decision
        authoritative = bool(decision.get("authoritative_user_text") and decision.get("asr_text"))
        try:
            copied = dict(decision)
            if authoritative and user_text and copied.get("recovered_from_incomplete_json"):
                fallback = self.planner._local_fallback_plan(user_text)
                if self._fallback_decision_is_specific(fallback):
                    copied = dict(fallback)
                    copied.update(
                        {
                            "user_text": user_text,
                            "asr_text": user_text,
                            "asr_text_source": decision.get("asr_text_source"),
                            "authoritative_user_text": True,
                            "recovered_from_incomplete_json": True,
                            "recovered_from_asr_due_incomplete_model": True,
                            "model_error": decision.get("model_error") or decision.get("error"),
                            "model_confidence_before_recovery": decision.get("confidence"),
                        }
                    )
            if authoritative and user_text and not copied.get("task_groups") and not copied.get("ask_user"):
                semantic_analysis = copied.get("intent_analysis")
                if not isinstance(semantic_analysis, dict) and not copied.get("semantic_adjudication_completed"):
                    fallback = self.planner._local_fallback_plan(user_text)
                    fallback_is_specific = self._fallback_decision_is_specific(fallback)
                    model_answer_is_usable = bool(
                        copied.get("ok", True)
                        and copied.get("decision_type") == "answer"
                        and str(copied.get("reply") or "").strip()
                    )
                    # Preserve a substantive model answer for ordinary conversation.
                    # Replace it only when authoritative ASR proves a concrete command
                    # or the model did not produce a usable answer at all.
                    if fallback_is_specific or not model_answer_is_usable:
                        copied = fallback
                        copied.update(
                            {
                                "user_text": user_text,
                                "asr_text": user_text,
                                "asr_text_source": decision.get("asr_text_source"),
                                "authoritative_user_text": True,
                                "recovered_from_asr_only": True,
                                "model_error": decision.get("model_error") or decision.get("error"),
                            }
                        )
            return self.planner._postprocess_decision(
                copied,
                user_text,
                authoritative_user_text=authoritative,
            )
        except Exception as exc:
            copied = dict(decision)
            copied.setdefault("postprocess_error", str(exc))
            return copied

    @staticmethod
    def _fallback_decision_is_specific(decision: dict[str, Any]) -> bool:
        for group in decision.get("task_groups") or []:
            if isinstance(group, dict) and any(
                isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "").strip()
                for step in group.get("steps") or []
            ):
                return True
        ask_user = decision.get("ask_user")
        if not isinstance(ask_user, dict):
            return False
        missing = {str(item) for item in ask_user.get("missing_slots") or []}
        return bool(missing and missing != {"intent"})

    def _drop_unbound_ask_user(self, decision: dict[str, Any], session: CommandSession, execution: dict[str, Any], reason: str) -> dict[str, Any]:
        dropped = dict(decision)
        dropped["ask_user"] = None
        if dropped.get("decision_type") == "ask_user":
            dropped["decision_type"] = "task_plan" if dropped.get("task_groups") else "noop"
        dropped["dropped_ask_user"] = {
            "reason": reason,
            "ask_user": decision.get("ask_user"),
        }
        self.store.append_event(
            "ask_user_dropped_without_waiting_task_group",
            {
                "session_id": session.session_id,
                "reason": reason,
                "ask_user": decision.get("ask_user"),
                "execution": execution,
            },
        )
        return dropped

    def _recover_bare_voice_answer_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict):
            return decision
        if decision.get("decision_type") != "answer":
            return decision
        if str(decision.get("interaction_type") or "").strip().lower() in {
            "conversation",
            "capability_question",
            "information_question",
            "social",
        }:
            return decision
        if decision.get("task_groups") or isinstance(decision.get("ask_user"), dict):
            return decision
        text = str(decision.get("reply") or decision.get("user_text") or "").strip()
        if not text or self.realtime_voice is None:
            return decision
        try:
            recovered = self.realtime_voice._parse_decision(text, "command")
        except Exception as exc:
            copied = dict(decision)
            copied.setdefault("bare_answer_recovery_error", str(exc))
            return copied
        if not isinstance(recovered, dict):
            return decision
        recovered_is_structured = bool(recovered.get("task_groups") or isinstance(recovered.get("ask_user"), dict))
        recovered_is_echo_noop = recovered.get("decision_type") == "noop" and recovered.get("reason") == "robot_prompt_echo"
        recovered_from_assistant_text = bool(recovered.get("recovered_from_non_json_text") or recovered.get("recovered_from_assistant_intent"))
        if not ((recovered_is_structured and recovered_from_assistant_text) or recovered_is_echo_noop):
            return decision
        recovered = dict(recovered)
        recovered.setdefault("user_text", decision.get("user_text") or recovered.get("user_text") or "")
        recovered["recovered_from_bare_answer_decision"] = {
            "reply": decision.get("reply"),
            "user_text": decision.get("user_text"),
            "confidence": decision.get("confidence"),
        }
        return recovered

    @staticmethod
    def _voice_decision_fallback_user_text(decision: dict[str, Any]) -> str:
        for group in decision.get("task_groups") or []:
            if not isinstance(group, dict):
                continue
            for key in ("user_instruction", "title"):
                value = str(group.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _voice_decision_error_reply(error: str) -> str:
        lowered = error.lower()
        if "device or resource busy" in lowered or "arecord failed" in lowered:
            return "麦克风刚才还在忙，这句话没有录完整。"
        if "invalid_skill" in lowered or "unknown skill" in lowered:
            return "这个功能现在还不能使用。"
        if "invalid_json_decision" in lowered or "incomplete_json_decision" in lowered:
            return "这句话我只听明白了一部分，没有贸然执行。"
        if "connection" in lowered or "websocket" in lowered or "close frame" in lowered:
            return "刚才的语音连接中断了，这句话没有处理完成。"
        return "我没有听清楚，请再说一遍"

    def _route_voice_decision_to_pending_followup(
        self,
        decision: dict[str, Any],
        user_text: str,
        *,
        execute: bool,
        dry_run: bool,
        enqueue: bool,
        wakeup_event: WakeupEvent | None = None,
        command_session: CommandSession | None = None,
        decision_backend: str = "doubao_realtime",
    ) -> dict[str, Any] | None:
        pending = self._best_pending_followup_task_group(decision, user_text)
        if pending is None:
            return None
        if not self._should_route_command_decision_to_followup(decision, user_text, pending):
            return None
        routed = dict(decision)
        routed["followup_text"] = self._followup_text_from_command_decision(routed, user_text)
        if wakeup_event is not None:
            routed["wakeup_event_id"] = wakeup_event.event_id
        result = self.answer_followup_decision(
            pending.task_group_id,
            routed,
            execute=execute,
            dry_run=dry_run,
            enqueue=enqueue,
        )
        task_group_session_id = result.get("session")
        result["routed_pending_followup"] = {
            "task_group_id": pending.task_group_id,
            "source": "command_session",
            "wakeup_event_id": wakeup_event.event_id if wakeup_event else None,
        }
        if command_session is not None:
            self._save_routed_followup_command_session(command_session, pending, routed, result, task_group_session_id, decision_backend=decision_backend)
            result["task_group_session"] = task_group_session_id
            result["session"] = command_session.session_id
        self.store.append_event(
            "command_session_routed_to_pending_followup",
            {
                "session_id": command_session.session_id if command_session else None,
                "task_group_id": pending.task_group_id,
                "task_group_session_id": task_group_session_id,
                "user_text": routed.get("followup_text"),
                "decision_type": decision.get("decision_type"),
                "wakeup_event_id": wakeup_event.event_id if wakeup_event else None,
            },
        )
        return result

    def _save_routed_followup_command_session(
        self,
        session: CommandSession,
        task_group: TaskGroup,
        routed_decision: dict[str, Any],
        result: dict[str, Any],
        task_group_session_id: Any,
        *,
        decision_backend: str = "doubao_realtime",
    ) -> None:
        answer_text = self._followup_text_from_command_decision(routed_decision, "")
        session.utterances.append(
            {
                "role": "user",
                "text": answer_text,
                "timestamp": time.time(),
                "kind": "routed_followup_answer",
                "decision_backend": decision_backend,
                "task_group_id": task_group.task_group_id,
                "task_group_session_id": task_group_session_id,
            }
        )
        session.task_group_ids = [task_group.task_group_id]
        decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
        session.status = SessionStatus.WAITING_USER.value if isinstance(decision.get("ask_user"), dict) else SessionStatus.COMPLETED.value
        session.ended_at = None if session.status == SessionStatus.WAITING_USER.value else time.time()
        session.metadata.update(
            {
                "decision_backend": decision_backend,
                "routed_to_existing_task_group": True,
                "routed_task_group_id": task_group.task_group_id,
                "task_group_session_id": task_group_session_id,
                "route_reason": "pending_followup_answer",
            }
        )
        self.store.save_session(session)

    def _best_pending_followup_task_group(self, decision: dict[str, Any], user_text: str) -> TaskGroup | None:
        text = self._followup_text_from_command_decision(decision, user_text)
        candidates: list[tuple[int, float, TaskGroup]] = []
        for task_group in self._pending_followup_task_groups():
            if not self._should_route_command_decision_to_followup(decision, text, task_group):
                continue
            pending = self._pending_followup(task_group) or {}
            timestamp = pending.get("timestamp") or task_group.started_at or task_group.created_at
            try:
                score_time = float(timestamp)
            except (TypeError, ValueError):
                score_time = float(task_group.created_at or 0.0)
            candidates.append((self._pending_followup_route_score(task_group, pending, text, decision), score_time, task_group))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    def _latest_pending_followup_task_group(self) -> TaskGroup | None:
        candidates = self._pending_followup_task_groups()
        if not candidates:
            return None
        candidates.sort(key=self._pending_followup_sort_timestamp, reverse=True)
        return candidates[0]

    def _pending_followup_sort_timestamp(self, task_group: TaskGroup) -> float:
        pending = self._pending_followup(task_group) or {}
        value = pending.get("timestamp") or task_group.started_at or task_group.created_at or 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(task_group.created_at or 0.0)

    def _pending_followup_task_groups(self) -> list[TaskGroup]:
        task_dir = self.store.root / "task_groups"
        if not task_dir.exists():
            return []
        candidates: list[TaskGroup] = []
        for path in task_dir.glob("*.json"):
            try:
                task_group = self.store.load_task_group(path.stem)
            except Exception:
                continue
            if task_group.status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.INTERRUPTED.value}:
                continue
            if not self._is_task_group_waiting_for_followup(task_group):
                continue
            candidates.append(task_group)
        return candidates

    def _pending_followup_route_score(self, task_group: TaskGroup, pending: dict[str, Any], text: str, decision: dict[str, Any]) -> int:
        score = 0
        missing = {str(item) for item in pending.get("missing_slots") or []}
        candidates = {str(item) for item in pending.get("candidate_skills") or []}
        if "environment_override" in missing:
            action = self._classify_environment_override_reply(text)
            if action in {"continue", "continue_without_projector", "cancel"}:
                score += 120
            if self._environment_override_answer_has_known_point(text):
                score += 90
        exercise = self._extract_followup_exercise_type(text)
        where = self._extract_followup_where(text)
        projector = self._classify_projector_followup_reply(pending, text)
        movement_skill = self._extract_followup_movement_skill(text)
        if exercise:
            score += 100
        if where:
            score += 70
        if projector is not None:
            score += 50
        if movement_skill and ("direction" in missing or candidates & {"move_forward", "move_backward", "move_left", "move_right"}):
            score += 100
        if candidates & FITNESS_SKILLS:
            score += 10
        decision_skills = self._decision_skill_names(decision)
        if decision_skills and candidates:
            score += 30 if self._decision_skills_match_pending(decision_skills, candidates) else -100
        if self._pending_followup(task_group) and not (self._pending_followup(task_group) or {}).get("answer"):
            score += 5
        return score

    def _should_route_command_decision_to_followup(self, decision: dict[str, Any], user_text: str, task_group: TaskGroup) -> bool:
        text = self._followup_text_from_command_decision(decision, user_text)
        if not text:
            return False
        pending = self._pending_followup(task_group) or {}
        decision_skills = self._decision_skill_names(decision)
        pending_skills = {str(item) for item in pending.get("candidate_skills") or []}
        if decision_skills and pending_skills and not self._decision_skills_match_pending(decision_skills, pending_skills):
            return False
        if self._followup_answer_matches_pending(task_group, pending, text):
            return True
        return False

    @staticmethod
    def _followup_text_from_command_decision(decision: dict[str, Any], user_text: str) -> str:
        for key in ("followup_text", "user_text", "text"):
            value = str(decision.get(key) or "").strip()
            if value:
                return value
        raw = decision.get("raw_decision")
        if isinstance(raw, dict):
            for key in ("followup_text", "user_text", "text", "answer"):
                value = str(raw.get(key) or "").strip()
                if value:
                    return value
        return str(user_text or "").strip()

    def _decision_skill_names(self, decision: dict[str, Any]) -> set[str]:
        skills: set[str] = set()
        for group in decision.get("task_groups") or []:
            if not isinstance(group, dict):
                continue
            for step in group.get("steps") or []:
                if isinstance(step, dict):
                    skill = str(step.get("skill_name") or step.get("name") or "").strip()
                    if skill:
                        skills.add(skill)
        return skills

    @staticmethod
    def _decision_skills_match_pending(decision_skills: set[str], pending_skills: set[str]) -> bool:
        support_skills = {"head_control", "environment_perception", "navigation_goto", "projector_control"}
        allowed = set(pending_skills) | support_skills
        return bool(decision_skills & allowed) and decision_skills <= allowed

    def _followup_answer_matches_pending(self, task_group: TaskGroup, pending: dict[str, Any], text: str) -> bool:
        if self._looks_like_robot_followup_prompt(text):
            return False
        missing = {str(item) for item in pending.get("missing_slots") or []}
        if "intent" in missing:
            return False
        if self._direct_followup_answer_matches_pending(task_group, pending, text):
            return True
        cleaned = re.sub(r"[\s\u3000，。,.！？!、；;：:]+", "", text or "")
        generic_answer_markers = (
            "就在这里",
            "这里",
            "继续",
            "可以",
            "好的",
            "不用",
            "不要",
            "打开",
            "投影",
            "换地方",
            "取消",
            "算了",
            "深蹲",
            "俯卧撑",
            "引体",
        )
        return any(marker in cleaned for marker in generic_answer_markers)

    def _direct_followup_answer_matches_pending(self, task_group: TaskGroup, pending: dict[str, Any], text: str) -> bool:
        missing = {str(item) for item in pending.get("missing_slots") or []}
        candidates = {str(item) for item in pending.get("candidate_skills") or []}
        if "environment_override" in missing:
            return self._classify_environment_override_reply(text) != "planner" or self._environment_override_answer_has_known_point(text)
        if self._classify_projector_followup_reply(pending, text) is not None:
            return True
        if "exercise_type" in missing or candidates & FITNESS_SKILLS or any(step.skill_name in FITNESS_SKILLS for step in task_group.steps):
            if self._extract_followup_exercise_type(text) or self._extract_followup_where(text):
                return True
        if "direction" in missing or candidates & {"move_forward", "move_backward", "move_left", "move_right"}:
            if self._extract_followup_movement_skill(text):
                return True
        return False

    def handle_heard_voice(
        self,
        heard: dict[str, Any],
        execute: bool = False,
        dry_run: bool = True,
        wakeup_event: WakeupEvent | None = None,
    ) -> dict[str, Any]:
        if not heard.get("text"):
            session = CommandSession(wakeup_event_id=wakeup_event.event_id if wakeup_event else None, session_type="voice")
            session.status = SessionStatus.FAILED.value
            session.ended_at = time.time()
            session.utterances.append(
                {
                    "role": "user",
                    "text": "",
                    "timestamp": time.time(),
                    "audio_path": heard.get("audio_path"),
                    "asr_error": heard.get("error", "empty_asr_text"),
                }
            )
            self.store.save_session(session)
            self.store.append_event("asr_empty", {"session_id": session.session_id, "audio_path": heard.get("audio_path"), "error": heard.get("error")})
            self.audio.speak_text("我没有听清楚，请再说一遍")
            return {"ok": False, "session": session.session_id, "error": "asr_empty", "heard": heard}
        return self.handle_text(heard["text"], session_type="voice", execute=execute, dry_run=dry_run, enqueue=True, wakeup_event=wakeup_event)

    def handle_followup_voice(self, task_group_id: str, seconds: float | None = None, execute: bool = False, dry_run: bool = True) -> dict[str, Any]:
        if self.realtime_voice is not None:
            try:
                task_group = self.store.load_task_group(task_group_id)
                pending = self._pending_followup(task_group) or {}
            except Exception:
                task_group = None
                pending = {}
            context = {
                "question": pending.get("question"),
                "task_group": self._task_group_to_decision_payload(task_group) if task_group is not None else {},
            }
            result = self.realtime_voice.decide_once(seconds=seconds, mode="followup", context=context)
            if self._is_followup_question_echo(result, pending):
                self.store.append_event(
                    "followup_question_echo_ignored",
                    {
                        "task_group_id": task_group_id,
                        "question": pending.get("question"),
                        "heard": self._followup_echo_texts(result),
                    },
                )
                return {"ok": False, "task_group_id": task_group_id, "error": "robot_question_echo", "heard": result}
            if not result.get("ok", True):
                return {"ok": False, "error": result.get("error") or "doubao_followup_empty", "heard": result}
            answer_text = self._plain_followup_answer_text(result)
            answer_text, contextual_result = self._contextualize_followup_answer(
                task_group,
                pending,
                result,
                answer_text,
                execute=execute,
            )
            if contextual_result is not None:
                return contextual_result
            if answer_text != self._plain_followup_answer_text(result):
                result = dict(result)
                result["contextual_original_text"] = self._plain_followup_answer_text(result)
                result["followup_text"] = answer_text
                result["user_text"] = answer_text
            memory_routed = self._route_followup_memory(
                task_group,
                pending,
                result,
                answer_text,
                execute=execute,
            )
            if memory_routed is not None:
                return memory_routed
            explicit_followup_types = {
                TASK_MODIFICATION,
                TASK_CANCEL,
                TASK_PAUSE,
                TASK_RESTART,
                TASK_REPLACEMENT,
                TEMPORARY_TASK,
                TASK_QUERY,
                CONVERSATION,
            }
            if (
                self._followup_answer_matches_pending(task_group, pending, answer_text)
                and str(result.get("interaction_type") or "").strip().lower() not in explicit_followup_types
            ):
                result = dict(result)
                result["interaction_type"] = FOLLOWUP_ANSWER
                result["task_operation"] = "none"
            routed = self._route_followup_interaction(
                task_group_id,
                task_group,
                pending,
                result,
                answer_text,
                execute=execute,
                dry_run=dry_run,
            )
            if routed is not None:
                return routed
            invalid_answer = self._invalid_followup_answer_result(task_group_id, task_group, pending, answer_text, result)
            if invalid_answer is not None:
                return invalid_answer
            if answer_text and self._looks_like_assistant_followup_prose(answer_text) and not self._followup_answer_matches_pending(task_group, pending, answer_text):
                self.store.append_event(
                    "followup_assistant_prose_ignored",
                    {
                        "task_group_id": task_group_id,
                        "question": pending.get("question"),
                        "heard": answer_text,
                    },
                )
                return {"ok": False, "task_group_id": task_group_id, "error": "assistant_followup_prose", "heard": result}
            if self._should_force_plain_followup_route(pending, result, answer_text):
                self.store.append_event(
                    "followup_forced_to_task_group",
                    {
                        "task_group_id": task_group_id,
                        "question": pending.get("question"),
                        "decision_type": result.get("decision_type"),
                        "has_task_groups": bool(result.get("task_groups")),
                        "has_ask_user": bool(result.get("ask_user")),
                        "answer_text": answer_text,
                    },
                )
                return self.answer_followup(task_group_id, answer_text, execute=execute, dry_run=dry_run, enqueue=True)
            if not result.get("task_groups") and not result.get("ask_user"):
                if not answer_text:
                    return {"ok": False, "error": "doubao_followup_missing_decision", "heard": result}
                if self._looks_like_assistant_followup_prose(answer_text):
                    self.store.append_event(
                        "followup_assistant_prose_replanned",
                        {
                            "task_group_id": task_group_id,
                            "question": pending.get("question"),
                            "heard": answer_text,
                        },
                    )
                return self.answer_followup(task_group_id, answer_text, execute=execute, dry_run=dry_run, enqueue=True)
            return self.answer_followup_decision(task_group_id, result, execute=execute, dry_run=dry_run, enqueue=True)
        return {
            "ok": False,
            "task_group_id": task_group_id,
            "error": "realtime_voice_not_configured",
            "message": "follow-up voice path requires a configured realtime voice backend",
        }

    def _is_followup_question_echo(self, decision: dict[str, Any], pending: dict[str, Any]) -> bool:
        question = str(pending.get("question") or "")
        norm_question = self._normalize_echo_compare_text(question)
        if len(norm_question) < 8:
            return False
        for text in self._followup_echo_texts(decision):
            norm_text = self._normalize_echo_compare_text(text)
            if len(norm_text) < 4:
                continue
            if norm_text == norm_question:
                return True
            if len(norm_text) >= 8 and (norm_text in norm_question or norm_question in norm_text):
                return True
            ratio = difflib.SequenceMatcher(None, norm_text, norm_question).ratio()
            if ratio >= 0.78:
                return True
            if (
                "深蹲" in norm_text
                and "俯卧撑" in norm_text
                and "引体向上" in norm_text
                and ("投影" in norm_text or "地点" in norm_text or "这里" in norm_text)
            ):
                return True
        return False

    def _route_followup_memory(
        self,
        task_group: TaskGroup | None,
        pending: dict[str, Any],
        decision: dict[str, Any],
        answer_text: str,
        *,
        execute: bool,
    ) -> dict[str, Any] | None:
        if task_group is None:
            return None
        operation = self.user_memory.interpret(answer_text, decision)
        if operation.get("operation") == "none":
            return None
        memory_result = self.user_memory.apply(operation)
        if not memory_result.get("handled"):
            return None
        original_operation = self.user_memory.interpret(task_group.user_instruction, {})
        belongs_to_pending_task = original_operation.get("operation") in {"remember", "forget", "query"} and not task_group.steps
        if belongs_to_pending_task:
            pending["answer"] = answer_text
            pending["answered_at"] = time.time()
            task_group.status = TaskStatus.COMPLETED.value if memory_result.get("ok") else TaskStatus.FAILED.value
            task_group.ended_at = time.time()
            task_group.result_summary = str(memory_result.get("reply") or "")
            task_group.metadata.update({"dialogue_state": "completed", "memory_result": memory_result})
            self.store.save_task_group(task_group)
            try:
                session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else CommandSession(session_type="followup")
            except Exception:
                session = CommandSession(session_type="followup")
            session.status = SessionStatus.COMPLETED.value if memory_result.get("ok") else SessionStatus.FAILED.value
            session.ended_at = time.time()
            session.utterances.append(
                {
                    "role": "user",
                    "text": answer_text,
                    "kind": "memory_operation",
                    "memory_operation": operation.get("operation"),
                    "task_group_id": task_group.task_group_id,
                    "timestamp": time.time(),
                }
            )
            self.store.save_session(session)
            reply = str(memory_result.get("reply") or "")
            if execute and reply:
                self.audio.speak_text(reply)
            return {
                "ok": bool(memory_result.get("ok")),
                "session": session.session_id,
                "task_group_id": task_group.task_group_id,
                "decision": {"decision_type": "answer", "interaction_type": "conversation", "memory_operation": operation.get("operation"), "reply": reply, "task_groups": [], "ask_user": None},
                "execution": {"executed": []},
                "memory": memory_result,
            }
        interaction = self.interactions.classify(
            {"decision_type": "answer", "interaction_type": "conversation", "reply": memory_result.get("reply"), "confidence": 1.0},
            answer_text,
            has_pending_followup=True,
        )
        return self._preserve_followup_after_overlay(
            task_group,
            interaction,
            pending,
            str(memory_result.get("reply") or ""),
            execute=execute,
        )

    def _route_followup_interaction(
        self,
        task_group_id: str,
        task_group: TaskGroup | None,
        pending: dict[str, Any],
        decision: dict[str, Any],
        answer_text: str,
        *,
        execute: bool,
        dry_run: bool,
    ) -> dict[str, Any] | None:
        if task_group is None:
            return None
        interaction = self.interactions.classify(decision, answer_text, has_pending_followup=True)
        if interaction.interaction_type in {FOLLOWUP_ANSWER, TASK_RESUME}:
            return None
        if interaction.interaction_type == TASK_MODIFICATION:
            self._apply_task_group_revision(task_group, interaction, decision, reason="followup_task_modification")
            if self._interaction_answers_pending_slot(interaction, pending):
                return None
            return self._apply_unrelated_followup_modification(
                task_group,
                pending,
                decision,
                interaction,
                execute=execute,
                dry_run=dry_run,
            )
        if interaction.interaction_type == TASK_RESTART:
            self._restart_task_group_revision(task_group, interaction)
            return None if decision.get("task_groups") or interaction.slot_updates else self._preserve_followup_after_overlay(
                task_group, interaction, pending, self.speech_policy.resume_ack(task_group, restart=True), execute=execute
            )
        if interaction.interaction_type == TASK_CANCEL:
            return self._cancel_waiting_task_group(task_group, interaction, execute=execute)
        if interaction.interaction_type == TASK_PAUSE:
            task_group.metadata.update(
                {
                    "suspend_reason": "user_pause",
                    "dialogue_state": "waiting_followup",
                    "paused_at": time.time(),
                }
            )
            self.store.save_task_group(task_group)
            return self._preserve_followup_after_overlay(task_group, interaction, pending, self.speech_policy.paused(task_group), execute=execute)
        if interaction.interaction_type in {CONVERSATION, TASK_QUERY, AMBIGUOUS}:
            fallback = "刚才的问题还没有确认。" if interaction.interaction_type == AMBIGUOUS else ""
            return self._preserve_followup_after_overlay(
                task_group,
                interaction,
                pending,
                interaction.reply or fallback,
                execute=execute,
            )
        if interaction.interaction_type in {TEMPORARY_TASK, TASK_REPLACEMENT}:
            if not decision.get("task_groups"):
                return self._preserve_followup_after_overlay(
                    task_group,
                    interaction,
                    pending,
                    "我还不能确定你想执行的新任务，请再说具体一点。",
                    execute=execute,
                )
            if interaction.interaction_type == TEMPORARY_TASK:
                task_group.status = TaskStatus.INTERRUPTED.value
                task_group.resume_context = dict(task_group.resume_context or {})
                task_group.resume_context.update(
                    {
                        "can_resume": True,
                        "interrupted_at": time.time(),
                        "interrupt_reason": "temporary_task_during_followup",
                        "pending_followup": dict(pending),
                    }
                )
                task_group.metadata.update({"suspend_reason": "temporary_task", "dialogue_state": "interrupted_by_task"})
                self.store.push_interrupted(task_group)
            else:
                task_group.status = TaskStatus.CANCELLED.value
                task_group.ended_at = time.time()
                task_group.result_summary = "replaced"
                task_group.metadata.update({"replaced_at": time.time(), "replacement_reason": interaction.text})
                self.store.save_task_group(task_group)
            detached = dict(decision)
            detached["interaction_type"] = "command"
            detached["task_operation"] = "none"
            return self.handle_voice_decision(detached, execute=execute, dry_run=dry_run, enqueue=True)
        return None

    @staticmethod
    def _interaction_answers_pending_slot(interaction: Interaction, pending: dict[str, Any]) -> bool:
        asked = set(str(item) for item in pending.get("asked_slots") or [])
        if not asked:
            asked.update(str(item) for item in pending.get("missing_slots") or [])
            asked.update(str(item) for item in pending.get("optional_slots") or [])
        updates = set(interaction.slot_updates)
        updates.update(interaction.slot_clears)
        return bool(asked & updates)

    def _apply_unrelated_followup_modification(
        self,
        task_group: TaskGroup,
        pending: dict[str, Any],
        decision: dict[str, Any],
        interaction: Interaction,
        *,
        execute: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        pending["closed_at"] = time.time()
        pending["close_reason"] = "superseded_by_unrelated_task_modification"
        pending["modification_text"] = interaction.text
        try:
            session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else CommandSession(session_type="followup")
        except Exception:
            session = CommandSession(session_type="followup")
        session.utterances.append(
            {
                "role": "user",
                "text": interaction.text,
                "kind": "task_modification",
                "task_group_id": task_group.task_group_id,
                "timestamp": time.time(),
            }
        )
        new_groups = self._task_groups_from_decision(session, decision)
        if new_groups:
            replacement = self._select_followup_replacement(task_group, new_groups)
            task_group.title = replacement.title or task_group.title
            task_group.slots.update(replacement.slots)
            task_group.steps = self._merge_replanned_steps(task_group, replacement.steps)
        ask_user = decision.get("ask_user") if isinstance(decision.get("ask_user"), dict) else None
        if ask_user:
            self._append_pending_followup(task_group, ask_user)
            task_group.status = TaskStatus.NEEDS_INFO.value
            task_group.metadata["dialogue_state"] = "waiting_followup"
            session.status = SessionStatus.WAITING_USER.value
            session.ended_at = None
        else:
            task_group.status = TaskStatus.NEW.value
            task_group.metadata["dialogue_state"] = "ready"
        self.store.save_task_group(task_group)
        self.store.save_session(session)
        execution = {"executed": []}
        if ask_user:
            if execute:
                self._prepare_followup_voice([task_group], ask_user)
                self.audio.speak_text(str(ask_user.get("question") or decision.get("reply") or ""))
        else:
            self._enqueue_ready_session_task_groups(session)
            if execute:
                execution = self.drain_session_queue(session, dry_run=dry_run)
        return {
            "ok": True,
            "session": session.session_id,
            "task_group_id": task_group.task_group_id,
            "decision": decision,
            "execution": execution,
            "task_modified": True,
            "answered_pending_slot": False,
        }

    def _apply_task_group_revision(
        self,
        task_group: TaskGroup,
        interaction: Interaction,
        decision: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        previous_revision = int(task_group.metadata.get("revision", 1))
        before_slots = dict(task_group.slots or {})
        task_group.slots.update(interaction.slot_updates)
        for slot in interaction.slot_clears:
            task_group.slots.pop(slot, None)
        task_group.metadata["revision"] = previous_revision + 1
        task_group.metadata["supersedes_revision"] = previous_revision
        task_group.metadata.setdefault("slot_change_history", []).append(
            {
                "timestamp": time.time(),
                "reason": reason,
                "user_text": interaction.text,
                "before": before_slots,
                "updates": dict(interaction.slot_updates),
                "clears": list(interaction.slot_clears),
                "after": dict(task_group.slots),
            }
        )
        task_group.metadata["dialogue_state"] = "waiting_followup"
        self.store.save_task_group(task_group)
        decision.setdefault("task_revision", task_group.metadata["revision"])

    def _restart_task_group_revision(self, task_group: TaskGroup, interaction: Interaction) -> None:
        self._apply_task_group_revision(task_group, interaction, {}, reason="user_restart")
        for step in task_group.steps:
            step.status = TaskStatus.NEW.value
            step.started_at = None
            step.ended_at = None
            step.result = None
            step.error = None
        task_group.result_summary = ""
        task_group.ended_at = None
        task_group.metadata["restart_requested_at"] = time.time()
        task_group.resume_context = {}
        self.store.save_task_group(task_group)

    def _cancel_waiting_task_group(self, task_group: TaskGroup, interaction: Interaction, *, execute: bool) -> dict[str, Any]:
        pending = self._pending_followup(task_group)
        if pending is not None:
            pending["cancelled_at"] = time.time()
            pending["cancel_reason"] = interaction.text
        task_group.status = TaskStatus.CANCELLED.value
        task_group.ended_at = time.time()
        task_group.result_summary = "cancelled"
        task_group.metadata.update({"dialogue_state": "cancelled", "cancelled_by_user": True})
        self.store.save_task_group(task_group)
        try:
            session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else None
        except Exception:
            session = None
        if session is not None:
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            session.utterances.append({"role": "user", "text": interaction.text, "kind": "task_cancel", "timestamp": time.time()})
            self.store.save_session(session)
        reply = interaction.reply or self.speech_policy.cancelled(task_group)
        if execute:
            self.audio.speak_text(reply)
        return {
            "ok": True,
            "session": task_group.command_session_id,
            "task_group_id": task_group.task_group_id,
            "decision": {"decision_type": "answer", "interaction_type": TASK_CANCEL, "reply": reply, "task_groups": [], "ask_user": None},
            "execution": {"executed": []},
            "cancelled": True,
        }

    def _preserve_followup_after_overlay(
        self,
        task_group: TaskGroup,
        interaction: Interaction,
        pending: dict[str, Any],
        reply: str,
        *,
        execute: bool,
    ) -> dict[str, Any]:
        task_group.status = TaskStatus.NEEDS_INFO.value
        task_group.metadata.update(
            {
                "dialogue_state": "waiting_followup",
                "suspended_by": interaction.interaction_type,
                "resume_policy": "return_to_pending_question",
            }
        )
        task_group.metadata.setdefault("dialogue_overlays", []).append(
            {
                "timestamp": time.time(),
                "interaction_type": interaction.interaction_type,
                "text": interaction.text,
                "reply": reply,
            }
        )
        self.store.save_task_group(task_group)
        try:
            session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else None
        except Exception:
            session = None
        if session is not None:
            session.status = SessionStatus.WAITING_USER.value
            session.ended_at = None
            session.utterances.append(
                {
                    "role": "user",
                    "text": interaction.text,
                    "kind": "dialogue_overlay",
                    "interaction_type": interaction.interaction_type,
                    "task_group_id": task_group.task_group_id,
                    "timestamp": time.time(),
                }
            )
            self.store.save_session(session)
        question = str(pending.get("question") or "").strip()
        reply = str(reply or "").strip()
        if reply in {"我明白了", "我明白了。", "好的", "好的。", "收到", "收到。"}:
            reply = "刚才这句话我没有完全确定你的意思。"
        if reply and question:
            spoken = f"{reply} 回到刚才的问题，{question}"
        else:
            spoken = reply or question
        if execute and spoken:
            self.audio.speak_text(spoken)
        ask_user = dict(pending)
        return {
            "ok": True,
            "session": task_group.command_session_id,
            "task_group_id": task_group.task_group_id,
            "decision": {
                "decision_type": "ask_user",
                "interaction_type": interaction.interaction_type,
                "task_operation": interaction.task_operation,
                "reply": spoken,
                "task_groups": [self._task_group_to_decision_payload(task_group)],
                "ask_user": ask_user,
                "confidence": interaction.confidence,
            },
            "execution": {"executed": []},
            "dialogue_overlay": True,
            "continue_followup": True,
            "non_slot_turn": True,
        }

    def _contextualize_followup_answer(
        self,
        task_group: TaskGroup | None,
        pending: dict[str, Any],
        decision: dict[str, Any],
        answer_text: str,
        *,
        execute: bool,
    ) -> tuple[str, dict[str, Any] | None]:
        if task_group is None or not isinstance(pending, dict):
            return answer_text, None
        missing = {str(item) for item in pending.get("missing_slots") or []}
        confirmation = pending.get("contextual_confirmation") if isinstance(pending.get("contextual_confirmation"), dict) else None
        direct_where = self._extract_followup_where(answer_text)
        if confirmation and confirmation.get("slot") == "where":
            if direct_where:
                self._record_contextual_followup_resolution(task_group, pending, answer_text, direct_where, "explicit_location")
                return self._canonical_where_answer(direct_where), None
            cleaned = re.sub(r"[\s\u3000，。,.！？!、；;：:]+", "", answer_text or "")
            if self._is_affirmative_resume_reply(cleaned):
                self._record_contextual_followup_resolution(task_group, pending, answer_text, "here", "affirmed_candidate")
                return "就在这里做", None
            if self._is_negative_contextual_confirmation(cleaned):
                original_question = str(confirmation.get("original_question") or "想在这里做，还是换个地方？")
                pending.pop("contextual_confirmation", None)
                pending["question"] = original_question
                pending["timestamp"] = time.time()
                self.store.save_task_group(task_group)
                if execute:
                    self._prepare_followup_voice([task_group], pending)
                    self.audio.speak_text(f"好，那我再确认一下。{original_question}")
                return answer_text, self._contextual_followup_wait_result(task_group, pending, answer_text, "candidate_rejected")
        if "where" not in missing or direct_where:
            return answer_text, None
        if not self._looks_like_ambiguous_here_answer(answer_text):
            return answer_text, None
        original_question = str(pending.get("question") or "想在这里做，还是换个地方？")
        question = "我不太确定刚才这句。你是想说就在这里做吗？"
        pending["contextual_confirmation"] = {
            "slot": "where",
            "candidate_value": "here",
            "heard_text": answer_text,
            "original_question": original_question,
            "created_at": time.time(),
        }
        pending["question"] = question
        pending["timestamp"] = time.time()
        task_group.status = TaskStatus.NEEDS_INFO.value
        task_group.metadata["dialogue_state"] = "waiting_contextual_confirmation"
        self.store.save_task_group(task_group)
        if execute:
            self._prepare_followup_voice([task_group], pending)
            self.audio.speak_text(question)
        return answer_text, self._contextual_followup_wait_result(task_group, pending, answer_text, "ambiguous_where_answer")

    def _contextual_followup_wait_result(
        self,
        task_group: TaskGroup,
        pending: dict[str, Any],
        answer_text: str,
        reason: str,
    ) -> dict[str, Any]:
        self.store.append_event(
            "followup_contextual_confirmation_requested",
            {"task_group_id": task_group.task_group_id, "answer_text": answer_text, "reason": reason, "question": pending.get("question")},
        )
        ask_user = dict(pending)
        return {
            "ok": True,
            "session": task_group.command_session_id,
            "task_group_id": task_group.task_group_id,
            "decision": {
                "decision_type": "ask_user",
                "interaction_type": AMBIGUOUS,
                "reply": str(pending.get("question") or ""),
                "task_groups": [self._task_group_to_decision_payload(task_group)],
                "ask_user": ask_user,
                "confidence": 1.0,
            },
            "execution": {"executed": []},
            "continue_followup": True,
            "contextual_confirmation": True,
            "non_slot_turn": True,
        }

    def _record_contextual_followup_resolution(
        self,
        task_group: TaskGroup,
        pending: dict[str, Any],
        heard_text: str,
        value: str,
        reason: str,
    ) -> None:
        confirmation = dict(pending.get("contextual_confirmation") or {})
        pending.pop("contextual_confirmation", None)
        task_group.metadata.setdefault("contextual_followup_resolutions", []).append(
            {
                "timestamp": time.time(),
                "slot": "where",
                "value": value,
                "heard_text": heard_text,
                "reason": reason,
                "confirmation": confirmation,
            }
        )
        task_group.metadata["dialogue_state"] = "waiting_followup"
        self.store.save_task_group(task_group)

    @staticmethod
    def _canonical_where_answer(where: str) -> str:
        return "就在这里做" if where == "here" else f"去{where}做"

    @staticmethod
    def _is_negative_contextual_confirmation(cleaned: str) -> bool:
        return cleaned in {"不是", "不对", "没有", "不是这个意思", "我不是这个意思"} or any(
            marker in cleaned for marker in ("换地方", "去别的地方", "不是这里")
        )

    @staticmethod
    def _looks_like_ambiguous_here_answer(text: str) -> bool:
        cleaned = re.sub(r"[\s\u3000，。,.！？!、；;：:]+", "", text or "")
        if not cleaned or len(cleaned) > 6:
            return False
        if any(marker in cleaned for marker in ("聊", "讲", "介绍", "什么", "哪个", "星座是")):
            return False
        return bool(re.search(r"[做坐座](?:吧|呀|啊|啦)?$", cleaned))

    def _followup_echo_texts(self, decision: dict[str, Any]) -> list[str]:
        texts = [
            str(decision.get("followup_text") or ""),
            str(decision.get("user_text") or ""),
            str(decision.get("reply") or ""),
            str(decision.get("raw_text") or ""),
        ]
        ask_user = decision.get("ask_user")
        if isinstance(ask_user, dict):
            texts.append(str(ask_user.get("question") or ""))
        for group in decision.get("task_groups") or []:
            if isinstance(group, dict):
                texts.append(str(group.get("user_instruction") or ""))
                texts.append(str(group.get("title") or ""))
        return [text for text in texts if text]

    def _prepare_followup_voice(self, task_groups: list[TaskGroup], ask_user: dict[str, Any]) -> dict[str, Any]:
        if self.realtime_voice is None or not isinstance(ask_user, dict):
            return {"ok": True, "skipped": True, "reason": "realtime_voice_not_configured"}
        question = str(ask_user.get("question") or "")
        target_title = str(ask_user.get("task_title") or "")
        target = None
        for group in task_groups:
            if target_title and group.title == target_title:
                target = group
                break
        if target is None and task_groups:
            target = task_groups[-1]
        context = {
            "question": question,
            "task_group": self._task_group_to_decision_payload(target) if target is not None else {},
        }
        result = self.realtime_voice.prepare_once(mode="followup", context=context)
        self.store.append_event(
            "followup_voice_prepared",
            {
                "task_group_id": target.task_group_id if target is not None else None,
                "question": question,
                "result": result,
            },
        )
        return result

    @staticmethod
    def _plain_followup_answer_text(decision: dict[str, Any]) -> str:
        for key in ("followup_text", "user_text", "text"):
            value = str(decision.get(key) or "").strip()
            if value:
                return value
        raw_decision = decision.get("raw_decision")
        if isinstance(raw_decision, dict):
            for key in ("text", "answer", "user_text", "followup_text"):
                value = str(raw_decision.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _should_force_plain_followup_route(pending: dict[str, Any] | None, decision: dict[str, Any], answer_text: str) -> bool:
        if not isinstance(pending, dict) or not pending:
            return False
        if not str(answer_text or "").strip():
            return False
        if pending.get("answer"):
            return False
        has_followup_contract = bool(
            pending.get("question")
            or pending.get("missing_slots")
            or pending.get("optional_slots")
            or pending.get("candidate_skills")
            or pending.get("runtime_followup")
        )
        if not has_followup_contract:
            return False
        return True

    @staticmethod
    def _looks_like_assistant_followup_prose(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        assistant_markers = (
            "我得先",
            "我得",
            "我这就",
            "我先看看",
            "我来看看",
            "我现在",
            "好嘞",
            "咱们",
            "那咱们",
            "我会",
            "我将",
            "我准备",
            "我马上",
            "调用前摄",
            "调用后摄",
            "调用摄像头",
            "进行感知",
            "进行检查",
            "运动空间是否",
            "墙面适不适合投影",
        )
        return any(marker in compact for marker in assistant_markers)

    @staticmethod
    def _looks_like_robot_followup_prompt(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        robot_openers = (
            "我得",
            "我先",
            "我这就",
            "我来",
            "我马上",
            "我会",
            "我将",
            "我准备",
            "那我",
            "那我们先",
            "好的那我",
            "好的那我们先",
            "好的我",
            "好呀那我",
            "好呀那我们先",
            "好嘞那我",
            "好嘞那我们先",
            "请问你",
            "你想",
            "你希望",
            "需要我",
            "要不要我",
            "我可以",
        )
        if compact.startswith(robot_openers):
            return True
        has_question = any(token in compact for token in ("？", "?", "吗", "呢"))
        robot_question_markers = ("请问你", "你想", "你希望", "需要我", "要不要我", "要我")
        if has_question and any(marker in compact for marker in robot_question_markers):
            return True
        robot_action_markers = (
            "我得先",
            "我先确认",
            "我先看看",
            "我这就",
            "我来检查",
            "我来看看",
            "调用",
            "进行感知",
            "进行检查",
            "确认一下",
            "检查一下",
            "运动环境是否",
            "空间是否",
            "墙面适不适合",
            "投影仪显示",
            "动作指导",
        )
        return any(marker in compact for marker in robot_action_markers)

    def _invalid_followup_answer_result(
        self,
        task_group_id: str,
        task_group: TaskGroup | None,
        pending: dict[str, Any] | None,
        answer_text: str,
        heard: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        text = str(answer_text or "").strip()
        if not text:
            return {"ok": False, "task_group_id": task_group_id, "error": "empty_followup_text", "heard": heard or {}}
        pending = pending or {}
        if task_group is not None and self._looks_like_robot_followup_prompt(text):
            self.store.append_event(
                "followup_robot_prompt_ignored",
                {
                    "task_group_id": task_group_id,
                    "question": pending.get("question"),
                    "heard": text,
                },
            )
            return {"ok": False, "task_group_id": task_group_id, "error": "assistant_followup_prose", "heard": heard or {"text": text}}
        if (
            task_group is not None
            and self._looks_like_assistant_followup_prose(text)
            and not self._direct_followup_answer_matches_pending(task_group, pending, text)
        ):
            self.store.append_event(
                "followup_assistant_prose_ignored",
                {
                    "task_group_id": task_group_id,
                    "question": pending.get("question"),
                    "heard": text,
                },
            )
            return {"ok": False, "task_group_id": task_group_id, "error": "assistant_followup_prose", "heard": heard or {"text": text}}
        return None

    @staticmethod
    def _normalize_echo_compare_text(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()

    def answer_followup_decision(self, task_group_id: str, decision: dict[str, Any], execute: bool = False, dry_run: bool = True, enqueue: bool = True) -> dict[str, Any]:
        requested_task_group_id = task_group_id
        task_group = self.store.load_task_group(task_group_id)
        session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else CommandSession(session_type="followup")
        if not self._is_task_group_waiting_for_followup(task_group):
            redirected = self._find_pending_followup_task_group(session)
            if redirected is None:
                self.store.append_event(
                    "followup_rejected",
                    {
                        "requested_task_group_id": requested_task_group_id,
                        "reason": "task_group_not_waiting_for_followup",
                        "decision_backend": "doubao_realtime",
                    },
                )
                return {
                    "ok": False,
                    "task_group_id": requested_task_group_id,
                    "error": "task_group_not_waiting_for_followup",
                    "message": "当前任务不在等待追问补充",
                }
            self.store.append_event(
                "followup_redirected",
                {
                    "requested_task_group_id": requested_task_group_id,
                    "target_task_group_id": redirected.task_group_id,
                    "decision_backend": "doubao_realtime",
                },
            )
            task_group = redirected
            task_group_id = task_group.task_group_id

        pending_followup = self._pending_followup(task_group)
        answer_text = self._plain_followup_answer_text(decision)
        invalid_answer = self._invalid_followup_answer_result(task_group_id, task_group, pending_followup, answer_text, decision)
        if invalid_answer is not None:
            return invalid_answer
        session.utterances.append(
            {
                "role": "user",
                "text": answer_text,
                "timestamp": time.time(),
                "kind": "followup_answer",
                "task_group_id": task_group_id,
                "decision_backend": "doubao_realtime",
            }
        )
        self._record_followup_answer(task_group, answer_text)

        environment_result = self._answer_environment_override_followup(task_group, session, answer_text, execute=execute, dry_run=dry_run, enqueue=enqueue)
        if environment_result is not None:
            return environment_result

        projector_reply = self._classify_projector_followup_reply(pending_followup, answer_text)
        if projector_reply is not None:
            task_group.slots["projector"] = projector_reply
        local_followup_decision = self._local_movement_followup_decision(task_group, answer_text)
        if local_followup_decision is None:
            local_followup_decision = self._local_fitness_followup_decision(task_group, answer_text)
        if local_followup_decision is not None:
            decision = local_followup_decision
        else:
            source_text = "\n".join(
                part
                for part in (
                    task_group.user_instruction,
                    f"ROBOT_QUESTION:{pending_followup.get('question')}" if isinstance(pending_followup, dict) and pending_followup.get("question") else "",
                    f"FOLLOWUP_ANSWER:{answer_text}" if answer_text else "",
                )
                if part
            )
            decision = self._postprocess_voice_decision(decision, source_text or answer_text)
        decision = self._limit_followup_slots(
            decision,
            f"{task_group.user_instruction}\n用户补充：{answer_text}",
        )

        new_groups = self._task_groups_from_decision(session, decision)
        if new_groups:
            replacement = self._select_followup_replacement(task_group, new_groups)
            task_group.title = replacement.title or task_group.title
            task_group.slots.update(replacement.slots)
            task_group.followups = self._merge_followups(task_group.followups, replacement.followups)
            task_group.steps = self._merge_replanned_steps(task_group, replacement.steps)
            task_group.status = replacement.status if replacement.status == TaskStatus.NEEDS_INFO.value else TaskStatus.NEW.value
            task_group.metadata.update(
                {
                    "planner_confidence": decision.get("confidence"),
                    "decision_backend": "doubao_realtime",
                    "replanned_from_followup": True,
                    "ignored_followup_task_groups": max(0, len(new_groups) - 1),
                }
            )
            self.store.save_task_group(task_group)
            decision = dict(decision)
            decision["task_groups"] = [self._task_group_to_decision_payload(task_group)]
        else:
            self.store.save_task_group(task_group)

        if enqueue and not decision.get("ask_user"):
            self._enqueue_ready_session_task_groups(session)
        execution = self.drain_session_queue(session, dry_run=dry_run) if execute and not decision.get("ask_user") else {"executed": []}
        if decision.get("ask_user"):
            session.status = SessionStatus.WAITING_USER.value
            session.ended_at = None
            self.store.save_session(session)
            if execute:
                self._prepare_followup_voice([task_group], decision["ask_user"])
                self.audio.speak_text(decision["ask_user"]["question"])
        else:
            decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
            if not has_runtime_ask:
                session.status = SessionStatus.COMPLETED.value
                session.ended_at = time.time()
                self.store.save_session(session)
        return {
            "ok": True,
            "session": session.session_id,
            "task_group_id": task_group.task_group_id,
            "requested_task_group_id": requested_task_group_id,
            "decision": decision,
            "execution": execution,
        }

    def answer_followup(self, task_group_id: str, answer_text: str, execute: bool = False, dry_run: bool = True, enqueue: bool = True) -> dict[str, Any]:
        requested_task_group_id = task_group_id
        task_group = self.store.load_task_group(task_group_id)
        session = self.store.load_session(task_group.command_session_id) if task_group.command_session_id else CommandSession(session_type="followup")
        if not self._is_task_group_waiting_for_followup(task_group):
            redirected = self._find_pending_followup_task_group(session)
            if redirected is None:
                self.store.append_event(
                    "followup_rejected",
                    {
                        "requested_task_group_id": requested_task_group_id,
                        "reason": "task_group_not_waiting_for_followup",
                        "answer_text": answer_text,
                    },
                )
                return {
                    "ok": False,
                    "task_group_id": requested_task_group_id,
                    "error": "task_group_not_waiting_for_followup",
                    "message": "当前任务不在等待追问补充",
                }
            self.store.append_event(
                "followup_redirected",
                {
                    "requested_task_group_id": requested_task_group_id,
                    "target_task_group_id": redirected.task_group_id,
                    "answer_text": answer_text,
                },
            )
            task_group = redirected
            task_group_id = task_group.task_group_id
        pending_followup = self._pending_followup(task_group)
        routing_decision = self.planner.plan(answer_text)
        if self._followup_answer_matches_pending(task_group, pending_followup or {}, answer_text):
            routing_decision["interaction_type"] = FOLLOWUP_ANSWER
            routing_decision["user_text"] = answer_text
        routed = self._route_followup_interaction(
            task_group_id,
            task_group,
            pending_followup or {},
            routing_decision,
            answer_text,
            execute=execute,
            dry_run=dry_run,
        )
        if routed is not None:
            return routed
        invalid_answer = self._invalid_followup_answer_result(task_group_id, task_group, pending_followup, answer_text, {"text": answer_text})
        if invalid_answer is not None:
            return invalid_answer
        session.utterances.append({"role": "user", "text": answer_text, "timestamp": time.time(), "kind": "followup_answer", "task_group_id": task_group_id})
        self._record_followup_answer(task_group, answer_text)

        environment_result = self._answer_environment_override_followup(task_group, session, answer_text, execute=execute, dry_run=dry_run, enqueue=enqueue)
        if environment_result is not None:
            return environment_result

        projector_reply = self._classify_projector_followup_reply(pending_followup, answer_text)
        if projector_reply is not None:
            task_group.slots["projector"] = projector_reply
            decision = self._replan_existing_task_group(task_group, answer_text)
        else:
            decision = self._replan_existing_task_group(task_group, answer_text)

        new_groups = self._task_groups_from_decision(session, decision)
        if new_groups:
            replacement = self._select_followup_replacement(task_group, new_groups)
            task_group.title = replacement.title or task_group.title
            task_group.slots.update(replacement.slots)
            task_group.followups = self._merge_followups(task_group.followups, replacement.followups)
            task_group.steps = self._merge_replanned_steps(task_group, replacement.steps)
            task_group.status = replacement.status if replacement.status == TaskStatus.NEEDS_INFO.value else TaskStatus.NEW.value
            task_group.metadata.update({
                "planner_confidence": decision.get("confidence"),
                "replanned_from_followup": True,
                "ignored_followup_task_groups": max(0, len(new_groups) - 1),
            })
            self.store.save_task_group(task_group)
            decision = dict(decision)
            decision["task_groups"] = [self._task_group_to_decision_payload(task_group)]
        else:
            self.store.save_task_group(task_group)

        if enqueue and not decision.get("ask_user"):
            self._enqueue_ready_session_task_groups(session)
        execution = self.drain_session_queue(session, dry_run=dry_run) if execute and not decision.get("ask_user") else {"executed": []}
        if decision.get("ask_user"):
            session.status = SessionStatus.WAITING_USER.value
            session.ended_at = None
            self.store.save_session(session)
            if execute:
                self._prepare_followup_voice([task_group], decision["ask_user"])
                self.audio.speak_text(decision["ask_user"]["question"])
        else:
            decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
            if not has_runtime_ask:
                session.status = SessionStatus.COMPLETED.value
                session.ended_at = time.time()
                self.store.save_session(session)
        return {
            "ok": True,
            "session": session.session_id,
            "task_group_id": task_group.task_group_id,
            "requested_task_group_id": requested_task_group_id,
            "decision": decision,
            "execution": execution,
        }

    def _enqueue_ready_session_task_groups(self, session: CommandSession) -> None:
        for task_group_id in session.task_group_ids:
            try:
                task_group = self.store.load_task_group(task_group_id)
            except Exception:
                continue
            if task_group.status == TaskStatus.COMPLETED.value:
                continue
            if task_group.status in {
                TaskStatus.NEEDS_INFO.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.INTERRUPTED.value,
            }:
                break
            self.store.enqueue_task_group(task_group)

    def suspend_waiting_followup_safely(
        self,
        task_group_id: str,
        *,
        dry_run: bool = False,
        reason: str = "followup_flow_paused",
    ) -> dict[str, Any]:
        task_group = self.store.load_task_group(task_group_id)
        if not self._is_task_group_waiting_for_followup(task_group):
            return {"ok": True, "suspended": False, "reason": "task_not_waiting_for_followup"}
        head_steps = [
            step
            for step in task_group.steps
            if step.skill_name == "head_control"
            and step.status == TaskStatus.COMPLETED.value
            and self._is_non_level_head_action(str((step.arguments or {}).get("action") or ""))
        ]
        projector_steps = [
            step
            for step in task_group.steps
            if step.skill_name == "projector_control"
            and step.status == TaskStatus.COMPLETED.value
            and self._is_projector_on_action(str((step.arguments or {}).get("action") or ""))
        ]
        maintenance: list[TaskStep] = []
        if head_steps:
            maintenance.append(TaskStep(skill_name="head_control", arguments={"action": "level"}, reason="safe waiting posture"))
        if projector_steps:
            maintenance.append(TaskStep(skill_name="projector_control", arguments={"action": "off"}, reason="safe waiting projector state"))
        executed = self._execute_maintenance_steps(maintenance, dry_run=dry_run) if maintenance else []
        results: list[dict[str, Any]] = []
        succeeded = {step.skill_name for step, result in executed if result.get("ok")}
        for step, result in executed:
            results.append({"skill_name": step.skill_name, "arguments": dict(step.arguments or {}), "result": result})
        reset_step_ids: list[str] = []
        if "head_control" in succeeded:
            first_head_order = min(step.order for step in head_steps)
            for step in task_group.steps:
                if step in head_steps or (
                    step.skill_name == "environment_perception"
                    and step.status == TaskStatus.COMPLETED.value
                    and step.order >= first_head_order
                ):
                    self._reset_step_for_waiting_resume(step)
                    reset_step_ids.append(step.step_id)
        if "projector_control" in succeeded:
            for step in projector_steps:
                self._reset_step_for_waiting_resume(step)
                reset_step_ids.append(step.step_id)
        task_group.metadata["safe_waiting_state"] = {
            "timestamp": time.time(),
            "reason": reason,
            "maintenance": results,
            "reset_step_ids": list(dict.fromkeys(reset_step_ids)),
            "resume_policy": "rerun_invalidated_setup_steps",
        }
        task_group.metadata["dialogue_state"] = "waiting_followup_safe"
        self.store.save_task_group(task_group)
        ok = all(result.get("ok") for _step, result in executed)
        self.store.append_event(
            "waiting_followup_safely_suspended",
            {"task_group_id": task_group_id, "reason": reason, "ok": ok, "maintenance": results, "reset_step_ids": reset_step_ids},
        )
        return {
            "ok": ok,
            "suspended": True,
            "task_group_id": task_group_id,
            "maintenance": results,
            "reset_step_ids": list(dict.fromkeys(reset_step_ids)),
        }

    @staticmethod
    def _reset_step_for_waiting_resume(step: TaskStep) -> None:
        step.status = TaskStatus.NEW.value
        step.started_at = None
        step.ended_at = None
        step.result = None
        step.error = None

    def begin_interrupt_active_task_fast(self, wakeup_event: WakeupEvent, new_session_reason: str = "user_wakeup") -> dict[str, Any]:
        started = time.time()
        state = self.store.load_state()
        active_id = state.get("active_task_group_id")
        if not active_id:
            return {"ok": True, "interrupted": False, "mode": "fast", "elapsed_seconds": round(time.time() - started, 4)}

        self.audio.stop_speech()
        task_group = self.store.load_task_group(active_id)
        interrupt_result = self.executor.request_interrupt(signal_process=True)
        safe_interrupt_result = self._serializable_interrupt_result(interrupt_result)
        fast_state = self.robot_state.snapshot(active_task_group_id=active_id, active_step=self.executor.current_step, fast=True)
        executor_snapshot = (interrupt_result.get("snapshot") or {}) if isinstance(interrupt_result, dict) else {}
        current_step_payload = executor_snapshot.get("current_step") if isinstance(executor_snapshot.get("current_step"), dict) else None
        active_step = self._step_from_payload_or_task(task_group, current_step_payload)
        if active_step is not None:
            for step in task_group.steps:
                if step.step_id == active_step.step_id:
                    step.status = TaskStatus.INTERRUPTED.value
                    step.error = "interrupted"
                    step.result = {
                        "ok": False,
                        "interrupted": True,
                        "error": "interrupted",
                        "last_progress": executor_snapshot.get("last_progress"),
                        "speech_events": executor_snapshot.get("speech_events", []),
                    }
                elif step.status == TaskStatus.RUNNING.value:
                    step.status = TaskStatus.INTERRUPTED.value

        task_group.status = TaskStatus.INTERRUPTED.value
        task_group.interruption_count += 1
        completed_steps = [step.step_id for step in task_group.steps if step.status == TaskStatus.COMPLETED.value]
        pending_steps = [
            step.step_id
            for step in task_group.steps
            if active_step is None or (step.step_id != active_step.step_id and step.status != TaskStatus.COMPLETED.value)
        ]
        task_group.resume_context = {
            "reason": new_session_reason,
            "wakeup_event_id": wakeup_event.event_id,
            "interrupted_at": time.time(),
            "active_step_id": active_step.step_id if active_step else None,
            "active_skill": active_step.skill_name if active_step else None,
            "completed_steps": completed_steps,
            "pending_steps": pending_steps,
            "step_status": [step.__dict__ for step in task_group.steps],
            "executor_interrupt_result": safe_interrupt_result,
            "last_progress": executor_snapshot.get("last_progress"),
            "speech_events": executor_snapshot.get("speech_events", []),
            "robot_state_at_interrupt_request": fast_state,
            "robot_state_before_interrupt": fast_state,
            "interrupt_ownership": self._interrupt_task_ownership(task_group, active_step),
            "can_resume": True,
            "resume_strategy": self._resume_strategy_for_step(active_step),
            "recovery": self._recovery_for_step(active_step),
            "context_finalized": False,
        }
        self.store.push_interrupted(task_group)
        elapsed = round(time.time() - started, 4)
        self.store.append_event(
            "interrupt_fast_requested",
            {"task_group_id": task_group.task_group_id, "wakeup_event": wakeup_event.__dict__, "elapsed_seconds": elapsed},
        )
        self._start_interrupt_context_finalizer(task_group.task_group_id, active_step)
        return {"ok": True, "interrupted": True, "mode": "fast", "task_group_id": task_group.task_group_id, "elapsed_seconds": elapsed}

    def _start_interrupt_context_finalizer(self, task_group_id: str, active_step: TaskStep | None) -> None:
        step_copy = TaskStep(**active_step.__dict__) if active_step is not None else None
        thread = threading.Thread(target=self._finalize_interrupt_context, args=(task_group_id, step_copy), daemon=True)
        thread.start()

    def _finalize_interrupt_context(self, task_group_id: str, active_step: TaskStep | None) -> None:
        started = time.time()
        try:
            full_state = self.robot_state.snapshot(active_task_group_id=task_group_id, active_step=active_step, fast=False)
            finalized_at = time.time()

            def merge_snapshot(task_group: TaskGroup) -> TaskGroup:
                if task_group.status != TaskStatus.INTERRUPTED.value:
                    return task_group
                context = dict(task_group.resume_context or {})
                if context.get("neutralized_for_new_session"):
                    context["robot_state_after_neutralize_full_snapshot"] = full_state
                else:
                    context["robot_state_full_snapshot"] = full_state
                context["context_finalized"] = True
                context["context_finalized_at"] = finalized_at
                task_group.resume_context = context
                return task_group

            task_group = self.store.update_task_group(task_group_id, merge_snapshot)
            if task_group.status != TaskStatus.INTERRUPTED.value:
                return
            self.store.append_event(
                "interrupt_context_finalized",
                {"task_group_id": task_group_id, "elapsed_seconds": round(time.time() - started, 4)},
            )
        except Exception as exc:
            self.store.append_event(
                "interrupt_context_finalize_failed",
                {"task_group_id": task_group_id, "elapsed_seconds": round(time.time() - started, 4), "error": str(exc)},
            )

    def interrupt_active_task(self, wakeup_event: WakeupEvent, new_session_reason: str = "user_wakeup") -> dict[str, Any]:
        state = self.store.load_state()
        active_id = state.get("active_task_group_id")
        if not active_id:
            return {"ok": True, "interrupted": False}
        task_group = self.store.load_task_group(active_id)
        self.audio.stop_speech()
        state_at_request = self.robot_state.snapshot(active_task_group_id=active_id, active_step=self.executor.current_step)
        interrupt_result = self.executor.interrupt_current()
        safe_interrupt_result = self._serializable_interrupt_result(interrupt_result)
        state_after_stop = self.robot_state.snapshot(active_task_group_id=active_id, active_step=self.executor.current_step)
        executor_snapshot = (interrupt_result.get("snapshot") or {}) if isinstance(interrupt_result, dict) else {}
        current_step_payload = executor_snapshot.get("current_step") if isinstance(executor_snapshot.get("current_step"), dict) else None
        active_step = self._step_from_payload_or_task(task_group, current_step_payload)
        if active_step is not None:
            for step in task_group.steps:
                if step.step_id == active_step.step_id:
                    step.status = TaskStatus.INTERRUPTED.value
                    step.error = "interrupted"
                    step.result = {
                        "ok": False,
                        "interrupted": True,
                        "error": "interrupted",
                        "last_progress": executor_snapshot.get("last_progress"),
                        "speech_events": executor_snapshot.get("speech_events", []),
                    }
                elif step.status == TaskStatus.RUNNING.value:
                    step.status = TaskStatus.INTERRUPTED.value
        task_group.status = TaskStatus.INTERRUPTED.value
        task_group.interruption_count += 1
        task_group.interrupted_by_session_id = None
        completed_steps = [step.step_id for step in task_group.steps if step.status == TaskStatus.COMPLETED.value]
        pending_steps = [
            step.step_id
            for step in task_group.steps
            if active_step is None or (step.step_id != active_step.step_id and step.status != TaskStatus.COMPLETED.value)
        ]
        task_group.resume_context = {
            "reason": new_session_reason,
            "wakeup_event_id": wakeup_event.event_id,
            "interrupted_at": time.time(),
            "active_step_id": active_step.step_id if active_step else None,
            "active_skill": active_step.skill_name if active_step else None,
            "completed_steps": completed_steps,
            "pending_steps": pending_steps,
            "step_status": [step.__dict__ for step in task_group.steps],
            "executor_interrupt_result": safe_interrupt_result,
            "last_progress": executor_snapshot.get("last_progress"),
            "speech_events": executor_snapshot.get("speech_events", []),
            "robot_state_at_interrupt_request": state_at_request,
            "robot_state_before_interrupt": state_at_request,
            "robot_state_after_stop": state_after_stop,
            "interrupt_ownership": self._interrupt_task_ownership(task_group, active_step),
            "can_resume": True,
            "resume_strategy": self._resume_strategy_for_step(active_step),
            "recovery": self._recovery_for_step(active_step),
        }
        self.store.push_interrupted(task_group)
        self.store.append_event("task_group_interrupted", {"task_group_id": task_group.task_group_id, "wakeup_event": wakeup_event.__dict__})
        return {"ok": True, "interrupted": True, "task_group_id": task_group.task_group_id}

    @staticmethod
    def _serializable_interrupt_result(interrupt_result: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(interrupt_result, dict):
            return {"ok": False, "error": str(interrupt_result)}
        safe = {key: value for key, value in interrupt_result.items() if key not in {"process", "processes"}}
        process = interrupt_result.get("process")
        processes = interrupt_result.get("processes")
        process_items = processes if isinstance(processes, list) else ([process] if process is not None else [])
        pids: list[int] = []
        for item in process_items:
            pid = getattr(item, "pid", None)
            if isinstance(pid, int):
                pids.append(pid)
        safe["process_count"] = len(process_items)
        safe["process_pids"] = pids
        return to_dict(safe)

    def neutralize_interrupted_task_for_new_session(
        self,
        interrupt_result: dict[str, Any] | None,
        dry_run: bool = False,
        fast_snapshot: bool = False,
    ) -> dict[str, Any]:
        interrupt_result = interrupt_result or {}
        if not interrupt_result.get("interrupted"):
            return {"ok": True, "neutralized": False, "reason": "no_interrupted_task"}
        task_group_id = interrupt_result.get("task_group_id")
        if not task_group_id:
            return {"ok": False, "neutralized": False, "error": "missing_task_group_id"}
        try:
            task_group = self.store.load_task_group(str(task_group_id))
        except Exception as exc:
            return {"ok": False, "neutralized": False, "task_group_id": task_group_id, "error": str(exc)}

        active_step = self._active_resume_step(task_group)
        context = dict(task_group.resume_context or {})
        before = self._snapshot_before_neutralize(str(task_group_id), active_step, fast=fast_snapshot)
        saved_state = self._saved_state_for_resume_before_neutralize(task_group, before)
        ownership = context.get("interrupt_ownership") if isinstance(context.get("interrupt_ownership"), dict) else None
        if ownership is None:
            ownership = self._interrupt_task_ownership(task_group, active_step)
        steps = self._neutralization_steps_for_interrupt(task_group, active_step, ownership)
        results: list[dict[str, Any]] = []
        ok = True
        step_results = self._execute_maintenance_steps(steps, dry_run=dry_run)
        for order, (step, result) in enumerate(step_results):
            step.order = order
            step.resources = self.resources.resources_for_skill(step.skill_name, step.arguments)
            step.result = result
            step.status = TaskStatus.COMPLETED.value if result.get("ok") else TaskStatus.FAILED.value
            step.error = "" if result.get("ok") else str(result.get("error") or "neutralization_failed")
            if result.get("ok") and not dry_run:
                self.robot_state.record_step_effect(step, result)
            if not result.get("ok"):
                ok = False
            results.append(
                {
                    "skill_name": step.skill_name,
                    "arguments": step.arguments,
                    "status": step.status,
                    "error": step.error,
                    "result": result,
                }
            )
        after = self.robot_state.snapshot(active_task_group_id=None, active_step=None, fast=True)
        neutralized_at = time.time()
        context_update = {
            "neutralized_for_new_session": True,
            "neutralized_at": neutralized_at,
            "neutralization_fast_snapshot": bool(fast_snapshot),
            "neutralization_ok": ok,
            "neutralization_ownership": ownership,
            "robot_state_before_neutralize": before,
            "robot_state_after_neutralize": after,
            "neutralization_steps": results,
            "saved_interrupt_state_for_resume": saved_state,
        }

        def merge_neutralization(latest: TaskGroup) -> TaskGroup:
            latest_context = dict(latest.resume_context or {})
            latest_context.update(context_update)
            latest.resume_context = latest_context
            latest.metadata.setdefault("interrupt_neutralization", []).append(
                {
                    "timestamp": neutralized_at,
                    "ok": ok,
                    "steps": results,
                    "ownership": ownership,
                }
            )
            return latest

        task_group = self.store.update_task_group(task_group.task_group_id, merge_neutralization, fallback=task_group)
        self.store.append_event(
            "interrupted_task_neutralized_for_new_session",
            {"task_group_id": task_group.task_group_id, "ok": ok, "steps": results, "ownership": ownership},
        )
        return {
            "ok": ok,
            "neutralized": bool(steps),
            "task_group_id": task_group.task_group_id,
            "steps": results,
            "ownership": ownership,
            "fast_snapshot": bool(fast_snapshot),
        }

    def _execute_maintenance_steps(
        self,
        steps: list[TaskStep],
        dry_run: bool,
    ) -> list[tuple[TaskStep, dict[str, Any]]]:
        if "execute_maintenance_step" in vars(self.executor):
            # Preserve injected hardware adapters used by self-checks and
            # deployments that replace the executor method at runtime.
            return [(step, self.executor.execute_maintenance_step(step, dry_run=dry_run)) for step in steps]
        if len(steps) <= 1:
            return [(step, self.executor.execute_maintenance_step(step, dry_run=dry_run)) for step in steps]

        # Neutralization steps own disjoint resources (for example head_motor and
        # projector_i2c). Independent executors prevent their internal process
        # bookkeeping from overwriting each other while the resource locks still
        # serialize any future conflicting maintenance command.
        def run(step: TaskStep) -> dict[str, Any]:
            executor = SkillExecutor(self.config, self.registry, self.resources)
            return executor.execute_maintenance_step(step, dry_run=dry_run)

        ordered: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as pool:
            futures = {pool.submit(run, step): step for step in steps}
            for future in concurrent.futures.as_completed(futures):
                step = futures[future]
                try:
                    ordered[step.step_id] = future.result()
                except Exception as exc:
                    ordered[step.step_id] = {"ok": False, "error": str(exc)}
        return [(step, ordered[step.step_id]) for step in steps]

    def _snapshot_before_neutralize(self, task_group_id: str, active_step: TaskStep | None, fast: bool = False) -> dict[str, Any]:
        try:
            return self.robot_state.snapshot(active_task_group_id=task_group_id, active_step=active_step, fast=fast)
        except Exception as exc:
            fallback = self.robot_state.snapshot(active_task_group_id=task_group_id, active_step=active_step, fast=True)
            fallback["full_snapshot_error"] = str(exc)
            return fallback

    def _saved_state_for_resume_before_neutralize(self, task_group: TaskGroup, before: dict[str, Any]) -> dict[str, Any]:
        saved = self._resume_saved_robot_state(task_group)
        if not isinstance(before, dict):
            return self._inject_owned_resume_state(task_group, saved)
        if not saved:
            return self._inject_owned_resume_state(task_group, before)
        merged = dict(saved)
        for key in ("pose", "head", "peripherals", "motors", "navigation", "base"):
            value = before.get(key)
            if isinstance(value, dict) and value:
                if key == "pose" and not value.get("valid"):
                    continue
                merged[key] = value
        return self._inject_owned_resume_state(task_group, merged)

    def _inject_owned_resume_state(self, task_group: TaskGroup, state: dict[str, Any]) -> dict[str, Any]:
        merged = dict(state or {})
        ownership = task_group.resume_context.get("interrupt_ownership") if isinstance(task_group.resume_context, dict) else None
        active_step = self._active_resume_step(task_group)
        if not isinstance(ownership, dict):
            ownership = self._interrupt_task_ownership(task_group, active_step)
        if ownership.get("projector"):
            peripherals = dict(merged.get("peripherals") or {})
            projector = peripherals.get("projector")
            if not isinstance(projector, dict) or not self._is_projector_on_action(str(projector.get("action") or projector.get("state") or projector.get("status") or "")):
                action = ownership.get("projector_action") or ownership.get("projector_snapshot_action") or "fitness_video_on"
                peripherals["projector"] = {"action": action, "source": "interrupt_ownership_inferred", "timestamp": time.time()}
                merged["peripherals"] = peripherals
        if ownership.get("head"):
            head = merged.get("head")
            if not isinstance(head, dict) or not head.get("valid"):
                action = str(ownership.get("head_action") or "up").strip().lower()
                defaults = self.config.get("robot_state", {}).get("head_angles", {})
                angle = defaults.get(action)
                if angle is None and action not in {"level", "horizontal", "center", "neutral", "stop"}:
                    angle = defaults.get("up", 205)
                merged["head"] = {
                    "valid": True,
                    "action": action or "up",
                    "angle": int(angle if angle is not None else defaults.get("level", 185)),
                    "source": "interrupt_ownership_inferred",
                    "timestamp": time.time(),
                }
        return merged

    def _neutralization_steps_for_interrupt(self, task_group: TaskGroup, active_step: TaskStep | None, ownership: dict[str, Any]) -> list[TaskStep]:
        steps: list[TaskStep] = []
        if ownership.get("head"):
            head_wait = float(self.config.get("execution", {}).get("interrupt_head_wait_seconds", 0.3))
            steps.append(
                TaskStep(
                    skill_name="head_control",
                    arguments={"action": "level", "wait": max(0.0, head_wait)},
                    reason="neutralize interrupted task head before new command",
                )
            )
        if ownership.get("projector"):
            steps.append(
                TaskStep(
                    skill_name="projector_control",
                    arguments={"action": "off"},
                    reason="neutralize interrupted task projector before new command",
                )
            )
        return self._filter_restore_steps(steps)

    def _interrupt_task_ownership(self, task_group: TaskGroup, active_step: TaskStep | None = None) -> dict[str, Any]:
        active_order = active_step.order if active_step is not None else None
        head_action = self._last_task_action_before_interrupt(task_group, "head_control", active_order)
        projector_action = self._last_task_action_before_interrupt(task_group, "projector_control", active_order)
        active_skill = active_step.skill_name if active_step else None
        head_owned = self._is_non_level_head_action(head_action)
        if active_skill in FITNESS_SKILLS | {"face_recognition", "face_registration"}:
            head_owned = True
        if active_skill == "head_control":
            head_owned = self._is_non_level_head_action(str((active_step.arguments or {}).get("action") or ""))
        projector_owned = self._is_projector_on_action(projector_action)
        if active_skill == "projector_control":
            projector_owned = self._is_projector_on_action(str((active_step.arguments or {}).get("action") or ""))
        projector_snapshot = self._projector_snapshot_for_interrupt(task_group)
        projector_snapshot_action = str(projector_snapshot.get("action") or "").strip().lower() if projector_snapshot else None
        projector_requested_by_task = bool((task_group.slots or {}).get("projector"))
        fitness_task = self._is_fitness_task_group(task_group)
        if not projector_owned and self._is_projector_on_action(projector_snapshot_action):
            if (
                projector_requested_by_task
                or fitness_task
                or self._task_contains_projector_on_step(task_group, active_order)
                or self._task_contains_projector_on_step(task_group, None)
            ):
                projector_owned = True
                projector_action = projector_action or projector_snapshot_action
        if not projector_owned and projector_requested_by_task and self._task_contains_projector_on_step(task_group, None):
            projector_owned = True
            projector_action = projector_action or self._last_task_action_before_interrupt(task_group, "projector_control", None)
        if not projector_owned and projector_requested_by_task and fitness_task:
            projector_owned = True
            projector_action = projector_action or projector_snapshot_action or self._last_task_action_before_interrupt(task_group, "projector_control", None)
        if not head_owned and self._task_contains_non_level_head_step(task_group, active_order):
            head_owned = True
            head_action = head_action or self._last_task_action_before_interrupt(task_group, "head_control", active_order)
        if not head_owned and self._task_contains_non_level_head_step(task_group, None):
            head_owned = True
            head_action = head_action or self._last_task_action_before_interrupt(task_group, "head_control", None)
        return {
            "head": bool(head_owned),
            "head_action": head_action,
            "projector": bool(projector_owned),
            "projector_action": projector_action,
            "projector_snapshot_action": projector_snapshot_action,
            "projector_requested_by_task": projector_requested_by_task,
            "active_skill": active_skill,
        }

    @staticmethod
    def _is_fitness_task_group(task_group: TaskGroup) -> bool:
        if any(step.skill_name in FITNESS_SKILLS for step in task_group.steps):
            return True
        exercise = str((task_group.slots or {}).get("exercise_type") or "").strip()
        return exercise in FITNESS_SKILLS

    def _task_contains_non_level_head_step(self, task_group: TaskGroup, active_order: int | None) -> bool:
        action = self._last_task_action_before_interrupt(task_group, "head_control", active_order)
        return self._is_non_level_head_action(action)

    def _task_contains_projector_on_step(self, task_group: TaskGroup, active_order: int | None) -> bool:
        for step in task_group.steps:
            if step.skill_name != "projector_control":
                continue
            if active_order is not None and step.order > active_order:
                continue
            action = str((step.arguments or {}).get("action") or "").strip().lower()
            if self._is_projector_on_action(action):
                return True
            parsed = self._step_parsed_json(step)
            if self._is_projector_on_action(str(parsed.get("action") or "")):
                return True
        return False

    def _projector_snapshot_for_interrupt(self, task_group: TaskGroup) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        context = task_group.resume_context or {}
        for key in (
            "robot_state_at_interrupt_request",
            "robot_state_before_interrupt",
            "robot_state_full_snapshot",
            "robot_state_after_stop",
            "saved_interrupt_state_for_resume",
        ):
            value = context.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        try:
            candidates.append(self.robot_state.snapshot(fast=True))
        except Exception:
            pass
        for state in candidates:
            projector = ((state.get("peripherals") or {}).get("projector") if isinstance(state.get("peripherals"), dict) else None)
            if isinstance(projector, dict):
                return projector
            cache = state.get("cache") if isinstance(state.get("cache"), dict) else {}
            projector = ((cache.get("peripherals") or {}).get("projector") if isinstance(cache.get("peripherals"), dict) else None)
            if isinstance(projector, dict):
                return projector
        return {}

    def _last_task_action_before_interrupt(self, task_group: TaskGroup, skill_name: str, active_order: int | None) -> str | None:
        candidates: list[tuple[int, str]] = []
        for step in task_group.steps:
            if step.skill_name != skill_name:
                continue
            if active_order is not None and step.order > active_order:
                continue
            if step.status not in {TaskStatus.COMPLETED.value, TaskStatus.RUNNING.value, TaskStatus.INTERRUPTED.value}:
                continue
            action = str((step.arguments or {}).get("action") or "").strip().lower()
            if not action:
                parsed = self._step_parsed_json(step)
                action = str(parsed.get("action") or "").strip().lower()
            candidates.append((int(step.order or 0), action))
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]

    def _is_non_level_head_action(self, action: str | None) -> bool:
        action = str(action or "").strip().lower()
        if not action:
            return False
        return action not in {"level", "horizontal", "center", "neutral", "stop"}

    def _is_projector_on_action(self, action: str | None) -> bool:
        action = str(action or "").strip().lower()
        return action in {"on", "internal_on", "fitness_video_on", "meeting_presentation_on", "external_video_on", "checkerboard", "pattern", "open", "start", "enable"}

    def drain_queue(self, dry_run: bool = True) -> dict[str, Any]:
        executed = []
        while True:
            task_group = self.store.pop_next_task_group()
            if task_group is None:
                break
            item, should_stop = self._execute_queued_task_group(task_group, dry_run=dry_run)
            executed.append(item)
            if should_stop:
                break
        return {"executed": executed}

    def drain_session_queue(self, session: CommandSession, dry_run: bool = True) -> dict[str, Any]:
        return self.drain_task_group_ids(session.task_group_ids, dry_run=dry_run)

    def drain_task_group_ids(self, task_group_ids: list[str], dry_run: bool = True) -> dict[str, Any]:
        executed = []
        ordered_ids = [str(item) for item in list(task_group_ids)]
        for index, task_group_id in enumerate(ordered_ids):
            task_group = self.store.pop_task_group(str(task_group_id))
            if task_group is None:
                continue
            item, should_stop = self._execute_queued_task_group(task_group, dry_run=dry_run)
            executed.append(item)
            if should_stop:
                remaining = ordered_ids[index + 1 :]
                status = str(item.get("status") or "")
                if item.get("cleanup_failed"):
                    self._pause_remaining_task_groups(remaining, "blocked_by_cleanup_failed")
                elif status in {TaskStatus.NEEDS_INFO.value, TaskStatus.INTERRUPTED.value}:
                    self._pause_remaining_task_groups(remaining, f"blocked_by_{status}")
                elif status in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
                    self._cancel_remaining_task_groups(remaining, f"blocked_by_{status}")
                break
        return {"executed": executed}

    def _remove_task_group_ids_from_queue(self, task_group_ids: list[str]) -> list[str]:
        target = {str(item) for item in task_group_ids if item}
        if not target:
            return []
        state = self.store.load_state()
        queue = [str(item) for item in state.get("task_queue", [])]
        removed = [item for item in queue if item in target]
        if removed:
            state["task_queue"] = [item for item in queue if item not in target]
            self.store.save_state(state)
        return removed

    def _pause_remaining_task_groups(self, task_group_ids: list[str], reason: str) -> None:
        removed = self._remove_task_group_ids_from_queue(task_group_ids)
        changed: list[str] = []
        for task_group_id in task_group_ids:
            try:
                task_group = self.store.load_task_group(str(task_group_id))
            except Exception:
                continue
            if task_group.status == TaskStatus.QUEUED.value:
                task_group.status = TaskStatus.NEW.value
                task_group.result_summary = ""
                self.store.save_task_group(task_group)
                changed.append(task_group.task_group_id)
        if removed or changed:
            self.store.append_event("remaining_task_groups_paused", {"reason": reason, "removed_from_queue": removed, "reset_to_new": changed})

    def _cancel_remaining_task_groups(self, task_group_ids: list[str], reason: str) -> None:
        removed = self._remove_task_group_ids_from_queue(task_group_ids)
        cancelled: list[str] = []
        for task_group_id in task_group_ids:
            try:
                task_group = self.store.load_task_group(str(task_group_id))
            except Exception:
                continue
            if task_group.status in {TaskStatus.NEW.value, TaskStatus.QUEUED.value, TaskStatus.NEEDS_INFO.value}:
                task_group.status = TaskStatus.CANCELLED.value
                task_group.ended_at = time.time()
                task_group.result_summary = reason
                task_group.metadata["cancel_reason"] = reason
                self.store.save_task_group(task_group)
                cancelled.append(task_group.task_group_id)
        if removed or cancelled:
            self.store.append_event("remaining_task_groups_cancelled", {"reason": reason, "removed_from_queue": removed, "cancelled": cancelled})

    def _execute_queued_task_group(self, task_group: TaskGroup, dry_run: bool = True) -> tuple[dict[str, Any], bool]:
        if task_group.status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            self.store.set_active(None)
            self.store.append_event(
                "terminal_task_group_execution_skipped",
                {"task_group_id": task_group.task_group_id, "status": task_group.status},
            )
            item = {
                "task_group_id": task_group.task_group_id,
                "status": task_group.status,
                "result_summary": task_group.result_summary,
                "skipped": True,
                "reason": "terminal_task_group",
            }
            return item, False
        if task_group.status in {TaskStatus.NEEDS_INFO.value, TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value}:
            self.store.set_active(None)
            self.store.append_event(
                "non_ready_task_group_execution_skipped",
                {"task_group_id": task_group.task_group_id, "status": task_group.status},
            )
            item = {
                "task_group_id": task_group.task_group_id,
                "status": task_group.status,
                "result_summary": task_group.result_summary,
                "skipped": True,
                "reason": "non_ready_task_group",
            }
            return item, True
        self.store.append_event("task_group_started", {"task_group_id": task_group.task_group_id})
        try:
            updated = self.executor.execute_task_group(task_group, dry_run=dry_run)
        except Exception as exc:
            task_group.status = TaskStatus.FAILED.value
            task_group.ended_at = time.time()
            task_group.result_summary = str(exc)
            task_group.metadata["executor_exception"] = str(exc)
            self.store.save_task_group(task_group)
            self.store.set_active(None)
            self.store.append_event(
                "task_group_executor_exception",
                {"task_group_id": task_group.task_group_id, "error": str(exc)},
            )
            return {"task_group_id": task_group.task_group_id, "status": task_group.status, "result_summary": task_group.result_summary}, True
        if updated.status == TaskStatus.INTERRUPTED.value:
            updated = self._merge_interrupted_task_group_with_store(updated)
        self.store.save_task_group(updated)
        self.store.set_active(None)
        if updated.status == TaskStatus.INTERRUPTED.value:
            self.store.push_interrupted(updated)
            self.store.append_event("task_group_interrupted", {"task_group_id": updated.task_group_id, "resume_context": updated.resume_context})
            return {"task_group_id": updated.task_group_id, "status": updated.status, "result_summary": updated.result_summary}, True
        if updated.status == TaskStatus.NEEDS_INFO.value:
            ask_user = updated.metadata.get("runtime_ask_user") if isinstance(updated.metadata, dict) else None
            self.store.append_event("task_group_waiting_runtime_followup", {"task_group_id": updated.task_group_id, "ask_user": ask_user})
            return {"task_group_id": updated.task_group_id, "status": updated.status, "result_summary": updated.result_summary, "ask_user": ask_user}, True
        if updated.status == TaskStatus.FAILED.value:
            failure_summary = self._summarize_failed_task_group(updated)
            if failure_summary:
                updated.result_summary = failure_summary
                self.store.save_task_group(updated)
                ok = self.audio.speak_text(failure_summary)
                self.store.append_event(
                    "task_group_failure_speech",
                    {"task_group_id": updated.task_group_id, "text": failure_summary, "ok": bool(ok)},
                )
        if updated.status == TaskStatus.COMPLETED.value:
            updated.result_summary = self._summarize_task_group(updated)
            cleanup_failed = bool((updated.metadata or {}).get("cleanup_failed"))
            if cleanup_failed:
                cleanup_message = "\u4efb\u52a1\u5df2\u5b8c\u6210\uff0c\u4f46\u786c\u4ef6\u6e05\u7406\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u6295\u5f71\u548c\u5934\u90e8\u72b6\u6001"
                updated.result_summary = f"{updated.result_summary}\uff1b{cleanup_message}" if updated.result_summary else cleanup_message
            if updated.history_refs:
                self.store.append_event(
                    "task_group_history_write_skipped",
                    {"task_group_id": updated.task_group_id, "history_refs": list(updated.history_refs), "reason": "history_already_recorded"},
                )
            else:
                history_id = self.store.write_history(updated)
                updated.history_refs.append(history_id)
            self.store.save_task_group(updated)
            if updated.result_summary:
                ok = self.audio.speak_text(updated.result_summary)
                self.store.append_event(
                    "task_group_summary_speech",
                    {"task_group_id": updated.task_group_id, "text": updated.result_summary, "ok": bool(ok)},
                )
        cleanup_failed = bool((updated.metadata or {}).get("cleanup_failed"))
        if cleanup_failed:
            self.store.clear_task_queue(reason="blocked_by_cleanup_failed")
        should_stop = updated.status in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value} or cleanup_failed
        item = {"task_group_id": updated.task_group_id, "status": updated.status, "result_summary": updated.result_summary}
        if cleanup_failed:
            item["cleanup_failed"] = True
            item["cleanup_failed_finalizers"] = (updated.metadata or {}).get("cleanup_failed_finalizers", [])
        return item, should_stop

    def _runtime_ask_user_item_from_execution(self, execution: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for item in execution.get("executed") or []:
            if not isinstance(item, dict):
                continue
            ask_user = item.get("ask_user")
            if isinstance(ask_user, dict):
                return item, ask_user
        return None

    def _runtime_ask_user_from_execution(self, execution: dict[str, Any]) -> dict[str, Any] | None:
        found = self._runtime_ask_user_item_from_execution(execution)
        return found[1] if found else None

    def _apply_runtime_ask_user(
        self,
        session: CommandSession,
        decision: dict[str, Any],
        execution: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        found = self._runtime_ask_user_item_from_execution(execution)
        if not found:
            return decision, False
        item, runtime_ask_user = found
        task_group_id = item.get("task_group_id")
        if task_group_id:
            self._ensure_runtime_followup_pending(str(task_group_id), runtime_ask_user)
        decision = dict(decision)
        decision["decision_type"] = "ask_user"
        decision["ask_user"] = runtime_ask_user
        decision["reply"] = runtime_ask_user.get("question") or decision.get("reply") or ""
        session.status = SessionStatus.WAITING_USER.value
        session.ended_at = None
        self.store.save_session(session)
        question = str(runtime_ask_user.get("question") or "")
        if question:
            prepared_groups: list[TaskGroup] = []
            if task_group_id:
                with contextlib.suppress(Exception):
                    prepared_groups.append(self.store.load_task_group(str(task_group_id)))
            self._prepare_followup_voice(prepared_groups, runtime_ask_user)
            ok = self.audio.speak_text(question)
            self.store.append_event(
                "runtime_ask_user_spoken",
                {"session_id": session.session_id, "task_group_id": task_group_id, "question": question, "ok": bool(ok)},
            )
        return decision, True

    def _ensure_runtime_followup_pending(self, task_group_id: str, ask_user: dict[str, Any]) -> None:
        try:
            task_group = self.store.load_task_group(task_group_id)
        except Exception:
            return
        question = ask_user.get("question")
        runtime_followup = ask_user.get("runtime_followup") or self._infer_runtime_followup_kind(ask_user)
        target: dict[str, Any] | None = None
        for followup in reversed(task_group.followups or []):
            if followup.get("answer"):
                continue
            if question and followup.get("question") == question:
                target = followup
                break
            if runtime_followup and followup.get("runtime_followup") == runtime_followup:
                target = followup
                break
        payload = self._followup_payload(ask_user)
        if runtime_followup:
            payload["runtime_followup"] = runtime_followup
        if target is None:
            task_group.followups.append(payload)
        else:
            target.update(payload)
            target.pop("answer", None)
            target.pop("answered_at", None)
        self.store.save_task_group(task_group)

    def _infer_runtime_followup_kind(self, ask_user: dict[str, Any]) -> str | None:
        missing = set(ask_user.get("missing_slots") or [])
        if "environment_override" in missing:
            return "environment_override"
        return None

    def _answer_environment_override_followup(
        self,
        task_group: TaskGroup,
        session: CommandSession,
        answer_text: str,
        execute: bool = False,
        dry_run: bool = True,
        enqueue: bool = True,
    ) -> dict[str, Any] | None:
        waiting = task_group.metadata.get("waiting_environment_override") if isinstance(task_group.metadata, dict) else None
        if not isinstance(waiting, dict):
            return None
        action = self._classify_environment_override_reply(answer_text)
        if action == "planner":
            if self._environment_override_answer_has_known_point(answer_text):
                return None
            return self._ask_environment_override_again(task_group, session, answer_text)
        if action == "continue":
            self.store.append_event(
                "environment_override_continue_accepted",
                {"task_group_id": task_group.task_group_id, "answer_text": answer_text},
            )
        if action == "continue_without_projector":
            self.store.append_event(
                "environment_override_continue_without_projector_accepted",
                {"task_group_id": task_group.task_group_id, "answer_text": answer_text},
            )
        if action == "cancel":
            self.store.append_event(
                "environment_override_cancel_accepted",
                {"task_group_id": task_group.task_group_id, "answer_text": answer_text},
            )
        if action == "cancel":
            task_group.status = TaskStatus.CANCELLED.value
            task_group.ended_at = time.time()
            task_group.result_summary = "cancelled"
            task_group.metadata.pop("waiting_environment_override", None)
            task_group.metadata.pop("runtime_ask_user", None)
            finalizers = self.executor.finalize_cancelled_task_group(task_group, dry_run=False)
            if finalizers:
                task_group.metadata.setdefault("cancel_finalizers", []).extend(finalizers)
            self.store.save_task_group(task_group)
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            self.store.save_session(session)
            cancel_reply = self.speech_policy.cancelled(task_group)
            self.audio.speak_text(cancel_reply)
            return {
                "ok": True,
                "session": session.session_id,
                "task_group_id": task_group.task_group_id,
                "decision": {"decision_type": "cancel", "ask_user": None, "reply": cancel_reply, "task_groups": [self._task_group_to_decision_payload(task_group)]},
                "execution": {"executed": [{"task_group_id": task_group.task_group_id, "status": task_group.status, "result_summary": task_group.result_summary}]},
            }

        task_group.metadata["environment_override_accepted"] = {
            "answer": answer_text,
            "accepted_at": time.time(),
            "waiting": waiting,
        }
        task_group.metadata.pop("waiting_environment_override", None)
        task_group.metadata.pop("runtime_ask_user", None)
        if action == "continue_without_projector":
            task_group.metadata["projection_disabled_by_user"] = True
            for step in task_group.steps:
                if step.skill_name == "projector_control" and step.status != TaskStatus.COMPLETED.value:
                    step.status = TaskStatus.CANCELLED.value
                    step.error = "projection_disabled_by_environment_override"
        for step in task_group.steps:
            if step.status in {TaskStatus.RUNNING.value, TaskStatus.INTERRUPTED.value, TaskStatus.FAILED.value}:
                step.status = TaskStatus.NEW.value
                step.error = None
        task_group.status = TaskStatus.NEW.value
        task_group.ended_at = None
        task_group.result_summary = ""
        self.store.save_task_group(task_group)
        if enqueue:
            self._enqueue_ready_session_task_groups(session)
        execution = self.drain_session_queue(session, dry_run=dry_run) if execute else {"executed": []}
        decision = {
            "decision_type": "task_plan",
            "ask_user": None,
            "reply": "\u7ee7\u7eed\u6267\u884c",
            "task_groups": [self._task_group_to_decision_payload(task_group)],
        }
        decision, has_runtime_ask = self._apply_runtime_ask_user(session, decision, execution)
        if not has_runtime_ask:
            session.status = SessionStatus.COMPLETED.value
            session.ended_at = time.time()
            self.store.save_session(session)
        return {
            "ok": True,
            "session": session.session_id,
            "task_group_id": task_group.task_group_id,
            "decision": decision,
            "execution": execution,
        }

    def _ask_environment_override_again(self, task_group: TaskGroup, session: CommandSession, answer_text: str) -> dict[str, Any]:
        waiting = task_group.metadata.get("waiting_environment_override") if isinstance(task_group.metadata, dict) else None
        ask_user = waiting.get("ask_user") if isinstance(waiting, dict) and isinstance(waiting.get("ask_user"), dict) else {}
        blockers = list(waiting.get("blockers") or []) if isinstance(waiting, dict) else []
        question = ask_user.get("question") or self.speech_policy.environment_override_question(task_group, blockers)
        optional_slots = list(ask_user.get("optional_slots") or ["where", "projector_control"])
        candidate_skills = list(ask_user.get("candidate_skills") or ["environment_perception", "navigation_goto", "projector_control"])
        task_group.status = TaskStatus.NEEDS_INFO.value
        task_group.ended_at = None
        task_group.followups.append(
            {
                "question": question,
                "task_title": task_group.title,
                "missing_slots": ["environment_override"],
                "optional_slots": optional_slots,
                "candidate_skills": candidate_skills,
                "timestamp": time.time(),
                "runtime_followup": "environment_override",
            }
        )
        self.store.save_task_group(task_group)
        session.status = SessionStatus.WAITING_USER.value
        session.ended_at = None
        self.store.save_session(session)
        decision = {
            "decision_type": "ask_user",
            "reply": question,
            "task_groups": [self._task_group_to_decision_payload(task_group)],
            "ask_user": {
                "task_title": task_group.title,
                "question": question,
                "missing_slots": ["environment_override"],
                "optional_slots": optional_slots,
                "candidate_skills": candidate_skills,
            },
            "confidence": 1.0,
        }
        self.store.append_event(
            "environment_override_answer_unclear",
            {"task_group_id": task_group.task_group_id, "answer_text": answer_text, "question": question},
        )
        if self.realtime_voice is not None:
            self._prepare_followup_voice([task_group], decision["ask_user"])
        self.audio.speak_text(question)
        return {
            "ok": True,
            "session": session.session_id,
            "task_group_id": task_group.task_group_id,
            "decision": decision,
            "execution": {"executed": []},
        }

    def _environment_override_answer_has_known_point(self, text: str) -> bool:
        if not text:
            return False
        try:
            points = self.planner._known_navigation_points()
        except Exception:
            points = []
        return bool(self.planner._find_known_point_in_text(text, points))

    @staticmethod
    def _looks_like_environment_override_force_answer(text: str) -> bool:
        cleaned = re.sub(r"[\s\u3000\uff0c\u3002,.？！!?、；;：:]+", "", text or "")
        return any(
            word in cleaned
            for word in (
                "继续",
                "接着",
                "不用换",
                "不要换",
                "不换",
                "没关系",
                "没事",
                "安全",
                "强行",
                "直接做",
                "直接开始",
                "就这样",
            )
        )

    def _looks_like_fitness_setup_answer(self, text: str) -> bool:
        if not text:
            return False
        cleaned = re.sub(r"[\s\u3000\uff0c\u3002,.？！!?、；;：:]+", "", text or "")
        has_exercise = self._extract_followup_exercise_type(text) is not None
        has_projector = "投影" in cleaned
        has_where = self._extract_followup_where(text) is not None
        has_setup_intent = any(word in cleaned for word in ("我想", "我要", "做", "开始", "训练", "运动"))
        return has_setup_intent and has_exercise and (has_projector or has_where)

    def _classify_environment_override_reply(self, text: str) -> str:
        cleaned = re.sub(r"[\s\u3000，,。.!！?？、；;：:]+", "", text or "")
        if not cleaned:
            return "planner"
        if self._looks_like_robot_followup_prompt(text) or self._looks_like_assistant_followup_prose(text):
            return "planner"
        if self._looks_like_fitness_setup_answer(text) and not self._looks_like_environment_override_force_answer(text):
            return "planner"
        if any(word in cleaned for word in ("\u53d6\u6d88", "\u4e0d\u505a", "\u7b97\u4e86", "\u505c\u6b62", "\u7ed3\u675f")):
            return "cancel"
        if any(word in cleaned for word in ("\u4e0d\u6295\u5f71", "\u4e0d\u7528\u6295\u5f71", "\u4e0d\u9700\u8981\u6295\u5f71", "\u5173\u6389\u6295\u5f71")):
            return "continue_without_projector"
        continue_words = (
            "\u5c31\u5728\u8fd9",
            "\u8fd9\u91cc\u505a",
            "\u8fd9\u91cc\u7ee7\u7eed",
            "\u539f\u5730\u7ee7\u7eed",
            "\u5c31\u8fd9\u6837\u7ee7\u7eed",
            "\u53ef\u4ee5\u7ee7\u7eed",
            "\u7ee7\u7eed",
            "\u7ee7\u7eed\u5427",
            "\u63a5\u7740",
            "\u63a5\u7740\u505a",
            "\u63a5\u7740\u7ec3",
            "\u76f4\u63a5\u505a",
            "\u76f4\u63a5\u7ee7\u7eed",
            "\u5f3a\u884c\u7ee7\u7eed",
            "\u4e0d\u7528\u6362",
            "\u6ca1\u5173\u7cfb",
            "\u6ca1\u4e8b",
            "\u5b89\u5168",
        )
        if any(word in cleaned for word in continue_words):
            return "continue"
        return "planner"

    def _merge_interrupted_task_group_with_store(self, updated: TaskGroup) -> TaskGroup:
        try:
            stored = self.store.load_task_group(updated.task_group_id)
        except Exception:
            return updated
        if stored.status != TaskStatus.INTERRUPTED.value and not stored.resume_context:
            return updated
        stored_context = dict(stored.resume_context or {})
        updated_context = dict(updated.resume_context or {})
        merged_context = dict(stored_context)
        for key, value in updated_context.items():
            if value not in (None, [], {}):
                merged_context[key] = value
        for key in (
            "reason",
            "wakeup_event_id",
            "robot_state_at_interrupt_request",
            "robot_state_before_interrupt",
            "robot_state_after_stop",
            "interrupt_ownership",
            "executor_interrupt_result",
            "speech_events",
            "last_progress",
        ):
            if key in stored_context and stored_context.get(key) not in (None, [], {}):
                merged_context[key] = stored_context[key]
        updated.resume_context = merged_context
        active_step = self._active_resume_step(updated)
        if not isinstance(updated.resume_context.get("interrupt_ownership"), dict):
            updated.resume_context["interrupt_ownership"] = self._interrupt_task_ownership(updated, active_step)
        updated.interruption_count = max(int(updated.interruption_count or 0), int(stored.interruption_count or 0), 1)
        updated.interrupted_by_session_id = stored.interrupted_by_session_id or updated.interrupted_by_session_id
        updated.metadata = {**(stored.metadata or {}), **(updated.metadata or {})}
        updated.history_refs = list(dict.fromkeys([*(stored.history_refs or []), *(updated.history_refs or [])]))
        return updated

    def _summarize_task_group(self, task_group: TaskGroup) -> str:
        if not task_group.steps:
            return ""
        messages: list[str] = []
        suppressed_by_realtime_event = False
        resumed_context = task_group.resume_context or {}
        pre_resume_completed = set(resumed_context.get("completed_steps") or []) if resumed_context.get("resumed_at") else set()
        realtime_complete_skills = {step.skill_name for step in task_group.steps if self._has_realtime_complete_event(step)}
        support_summary_skills = {"projector_control", "light_control", "fan_control", "feeder_control", "head_control"}
        for step in task_group.steps:
            if self._is_restore_step(step):
                continue
            if step.step_id in pre_resume_completed:
                continue
            parsed = self._step_parsed_json(step)
            skill = step.skill_name
            result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
            status = result.get("status") or parsed.get("status")
            if skill in realtime_complete_skills and skill in support_summary_skills:
                continue
            if self._has_realtime_complete_event(step):
                suppressed_by_realtime_event = True
                continue
            natural_summary = self.speech_policy.step_summary(step, parsed)
            if natural_summary:
                messages.append(natural_summary)
            elif skill == "reminder_schedule":
                reminder = result.get("reminder") if isinstance(result.get("reminder"), dict) else {}
                content = str(reminder.get("content") or step.arguments.get("content") or "").strip()
                display_time = str(reminder.get("display_time") or reminder.get("trigger_condition") or reminder.get("trigger_time") or "").strip()
                if content and display_time:
                    messages.append(f"好的，我会在{display_time}提醒你{content}")
                elif content:
                    messages.append(f"提醒已设置，内容是{content}")
                else:
                    messages.append("提醒已设置")
            elif skill == "reminder_query":
                reminders = result.get("reminders") if isinstance(result.get("reminders"), list) else []
                if not reminders:
                    messages.append("目前没有待执行的提醒")
                else:
                    descriptions = []
                    for item in reminders[:5]:
                        if not isinstance(item, dict):
                            continue
                        content = str(item.get("content") or "提醒")
                        display_time = str(item.get("display_time") or item.get("trigger_condition") or item.get("trigger_time") or "时间未设置")
                        descriptions.append(f"{display_time}提醒你{content}")
                    suffix = f"，另外还有{len(reminders) - 5}个" if len(reminders) > 5 else ""
                    messages.append("你有" + "；".join(descriptions) + suffix)
            elif skill == "reminder_cancel":
                cancelled = int(result.get("cancelled") or 0)
                messages.append(f"已取消{cancelled}个提醒" if cancelled else "没有找到匹配的待执行提醒")
            elif skill.startswith("move_") or skill == "head_control":
                continue
        if messages:
            return "；".join(messages)
        return ""

    def _summarize_failed_task_group(self, task_group: TaskGroup) -> str:
        """Return only failure text explicitly allowed by each skill contract."""
        messages: list[str] = []
        for step in task_group.steps:
            if step.status != TaskStatus.FAILED.value:
                continue
            spec = self.registry.get(step.skill_name) or {}
            contract = spec.get("speech_contract")
            if not isinstance(contract, dict) or contract.get("speak_failure_summary") is not True:
                continue
            parsed = self._step_parsed_json(step)
            field = str(contract.get("failure_summary_field") or "message").strip()
            value = parsed.get(field)
            nested_result = parsed.get("result")
            if value in (None, "") and isinstance(nested_result, dict):
                value = nested_result.get(field)
            if value in (None, ""):
                value = contract.get("failure_fallback_zh")
            if isinstance(value, (str, int, float)):
                message = str(value).strip()
                if message:
                    messages.append(message)
        return "；".join(dict.fromkeys(messages))

    def _is_restore_step(self, step: TaskStep) -> bool:
        return str(step.reason or "").startswith("restore interrupted task ")

    def _speak_skill_event(self, step: TaskStep, event: SpeechEvent) -> bool:
        ok = True
        if event.text:
            spoken_text = self.speech_policy.clean(event.text)
            speech_started_at = time.time()
            ok = self.audio.speak_text(spoken_text)
            speech_finished_at = time.time()
            self.store.append_event(
                "skill_speech_event",
                {
                    "step_id": step.step_id,
                    "skill_name": step.skill_name,
                    "kind": event.kind,
                    "text": event.text,
                    "spoken_text": spoken_text,
                    "ok": bool(ok),
                    "speech_started_at": speech_started_at,
                    "speech_finished_at": speech_finished_at,
                    "elapsed_seconds": round(speech_finished_at - speech_started_at, 4),
                },
            )
        return bool(ok)

    def _has_realtime_complete_event(self, step: TaskStep) -> bool:
        result = step.result if isinstance(step.result, dict) else {}
        events = result.get("speech_events")
        if not isinstance(events, list):
            return False
        for event in events:
            if isinstance(event, dict) and event.get("kind") == "complete":
                return True
        return False

    def _followup_payload(self, ask_user: dict[str, Any]) -> dict[str, Any]:
        return {
            "question": ask_user.get("question"),
            "task_title": ask_user.get("task_title"),
            "missing_slots": list(ask_user.get("missing_slots") or []),
            "optional_slots": list(ask_user.get("optional_slots") or []),
            "deferred_missing_slots": list(ask_user.get("deferred_missing_slots") or []),
            "deferred_optional_slots": list(ask_user.get("deferred_optional_slots") or []),
            "all_missing_slots": list(ask_user.get("all_missing_slots") or ask_user.get("missing_slots") or []),
            "all_optional_slots": list(ask_user.get("all_optional_slots") or ask_user.get("optional_slots") or []),
            "asked_slots": list(ask_user.get("asked_slots") or []),
            "question_policy": dict(ask_user.get("question_policy") or {}),
            "candidate_skills": list(ask_user.get("candidate_skills") or []),
            "timestamp": time.time(),
        }

    def _pending_followup(self, task_group: TaskGroup) -> dict[str, Any] | None:
        for followup in reversed(task_group.followups or []):
            if not followup.get("answer") and not followup.get("closed_at"):
                return followup
        return None

    def _append_pending_followup(self, task_group: TaskGroup, ask_user: dict[str, Any]) -> None:
        question = ask_user.get("question")
        pending = self._pending_followup(task_group)
        if pending is not None and pending.get("question") == question:
            pending.update(
                {
                    "task_title": ask_user.get("task_title") or pending.get("task_title"),
                    "missing_slots": list(ask_user.get("missing_slots") or []),
                    "optional_slots": list(ask_user.get("optional_slots") or []),
                    "deferred_missing_slots": list(ask_user.get("deferred_missing_slots") or []),
                    "deferred_optional_slots": list(ask_user.get("deferred_optional_slots") or []),
                    "all_missing_slots": list(ask_user.get("all_missing_slots") or ask_user.get("missing_slots") or []),
                    "all_optional_slots": list(ask_user.get("all_optional_slots") or ask_user.get("optional_slots") or []),
                    "asked_slots": list(ask_user.get("asked_slots") or []),
                    "question_policy": dict(ask_user.get("question_policy") or {}),
                    "candidate_skills": list(ask_user.get("candidate_skills") or []),
                }
            )
            return
        task_group.followups.append(self._followup_payload(ask_user))

    def _record_followup_answer(self, task_group: TaskGroup, answer_text: str) -> None:
        pending = self._pending_followup(task_group)
        if pending is None:
            task_group.followups.append({"answer": answer_text, "answered_at": time.time()})
            return
        pending["answer"] = answer_text
        pending["answered_at"] = time.time()

    def _merge_followups(self, current: list[dict[str, Any]], replacement: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = [dict(item) for item in (current or []) if isinstance(item, dict)]
        for item in replacement or []:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            answer = item.get("answer")
            matched = None
            for existing in merged:
                if question and existing.get("question") == question:
                    matched = existing
                    break
            if matched is not None:
                for key, value in item.items():
                    if key in {"answer", "answered_at"} and matched.get(key):
                        continue
                    if value not in (None, [], {}):
                        matched[key] = value
                continue
            merged.append(dict(item))
        return merged

    def _merge_replanned_steps(self, task_group: TaskGroup, replacement: list[TaskStep]) -> list[TaskStep]:
        """Preserve only completed steps whose declared slot dependencies remain valid."""
        history = task_group.metadata.get("slot_change_history") or []
        latest = history[-1] if history and isinstance(history[-1], dict) else {}
        changed_slots = set(str(item) for item in (latest.get("updates") or {}).keys())
        changed_slots.update(str(item) for item in latest.get("clears") or [])
        completed: dict[tuple[str, str], list[TaskStep]] = {}
        for step in task_group.steps:
            if step.status != TaskStatus.COMPLETED.value:
                continue
            signature = (step.skill_name, json.dumps(step.arguments or {}, ensure_ascii=False, sort_keys=True, default=str))
            completed.setdefault(signature, []).append(step)
        merged: list[TaskStep] = []
        for order, step in enumerate(replacement):
            signature = (step.skill_name, json.dumps(step.arguments or {}, ensure_ascii=False, sort_keys=True, default=str))
            candidates = completed.get(signature) or []
            declared_dependencies = set(step.depends_on_slots or [])
            dependencies_valid = not changed_slots or bool(declared_dependencies) and declared_dependencies.isdisjoint(changed_slots)
            if candidates and dependencies_valid:
                preserved = candidates.pop(0)
                preserved.order = order
                preserved.reason = step.reason or preserved.reason
                preserved.resources = list(step.resources or preserved.resources)
                preserved.depends_on_slots = list(step.depends_on_slots or preserved.depends_on_slots)
                merged.append(preserved)
                continue
            step.order = order
            merged.append(step)
        return merged

    def _classify_projector_followup_reply(self, followup: dict[str, Any] | None, answer_text: str) -> bool | None:
        if not isinstance(followup, dict):
            return None
        if self._looks_like_robot_followup_prompt(answer_text):
            return None
        optional_slots = set(followup.get("optional_slots") or [])
        candidate_skills = set(followup.get("candidate_skills") or [])
        question = str(followup.get("question") or "")
        if "projector_control" not in optional_slots and "projector_control" not in candidate_skills and "投影" not in question:
            return None
        cleaned = re.sub(r"[\s\u3000，,。.!！?？、；;]+", "", answer_text or "")
        if not cleaned:
            return None
        if any(word in cleaned for word in ("不需要", "不用", "不要", "不开", "别开", "不用打开", "否", "不")):
            return False
        explicit_projection = "投影" in cleaned
        short_generic_yes = cleaned in {"要", "需要", "可以", "好", "好的", "是", "是的", "打开", "开"}
        explicit_projection_positive = explicit_projection and any(
            word in cleaned
            for word in ("需要", "要", "用", "可以", "好", "好的", "打开", "开", "是", "投影")
        )
        if short_generic_yes or explicit_projection_positive:
            return True
        return None

    def _replan_existing_task_group(self, task_group: TaskGroup, answer_text: str) -> dict[str, Any]:
        movement = self._local_movement_followup_decision(task_group, answer_text)
        if movement is not None:
            return self._limit_followup_slots(movement, f"{task_group.user_instruction}\n用户补充：{answer_text}")
        fitness = self._local_fitness_followup_decision(task_group, answer_text)
        if fitness is not None:
            return self._limit_followup_slots(fitness, f"{task_group.user_instruction}\n用户补充：{answer_text}")
        payload = self._task_group_to_decision_payload(task_group)
        decision = {
            "decision_type": "task_plan",
            "reply": "",
            "task_groups": [payload],
            "ask_user": None,
            "confidence": 1.0,
        }
        return self.planner._postprocess_decision(decision, f"{task_group.user_instruction}\n用户补充：{answer_text}")

    def _limit_followup_slots(self, decision: dict[str, Any], user_text: str) -> dict[str, Any]:
        limiter = getattr(self.planner, "limit_followup_slots", None)
        return limiter(decision, user_text) if callable(limiter) else decision

    def _local_movement_followup_decision(self, task_group: TaskGroup, answer_text: str) -> dict[str, Any] | None:
        pending = self._pending_followup(task_group) or (task_group.followups[-1] if task_group.followups else {})
        candidates = {str(item) for item in pending.get("candidate_skills") or []}
        missing = {str(item) for item in pending.get("missing_slots") or []}
        movement_skills = {"move_forward", "move_backward", "move_left", "move_right"}
        if not (candidates & movement_skills or "direction" in missing or any(step.skill_name in movement_skills for step in task_group.steps)):
            return None
        skill = self._extract_followup_movement_skill(answer_text)
        group = {
            "title": task_group.title or "移动任务",
            "user_instruction": task_group.user_instruction,
            "slots": dict(task_group.slots or {}),
            "followups": list(task_group.followups or []),
            "steps": [],
        }
        if not skill:
            question = "你想让我往前、往后、往左还是往右移动？"
            return {
                "decision_type": "ask_user",
                "reply": question,
                "task_groups": [group],
                "ask_user": {
                    "task_title": group["title"],
                    "question": question,
                    "missing_slots": ["direction"],
                    "optional_slots": ["duration"],
                    "candidate_skills": ["move_forward", "move_backward", "move_left", "move_right"],
                },
                "confidence": 0.8,
            }
        arguments: dict[str, Any] = {}
        duration = self._extract_followup_duration_seconds(answer_text)
        if duration is not None:
            arguments["duration"] = duration
        group["slots"]["direction"] = skill
        if duration is not None:
            group["slots"]["duration"] = duration
        group["steps"].append(
            {
                "skill_name": skill,
                "arguments": arguments,
                "reason": "用户补充了移动方向和时长" if duration is not None else "用户补充了移动方向",
            }
        )
        return {
            "decision_type": "task_plan",
            "reply": "好的，我来移动。",
            "task_groups": [group],
            "ask_user": None,
            "confidence": 1.0,
        }

    @staticmethod
    def _extract_followup_movement_skill(text: str) -> str | None:
        cleaned = re.sub(r"[\s\u3000，。,.！？!、；;：:]+", "", text or "").lower()
        if not cleaned:
            return None
        if any(word in cleaned for word in ("往前", "向前", "前进", "朝前", "走前", "forward", "ahead")):
            return "move_forward"
        if any(word in cleaned for word in ("往后", "向后", "后退", "倒退", "朝后", "backward", "back")):
            return "move_backward"
        if any(word in cleaned for word in ("往左", "向左", "左转", "左边", "朝左", "left")):
            return "move_left"
        if any(word in cleaned for word in ("往右", "向右", "右转", "右边", "朝右", "right")):
            return "move_right"
        return None

    @staticmethod
    def _extract_followup_duration_seconds(text: str) -> float | None:
        if not text:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|second)", text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return min(value, 30.0) if value > 0 else None
        zh_digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        match = re.search(r"([零一二两三四五六七八九十])\s*秒", text)
        if match:
            value = float(zh_digits.get(match.group(1), 0))
            return value or None
        return None

    def _local_fitness_followup_decision(self, task_group: TaskGroup, answer_text: str) -> dict[str, Any] | None:
        pending = self._pending_followup(task_group) or (task_group.followups[-1] if task_group.followups else {})
        candidates = set(pending.get("candidate_skills") or [])
        missing = set(pending.get("missing_slots") or [])
        optional = set(pending.get("optional_slots") or [])
        text = f"{task_group.user_instruction}\n用户补充：{answer_text}"
        looks_fitness = bool(candidates & FITNESS_SKILLS or missing & {"exercise_type", "where"} or any(word in text for word in ("运动", "锻炼", "训练", "深蹲", "俯卧撑", "引体")))
        if not looks_fitness:
            return None
        exercise_type = self._extract_followup_exercise_type(answer_text) or task_group.slots.get("exercise_type")
        where = self._extract_followup_where(answer_text) or task_group.slots.get("where")
        projector = self._classify_projector_followup_reply(pending, answer_text)
        if projector is None and "projector" in task_group.slots:
            projector = bool(task_group.slots.get("projector"))
        slots = dict(task_group.slots or {})
        if exercise_type:
            slots["exercise_type"] = exercise_type
        if where:
            slots["where"] = where
        if projector is not None:
            slots["projector"] = projector
        group = {
            "title": task_group.title or "运动任务",
            "user_instruction": task_group.user_instruction,
            "slots": slots,
            "followups": list(task_group.followups or []),
            "steps": [],
        }
        if exercise_type and where:
            if projector is None and ("projector_control" in optional or "projector_control" in candidates or "投影" in str(pending.get("question") or "")):
                question = "需要我打开投影辅助训练吗？"
                return {
                    "decision_type": "ask_user",
                    "reply": question,
                    "task_groups": [group],
                    "ask_user": {
                        "task_title": group["title"],
                        "question": question,
                        "missing_slots": [],
                        "optional_slots": ["projector_control"],
                        "candidate_skills": ["projector_control"],
                    },
                    "confidence": 0.9,
                }
            if where != "here":
                group["steps"].append({"skill_name": "navigation_goto", "arguments": {"action": "goto", "point": where}, "reason": "用户补充了运动地点"})
            group["steps"].append({"skill_name": "head_control", "arguments": {"action": "up", "_scheduler": {"parallel_group": "fitness_setup", "can_parallel": True}}, "reason": "运动计数前调整头部"})
            group["steps"].append({"skill_name": "environment_perception", "arguments": {"camera": "both", "purpose": "fitness_projection", "exercise_type": exercise_type}, "reason": "检查运动和投影环境"})
            if projector is True:
                group["steps"].append({"skill_name": "projector_control", "arguments": {"action": "fitness_video_on"}, "reason": "用户需要运动视频投影辅助"})
            group["steps"].append({"skill_name": exercise_type, "arguments": {"action": "run"}, "reason": "用户补充了运动类型"})
            return {"decision_type": "task_plan", "reply": "好的，我来处理。", "task_groups": [group], "ask_user": None, "confidence": 1.0}
        missing_slots = []
        if not exercise_type:
            missing_slots.append("exercise_type")
        if not where:
            missing_slots.append("where")
        question = "你想做深蹲、俯卧撑还是引体向上？是在这里做，还是去某个已保存的地点做？"
        return {
            "decision_type": "ask_user",
            "reply": question,
            "task_groups": [group],
            "ask_user": {
                "task_title": group["title"],
                "question": question,
                "missing_slots": missing_slots,
                "optional_slots": ["projector_control"] if projector is None else [],
                "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
            },
            "confidence": 0.8,
        }

    def _extract_followup_exercise_type(self, text: str) -> str | None:
        if any(word in text for word in ("深蹲", "下蹲", "蹲")):
            return "squat"
        if any(word in text for word in ("俯卧撑", "伏地挺身")):
            return "push_up"
        if any(word in text for word in ("引体向上", "引体")):
            return "pull_up"
        return None

    def _extract_followup_where(self, text: str) -> str | None:
        cleaned = re.sub(r"[\s，。,.？！!?、；;：:]+", "", text or "")
        if any(word in cleaned for word in ("这里", "就在这", "就在这里", "原地", "当前")):
            return "here"
        try:
            points = self.planner._known_navigation_points()
        except Exception:
            points = []
        for point in points:
            name = str(point.get("name") or "")
            display = str(point.get("display_name") or point.get("label") or "")
            aliases = [str(item) for item in point.get("aliases") or []]
            if any(item and item in text for item in [name, display, *aliases]):
                return name
        return None

    def _step_parsed_json(self, step: TaskStep) -> dict[str, Any]:
        result = step.result if isinstance(step.result, dict) else {}
        parsed = result.get("parsed_json")
        if isinstance(parsed, dict):
            return parsed
        stdout = result.get("stdout")
        if not isinstance(stdout, str):
            return {}
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if isinstance(data, dict):
                    return data
        return {}

    def ask_resume_confirmation(self) -> dict[str, Any] | None:
        if not self.config.get("planner", {}).get("ask_resume_interrupted_task", True):
            return None
        task_group = self.store.peek_interrupted()
        if not task_group:
            return None
        title = self.speech_policy.task_name(task_group)
        question = self.speech_policy.resume_question(task_group)
        payload = {"task_group_id": task_group.task_group_id, "title": title, "question": question}
        self.store.append_event("resume_confirmation_prompted", payload)
        self.audio.speak_text(question)
        return payload

    def classify_resume_reply(self, text: str) -> str:
        cleaned = re.sub(r"[\s，。,.？！!?、；;：:]+", "", text or "")
        if not cleaned:
            return "unknown"
        decline_words = (
            "不用恢复",
            "不要恢复",
            "不用继续",
            "不要继续",
            "别继续",
            "先不恢复",
            "先不继续",
            "不需要",
            "取消",
            "算了",
            "停止",
            "结束",
            "不做了",
        )
        exact_declines = {"不用", "不要", "不用了", "不要了"}
        if cleaned in exact_declines or any(word in cleaned for word in decline_words):
            return "cancel"
        explicit_resume_words = (
            "恢复",
            "继续刚才",
            "接着刚才",
            "继续那个任务",
            "接着那个任务",
            "继续执行",
            "恢复执行",
        )
        if any(word in cleaned for word in explicit_resume_words):
            return "resume"
        if "继续" in cleaned and any(word in cleaned for word in ("不用回", "不回去", "就在这里", "原地继续")):
            return "resume"
        if self._is_affirmative_resume_reply(cleaned):
            return "resume"
        return "command"

    @staticmethod
    def _is_affirmative_resume_reply(cleaned: str) -> bool:
        """Accept a short affirmative dialogue act without swallowing a new command."""
        remainder = str(cleaned or "")
        tokens = sorted(
            {
                "当然可以",
                "当然要",
                "继续执行",
                "恢复执行",
                "继续做",
                "接着做",
                "是的",
                "对的",
                "没错",
                "好的",
                "可以",
                "需要",
                "要的",
                "需要的",
                "继续",
                "接着",
                "恢复",
                "执行",
                "当然",
                "请",
                "是",
                "对",
                "嗯嗯",
                "嗯",
                "好",
                "行",
                "要",
                "做",
                "吧",
                "啊",
                "呀",
                "呢",
                "啦",
            },
            key=len,
            reverse=True,
        )
        consumed = False
        while remainder:
            token = next((item for item in tokens if remainder.startswith(item)), None)
            if token is None:
                return False
            remainder = remainder[len(token) :]
            consumed = True
        return consumed

    def cancel_last_interrupted(self, reason: str = "user_declined_resume", *, speak: bool = True) -> dict[str, Any]:
        task_group = self.store.pop_interrupted()
        if not task_group:
            return {"ok": True, "cancelled": False}
        task_group.status = TaskStatus.CANCELLED.value
        task_group.ended_at = time.time()
        task_group.result_summary = "cancelled"
        task_group.resume_context = dict(task_group.resume_context or {})
        task_group.resume_context.update({"cancelled_at": time.time(), "cancel_reason": reason, "can_resume": False})
        finalizers = self.executor.finalize_cancelled_task_group(task_group, dry_run=False)
        if finalizers:
            task_group.metadata.setdefault("cancel_finalizers", []).extend(finalizers)
        self.store.save_task_group(task_group)
        self.store.append_event("interrupted_task_cancelled", {"task_group_id": task_group.task_group_id, "reason": reason})
        if speak:
            self.audio.speak_text(self.speech_policy.cancelled(task_group))
        return {"ok": True, "cancelled": True, "task_group_id": task_group.task_group_id}

    def preview_resume_scene_restore(self) -> dict[str, Any]:
        task_group = self.store.peek_interrupted()
        if not task_group:
            return {"ok": True, "has_interrupted": False}
        active_step = self._active_resume_step(task_group)
        requirements = self._scene_restore_requirements_for_task_group(task_group, active_step)
        saved_state = self._resume_saved_robot_state(task_group)
        neutralized = bool((task_group.resume_context or {}).get("neutralized_for_new_session"))
        # Skills update the state cache after every successful hardware effect.
        # Once the runtime itself neutralized an interrupted task, that cache is
        # the authoritative current scene and avoids a multi-second ROS pose probe.
        current_state = self.robot_state.snapshot(active_task_group_id=None, active_step=None, fast=neutralized)
        diff = self.robot_restorer.diff(saved_state, current_state, requirements=requirements)
        restore_steps = self._filter_restore_steps(self.robot_restorer.build_restore_steps(diff))
        restore_steps = self._suppress_duplicate_active_restore(restore_steps, active_step)
        return {
            "ok": True,
            "has_interrupted": True,
            "task_group_id": task_group.task_group_id,
            "active_skill": active_step.skill_name if active_step else None,
            "neutralized_for_new_session": neutralized,
            "scene_restore_requirements": requirements,
            "scene_changed": bool(diff.get("scene_changed") and restore_steps),
            "diff": diff,
            "restore_steps": [self._step_to_payload(step) for step in restore_steps],
            "question": self.speech_policy.scene_restore_question(task_group),
        }

    def classify_scene_restore_reply(self, text: str) -> str:
        cleaned = re.sub(r"[\s\u3000\uff0c\u3002,.？！!?、；;：:]+", "", text or "")
        if any(word in cleaned for word in ("\u5c31\u5728\u8fd9", "\u4e0d\u7528\u56de", "\u4e0d\u8981\u56de", "\u4e0d\u6062\u590d\u73b0\u573a", "\u8df3\u8fc7\u6062\u590d")):
            return "skip_restore"
        if any(word in cleaned for word in ("\u56de\u53bb", "\u6062\u590d\u73b0\u573a", "\u5148\u56de\u5230\u521a\u624d", "\u56de\u539f\u6765")):
            return "restore"
        action = self.classify_resume_reply(text)
        if action == "cancel":
            return "skip_restore"
        if action == "resume":
            return "restore"
        return "restore"

    def resume_last_interrupted(
        self,
        execute: bool = False,
        dry_run: bool = True,
        restore_scene: bool = True,
        scene_preview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_group = self.store.peek_interrupted()
        if not task_group:
            return {"ok": True, "resumed": False}
        try:
            resume_plan = self._prepare_resume_task_group(
                task_group,
                restore_scene=restore_scene,
                scene_preview=scene_preview,
            )
        except Exception as exc:
            self.store.append_event(
                "resume_prepare_failed",
                {"task_group_id": task_group.task_group_id, "error": str(exc)},
            )
            return {"ok": False, "resumed": False, "task_group_id": task_group.task_group_id, "error": str(exc)}
        task_group.status = TaskStatus.QUEUED.value
        task_group.ended_at = None
        task_group.result_summary = ""
        self.store.save_task_group(task_group)
        self.store.enqueue_task_group(task_group)
        popped = self.store.remove_interrupted(task_group.task_group_id)
        if popped is None or popped.task_group_id != task_group.task_group_id:
            self.store.append_event(
                "resume_interrupted_stack_mismatch",
                {
                    "expected_task_group_id": task_group.task_group_id,
                    "popped_task_group_id": popped.task_group_id if popped else None,
                },
            )
        execution = self.drain_task_group_ids([task_group.task_group_id], dry_run=dry_run) if execute else {"executed": []}
        return {"ok": True, "resumed": True, "task_group_id": task_group.task_group_id, "resume_plan": resume_plan, "execution": execution}

    def _prepare_resume_task_group(
        self,
        task_group: TaskGroup,
        restore_scene: bool = True,
        scene_preview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(task_group.resume_context or {})
        task_group.steps = self._strip_previous_restore_steps(task_group.steps)
        active_step = self._active_resume_step(task_group)
        requirements = self._scene_restore_requirements_for_task_group(task_group, active_step)
        restore_steps: list[TaskStep] = []
        preview_diff = scene_preview.get("diff") if isinstance(scene_preview, dict) else None
        preview_matches = bool(
            isinstance(preview_diff, dict)
            and scene_preview.get("task_group_id") == task_group.task_group_id
            and isinstance(preview_diff.get("current"), dict)
            and isinstance(preview_diff.get("saved"), dict)
        )
        if preview_matches:
            current_state = dict(preview_diff["current"])
            saved_state = dict(preview_diff["saved"])
            scene_diff = preview_diff
        else:
            current_state = self.robot_state.snapshot(active_task_group_id=None, active_step=None)
            saved_state = self._resume_saved_robot_state(task_group)
            scene_diff = self.robot_restorer.diff(saved_state, current_state, requirements=requirements)
        if restore_scene and requirements:
            restore_steps = self._filter_restore_steps(self.robot_restorer.build_restore_steps(scene_diff))
            restore_steps = self._suppress_duplicate_active_restore(restore_steps, active_step)

        if active_step is not None:
            resume_args = self._resume_arguments_for_step(active_step, context)
            active_step.arguments.update(resume_args)
            active_step.status = TaskStatus.NEW.value
            active_step.error = None

        active_found = active_step is None
        for step in task_group.steps:
            if step is active_step:
                active_found = True
                continue
            if active_found and step.status != TaskStatus.COMPLETED.value:
                step.status = TaskStatus.NEW.value
                step.error = None

        if restore_steps:
            task_group.steps = restore_steps + task_group.steps
            for index, step in enumerate(task_group.steps):
                step.order = index

        context.update(
            {
                "resumed_at": time.time(),
                "resume_restore_scene": bool(restore_scene),
                "current_robot_state_before_resume": current_state,
                "scene_diff_before_resume": scene_diff,
                "scene_restore_requirements": requirements,
                "restore_steps": [self._step_to_payload(step) for step in restore_steps],
                "resume_step": self._step_to_payload(active_step) if active_step else None,
                "resume_strategy": context.get("resume_strategy") or self._resume_strategy_for_step(active_step),
                "recovery": self._recovery_for_step(active_step),
            }
        )
        task_group.resume_context = context
        task_group.metadata.update({"resume_plan": context})
        return {
            "restore_scene": bool(restore_scene),
            "scene_changed": bool(scene_diff.get("scene_changed") and restore_steps),
            "restore_steps": context["restore_steps"],
            "resume_step": context["resume_step"],
        }

    def _active_resume_step(self, task_group: TaskGroup) -> TaskStep | None:
        context = dict(task_group.resume_context or {})
        active_step_id = context.get("active_step_id")
        active_step = self._find_step(task_group, active_step_id)
        if active_step is not None:
            return active_step
        step_status = context.get("step_status")
        if isinstance(step_status, list):
            for item in step_status:
                if not isinstance(item, dict):
                    continue
                if item.get("status") in {TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value}:
                    found = self._find_step(task_group, item.get("step_id"))
                    if found is not None:
                        return found
        return self._first_interrupted_or_pending_step(task_group)

    def _step_from_payload_or_task(self, task_group: TaskGroup, payload: dict[str, Any] | None) -> TaskStep | None:
        if isinstance(payload, dict):
            found = self._find_step(task_group, payload.get("step_id"))
            if found is not None:
                return found
            if payload.get("skill_name"):
                for step in task_group.steps:
                    if step.skill_name == payload.get("skill_name") and step.status != TaskStatus.COMPLETED.value:
                        return step
        return self._first_interrupted_or_pending_step(task_group)

    def _recovery_for_step(self, step: TaskStep | None) -> dict[str, Any]:
        if step is None:
            return {}
        spec = self.registry.get(step.skill_name) or {}
        recovery = spec.get("recovery") if isinstance(spec.get("recovery"), dict) else {}
        return dict(recovery)

    def _resume_strategy_for_step(self, step: TaskStep | None) -> str:
        recovery = self._recovery_for_step(step)
        if recovery.get("resume_strategy"):
            return str(recovery["resume_strategy"])
        if step is None:
            return "restart_step"
        if step.skill_name in FITNESS_SKILLS:
            return "resume_with_arguments"
        if step.skill_name == "navigation_goto":
            return "replan_to_original_goal"
        if step.skill_name in {"person_tracking", "pet_tracking"}:
            return "restore_scene_then_restart_search"
        return "restart_step"

    def _scene_restore_requirements_for_step(self, step: TaskStep | None) -> list[str]:
        recovery = self._recovery_for_step(step)
        if recovery and recovery.get("requires_scene_restore") is not True:
            return []
        requirements = recovery.get("scene_restore_requirements") if isinstance(recovery.get("scene_restore_requirements"), list) else None
        if requirements is not None:
            return [str(item) for item in requirements if str(item).strip()]
        if step is None:
            return []
        if step.skill_name in FITNESS_SKILLS:
            return ["pose", "head", "back_camera", "npu"]
        if step.skill_name in {"person_tracking", "pet_tracking"}:
            return ["pose", "head", "front_camera", "base", "npu"]
        if step.skill_name in {"face_recognition", "face_registration", "environment_perception"}:
            return ["head", "front_camera"]
        return []

    def _scene_restore_requirements_for_task_group(self, task_group: TaskGroup, active_step: TaskStep | None) -> list[str]:
        requirements: list[str] = list(self._scene_restore_requirements_for_step(active_step))
        active_order = active_step.order if active_step is not None else None
        if self._is_fitness_task_group(task_group) and bool((task_group.slots or {}).get("projector")):
            requirements.append("projector")
        if self._task_contains_projector_on_step(task_group, active_order) or self._task_contains_projector_on_step(task_group, None):
            requirements.append("projector")
        for step in task_group.steps:
            if active_order is not None and step.order > active_order:
                continue
            if step.status not in {TaskStatus.COMPLETED.value, TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value}:
                continue
            if step.skill_name == "head_control":
                requirements.append("head")
            elif step.skill_name == "projector_control":
                requirements.append("projector")
            elif step.skill_name == "light_control":
                requirements.append("light")
            elif step.skill_name == "fan_control":
                requirements.append("fan")
        return list(dict.fromkeys(item for item in requirements if item))

    def _resume_saved_robot_state(self, task_group: TaskGroup) -> dict[str, Any]:
        context = task_group.resume_context or {}
        keys: list[str] = ["saved_interrupt_state_for_resume"]
        if not context.get("neutralized_for_new_session"):
            keys.append("robot_state_full_snapshot")
        keys.extend(
            [
                "robot_state_before_interrupt",
                "robot_state_at_interrupt_request",
                "robot_state_after_stop",
            ]
        )
        if context.get("neutralized_for_new_session"):
            keys.append("robot_state_full_snapshot")
        for key in keys:
            value = context.get(key)
            if isinstance(value, dict):
                return self._sanitize_resume_robot_state(value, key)
        nested = context.get("executor_interrupt_result") if isinstance(context.get("executor_interrupt_result"), dict) else {}
        snapshot = nested.get("snapshot") if isinstance(nested.get("snapshot"), dict) else {}
        value = snapshot.get("robot_state")
        if isinstance(value, dict):
            return self._sanitize_resume_robot_state(value, "executor_interrupt_result.snapshot.robot_state")
        return {}

    def _sanitize_resume_robot_state(self, state: dict[str, Any], source_key: str) -> dict[str, Any]:
        sanitized = dict(state)
        pose = sanitized.get("pose")
        if isinstance(pose, dict) and self._resume_pose_is_cached(sanitized, pose):
            ignored_pose = dict(pose)
            ignored_pose.update(
                {
                    "valid": False,
                    "ignored_for_resume": True,
                    "ignore_reason": "cached_pose_not_current",
                    "original_valid": bool(pose.get("valid")),
                    "resume_source_key": source_key,
                }
            )
            ignored_pose["source"] = f"{pose.get('source') or 'cached_pose'}_ignored_for_resume"
            sanitized["pose"] = ignored_pose
        return sanitized

    def _resume_pose_is_cached(self, state: dict[str, Any], pose: dict[str, Any]) -> bool:
        source = str(pose.get("source") or "")
        return bool(
            pose.get("from_cache")
            or state.get("snapshot_mode") == "fast"
            or source.startswith("fast_cached")
        )

    def _filter_restore_steps(self, steps: list[TaskStep]) -> list[TaskStep]:
        filtered = []
        for step in steps:
            ok, _reason = self.registry.validate_step(step.skill_name)
            if ok:
                step.resources = self.resources.resources_for_skill(step.skill_name, step.arguments)
                filtered.append(step)
        return filtered

    @staticmethod
    def _suppress_duplicate_active_restore(steps: list[TaskStep], active_step: TaskStep | None) -> list[TaskStep]:
        if active_step is None or active_step.skill_name != "projector_control":
            return steps
        action = str((active_step.arguments or {}).get("action") or "").strip().lower()
        if action != "meeting_presentation_on":
            return steps
        # The active long-running projector step recreates its own content.
        # Restoring it as a prerequisite would block forever before resume.
        return [step for step in steps if step.skill_name != "projector_control"]

    def _find_step(self, task_group: TaskGroup, step_id: Any) -> TaskStep | None:
        if not step_id:
            return None
        for step in task_group.steps:
            if step.step_id == step_id:
                return step
        return None

    def _first_running_step(self, task_group: TaskGroup) -> TaskStep | None:
        for step in task_group.steps:
            if step.status == TaskStatus.RUNNING.value:
                return step
        return None

    def _first_interrupted_or_pending_step(self, task_group: TaskGroup) -> TaskStep | None:
        for step in task_group.steps:
            if step.status in {TaskStatus.INTERRUPTED.value, TaskStatus.RUNNING.value, TaskStatus.NEW.value, TaskStatus.QUEUED.value}:
                return step
        return None

    def _resume_arguments_for_step(self, step: TaskStep, context: dict[str, Any]) -> dict[str, Any]:
        if step.skill_name in FITNESS_SKILLS:
            progress_payload = self._progress_payload(context)
            count = self._progress_number(progress_payload, ("current_count", "count", "total_count"))
            elapsed = self._progress_number(progress_payload, ("elapsed_seconds", "total_elapsed_seconds"))
            args: dict[str, Any] = {"resume_from_interrupt": True}
            back_camera = self.config.get("cameras", {}).get("back", {}).get("device")
            if back_camera:
                args["camera"] = back_camera
            if count is not None and count > 0:
                args["initial_count"] = int(count)
            if elapsed is not None and elapsed > 0:
                args["initial_elapsed_seconds"] = round(float(elapsed), 2)
                duration = step.arguments.get("duration", 30)
                try:
                    remaining = max(5, int(float(duration) - float(elapsed)))
                    args["duration"] = remaining
                except Exception:
                    pass
            return args
        if step.skill_name in {"person_tracking", "pet_tracking"}:
            args = {"resume_from_interrupt": True}
            payload = self._progress_payload(context)
            target = payload.get("target") or step.arguments.get("target")
            if target:
                args["target"] = target
            pet = payload.get("pet") or step.arguments.get("pet")
            if pet and step.skill_name == "pet_tracking":
                args["pet"] = pet
            return args
        if step.skill_name == "navigation_goto":
            return {"resume_from_interrupt": True}
        return {}

    def _strip_previous_restore_steps(self, steps: list[TaskStep]) -> list[TaskStep]:
        result: list[TaskStep] = []
        for step in steps:
            reason = str(step.reason or "")
            if reason.startswith("restore interrupted task "):
                continue
            result.append(step)
        return result

    def _progress_payload(self, context: dict[str, Any]) -> dict[str, Any]:
        progress = context.get("last_progress")
        if isinstance(progress, dict):
            payload = progress.get("payload")
            if isinstance(payload, dict):
                return payload
            return progress
        return {}

    def _progress_number(self, payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = payload.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except Exception:
                continue
        return None

    def _step_to_payload(self, step: TaskStep | None) -> dict[str, Any] | None:
        if step is None:
            return None
        return {"step_id": step.step_id, "skill_name": step.skill_name, "arguments": step.arguments, "reason": step.reason, "resources": step.resources}

    def _task_groups_from_decision(self, session: CommandSession, decision: dict[str, Any]) -> list[TaskGroup]:
        task_groups = []
        ask_user = decision.get("ask_user") if isinstance(decision.get("ask_user"), dict) else None
        raw_groups = [dict(group) for group in (decision.get("task_groups") or []) if isinstance(group, dict)]
        ungrounded_checker = getattr(self.planner, "_is_ungrounded_intent_ask", None)
        if callable(ungrounded_checker) and ungrounded_checker(ask_user, raw_groups):
            decision["decision_type"] = "answer"
            decision["interaction_type"] = "conversation"
            decision["reply"] = self.planner._fallback_conversation_reply(
                session.utterances[0].get("text", "") if session.utterances else ""
            )
            decision["task_groups"] = []
            decision["ask_user"] = None
            decision["rejected_ungrounded_task_group"] = True
            return []
        ask_target_index = self._ask_user_target_group_index(raw_groups, ask_user) if ask_user else None
        if ask_user and raw_groups and ask_target_index is None:
            raw_groups.append(
                {
                    "title": ask_user.get("task_title") or "需要补充信息",
                    "user_instruction": session.utterances[0]["text"] if session.utterances else "",
                    "slots": {},
                    "followups": [],
                    "steps": [],
                }
            )
            ask_target_index = len(raw_groups) - 1
            decision["task_groups"] = raw_groups
        if ask_user and not raw_groups:
            raw_groups.append(
                {
                    "title": ask_user.get("task_title") or "需要补充信息",
                    "user_instruction": session.utterances[0]["text"] if session.utterances else "",
                    "slots": {},
                    "followups": [],
                    "steps": [],
                }
            )
            ask_target_index = 0
            decision["task_groups"] = raw_groups
        if ask_user and ask_target_index is not None and 0 <= ask_target_index < len(raw_groups):
            target_title = raw_groups[ask_target_index].get("title") or ask_user.get("task_title")
            if target_title:
                ask_user = dict(ask_user)
                ask_user["task_title"] = target_title
                decision["ask_user"] = ask_user
        for group_index, raw_group in enumerate(raw_groups):
            group_is_waiting_for_ask = bool(ask_user and ask_target_index == group_index)
            task_group = TaskGroup(
                command_session_id=session.session_id,
                user_instruction=raw_group.get("user_instruction") or "",
                title=raw_group.get("title") or f"Task {group_index + 1}",
                slots=raw_group.get("slots") or {},
                followups=raw_group.get("followups") or [],
                status=TaskStatus.NEEDS_INFO.value if group_is_waiting_for_ask else TaskStatus.NEW.value,
                metadata={
                    "planner_confidence": decision.get("confidence"),
                    "revision": 1,
                    "dialogue_state": "waiting_followup" if group_is_waiting_for_ask else "ready",
                    "suspend_reason": None,
                    "resume_policy": None,
                },
            )
            if group_is_waiting_for_ask:
                self._append_pending_followup(task_group, ask_user)
            for order, raw_step in enumerate(raw_group.get("steps") or []):
                skill_name = raw_step.get("skill_name") or raw_step.get("name") or ""
                step = TaskStep(
                    order=order,
                    skill_name=skill_name,
                    arguments=raw_step.get("arguments") or {},
                    reason=raw_step.get("reason") or "",
                    resources=self.resources.resources_for_skill(skill_name, raw_step.get("arguments") or {}),
                    depends_on_slots=[str(item) for item in raw_step.get("depends_on_slots") or []],
                )
                task_group.steps.append(step)
            self._ensure_task_group_preconditions(task_group)
            task_groups.append(task_group)
        return task_groups

    def _ask_user_target_group_index(self, raw_groups: list[dict[str, Any]], ask_user: dict[str, Any] | None) -> int | None:
        if not raw_groups or not isinstance(ask_user, dict):
            return None
        ask_title = self._compact_match_text(ask_user.get("task_title"))
        if ask_title:
            for index, group in enumerate(raw_groups):
                if self._compact_match_text(group.get("title")) == ask_title:
                    return index
        best: tuple[int, int] | None = None
        for index, group in enumerate(raw_groups):
            score = self._ask_user_group_match_score(group, ask_user)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, index)
        return best[1] if best is not None else None

    def _ask_user_group_match_score(self, raw_group: dict[str, Any], ask_user: dict[str, Any]) -> int:
        title = self._compact_match_text(raw_group.get("title"))
        instruction = self._compact_match_text(raw_group.get("user_instruction"))
        ask_title = self._compact_match_text(ask_user.get("task_title"))
        question = self._compact_match_text(ask_user.get("question"))
        missing = {str(item) for item in ask_user.get("missing_slots") or []}
        candidates = {str(item) for item in ask_user.get("candidate_skills") or []}
        skills = self._raw_group_skill_names(raw_group)
        score = 0
        if ask_title:
            if title == ask_title:
                score += 100
            elif ask_title in title or title in ask_title:
                score += 70
            elif ask_title in instruction or instruction in ask_title:
                score += 50
        if candidates and skills:
            if skills & candidates:
                score += 80
            elif skills and not self._decision_skills_match_pending(skills, candidates):
                score -= 80
        if candidates & FITNESS_SKILLS or missing & {"exercise_type", "where"}:
            if self._raw_group_looks_like_fitness(raw_group):
                score += 80
            elif skills and not (skills & (FITNESS_SKILLS | {"head_control", "environment_perception", "projector_control", "navigation_goto"})):
                score -= 80
        movement_skills = {"move_forward", "move_backward", "move_left", "move_right"}
        if "direction" in missing or candidates & movement_skills:
            if skills & movement_skills:
                score += 80
            elif "移动" in title or "移动" in instruction or "动" in title:
                score += 40
        if not skills and ask_title and (ask_title in title or ask_title in instruction or title in question):
            score += 35
        if not skills and not title and not instruction:
            score += 5
        return score

    @staticmethod
    def _compact_match_text(value: Any) -> str:
        return re.sub(r"[\s\u3000，。,.！？!?、；;：:]+", "", str(value or "")).lower()

    @staticmethod
    def _raw_group_skill_names(raw_group: dict[str, Any]) -> set[str]:
        skills: set[str] = set()
        for raw_step in raw_group.get("steps") or []:
            if isinstance(raw_step, dict):
                skill = str(raw_step.get("skill_name") or raw_step.get("name") or "").strip()
                if skill:
                    skills.add(skill)
        return skills

    def _raw_group_looks_like_fitness(self, raw_group: dict[str, Any]) -> bool:
        slots = raw_group.get("slots") if isinstance(raw_group.get("slots"), dict) else {}
        if str(slots.get("intent") or "") in {"fitness", "exercise"}:
            return True
        if self._extract_followup_exercise_type(str(slots.get("exercise_type") or "")):
            return True
        skills = self._raw_group_skill_names(raw_group)
        if skills & FITNESS_SKILLS:
            return True
        text = f"{raw_group.get('title') or ''} {raw_group.get('user_instruction') or ''}"
        return bool(self._extract_followup_exercise_type(text) or any(word in text for word in ("运动", "锻炼", "训练", "健身")))

    def _ensure_task_group_preconditions(self, task_group: TaskGroup) -> None:
        if not task_group.steps:
            return
        updated: list[TaskStep] = []
        head_ready = False
        inserted = False
        for step in task_group.steps:
            if step.skill_name == "head_control":
                action = str((step.arguments or {}).get("action") or "").strip().lower()
                if self._is_non_level_head_action(action):
                    head_ready = True
                elif action in {"level", "horizontal", "center", "neutral", "stop"}:
                    head_ready = False
                updated.append(step)
                continue
            if self._skill_needs_head_up_before_camera(step) and not head_ready:
                updated.append(
                    TaskStep(
                        skill_name="head_control",
                        arguments={"action": "up"},
                        reason=f"auto precondition: raise head before {step.skill_name}",
                        resources=self.resources.resources_for_skill("head_control", {"action": "up"}),
                    )
                )
                head_ready = True
                inserted = True
            updated.append(step)
        if not inserted:
            return
        for order, step in enumerate(updated):
            step.order = order
            step.resources = self.resources.resources_for_skill(step.skill_name, step.arguments)
        task_group.steps = updated
        task_group.metadata.setdefault("auto_preconditions", []).append(
            {
                "kind": "head_control_up_before_camera",
                "skills": [step.skill_name for step in updated],
            }
        )

    def _skill_needs_head_up_before_camera(self, step: TaskStep) -> bool:
        skill = step.skill_name
        if skill in {
            "face_recognition",
            "face_registration",
            "person_tracking",
            "front_camera_capture",
            "front_camera_record",
        }:
            return True
        if skill in FITNESS_SKILLS:
            return True
        if skill == "environment_perception":
            camera = str((step.arguments or {}).get("camera") or (step.arguments or {}).get("camera_name") or "front").lower()
            return camera in {"front", "back", "both", ""}
        if skill in {"camera_capture", "camera_record"}:
            camera = str((step.arguments or {}).get("camera") or (step.arguments or {}).get("camera_name") or "front").lower()
            return camera in {"front", "back", ""}
        return False

    def _is_task_group_waiting_for_followup(self, task_group: TaskGroup) -> bool:
        if task_group.status != TaskStatus.NEEDS_INFO.value:
            return False
        if self._pending_followup(task_group) is not None:
            return True
        return not task_group.followups

    def _find_pending_followup_task_group(self, session: CommandSession) -> TaskGroup | None:
        candidates: list[TaskGroup] = []
        for item in session.task_group_ids:
            try:
                task_group = self.store.load_task_group(item)
            except Exception:
                continue
            if self._is_task_group_waiting_for_followup(task_group):
                candidates.append(task_group)
        if not candidates:
            return None
        candidates.sort(
            key=lambda task_group: (
                task_group.status == TaskStatus.NEEDS_INFO.value,
                bool(task_group.followups and not task_group.followups[-1].get("answer")),
                not bool(task_group.steps),
                task_group.created_at,
            ),
            reverse=True,
        )
        return candidates[0]

    def _select_followup_replacement(self, pending: TaskGroup, candidates: list[TaskGroup]) -> TaskGroup:
        if not candidates:
            return pending

        def candidate_score(candidate: TaskGroup) -> int:
            pending_text = f"{pending.title} {pending.user_instruction}"
            candidate_text = f"{candidate.title} {candidate.user_instruction}"
            score = 0
            if candidate.title and candidate.title == pending.title:
                score += 20
            if candidate.user_instruction and candidate.user_instruction == pending.user_instruction:
                score += 8
            if pending.title and pending.title in candidate_text:
                score += 4
            if pending.user_instruction and pending.user_instruction in candidate_text:
                score += 3
            if candidate.steps:
                score += 1

            skill_names = {step.skill_name for step in candidate.steps}
            hint_groups = [
                (("运动", "锻炼", "深蹲", "下蹲", "蹲下", "俯卧撑", "引体"), {"squat", "push_up", "pull_up", "projector_control"}),
                (("人脸", "识别", "注册"), {"face_recognition", "face_registration"}),
                (("追踪", "跟踪", "宠物", "行人", "人"), {"person_tracking", "pet_tracking"}),
            ]
            for terms, skills in hint_groups:
                if any(term in pending_text for term in terms) and skill_names & skills:
                    score += 12
            return score

        return max(candidates, key=candidate_score)

    def _task_group_to_decision_payload(self, task_group: TaskGroup) -> dict[str, Any]:
        return {
            "title": task_group.title,
            "user_instruction": task_group.user_instruction,
            "slots": task_group.slots,
            "followups": task_group.followups,
            "steps": [
                {
                    "skill_name": step.skill_name,
                    "arguments": step.arguments,
                    "reason": step.reason,
                }
                for step in task_group.steps
            ],
        }
