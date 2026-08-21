from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


FOLLOWUP_ANSWER = "slot_answer"
TASK_MODIFICATION = "task_modification"
TASK_CANCEL = "task_cancel"
TASK_PAUSE = "task_pause"
TASK_RESUME = "task_resume"
TASK_RESTART = "task_restart"
TASK_REPLACEMENT = "task_replacement"
TEMPORARY_TASK = "temporary_task"
TASK_QUERY = "task_query"
CONVERSATION = "conversation"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Interaction:
    interaction_type: str
    task_operation: str
    text: str
    reply: str
    slot_updates: dict[str, Any]
    slot_clears: list[str]
    resume_dialogue_after_reply: bool
    confidence: float


class InteractionRouter:
    """Normalize model dialogue acts without knowing any concrete robot skill."""

    _VALID_TYPES = {
        FOLLOWUP_ANSWER,
        TASK_MODIFICATION,
        TASK_CANCEL,
        TASK_PAUSE,
        TASK_RESUME,
        TASK_RESTART,
        TASK_REPLACEMENT,
        TEMPORARY_TASK,
        TASK_QUERY,
        CONVERSATION,
        AMBIGUOUS,
    }
    _TYPE_ALIASES = {
        "answer_slot": FOLLOWUP_ANSWER,
        "followup_answer": FOLLOWUP_ANSWER,
        "modify_task": TASK_MODIFICATION,
        "cancel_task": TASK_CANCEL,
        "pause_task": TASK_PAUSE,
        "resume_task": TASK_RESUME,
        "restart_task": TASK_RESTART,
        "replace_task": TASK_REPLACEMENT,
        "command": TEMPORARY_TASK,
        "new_task": TEMPORARY_TASK,
        "information_question": CONVERSATION,
        "capability_question": CONVERSATION,
        "social": CONVERSATION,
        "answer": CONVERSATION,
    }

    def classify(self, decision: dict[str, Any], text: str, *, has_pending_followup: bool) -> Interaction:
        text = str(text or decision.get("user_text") or decision.get("followup_text") or "").strip()
        raw_type = str(decision.get("interaction_type") or "").strip().lower()
        operation = str(decision.get("task_operation") or "none").strip().lower()
        interaction_type = self._TYPE_ALIASES.get(raw_type, raw_type)

        explicit_control = self._explicit_control_type(text)
        if explicit_control:
            interaction_type = explicit_control

        if not explicit_control and interaction_type not in self._VALID_TYPES:
            interaction_type = self._from_operation(operation)
        if interaction_type not in self._VALID_TYPES:
            interaction_type = self._fallback_type(decision, text, has_pending_followup)

        updates = decision.get("slot_updates")
        clears = decision.get("slot_clears")
        return Interaction(
            interaction_type=interaction_type,
            task_operation=operation if operation and operation != "none" else self._operation_for_type(interaction_type),
            text=text,
            reply=str(decision.get("reply") or "").strip(),
            slot_updates=dict(updates) if isinstance(updates, dict) else {},
            slot_clears=[str(item) for item in clears] if isinstance(clears, list) else [],
            resume_dialogue_after_reply=bool(decision.get("resume_dialogue_after_reply", interaction_type == CONVERSATION)),
            confidence=self._confidence(decision.get("confidence")),
        )

    def _fallback_type(self, decision: dict[str, Any], text: str, has_pending: bool) -> str:
        compact = re.sub(r"[\s\u3000，。,.！!？?、；;：:]+", "", text)
        if decision.get("task_groups"):
            return TASK_MODIFICATION if has_pending else TEMPORARY_TASK
        model_type = str(decision.get("decision_type") or "").lower()
        if model_type == "answer" and not decision.get("ask_user"):
            return CONVERSATION
        return FOLLOWUP_ANSWER if has_pending else AMBIGUOUS

    @staticmethod
    def _explicit_control_type(text: str) -> str:
        compact = re.sub(r"[\s\u3000，。,.！!？?、；;：:]+", "", text)
        if any(marker in compact for marker in ("算了不做了", "取消任务", "别做了", "不要继续", "不用继续")):
            return TASK_CANCEL
        if any(marker in compact for marker in ("从头开始", "重新开始", "重来一次", "重新做")):
            return TASK_RESTART
        if any(marker in compact for marker in ("暂停一下", "先停一下", "先暂停")):
            return TASK_PAUSE
        if any(marker in compact for marker in ("继续刚才", "恢复刚才", "接着做")):
            return TASK_RESUME
        return ""

    def _from_operation(self, operation: str) -> str:
        mapping = {
            "update": TASK_MODIFICATION,
            "modify": TASK_MODIFICATION,
            "cancel": TASK_CANCEL,
            "pause": TASK_PAUSE,
            "resume": TASK_RESUME,
            "restart": TASK_RESTART,
            "replace": TASK_REPLACEMENT,
            "temporary": TEMPORARY_TASK,
            "query": TASK_QUERY,
        }
        return mapping.get(operation, "")

    @staticmethod
    def _operation_for_type(interaction_type: str) -> str:
        return {
            TASK_MODIFICATION: "modify",
            TASK_CANCEL: "cancel",
            TASK_PAUSE: "pause",
            TASK_RESUME: "resume",
            TASK_RESTART: "restart",
            TASK_REPLACEMENT: "replace",
            TEMPORARY_TASK: "temporary",
            TASK_QUERY: "query",
        }.get(interaction_type, "none")

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0
