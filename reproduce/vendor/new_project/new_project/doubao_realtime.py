from __future__ import annotations

import argparse
import asyncio
import contextlib
import concurrent.futures
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .resources import ResourceManager
from .skill_registry import SkillRegistry
from .user_memory import UserMemoryStore


STRICT_JSON_PREFIX = (
    "CRITICAL OUTPUT CONTRACT:\n"
    "You must output exactly one JSON object. The first character must be { and the last character must be }.\n"
    "Never output Markdown, code fences, or natural language outside JSON.\n"
)


NEW_PROJECT_SCHEMA_PROMPT = """
你是家用机器人 new_project 的实时语音决策器。你只负责把用户语音转换成结构化 JSON；你不能直接调用 shell、不能直接执行 /home/test/single_function。

必须输出 new_project decision schema：
{
  "decision_type": "task_plan|ask_user|answer|noop",
  "reply": "要对用户说的简短中文",
  "task_groups": [
    {
      "title": "任务标题",
      "user_instruction": "用户原话或等价转写",
      "slots": {},
      "followups": [],
      "steps": [
        {"skill_name": "技能名", "arguments": {}, "reason": "为什么执行"}
      ]
    }
  ],
  "ask_user": null,
  "confidence": 0.0-1.0,
  "user_text": "你听到的用户文本"
}

追问信息不足时：
{
  "decision_type": "ask_user",
  "reply": "只问一个问题",
  "task_groups": [{"title":"...", "user_instruction":"...", "slots":{}, "followups":[], "steps":[]}],
  "ask_user": {"task_title":"...", "question":"只问一个问题", "missing_slots":[], "optional_slots":[], "candidate_skills":[]},
  "confidence": 0.8,
  "user_text": "..."
}

普通聊天或无法执行的请求使用 decision_type=answer，task_groups=[]。

关键规则：
- skill_name 必须来自可用技能列表；disabled skill 不能执行。
- 不确定地点、姓名、运动类型等必要信息时 ask_user。
- “我想做运动/锻炼”但没说项目时，问深蹲、俯卧撑还是引体向上；可同时问地点和是否打开投影。
- 明确运动项目时，通常先 head_control up；需要投影时加 projector_control fitness_video_on 播放运动视频；运动 step 用 squat/push_up/pull_up action=run。
- 找狗/找猫/找宠物：优先 pet_tracking action=find_route，arguments 包含 pet、search_strategy=current_then_known_points、track_after_found=true。
- 导航到已保存地点：navigation_goto action=goto，优先使用 point，不要编造 x/y。
- 不要声称任务已经完成，只能说“好的，我来处理”。
"""


RESUME_SCHEMA_PROMPT = """
你只判断用户对恢复刚才任务的回答。只能输出 JSON：
{"reply_type":"resume_confirmation","text":"用户原文","resume_action":"resume|cancel|command"}
resume 表示继续、恢复、接着、可以、好的；cancel 表示不用、不要、取消、先不继续；command 表示用户给了新的任务。
"""


FOLLOWUP_SCHEMA_PROMPT = """
The robot is waiting for information about an existing TaskGroup. The next
utterance is not necessarily a direct answer. It may modify/cancel/restart the
existing task, request a temporary new task, query task state, or be ordinary
conversation. Classify it before filling any slot.

Return the normal new_project decision JSON plus these fields:
{
  "interaction_type": "slot_answer|task_modification|task_cancel|task_pause|task_resume|task_restart|task_replacement|temporary_task|task_query|conversation|ambiguous",
  "task_operation": "none|modify|cancel|pause|resume|restart|replace|temporary|query",
  "slot_updates": {},
  "slot_clears": [],
  "resume_dialogue_after_reply": false
}

Rules:
- A user may revise any earlier choice, not only the slot in the current robot
  question. Preserve choices they did not revise.
- If the user changes an earlier choice but does not answer the current slot,
  use task_modification and leave that slot unresolved.
- Ordinary conversation uses interaction_type=conversation,
  decision_type=answer, task_groups=[], ask_user=null, and a useful Chinese
  reply. It must not alter, answer, cancel, or complete the existing TaskGroup.
- A new executable request that should run before returning to the existing
  task uses temporary_task. A request that abandons the old task uses
  task_replacement.
- Mixed utterances may include an "actions" array in spoken order. The primary
  interaction_type must describe the operation that affects the TaskGroup.
- Never treat an unrelated conversation answer as a slot value.
- Each generated step should declare depends_on_slots so a revised slot only
  invalidates dependent work and unrelated completed steps can be preserved.
"""


COMPACT_COMMAND_SCHEMA_PROMPT = """
You are the realtime voice decision engine for the home robot project named
new_project. Understand the user's complete spoken Chinese semantically, then
return exactly one compact JSON object. Never execute tools yourself.

Compact decision wire format v1:
{
  "v": 1,
  "type": "tasks|ask|answer|noop",
  "interaction": "command|slot_answer|task_modification|task_cancel|task_pause|task_resume|task_restart|task_replacement|temporary_task|task_query|conversation|capability_question|information_question|social|ambiguous",
  "operation": "none|modify|cancel|pause|resume|restart|replace|temporary|query",
  "updates": {},
  "clears": [],
  "resume": false,
  "actions": [],
  "tasks": [
    {"skill":"registered_skill_name","action":"allowed_action","args":{},"group":0,"depends":[]}
  ],
  "ask": null,
  "intent": {
    "speech_act": "explicit_command|implicit_request|question|conversation|social|unclear",
    "actionable": false,
    "authorization": "explicit|implied|none",
    "negated": false,
    "uncertain": false,
    "skill": null,
    "action": null,
    "args": {},
    "confidence": 0.0
  },
  "memory_op": "none|remember|forget|query",
  "fact": null,
  "fact_id": null,
  "query": "",
  "reply": "short natural Chinese robot speech",
  "confidence": 0.0,
  "text": "recognized user text"
}

Output only fields that carry information, except v, type, intent, confidence,
and text are always required. Do not output the legacy verbose fields
decision_type, task_groups, or intent_analysis. The local runtime expands this
compact object into the full internal TaskGroup representation.

Task rules:
- Use type=tasks for executable work. Each task needs skill plus action/args.
  Use the same group number for ordered steps of one goal and different group
  numbers for independent goals spoken in one utterance. Optional task fields
  are title, instruction, slots, depends, and reason.
- Every tasks decision must contain a complete intent safety verdict. For one
  implicit task, intent.skill, intent.action, and intent.args must describe the
  same action as tasks. For multiple explicit tasks they may be null/empty,
  while actionable, authorization, negated, and uncertain still cover the
  whole utterance. Never emit tasks when intent is negated or uncertain.
- An indirect observation, unmet household need, complaint, or location
  concern is an implicit request when one available capability clearly
  realizes the desired state. Set speech_act=implicit_request,
  actionable=true, authorization=implied. Infer meaning from the whole
  utterance and ordinary household context; skill metadata describes
  capabilities and constraints, not phrase lookup tables.
- Negation, refusal, quoted/example speech, speculation, hypothetical wording,
  and diagnostic questions do not authorize an action. Set actionable=false,
  authorization=none, and set negated or uncertain when applicable. A positive
  request to turn something off/close/stop is not negated.
- For irreversible effects, require a clear asserted need concerning the
  intended beneficiary and high confidence.
- Use only names/actions in skill_specs and never use disabled skills. Do not
  invent arguments. The runtime validates and sanitizes every task.

Other decision rules:
- Missing required information uses type=ask and ask={"question":"...",
  "title":"...","missing":[],"optional":[],"skills":[]}. Ask no more than
  two unresolved slots in one short natural Chinese question. Do not ask which
  action to use when the implied goal already determines it.
- Ordinary chat, greetings, capability questions, and information questions
  that need no realtime skill use type=answer, no tasks, and a useful concise
  Chinese reply. Answer capability questions from skill_specs.
- User-facing text must not expose TaskGroup, slots, JSON, models, ASR, or
  execution internals and must not teach fixed command phrases.
- Ask is legal only for a known executable scenario with genuinely unresolved
  slots; never ask for a generic "intent" in ordinary conversation.
- When existing task context is supplied, classify whether the user answers,
  revises, cancels, pauses, resumes, restarts, replaces, queries, temporarily
  interrupts, or merely chats. Preserve choices they did not revise.
- A wake word, filler, silence, prompt echo, or unclear fragment without a new
  actionable command uses type=noop and no tasks.
- For user memory, add memory_op=remember|forget|query plus fact, fact_id, or
  query as needed; memory facts are not hardware tasks. Resolve references from
  supplied user_memory before asking again.
- Never claim execution has already completed. Avoid generic acknowledgement
  such as \"收到\" when the skill's result will provide the useful response.
- For finding a dog/cat/pet, prefer pet_tracking action=find_route with pet,
  search_strategy=current_then_known_points, and track_after_found=true.
- For navigation to a saved place, use navigation_goto action=goto with point;
  never invent x/y coordinates.
- For short base motion use move_forward, move_backward, move_left, or
  move_right with args.duration seconds, never navigation_move/base_move/move.
- For meeting projection, one projector_control meeting_presentation_on task is
  sufficient; deterministic local planning adds navigation, head control,
  environment perception, and projection steps.
- For exercise, use the chosen squat/push_up/pull_up action=run. Local planning
  adds required head/projector steps. Ask when the exercise is not specified.
"""


SEMANTIC_ADJUDICATION_PROMPT = """
This is a second-pass semantic adjudication of one already transcribed user
utterance. The authoritative transcript is supplied below and the same audio is
replayed only to preserve prosody. Ignore any previous assistant answer.

Reason from meaning and conversational context, never from an exact phrase
list. Decide whether the user explicitly or implicitly authorizes a robot
capability. An indirect observation or unmet household need is executable when
one available skill/action clearly realizes the desired state. A negation,
refusal, quotation, hypothetical, uncertain diagnosis, or question asking
whether a condition exists is not authorization.

Return the decision format required by the preceding schema and fill its
semantic intent verdict. If it is actionable, return executable tasks with the
same target skill/action. If it is not actionable, return answer or noop with
no tasks. Never ask which action to use when the inferred desired state already
determines that action.
"""

# ASCII-safe prompt overrides. They avoid locale/display corruption in the
# realtime prompt while still requiring Chinese user-facing replies.
NEW_PROJECT_SCHEMA_PROMPT = """
You are the realtime voice decision engine for the home robot project named
new_project. Convert the user's spoken Chinese into exactly one JSON object.
You must not execute shell commands and must not call /home/test/single_function
directly. The executor in new_project will run all skills.

Required decision schema:
{
  "decision_type": "task_plan|ask_user|answer|noop",
  "interaction_type": "command|slot_answer|task_modification|task_cancel|task_pause|task_resume|task_restart|task_replacement|temporary_task|task_query|conversation|capability_question|information_question|social|ambiguous",
  "task_operation": "none|modify|cancel|pause|resume|restart|replace|temporary|query",
  "slot_updates": {},
  "slot_clears": [],
  "resume_dialogue_after_reply": false,
  "memory_operation": "none|remember|forget|query",
  "memory_fact": null,
  "memory_query": "",
  "intent_analysis": {
    "speech_act": "explicit_command|implicit_request|question|conversation|social|unclear",
    "literal_meaning": "brief Chinese paraphrase of what the user said",
    "implied_goal": "desired result inferred from context, or empty",
    "actionable": false,
    "authorization": "explicit|pragmatically_implied|none",
    "negated": false,
    "uncertain": false,
    "target_skill": null,
    "target_action": null,
    "arguments": {},
    "task_title": "short Chinese task title, or empty",
    "reason": "brief Chinese semantic justification",
    "confidence": 0.0
  },
  "reply": "short Chinese sentence to say to the user",
  "task_groups": [
    {
      "title": "Chinese task title",
      "user_instruction": "transcribed or normalized user request",
      "slots": {},
      "followups": [],
      "steps": [
        {"skill_name": "registered_skill_name", "arguments": {}, "depends_on_slots": [], "reason": "Chinese reason"}
      ]
    }
  ],
  "ask_user": null,
  "confidence": 0.0,
  "user_text": "recognized user text"
}

Output JSON rules:
- The final answer must be parseable by Python json.loads.
- Use only ASCII commas and colons outside strings: "," and ":".
- Every object and array must be closed. In particular task_groups is an array
  of group objects, so close the group object and then the array before writing
  ask_user/confidence/user_text.

If required information is missing, output decision_type=ask_user and ask one
concise Chinese question in both reply and ask_user.question.
- Ask about no more than two unresolved slots in one turn. Keep the wording
  short and conversational. Remaining slots stay in the same TaskGroup and are
  collected by later follow-up turns.

Rules:
- Always perform pragmatic intent analysis before choosing decision_type. Do
  not require an imperative verb, device name, skill name, or memorized command
  phrase. Infer the user's likely desired world state from the whole utterance,
  household context, available capabilities, and ordinary conversational
  implicature.
- A declarative observation, unmet need, complaint, or location concern can be
  an implicit request. If one available skill and action unambiguously satisfy
  the implied goal, set speech_act=implicit_request, actionable=true,
  authorization=pragmatically_implied, and output the executable task_plan.
  Do not ask the user to choose an action that the implied goal already fixes.
- Semantic similarity matters, not literal token overlap with skill metadata.
  Skill descriptions and planning notes describe capabilities and constraints;
  they are not phrase lookup tables.
- Negation, refusal, quoted/example speech, speculation, and questions asking
  whether a condition is true do not authorize a state-changing action. Mark
  negated or uncertain accordingly. For irreversible effects, require a clear
  asserted need or request concerning the intended beneficiary and use high
  confidence; never act on a diagnostic question.
- negated means the user rejects or blocks the candidate execution. A positive
  request whose desired action happens to be off/close/stop is not negated.
- decision_type=answer is allowed only when intent_analysis.actionable=false.
  If actionable=true, target_skill and target_action must be valid according to
  skill_specs and the same action must appear in task_groups.
- User-facing reply and ask_user.question must sound like natural household
  conversation. Do not expose TaskGroup, slot, JSON, model, ASR, hardware-state,
  or execution-engine terminology.
- Never teach the user an exact phrase such as "你可以说..." or require fixed
  command words. Ask an open, context-specific question instead.
- Mention the actual task, target, place, count, or action when known. Avoid
  generic replies such as "收到", "操作完成", or "任务已完成".
- Do not combine more than two unresolved choices in one spoken question.
- User facts such as pet names and preferences are memory operations, not
  hardware TaskGroups. Use memory_operation=remember with a structured
  memory_fact. Use forget/query for deletion and recall. Resolve references
  from user_memory when possible instead of asking again.
- Ordinary conversation, greetings, capability questions, and information
  questions must use decision_type=answer, task_groups=[], and ask_user=null.
  Answer capability questions from skill_specs. Do not ask which task the user
  wants merely because the utterance does not map to one skill.
- If there is an existing task in the supplied context, classify the user's
  relationship to that task before planning. A user can answer a slot, revise
  any earlier slot, cancel, pause, resume, restart, replace the task, run a
  temporary task, query status, or chat without changing the task.
- ask_user is legal only after a concrete executable task or scenario is known
  and one of that task's required slots is missing. Never use missing_slots=["intent"]
  for conversation or capability questions.
- Use only skill names from skill_specs. Never use disabled skills.
- If unsure about required slots such as location, name, exercise type, or time,
  ask_user instead of guessing.
- If the user wants exercise but does not choose the exercise, ask whether they
  want squat, push_up, or pull_up; also ask location and projector preference if
  needed.
- For a clear exercise request, usually add head_control action=up first. If
  projector help is requested, add projector_control action=fitness_video_on. Then add
  squat/push_up/pull_up with action=run.
- For finding a dog/cat/pet, prefer one pet_tracking step with action=find_route,
  pet=dog/cat/all, search_strategy=current_then_known_points, track_after_found=true.
- For navigation to a saved place, use navigation_goto action=goto with point.
  Do not invent x/y coordinates.
- For short base movement commands such as "往前走五秒", "前进5秒",
  "后退", "左移", or "右移", use move_forward, move_backward, move_left,
  or move_right with arguments.duration in seconds. Never use navigation_move,
  base_move, move, or cmd_vel as a skill_name.
- If you accidentally think of navigation_move for "前进/往前/后退/左移/右移",
  replace it with move_forward/move_backward/move_left/move_right before output.
- If the recognized text only contains a wake word, greeting, filler, silence, or
  an unclear fragment without a new actionable command, output decision_type=noop,
  task_groups=[], ask_user=null. Never repeat or reuse a previous command.
- Do not claim the task is already completed. Use replies like "好的，我来处理。".
"""

RESUME_SCHEMA_PROMPT = """
Judge the user's answer to whether they want to resume the interrupted task.
Output exactly one JSON object:
{
  "reply_type": "resume_confirmation",
  "text": "recognized user text",
  "resume_action": "resume|cancel|command",
  "command_decision": null
}
Use resume for yes/continue/resume/OK. Use cancel for no/cancel/do not continue.
If the user gives a new command instead, use resume_action=command and include a
full new_project decision JSON object in command_decision for that command.
"""

FOLLOWUP_SCHEMA_PROMPT = """
You are only transcribing the user's answer to a pending robot follow-up
question. Do not plan, do not answer as the robot, and do not describe the next
robot action. The orchestrator will merge this text back into the existing
TaskGroup and replan with deterministic project logic.

Output exactly one JSON object:
{"reply_type":"followup_answer","text":"recognized Chinese user answer"}

If you did not hear a user answer, use an empty text field. Never output prose
outside JSON.
"""


class RealtimeAudioTransportError(RuntimeError):
    def __init__(self, message: str, audio_frames: list[bytes]):
        super().__init__(message)
        self.audio_frames = list(audio_frames)


class NewProjectDoubaoVoice:
    def __init__(self, base_cls: Any, args: argparse.Namespace, system_prompt: str, dialog_context: list[dict[str, str]] | None = None):
        self._base = base_cls(args)
        self.args = args
        self.system_prompt = system_prompt
        self.dialog_context = dialog_context or []
        self.audio_frames: list[bytes] = []
        self.audio_bytes = 0
        self.audio_send_error = ""
        self._base.session_request = self.session_request

    @property
    def ws(self) -> Any:
        return self._base.ws

    async def connect(self) -> None:
        await self._base.connect()

    async def close(self) -> None:
        await self._base.close()

    async def send_audio(self, audio: bytes) -> None:
        payload = bytes(audio)
        max_bytes = max(64_000, int(getattr(self.args, "audio_buffer_max_bytes", 1_048_576)))
        if self.audio_bytes + len(payload) <= max_bytes:
            self.audio_frames.append(payload)
            self.audio_bytes += len(payload)
        if self.audio_send_error:
            return
        try:
            await asyncio.wait_for(
                self._base.send_audio(payload),
                timeout=max(0.2, float(getattr(self.args, "audio_send_timeout", 1.5))),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep recording into the in-memory buffer. The session owner will
            # reconnect and replay this same utterance instead of asking the user
            # to repeat it or leaving the receive loop blocked forever.
            self.audio_send_error = f"{type(exc).__name__}: {exc}"

    async def send_event(self, event: int, body: dict[str, Any], with_session: bool = False) -> None:
        await self._base.send_event(event, body, with_session=with_session)

    def session_request(self) -> dict[str, Any]:
        return {
            "asr": {"extra": {"end_smooth_window_ms": getattr(self.args, "asr_end_smooth_ms", 300)}},
            "tts": {
                "speaker": self.args.speaker,
                "audio_config": {"channel": 1, "format": "pcm_s16le", "sample_rate": self.args.output_rate},
            },
            "dialog": {
                "bot_name": "豆包",
                "system_role": self.system_prompt,
                "speaking_style": "只输出严格 JSON。不要把 JSON 包在代码块里。",
                "dialog_context": self.dialog_context,
                "extra": {"strict_audit": False, "recv_timeout": 10, "input_mod": "audio"},
            },
        }


class DoubaoRealtimeSession:
    def __init__(self, config: dict[str, Any], registry: SkillRegistry, resources: ResourceManager):
        self.config = config
        self.registry = registry
        self.resources = resources
        self.user_memory = UserMemoryStore(config)
        self.audio = config.get("audio", {})
        self.voice_cfg = config.get("voice_decision", {})
        self._modules: dict[str, Any] | None = None
        self._client: NewProjectDoubaoVoice | None = None
        self._client_prompt = ""
        self._tts_client: Any | None = None
        self._thread_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    @staticmethod
    def _timing_mark(args: argparse.Namespace, name: str, *, first: bool = False) -> float:
        """Record one monotonic timestamp without performing any I/O."""
        timing = getattr(args, "_turn_timing", None)
        now = time.monotonic()
        if isinstance(timing, dict):
            if first:
                timing.setdefault(name, now)
            else:
                timing[name] = now
        return now

    @staticmethod
    def _timing_counter(args: argparse.Namespace, name: str, value: int | float, *, add: bool = False) -> None:
        timing = getattr(args, "_turn_timing", None)
        if not isinstance(timing, dict):
            return
        counters = timing.setdefault("counters", {})
        if not isinstance(counters, dict):
            counters = {}
            timing["counters"] = counters
        if add:
            counters[name] = counters.get(name, 0) + value
        else:
            counters[name] = value

    @staticmethod
    def _duration_ms(timing: dict[str, Any], start: str, end: str) -> float | None:
        start_value = timing.get(start)
        end_value = timing.get(end)
        if not isinstance(start_value, (int, float)) or not isinstance(end_value, (int, float)):
            return None
        return round(max(0.0, float(end_value) - float(start_value)) * 1000.0, 3)

    def _timing_report(self, args: argparse.Namespace) -> dict[str, Any]:
        timing = getattr(args, "_turn_timing", None)
        if not isinstance(timing, dict):
            return {}
        origin = timing.get("turn_started_at")
        if not isinstance(origin, (int, float)):
            numeric_marks = [value for key, value in timing.items() if key.endswith("_at") and isinstance(value, (int, float))]
            origin = min(numeric_marks) if numeric_marks else time.monotonic()

        marks_ms = {
            key: round((float(value) - float(origin)) * 1000.0, 3)
            for key, value in timing.items()
            if key.endswith("_at") and isinstance(value, (int, float))
        }
        duration_pairs = {
            "turn_total": ("turn_started_at", "turn_finished_at"),
            "primary_modules_load": ("primary.modules_load_started_at", "primary.modules_loaded_at"),
            "primary_prompt_build": ("primary.prompt_build_started_at", "primary.prompt_ready_at"),
            "primary_connect": ("primary.connect_started_at", "primary.connected_at"),
            "primary_mic_wait": ("primary.mic_wait_started_at", "primary.mic_acquired_at"),
            "primary_wait_for_voice": ("record_started_at", "voice_detected_at"),
            "primary_voice_capture": ("voice_detected_at", "vad_finished_at"),
            "primary_recording_until_vad_end": ("record_started_at", "vad_finished_at"),
            "primary_silence_tail_upload": ("tail_started_at", "tail_sent_at"),
            "primary_receive": ("primary.receive_started_at", "primary.receive_finished_at"),
            "primary_receive_to_first_websocket_message": ("primary.receive_started_at", "primary.first_websocket_message_at"),
            "primary_record_start_to_asr_final": ("record_started_at", "primary.asr_final_at"),
            "primary_record_start_to_first_model_text": ("record_started_at", "primary.first_model_text_at"),
            "primary_audio_done_to_asr_final": ("primary.audio_sender_done_at", "primary.asr_final_at"),
            "primary_audio_done_to_first_model_text": ("primary.audio_sender_done_at", "primary.first_model_text_at"),
            "primary_first_model_text_to_json_ready": ("primary.first_model_text_at", "primary.decision_ready_at"),
            "primary_json_signal_to_ready": ("primary.first_json_signal_at", "primary.decision_ready_at"),
            "primary_first_to_last_json_parse": ("primary.first_json_parse_started_at", "primary.last_json_parse_finished_at"),
            "primary_close": ("primary.close_started_at", "primary.close_finished_at"),
            "semantic_adjudication_total": ("semantic_adjudication.started_at", "semantic_adjudication.finished_at"),
            "semantic_adjudication_connect": ("semantic_adjudication.connect_started_at", "semantic_adjudication.connected_at"),
            "semantic_adjudication_receive": ("semantic_adjudication.receive_started_at", "semantic_adjudication.receive_finished_at"),
            "semantic_adjudication_audio_done_to_first_model_text": (
                "semantic_adjudication.audio_sender_done_at",
                "semantic_adjudication.first_model_text_at",
            ),
            "semantic_adjudication_first_model_text_to_json_ready": (
                "semantic_adjudication.first_model_text_at",
                "semantic_adjudication.decision_ready_at",
            ),
            "semantic_adjudication_json_signal_to_ready": (
                "semantic_adjudication.first_json_signal_at",
                "semantic_adjudication.decision_ready_at",
            ),
            "semantic_adjudication_close": ("semantic_adjudication.close_started_at", "semantic_adjudication.close_finished_at"),
            "transport_replay_total": ("transport_replay.started_at", "transport_replay.finished_at"),
        }
        durations_ms = {}
        for label, (start, end) in duration_pairs.items():
            value = self._duration_ms(timing, start, end)
            if value is not None:
                durations_ms[label] = value

        return {
            "trace_id": str(timing.get("trace_id") or ""),
            "clock": "monotonic",
            "marks_ms_from_turn_start": dict(sorted(marks_ms.items())),
            "durations_ms": durations_ms,
            "counters": dict(timing.get("counters") or {}),
        }

    def _attach_timing(self, decision: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
        self._timing_mark(args, "turn_finished_at")
        result = dict(decision or {})
        timing = self._timing_report(args)
        if timing:
            result["trace_id"] = timing.get("trace_id")
            result["timing"] = timing
        return result

    def decide_once(self, seconds: float | None = None, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._thread_lock:
            return self._run(self._decide_once_with_reconnect(seconds=seconds, mode=mode, context=context or {}))

    def prepare_once(self, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not bool(self.voice_cfg.get("preconnect_next_turn", True)):
            return {"ok": True, "backend": "doubao_realtime", "skipped": True, "reason": "preconnect_disabled"}
        try:
            with self._thread_lock:
                elapsed = self._run(self._prepare_once(mode=mode, context=context or {}), timeout=5.0)
            return {"ok": True, "backend": "doubao_realtime", "mode": mode, "elapsed_ms": int(elapsed * 1000)}
        except Exception as exc:
            return {"ok": False, "backend": "doubao_realtime", "mode": mode, "error": str(exc)}

    def speak_text(self, text: str) -> bool:
        if not text:
            return False
        try:
            with self._thread_lock:
                return bool(self._run(self._speak_text(text)))
        except Exception as exc:
            print(f"TTS_FAILED:{exc}", flush=True)
            return False

    def close(self) -> None:
        with self._thread_lock:
            with contextlib.suppress(Exception):
                self._run(self._close(), timeout=3.0)
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread is not None:
                self._loop_thread.join(timeout=2.0)
            self._loop = None
            self._loop_thread = None

    def warmup(self) -> dict[str, Any]:
        if not bool(self.voice_cfg.get("warmup_on_start", False)):
            return {"ok": True, "backend": "doubao_realtime", "skipped": True, "reason": "warmup_disabled"}
        try:
            with self._thread_lock:
                self._run(self._probe_realtime_connection())
            return {"ok": True, "backend": "doubao_realtime"}
        except Exception as exc:
            return {"ok": False, "backend": "doubao_realtime", "error": str(exc)}

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        if timeout is None:
            timeout = float(self.voice_cfg.get("operation_timeout_seconds", self.audio.get("first_response_timeout", 20.0) + 10.0))
        try:
            return future.result(timeout=max(1.0, float(timeout)))
        except concurrent.futures.TimeoutError:
            future.cancel()
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(lambda: None)
            raise RuntimeError("doubao realtime operation timeout")

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        self._loop_ready.clear()

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._loop_thread = threading.Thread(target=run_loop, name="doubao-realtime-loop", daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(timeout=3.0)
        if self._loop is None:
            raise RuntimeError("doubao realtime event loop did not start")
        return self._loop

    async def _decide_once(self, seconds: float | None, mode: str, context: dict[str, Any]) -> dict[str, Any]:
        args = self._voice_args(seconds=seconds, mode=mode)
        self._timing_mark(args, "primary.modules_load_started_at")
        modules = self._load_modules()
        self._timing_mark(args, "primary.modules_loaded_at")
        self._timing_mark(args, "primary.prompt_build_started_at")
        prompt = self._system_prompt(mode=mode, context=context)
        self._timing_mark(args, "primary.prompt_ready_at")
        self._timing_counter(args, "primary.prompt_chars", len(prompt))
        prepared_client_available = bool(
            self._client is not None
            and self._client_prompt == prompt
            and self._client_healthy(self._client)
        )
        self._timing_counter(args, "primary.prepared_client_reused", int(prepared_client_available))
        self._timing_mark(args, "primary.connect_started_at")
        client = await self._take_prepared_client(prompt, args)
        self._timing_mark(args, "primary.connected_at")
        replay_frames: list[bytes] = []
        captured_frames: list[bytes] = []
        decision: dict[str, Any] | None = None
        try:
            self._timing_mark(args, "primary.mic_wait_started_at")
            with self.resources.acquire(["mic"]):
                self._timing_mark(args, "primary.mic_acquired_at")
                send_task = asyncio.create_task(modules["record_one_turn"](client, args))
                self._timing_mark(args, "primary.audio_sender_started_at")
                try:
                    decision = await self._receive_decision(client, args, send_task, mode=mode, phase="primary")
                except RealtimeAudioTransportError as exc:
                    replay_frames = exc.audio_frames
                except Exception:
                    send_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await send_task
                    if client.audio_frames:
                        replay_frames = list(client.audio_frames)
                    else:
                        raise
                else:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await send_task
                captured_frames = list(client.audio_frames)
                self._timing_counter(args, "primary.captured_audio_frames", len(captured_frames))
                self._timing_counter(args, "primary.captured_audio_bytes", sum(len(frame) for frame in captured_frames))
        finally:
            self._timing_mark(args, "primary.close_started_at")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(client.close(), timeout=args.close_timeout)
            self._timing_mark(args, "primary.close_finished_at")
        if replay_frames:
            try:
                decision = await self._replay_buffered_audio(
                    replay_frames,
                    args,
                    prompt,
                    mode,
                    phase="transport_replay",
                )
                captured_frames = list(replay_frames)
            except Exception as exc:
                return self._attach_timing(
                    self._empty_decision(f"audio_replay_connection_failed: {type(exc).__name__}: {exc}", mode),
                    args,
                )
        if decision is None:
            return self._attach_timing(self._empty_decision("audio_transport_failed_without_buffer", mode), args)
        decision = await self._maybe_adjudicate_semantics(decision, captured_frames, args, mode)
        return self._attach_timing(decision, args)

    async def _maybe_adjudicate_semantics(
        self,
        primary: dict[str, Any],
        audio_frames: list[bytes],
        args: argparse.Namespace,
        mode: str,
    ) -> dict[str, Any]:
        should_adjudicate = self._should_adjudicate_semantics(primary, mode)
        self._timing_counter(args, "semantic_adjudication.triggered", int(should_adjudicate))
        if not should_adjudicate:
            return primary
        transcript = str(primary.get("asr_text") or primary.get("user_text") or "").strip()
        minimum_bytes = max(1, int(args.input_rate * 2 * 0.2))
        if not transcript or sum(len(frame) for frame in audio_frames) < minimum_bytes:
            return primary

        self._timing_mark(args, "semantic_adjudication.started_at")
        self._timing_counter(args, "semantic_adjudication.input_audio_frames", len(audio_frames))
        self._timing_counter(args, "semantic_adjudication.input_audio_bytes", sum(len(frame) for frame in audio_frames))
        adjudication_args = argparse.Namespace(**vars(args))
        adjudication_args.decision_timeout_after_input = float(
            self.voice_cfg.get("semantic_adjudication_timeout_after_input_seconds", 3.0)
        )
        adjudication_args.asr_command_fallback_timeout = float(
            self.voice_cfg.get("semantic_adjudication_asr_fallback_timeout_seconds", 6.0)
        )
        prompt = self._semantic_adjudication_prompt(transcript)
        self._timing_counter(args, "semantic_adjudication.prompt_chars", len(prompt))
        self._timing_counter(args, "semantic_adjudication.transcript_chars", len(transcript))
        try:
            adjudicated = await self._replay_buffered_audio(
                audio_frames,
                adjudication_args,
                prompt,
                "command",
                phase="semantic_adjudication",
            )
        except Exception as exc:
            self._timing_mark(args, "semantic_adjudication.finished_at")
            result = dict(primary)
            result["semantic_adjudication_error"] = f"{type(exc).__name__}: {exc}"
            return result
        self._timing_mark(args, "semantic_adjudication.finished_at")

        analysis = adjudicated.get("intent_analysis")
        if not isinstance(analysis, dict):
            result = dict(primary)
            result["semantic_adjudication_error"] = adjudicated.get("error") or "missing_intent_analysis"
            return result

        adjudicated = dict(adjudicated)
        adjudicated["semantic_adjudication_completed"] = True
        adjudicated["semantic_adjudication_primary"] = {
            "decision_type": primary.get("decision_type"),
            "interaction_type": primary.get("interaction_type"),
            "had_task_groups": bool(primary.get("task_groups")),
            "had_ask_user": isinstance(primary.get("ask_user"), dict),
            "confidence": primary.get("confidence"),
            "model_error": primary.get("model_error") or primary.get("error"),
        }
        if not adjudicated.get("task_groups") and not isinstance(adjudicated.get("ask_user"), dict):
            adjudicated["reply"] = adjudicated.get("reply") or primary.get("reply") or ""
        return adjudicated

    def _should_adjudicate_semantics(self, decision: dict[str, Any], mode: str) -> bool:
        if mode != "command" or not bool(self.voice_cfg.get("semantic_adjudication_on_unresolved", True)):
            return False
        if decision.get("semantic_adjudication_completed"):
            return False
        executable_steps = [
            step
            for group in decision.get("task_groups") or []
            if isinstance(group, dict)
            for step in group.get("steps") or []
            if isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "").strip()
        ]
        if executable_steps:
            return False
        ask_user = decision.get("ask_user")
        if isinstance(ask_user, dict):
            unresolved = {str(item) for item in ask_user.get("missing_slots") or []}
            if unresolved and not unresolved.issubset({"action", "intent"}):
                return False
        transcript = str(decision.get("asr_text") or decision.get("user_text") or "").strip()
        return bool(transcript and not self._transcript_is_non_actionable(transcript))

    def _semantic_adjudication_prompt(self, transcript: str) -> str:
        payload = self._prompt_payload()
        prompt = (
            STRICT_JSON_PREFIX
            + self._command_schema_prompt()
            + "\n"
            + SEMANTIC_ADJUDICATION_PROMPT
            + "\nAuthoritative transcript:\n"
            + json.dumps(str(transcript), ensure_ascii=False)
            + "\nAvailable robot capabilities:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        return self._trim_prompt(prompt)

    async def _replay_buffered_audio(
        self,
        audio_frames: list[bytes],
        args: argparse.Namespace,
        prompt: str,
        mode: str,
        phase: str = "transport_replay",
    ) -> dict[str, Any]:
        minimum_bytes = max(1, int(args.input_rate * 2 * 0.2))
        if sum(len(frame) for frame in audio_frames) < minimum_bytes:
            return self._empty_decision("audio_transport_failed_before_speech_buffered", mode)

        self._timing_mark(args, f"{phase}.started_at", first=True)
        self._timing_mark(args, f"{phase}.connect_started_at")
        client = await self._new_realtime_client(prompt, args)
        self._timing_mark(args, f"{phase}.connected_at")

        async def replay() -> bool:
            for frame in audio_frames:
                await client.send_audio(frame)
                if client.audio_send_error:
                    break
            return not bool(client.audio_send_error)

        send_task = asyncio.create_task(replay())
        self._timing_mark(args, f"{phase}.audio_sender_started_at")
        try:
            decision = await self._receive_decision(client, args, send_task, mode=mode, phase=phase)
            decision["audio_transport_replayed"] = True
            return decision
        except RealtimeAudioTransportError as exc:
            return self._empty_decision(f"audio_replay_failed: {exc}", mode)
        finally:
            if not send_task.done():
                send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task
            self._timing_mark(args, f"{phase}.close_started_at")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(client.close(), timeout=args.close_timeout)
            self._timing_mark(args, f"{phase}.close_finished_at")
            self._timing_mark(args, f"{phase}.finished_at")

    async def _decide_once_with_reconnect(self, seconds: float | None, mode: str, context: dict[str, Any]) -> dict[str, Any]:
        last_error = ""
        for attempt in range(2):
            try:
                result = await self._decide_once(seconds=seconds, mode=mode, context=context)
                timing = result.get("timing") if isinstance(result, dict) else None
                if isinstance(timing, dict):
                    counters = timing.setdefault("counters", {})
                    if isinstance(counters, dict):
                        counters["connection_attempt"] = attempt + 1
                        counters["reconnected"] = int(attempt > 0)
                return result
            except Exception as exc:
                last_error = str(exc)
                await self._reset_realtime_client()
                if attempt == 0 and self._is_reconnectable_error(exc):
                    continue
                break
        return self._empty_decision(last_error or "doubao_realtime_connection_failed", mode)

    async def _reset_realtime_client(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        self._client = None
        self._client_prompt = ""

    async def _prepare_once(self, mode: str, context: dict[str, Any]) -> float:
        started = time.monotonic()
        args = self._voice_args(mode=mode)
        prompt = self._system_prompt(mode=mode, context=context)
        await self._reset_realtime_client()
        self._client = await self._new_realtime_client(prompt, args)
        self._client_prompt = prompt
        return time.monotonic() - started

    async def _reset_tts_client(self) -> None:
        if self._tts_client is not None:
            with contextlib.suppress(Exception):
                await self._tts_client.close()
        self._tts_client = None

    async def _probe_realtime_connection(self) -> None:
        args = self._voice_args()
        client = await self._new_realtime_client(self._system_prompt(mode="command", context={}), args)
        with contextlib.suppress(Exception):
            await client.close()

    @staticmethod
    def _is_reconnectable_error(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(
            marker in text
            for marker in (
                "connectionclosed",
                "connection closed",
                "no close frame",
                "data transfer failed",
                "resume_reading",
                "websocket",
                "ssl",
                "broken pipe",
                "connection reset",
                "timeout",
            )
        )

    async def _speak_text(self, text: str) -> bool:
        modules = self._load_modules()
        args = self._voice_args()
        with self.resources.acquire(["speaker"]):
            for attempt in range(2):
                client = modules["DoubaoVoice"](args)
                try:
                    await client.connect()
                    if await self._speak_text_checked(client, args, text):
                        return True
                except Exception as exc:
                    if attempt == 1:
                        print(f"TTS_FAILED:{exc}", flush=True)
                finally:
                    with contextlib.suppress(Exception):
                        await client.close()
            return False

    async def _speak_text_checked(self, client: Any, args: argparse.Namespace, text: str) -> bool:
        parse_response = self._load_modules()["parse_response"]
        start_aplay = self._load_modules()["start_aplay"]
        wait_aplay_finished = self._load_modules()["wait_aplay_finished"]
        await self._drain_pending_responses(client, max_sec=0.5, idle_sec=0.1)
        await client.send_event(300, {"content": text}, with_session=True)
        aplay = None if args.no_play else start_aplay(args)
        got_audio = False
        got_end = False
        deadline = time.monotonic() + args.first_response_timeout
        try:
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(client.ws.recv(), timeout=0.8)
                except asyncio.TimeoutError:
                    continue
                response = parse_response(msg)
                body = response.get("body")
                if isinstance(body, bytes):
                    got_audio = True
                    if aplay and aplay.stdin:
                        aplay.stdin.write(body)
                        aplay.stdin.flush()
                    continue
                if response.get("type") == "error":
                    text_body = str(body)
                    if "52000042" in text_body or "DialogAudioIdleTimeoutError" in text_body:
                        if got_audio:
                            continue
                        break
                    print("TTS_ERROR:", body, flush=True)
                    break
                if response.get("event") == 359:
                    got_end = True
                    if got_audio or args.no_play:
                        break
            if not got_audio and not args.no_play:
                print("TTS_NO_AUDIO", flush=True)
                await self._reset_tts_client()
            return bool(got_audio or (args.no_play and got_end))
        finally:
            wait_aplay_finished(aplay)

    async def _drain_pending_responses(self, client: Any, max_sec: float = 0.5, idle_sec: float = 0.1) -> None:
        started = time.monotonic()
        last_seen = started
        while time.monotonic() - started < max_sec:
            try:
                await asyncio.wait_for(client.ws.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                if time.monotonic() - last_seen >= idle_sec:
                    break
                continue
            except Exception:
                break
            last_seen = time.monotonic()

    async def _close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.close(), timeout=1.5)
            self._client = None
        if self._tts_client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._tts_client.close(), timeout=1.5)
            self._tts_client = None

    async def _ensure_client(self, system_prompt: str) -> NewProjectDoubaoVoice:
        modules = self._load_modules()
        if self._client is not None and self._client_prompt == system_prompt and self._client_healthy(self._client):
            return self._client
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None
            self._client_prompt = ""
        args = self._voice_args()
        client = NewProjectDoubaoVoice(modules["DoubaoVoice"], args, system_prompt, self._dialog_context())
        await client.connect()
        self._client = client
        self._client_prompt = system_prompt
        return client

    async def _new_realtime_client(self, system_prompt: str, args: argparse.Namespace) -> NewProjectDoubaoVoice:
        modules = self._load_modules()
        client = NewProjectDoubaoVoice(modules["DoubaoVoice"], args, system_prompt, self._dialog_context())
        await asyncio.wait_for(client.connect(), timeout=args.connect_timeout)
        return client

    async def _take_prepared_client(self, system_prompt: str, args: argparse.Namespace) -> NewProjectDoubaoVoice:
        if self._client is not None and self._client_prompt == system_prompt and self._client_healthy(self._client):
            client = self._client
            self._client = None
            self._client_prompt = ""
            return client
        await self._reset_realtime_client()
        return await self._new_realtime_client(system_prompt, args)

    async def _ensure_tts_client(self) -> Any:
        modules = self._load_modules()
        if self._tts_client is not None and self._client_healthy(self._tts_client):
            return self._tts_client
        if self._tts_client is not None:
            with contextlib.suppress(Exception):
                await self._tts_client.close()
            self._tts_client = None
        args = self._voice_args()
        client = modules["DoubaoVoice"](args)
        await client.connect()
        self._tts_client = client
        return client

    @staticmethod
    def _client_healthy(client: Any) -> bool:
        ws = getattr(client, "ws", None)
        if ws is None:
            return False
        if bool(getattr(ws, "closed", False)):
            return False
        close_code = getattr(ws, "close_code", None)
        if close_code is not None:
            return False
        return True

    def _load_modules(self) -> dict[str, Any]:
        if self._modules is not None:
            return self._modules
        self_program_dir = Path(self.config["paths"]["self_program_dir"])
        if str(self_program_dir) not in sys.path:
            sys.path.insert(0, str(self_program_dir))
        from vocal_stream_llm import DoubaoVoice, load_env_file, parse_response, record_one_turn, start_aplay, wait_aplay_finished

        load_env_file()
        self._modules = {
            "DoubaoVoice": DoubaoVoice,
            "parse_response": parse_response,
            "record_one_turn": record_one_turn,
            "start_aplay": start_aplay,
            "wait_aplay_finished": wait_aplay_finished,
        }
        return self._modules

    def _voice_args(self, seconds: float | None = None, mode: str = "command") -> argparse.Namespace:
        vad = self.audio.get("vad", {})
        timeout_key = f"{mode}_decision_timeout_after_input"
        default_decision_timeout = 5.0 if mode == "followup" else 4.0 if mode == "resume" else 1.2
        decision_timeout = self.voice_cfg.get(timeout_key, self.voice_cfg.get("decision_timeout_after_input", default_decision_timeout))
        turn_started_at = time.monotonic()
        trace_id = f"voice_{uuid.uuid4().hex[:12]}"
        return argparse.Namespace(
            seconds=float(seconds or self.audio.get("default_voice_seconds") or self.audio.get("default_record_seconds", 10)),
            chunk_ms=int(self.audio.get("chunk_ms", 100)),
            silence_tail_sec=float(self.audio.get("silence_tail_sec", 0.25)),
            first_response_timeout=float(self.audio.get("first_response_timeout", 20.0)),
            model_text_first_timeout=float(self.voice_cfg.get("model_text_first_timeout_seconds", 7.0)),
            asr_command_fallback_timeout=float(self.voice_cfg.get("asr_command_fallback_timeout_seconds", 4.0)),
            decision_timeout_after_input=float(decision_timeout),
            listen_timeout=float(self.audio.get("listen_timeout", 8.0)),
            vad_frame_ms=int(vad.get("frame_ms", 20)),
            vad_aggressiveness=int(vad.get("aggressiveness", 2)),
            vad_min_rms=int(vad.get("min_rms", 150)),
            vad_start_speech_ms=int(vad.get("start_speech_ms", 160)),
            vad_end_silence_ms=int(vad.get("end_silence_ms", 350)),
            vad_min_speech_ms=int(vad.get("min_speech_ms", 300)),
            vad_pre_roll_ms=int(vad.get("pre_roll_ms", 300)),
            input_device=self.audio["input_device"],
            output_device=self.audio["output_device"],
            input_rate=int(self.audio["record_sample_rate"]),
            output_rate=int(self.audio["play_sample_rate"]),
            speaker=self.audio.get("speaker", "zh_female_xiaohe_jupiter_bigtts"),
            tts_system_role=self.audio.get(
                "tts_system_role",
                "你是一个亲切自然的家庭机器人语音助手。只朗读给定内容，不添加额外说明。",
            ),
            tts_speaking_style=self.audio.get(
                "tts_speaking_style",
                "使用自然口语语气，节奏轻松，停顿清楚，避免播音腔和逐字念稿感。",
            ),
            no_play=bool(self.audio.get("no_play", False)),
            asr_end_smooth_ms=int(self.audio.get("asr_end_smooth_ms", 300)),
            fast_tool_json=bool(self.voice_cfg.get("fast_tool_json", True)),
            stream_speech_after_chars=int(self.voice_cfg.get("stream_speech_after_chars", 24)),
            play_model_speech_stream=bool(self.voice_cfg.get("play_model_speech_stream", False)),
            debug_timing=bool(self.voice_cfg.get("debug_timing", self.audio.get("debug_timing", False))),
            audio_send_timeout=float(self.voice_cfg.get("audio_send_timeout_seconds", 1.5)),
            audio_buffer_max_bytes=int(self.voice_cfg.get("audio_buffer_max_bytes", 1_048_576)),
            connect_timeout=float(self.voice_cfg.get("connect_timeout_seconds", 6.0)),
            close_timeout=float(self.voice_cfg.get("close_timeout_seconds", 1.5)),
            detailed_timing=bool(self.voice_cfg.get("detailed_timing_log", True)),
            _turn_timing={
                "trace_id": trace_id,
                "turn_started_at": turn_started_at,
                "counters": {"mode": mode},
            },
        )

    def _system_prompt(self, mode: str, context: dict[str, Any]) -> str:
        if mode == "resume":
            return STRICT_JSON_PREFIX + RESUME_SCHEMA_PROMPT
        if mode == "followup":
            question = str(context.get("question") or "")
            payload = self._prompt_payload()
            prompt = (
                STRICT_JSON_PREFIX
                + NEW_PROJECT_SCHEMA_PROMPT
                + "\n"
                + FOLLOWUP_SCHEMA_PROMPT
                + f"\nRobot question: {question}\n"
                + "Existing dialogue context:\n"
                + json.dumps(context, ensure_ascii=False)
                + "\nAvailable robot capabilities:\n"
                + json.dumps(payload, ensure_ascii=False)
            )
            return self._trim_prompt(prompt)
        payload = self._prompt_payload()
        prompt = STRICT_JSON_PREFIX + self._command_schema_prompt() + "\n当前可用信息：\n" + json.dumps(payload, ensure_ascii=False)
        return self._trim_prompt(prompt)

    def _command_schema_prompt(self) -> str:
        try:
            compact_version = int(self.voice_cfg.get("compact_decision_version", 1))
        except (TypeError, ValueError):
            compact_version = 0
        if bool(self.voice_cfg.get("compact_decision_json", False)) and compact_version == 1:
            return COMPACT_COMMAND_SCHEMA_PROMPT
        return NEW_PROJECT_SCHEMA_PROMPT

    def _prompt_payload(self) -> dict[str, Any]:
        return {
            # Keep memory first: oversized skill specs are trimmed from the end
            # of the prompt, but user facts must remain available for reference
            # resolution and follow-up decisions.
            "user_memory": self.user_memory.compact_context(
                limit=int(self.config.get("memory", {}).get("prompt_context_items", 30))
            ),
            "skill_specs": self.registry.compact_specs_for_prompt(),
            "known_navigation_points": self._known_navigation_points(),
            "disabled_skills": sorted(self.registry.disabled_names()),
        }

    def _trim_prompt(self, prompt: str) -> str:
        max_chars = int(self.voice_cfg.get("system_prompt_max_chars", 12000))
        if len(prompt) > max_chars:
            prompt = prompt[: max_chars - 80] + "\n...技能列表已截断，但仍必须只使用已给出的技能名。"
        return prompt

    def _dialog_context(self) -> list[dict[str, str]]:
        return []

    async def _receive_decision(
        self,
        client: NewProjectDoubaoVoice,
        args: argparse.Namespace,
        send_task: asyncio.Task,
        mode: str,
        phase: str = "primary",
    ) -> dict[str, Any]:
        parse_response = self._load_modules()["parse_response"]
        prefix = f"{phase}."
        parts: list[str] = []
        final_texts: list[str] = []
        asr_final = ""
        asr_interim = ""
        got_response = False
        started_at = time.monotonic()
        input_deadline = started_at + args.listen_timeout + args.seconds + max(2.0, args.silence_tail_sec + 1.0)
        response_deadline = 0.0
        model_text_deadline = 0.0
        asr_final_deadline = 0.0
        after_input_deadline = 0.0
        json_fragment_deadline = 0.0
        audio_sender_done_marked = False
        self._timing_mark(args, f"{prefix}receive_started_at")

        def finish(decision: dict[str, Any]) -> dict[str, Any]:
            if self._decision_ready(decision, mode):
                self._timing_mark(args, f"{prefix}decision_ready_at", first=True)
            self._timing_mark(args, f"{prefix}receive_finished_at")
            self._timing_counter(args, f"{prefix}streamed_model_chars", sum(len(item) for item in parts))
            self._timing_counter(args, f"{prefix}final_model_chars", sum(len(item) for item in final_texts))
            self._timing_counter(args, f"{prefix}asr_final_chars", len(asr_final))
            self._timing_counter(args, f"{prefix}asr_interim_chars", len(asr_interim))
            return self._attach_authoritative_asr_text(decision, asr_final or asr_interim, mode)

        def parse_timed(raw_text: str) -> dict[str, Any]:
            self._timing_mark(args, f"{prefix}first_json_parse_started_at", first=True)
            parse_started_at = time.monotonic()
            parsed_decision = self._parse_decision(raw_text, mode)
            parse_elapsed_ms = (time.monotonic() - parse_started_at) * 1000.0
            self._timing_mark(args, f"{prefix}last_json_parse_finished_at")
            self._timing_counter(args, f"{prefix}json_parse_attempts", 1, add=True)
            self._timing_counter(args, f"{prefix}json_parse_cpu_ms", round(parse_elapsed_ms, 3), add=True)
            return parsed_decision

        async def stop_audio_sender() -> None:
            nonlocal audio_sender_done_marked
            if send_task.done():
                if not audio_sender_done_marked:
                    audio_sender_done_marked = True
                    self._timing_mark(args, f"{prefix}audio_sender_done_at")
                return
            send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task
            if not audio_sender_done_marked:
                audio_sender_done_marked = True
                self._timing_mark(args, f"{prefix}audio_sender_done_at")

        try:
            while True:
                now = time.monotonic()
                if asr_final_deadline and now >= asr_final_deadline:
                    await stop_audio_sender()
                    raw = self._best_model_text(parts, final_texts)
                    parsed = parse_timed(raw) if raw else self._empty_decision("asr_final_model_deadline", mode)
                    if self._is_incomplete_json_decision(parsed):
                        parsed = {
                            "ok": True,
                            "decision_type": "answer",
                            "reply": "",
                            "task_groups": [],
                            "ask_user": None,
                            "confidence": 0.0,
                            "recovered_from_incomplete_json": True,
                            "incomplete_model_text": raw[-500:],
                            "model_error": "asr_final_model_deadline",
                        }
                    parsed["asr_final_fallback_deadline_reached"] = True
                    return finish(parsed)
                if not send_task.done() and now > input_deadline:
                    send_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await send_task
                    if client.audio_frames:
                        raise RealtimeAudioTransportError("audio_capture_or_upload_timeout", client.audio_frames)
                    return finish(self._empty_decision("audio_capture_timeout", mode))
                if send_task.done():
                    if not audio_sender_done_marked:
                        audio_sender_done_marked = True
                        self._timing_mark(args, f"{prefix}audio_sender_done_at")
                        self._timing_counter(args, f"{prefix}uploaded_audio_frames", len(client.audio_frames))
                        self._timing_counter(args, f"{prefix}uploaded_audio_bytes", int(client.audio_bytes))
                    exc = send_task.exception()
                    if exc is not None:
                        raise exc
                    if client.audio_send_error:
                        raise RealtimeAudioTransportError(
                            f"audio_upload_failed: {client.audio_send_error}",
                            client.audio_frames,
                        )
                    if send_task.result() is False and not got_response:
                        return finish(self._empty_decision("no_valid_speech_detected", mode))
                    if not response_deadline:
                        response_deadline = time.monotonic() + args.first_response_timeout
                        model_text_deadline = time.monotonic() + min(
                            args.first_response_timeout,
                            max(args.decision_timeout_after_input, args.model_text_first_timeout),
                        )
                    raw = self._best_model_text(parts, final_texts)
                    if raw and not after_input_deadline:
                        after_input_deadline = time.monotonic() + args.decision_timeout_after_input
                    if raw and after_input_deadline and time.monotonic() > after_input_deadline:
                        if raw:
                            parsed = parse_timed(raw)
                            if not self._is_incomplete_json_decision(parsed):
                                return finish(parsed)
                            if not json_fragment_deadline:
                                json_fragment_deadline = time.monotonic() + max(
                                    3.0, min(6.0, args.decision_timeout_after_input * 2.0)
                                )
                            if time.monotonic() > min(response_deadline, json_fragment_deadline):
                                return finish(parsed)
                            after_input_deadline = time.monotonic() + max(1.0, args.decision_timeout_after_input)
                            continue
                    if not raw and model_text_deadline and time.monotonic() > model_text_deadline:
                        return finish(self._empty_decision("empty_model_text", mode))

                receive_timeout = 0.8
                deadline_candidates: list[float] = []
                if not send_task.done():
                    deadline_candidates.append(input_deadline)
                    if asr_final_deadline:
                        deadline_candidates.append(asr_final_deadline)
                else:
                    current_raw = self._best_model_text(parts, final_texts)
                    if current_raw and after_input_deadline:
                        deadline_candidates.append(after_input_deadline)
                    elif not current_raw and model_text_deadline:
                        deadline_candidates.append(model_text_deadline)
                    if response_deadline:
                        deadline_candidates.append(response_deadline)
                    if current_raw and json_fragment_deadline:
                        deadline_candidates.append(json_fragment_deadline)
                    if asr_final_deadline:
                        deadline_candidates.append(asr_final_deadline)
                if deadline_candidates:
                    remaining = min(deadline_candidates) - time.monotonic()
                    receive_timeout = max(0.02, min(receive_timeout, remaining))

                try:
                    msg = await asyncio.wait_for(client.ws.recv(), timeout=receive_timeout)
                except asyncio.TimeoutError:
                    if send_task.done() and response_deadline and time.monotonic() > response_deadline:
                        return finish(self._empty_decision("model_timeout", mode))
                    continue
                self._timing_mark(args, f"{prefix}first_websocket_message_at", first=True)
                self._timing_counter(args, f"{prefix}websocket_messages", 1, add=True)
                got_response = True
                response = parse_response(msg)
                body = response.get("body")
                if isinstance(body, bytes):
                    continue
                if response.get("type") == "error":
                    text = str(body)
                    if "DialogAudioIdleTimeoutError" in text:
                        return finish(self._empty_decision("model_audio_idle_timeout", mode))
                    return finish(self._empty_decision(f"doubao_realtime_error: {text}", mode))
                if response.get("event") == 451:
                    asr_text, is_interim = self._extract_asr_text(body)
                    if asr_text:
                        if is_interim:
                            self._timing_mark(args, f"{prefix}first_asr_interim_at", first=True)
                            self._timing_counter(args, f"{prefix}asr_interim_events", 1, add=True)
                            asr_interim = self._prefer_complete_transcript(asr_interim, asr_text)
                        else:
                            self._timing_mark(args, f"{prefix}asr_final_at", first=True)
                            self._timing_counter(args, f"{prefix}asr_final_events", 1, add=True)
                            asr_final = self._prefer_complete_transcript(asr_final, asr_text)
                            if mode == "resume":
                                await stop_audio_sender()
                                return finish(self._empty_decision("asr_transcript_ready", mode))
                            if mode == "command" and not asr_final_deadline:
                                asr_final_deadline = time.monotonic() + max(0.5, args.asr_command_fallback_timeout)
                    continue
                if response.get("event") == 459:
                    # Follow-up and resume turns only need the user's transcript.
                    # Returning here avoids waiting for a second model-generated JSON
                    # response and keeps TaskGroup state decisions deterministic.
                    if mode == "resume" and (asr_final or asr_interim):
                        return finish(self._empty_decision("asr_transcript_ready", mode))
                    continue
                text = self._extract_text(body)
                if response.get("event") == 550 and text:
                    self._timing_mark(args, f"{prefix}first_model_text_at", first=True)
                    self._timing_counter(args, f"{prefix}model_stream_events", 1, add=True)
                    self._timing_counter(args, f"{prefix}model_stream_chars", len(text), add=True)
                    parts.append(text)
                    after_input_deadline = time.monotonic() + args.decision_timeout_after_input
                elif response.get("event") == 351 and text:
                    self._timing_mark(args, f"{prefix}first_model_text_at", first=True)
                    self._timing_mark(args, f"{prefix}first_model_final_event_at", first=True)
                    self._timing_counter(args, f"{prefix}model_final_events", 1, add=True)
                    self._timing_counter(args, f"{prefix}model_final_chars", len(text), add=True)
                    final_texts.append(text)
                    after_input_deadline = time.monotonic() + args.decision_timeout_after_input
                current = self._best_model_text(parts, final_texts)
                if args.fast_tool_json and self._contains_json_signal(current):
                    self._timing_mark(args, f"{prefix}first_json_signal_at", first=True)
                    self._timing_counter(args, f"{prefix}json_boundary_checks", 1, add=True)
                    complete_json = self._extract_json_object(self._normalize_json_punctuation(current))
                    if complete_json:
                        self._timing_counter(args, f"{prefix}complete_json_boundaries", 1, add=True)
                        self._timing_mark(args, f"{prefix}first_complete_json_at", first=True)
                        parsed = parse_timed(complete_json)
                        if self._decision_ready(parsed, mode):
                            self._timing_mark(args, f"{prefix}decision_ready_at")
                            return finish(parsed)
                    else:
                        # Parsing/repairing every streamed fragment caused over one
                        # hundred JSON attempts on normal turns. Wait for the outer
                        # object to close; timeout/final-event paths below retain all
                        # legacy repair and salvage behavior for malformed output.
                        self._timing_counter(args, f"{prefix}incomplete_json_fragments_skipped", 1, add=True)
                if response.get("event") == 359:
                    self._timing_mark(args, f"{prefix}response_finished_event_at", first=True)
                    if send_task.done():
                        break
        finally:
            pass
        raw = self._best_model_text(parts, final_texts)
        parsed = parse_timed(raw)
        if self._decision_ready(parsed, mode):
            self._timing_mark(args, f"{prefix}decision_ready_at")
        return finish(parsed)

    def _best_model_text(self, streamed_parts: list[str], final_texts: list[str]) -> str:
        """Prefer a complete final event over an earlier streaming fragment."""
        streamed = "".join(streamed_parts).strip()
        final = "".join(final_texts).strip()
        for candidate in (final, streamed):
            if candidate and self._extract_json_object(self._normalize_json_punctuation(candidate)):
                return candidate
        return final if len(final) >= len(streamed) else streamed

    def _parse_decision(self, text: str, mode: str) -> dict[str, Any]:
        raw = self._strip_json_fence(text).strip()
        source_was_incomplete_json = bool(
            mode == "command"
            and self._looks_like_json_fragment(raw)
            and not self._extract_json_object(raw)
        )
        skill_error = self._invalid_skill_from_text(raw)
        if skill_error is not None:
            return skill_error
        normalized_raw = self._normalize_json_punctuation(raw)
        for candidate in (
            raw,
            normalized_raw,
            self._extract_json_object(raw),
            self._extract_json_object(normalized_raw),
            self._repair_json_decision(raw),
            self._repair_json_decision(normalized_raw),
            self._repair_task_groups_array_json(raw),
            self._repair_task_groups_array_json(normalized_raw),
            self._repair_incomplete_json(raw),
            self._repair_incomplete_json(normalized_raw),
        ):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                if mode == "resume":
                    if isinstance(parsed.get("command_decision"), dict):
                        parsed["command_decision"] = self._normalize_command_decision(parsed["command_decision"], text)
                    return parsed
                if mode == "followup":
                    return self._normalize_followup_decision(parsed, text)
                decision = self._normalize_command_decision(parsed, text)
                if source_was_incomplete_json:
                    decision["recovered_from_incomplete_json"] = True
                    decision["incomplete_model_text"] = raw[-500:]
                return decision
        salvaged = self._salvage_json_fragment_decision(raw, mode)
        if salvaged is not None:
            return salvaged
        if self._looks_like_json_fragment(raw) and not self._extract_json_object(raw):
            return self._incomplete_json_decision(raw, mode)
        if mode == "resume":
            return {"reply_type": "resume_confirmation", "text": text, "resume_action": "command"}
        if mode == "followup":
            return self._plain_followup_text_decision(raw)
        if self._contains_json_signal(raw):
            return {
                "ok": False,
                "error": "invalid_json_decision",
                "decision_type": "noop",
                "reply": "我没有听清楚，请再说一遍",
                "task_groups": [],
                "ask_user": None,
                "confidence": 0.0,
                "raw_text": raw[-500:],
            }
        salvaged_text = self._salvage_non_json_decision(raw, mode)
        if salvaged_text is not None:
            return salvaged_text
        return self._noop_decision("non_json_model_text", raw)

    def _salvage_non_json_decision(self, text: str, mode: str) -> dict[str, Any] | None:
        if mode != "command":
            return None
        raw = str(text or "").strip()
        if not raw:
            return None
        if self._looks_like_robot_prompt_echo(raw):
            return self._noop_decision("robot_prompt_echo", raw)
        # Natural-language model output is robot speech, not user evidence.
        # Never infer hardware skills from words inside that speech. The
        # authoritative event-451 transcript is attached later and the planner
        # grounds any executable plan against that user transcript.
        decision = self._answer_decision(raw, mode)
        decision["recovered_from_non_json_text"] = raw[-500:]
        decision["non_json_model_response"] = True
        return decision

    def _non_json_clarification_decision(self, raw: str) -> dict[str, Any] | None:
        if self._looks_like_fitness_natural_language_question(raw):
            return self._fitness_clarification_decision(raw)
        if self._looks_like_movement_natural_language_question(raw):
            question = "你想让我往前、往后、往左还是往右移动？"
            decision = {
                "decision_type": "ask_user",
                "reply": question,
                "task_groups": [
                    {
                        "title": "移动任务",
                        "user_instruction": "用户想让机器人移动，但方向还不明确",
                        "slots": {},
                        "followups": [],
                        "steps": [],
                    }
                ],
                "ask_user": {
                    "task_title": "移动任务",
                    "question": question,
                    "missing_slots": ["direction"],
                    "optional_slots": ["duration"],
                    "candidate_skills": ["move_forward", "move_backward", "move_left", "move_right"],
                },
                "confidence": 0.55,
                "user_text": "用户想让机器人移动，但方向还不明确",
                "recovered_from_non_json_text": raw[-500:],
            }
            return self._validate_decision(decision)
        if self._looks_like_generic_robot_question(raw):
            question = "你想让我执行哪个任务？"
            decision = {
                "decision_type": "ask_user",
                "reply": question,
                "task_groups": [
                    {
                        "title": "澄清指令",
                        "user_instruction": "用户指令不明确",
                        "slots": {},
                        "followups": [],
                        "steps": [],
                    }
                ],
                "ask_user": {
                    "task_title": "澄清指令",
                    "question": question,
                    "missing_slots": ["intent"],
                    "optional_slots": [],
                    "candidate_skills": self.registry.names(),
                },
                "confidence": 0.2,
                "user_text": "用户指令不明确",
                "recovered_from_non_json_text": raw[-500:],
            }
            return self._validate_decision(decision)
        return None

    def _assistant_intent_decision(self, raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        compact = re.sub(r"\s+", "", text)
        if not compact:
            return None
        if not self._looks_like_assistant_action_statement(compact):
            return None
        step_items: list[tuple[str, dict[str, Any]]] = []
        if self._assistant_statement_is_question(compact):
            return None

        pet_step = self._assistant_pet_intent_step(compact)
        if pet_step is not None:
            step_items.append(pet_step)
        else:
            move_steps = self._assistant_movement_intent_steps(compact)
            step_items.extend(move_steps)
            if any(token in compact for token in ("这是谁", "识别人脸", "人脸识别", "看看是谁", "看这是谁")):
                step_items.append(("人脸识别", {"skill_name": "face_recognition", "arguments": {}, "reason": "recovered from non-json assistant intent"}))
            elif any(token in compact for token in ("拍照", "拍一张", "照片", "抓拍")):
                step_items.append(("拍照", {"skill_name": "camera_capture", "arguments": {"camera_name": "front"}, "reason": "recovered from non-json assistant intent"}))
            elif "投影" in compact:
                if any(token in compact for token in ("关闭", "关掉", "关上", "关")):
                    step_items.append(("关闭投影", {"skill_name": "projector_control", "arguments": {"action": "off"}, "reason": "recovered from non-json assistant intent"}))
                elif any(token in compact for token in ("打开", "开启", "开")):
                    step_items.append(("打开投影", {"skill_name": "projector_control", "arguments": {"action": "internal_on"}, "reason": "recovered from non-json assistant intent"}))

        if not step_items:
            return None
        decision = {
            "decision_type": "task_plan",
            "reply": text,
            "task_groups": [
                {
                    "title": title,
                    "user_instruction": text,
                    "slots": {},
                    "followups": [],
                    "steps": [step],
                }
                for title, step in step_items
            ],
            "ask_user": None,
            "confidence": 0.45,
            "user_text": text,
            "recovered_from_non_json_text": text[-500:],
            "recovered_from_assistant_intent": True,
        }
        return self._validate_decision(decision)

    @staticmethod
    def _looks_like_assistant_action_statement(compact: str) -> bool:
        openers = (
            "好的我",
            "好的那我",
            "好我",
            "好那我",
            "我来",
            "我将",
            "我现在",
            "我马上",
            "我会",
            "开始",
        )
        return compact.startswith(openers) or compact.startswith("好的，") or compact.startswith("好，")

    @staticmethod
    def _assistant_statement_is_question(compact: str) -> bool:
        question_markers = ("？", "?", "请问你", "你想", "你希望", "需要我", "要不要", "是否需要", "吗", "呢")
        return any(marker in compact for marker in question_markers)

    def _assistant_pet_intent_step(self, compact: str) -> tuple[str, dict[str, Any]] | None:
        if not any(token in compact for token in ("狗", "小狗", "猫", "小猫", "宠物")):
            return None
        if not any(token in compact for token in ("找", "看看", "看一下", "在哪", "哪里", "跟随", "追踪")):
            return None
        pet = "all"
        title = "寻找宠物"
        if "狗" in compact:
            pet = "dog"
            title = "找小狗"
        elif "猫" in compact:
            pet = "cat"
            title = "找小猫"
        return (
            title,
            {
                "skill_name": "pet_tracking",
                "arguments": {"action": "find_route", "pet": pet, "search_strategy": "current_then_known_points", "track_after_found": True},
                "reason": "recovered from non-json assistant intent",
            },
        )

    def _assistant_movement_intent_step(self, compact: str) -> tuple[str, dict[str, Any]] | None:
        steps = self._assistant_movement_intent_steps(compact)
        return steps[0] if steps else None

    def _assistant_movement_intent_steps(self, compact: str) -> list[tuple[str, dict[str, Any]]]:
        matches: list[tuple[int, str, str]] = []
        direction_specs = [
            ("move_forward", "前进", ("往前", "向前", "前进", "朝前")),
            ("move_backward", "后退", ("往后", "向后", "后退", "倒退")),
            ("move_left", "左转", ("往左", "向左", "左转", "左移")),
            ("move_right", "右转", ("往右", "向右", "右转", "右移")),
        ]
        for skill, title, tokens in direction_specs:
            positions = [compact.find(token) for token in tokens if compact.find(token) >= 0]
            if positions:
                matches.append((min(positions), skill, title))
        if not matches:
            return []
        matches.sort(key=lambda item: item[0])
        result: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[int, str]] = set()
        for index, skill, title in matches:
            key = (index, skill)
            if key in seen:
                continue
            seen.add(key)
            arguments: dict[str, Any] = {}
            window = compact[index : index + 18]
            duration = self._duration_from_text(window)
            if duration is not None:
                arguments["duration"] = duration
                title = f"{title}{duration:g}秒"
            result.append((title, {"skill_name": skill, "arguments": arguments, "reason": "recovered from non-json assistant intent"}))
        return result

    def _assistant_movement_intent_step_legacy(self, compact: str) -> tuple[str, dict[str, Any]] | None:
        skill = ""
        title = ""
        if any(token in compact for token in ("往前", "向前", "前进", "朝前")):
            skill = "move_forward"
            title = "前进"
        elif any(token in compact for token in ("往后", "向后", "后退", "倒退")):
            skill = "move_backward"
            title = "后退"
        elif any(token in compact for token in ("往左", "向左", "左转", "左移")):
            skill = "move_left"
            title = "左转"
        elif any(token in compact for token in ("往右", "向右", "右转", "右移")):
            skill = "move_right"
            title = "右转"
        if not skill:
            return None
        arguments: dict[str, Any] = {}
        duration = self._duration_from_text(compact)
        if duration is not None:
            arguments["duration"] = duration
            title = f"{title}{duration:g}秒"
        return (title, {"skill_name": skill, "arguments": arguments, "reason": "recovered from non-json assistant intent"})

    def _fitness_clarification_decision(self, raw: str) -> dict[str, Any]:
        # `raw` is the assistant's clarification, not a user transcript. It often
        # lists all exercise choices; extracting the first option would silently
        # invent `squat`. Keep required slots unresolved until ASR event 451 or a
        # real follow-up answer supplies them.
        slots: dict[str, Any] = {"intent": "fitness"}
        missing_slots = ["exercise_type", "where"]
        title = "运动"
        user_instruction = "我想做运动"
        question = "你想做什么运动？深蹲、俯卧撑还是引体向上？另外，是在这里做还是去某个已保存地点做？需要打开投影吗？"
        decision = {
            "decision_type": "ask_user",
            "reply": question,
            "task_groups": [
                {
                    "title": title,
                    "user_instruction": user_instruction,
                    "slots": slots,
                    "followups": [],
                    "steps": [],
                }
            ],
            "ask_user": {
                "task_title": title,
                "question": question,
                "missing_slots": missing_slots,
                "optional_slots": ["projector_control"],
                "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
            },
            "confidence": 0.55,
            "user_text": user_instruction,
            "recovered_from_non_json_text": raw[-500:],
        }
        return self._validate_decision(decision)

    @staticmethod
    def _looks_like_fitness_natural_language_question(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        has_question = any(token in compact for token in ("？", "?", "吗", "呢", "要不要", "是否", "想做"))
        has_exercise_options = (
            ("深蹲" in compact and "俯卧撑" in compact)
            or ("深蹲" in compact and "引体向上" in compact)
            or ("俯卧撑" in compact and "引体向上" in compact)
            or "哪种运动" in compact
            or "什么运动" in compact
        )
        has_specific_exercise = any(token in compact for token in ("深蹲", "俯卧撑", "引体向上", "下蹲", "运动", "锻炼", "训练", "健身"))
        has_setup_question = any(token in compact for token in ("哪里", "在哪", "这里", "地点", "投影", "空间", "环境", "安全", "墙面", "辅助"))
        has_fitness_context = any(token in compact for token in ("运动", "锻炼", "训练", "健身", "运动空间", "投影仪辅助", "投影辅助"))
        return has_question and has_fitness_context and (has_exercise_options or (has_specific_exercise and has_setup_question))

    @staticmethod
    def _looks_like_movement_natural_language_question(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        has_question = any(token in compact for token in ("？", "?", "吗", "呢", "哪个方向", "哪边", "往哪"))
        has_move_context = any(token in compact for token in ("动一下", "移动", "方向", "往哪", "前后左右", "前", "后", "左", "右"))
        addressed_to_robot = any(token in compact for token in ("我", "机器人", "让", "要我"))
        return has_question and has_move_context and addressed_to_robot

    @staticmethod
    def _looks_like_generic_robot_question(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        has_question = any(token in compact for token in ("？", "?", "吗", "呢", "什么", "哪个", "哪里", "怎么"))
        if not has_question:
            return False
        return any(token in compact for token in ("你想让我", "需要我", "要我", "我可以", "请问你想", "你希望我"))

    @staticmethod
    def _exercise_type_from_text(text: str) -> str | None:
        if any(word in text for word in ("深蹲", "下蹲", "蹲下")):
            return "squat"
        if any(word in text for word in ("俯卧撑", "伏地挺身")):
            return "push_up"
        if any(word in text for word in ("引体向上", "引体")):
            return "pull_up"
        return None

    @staticmethod
    def _contains_here_or_known_where(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        return any(token in compact for token in ("这里", "这儿", "原地", "当前位置", "当前地点", "客厅", "白墙", "living_room", "wall"))

    def _salvage_json_fragment_decision(self, text: str, mode: str) -> dict[str, Any] | None:
        if mode != "command":
            return None
        if not self._contains_json_signal(text):
            return None
        skill_match = re.search(r'"skill_name"\s*:\s*"([^"]+)"', text)
        if not skill_match:
            return None
        raw_skill = skill_match.group(1).strip()
        arguments = self._salvage_step_arguments(text)
        user_text = self._salvage_json_string_field(text, "user_text") or self._salvage_json_string_field(text, "user_instruction")
        title = self._salvage_json_string_field(text, "title") or user_text or "语音任务"
        step = self._normalize_generated_step({"skill_name": raw_skill, "arguments": arguments, "reason": "recovered from incomplete realtime JSON"}, user_text or title)
        skill = str(step.get("skill_name") or "")
        ok, reason = self.registry.validate_step(skill)
        if not ok or skill in self.registry.disabled_names():
            return self._invalid_skill_decision(skill or raw_skill, reason)
        decision = {
            "decision_type": "task_plan",
            "reply": self._salvage_json_string_field(text, "reply") or "好的，我来处理。",
            "task_groups": [
                {
                    "title": title,
                    "user_instruction": user_text or title,
                    "slots": {},
                    "followups": [],
                    "steps": [step],
                }
            ],
            "ask_user": None,
            "confidence": 0.45,
            "user_text": user_text or title,
            "recovered_from_incomplete_json": True,
            "incomplete_model_text": text[-500:],
        }
        return self._validate_decision(decision)

    @staticmethod
    def _salvage_json_string_field(text: str, field: str) -> str:
        match = re.search(rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
        if not match:
            return ""
        try:
            return json.loads(f'"{match.group(1)}"')
        except Exception:
            return match.group(1)

    def _salvage_step_arguments(self, text: str) -> dict[str, Any]:
        match = re.search(r'"arguments"\s*:\s*(\{[^{}]*\})', text, re.S)
        if match:
            try:
                parsed = json.loads(self._normalize_json_punctuation(match.group(1)))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {}

    def _normalize_followup_decision(self, parsed: dict[str, Any], source_text: str) -> dict[str, Any]:
        if "decision_type" in parsed or "task_groups" in parsed or "ask_user" in parsed:
            decision = self._normalize_command_decision(parsed, source_text)
            decision["followup_text"] = (
                parsed.get("followup_text")
                or parsed.get("user_text")
                or parsed.get("text")
                or parsed.get("answer")
                or decision.get("user_text")
            )
            return decision
        text = str(parsed.get("text") or parsed.get("answer") or "")
        return self._plain_followup_text_decision(text, raw_decision=parsed)

    @staticmethod
    def _plain_followup_text_decision(text: str, raw_decision: dict[str, Any] | None = None) -> dict[str, Any]:
        followup_text = str(text or "").strip()
        return {
            "ok": bool(followup_text),
            "error": "" if followup_text else "empty_followup_text",
            "decision_type": "followup_text",
            "reply": "",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.2 if followup_text else 0.0,
            "user_text": followup_text,
            "followup_text": followup_text,
            "text": followup_text,
            "raw_decision": raw_decision if raw_decision is not None else {"raw": text},
        }

    def _normalize_command_decision(self, parsed: dict[str, Any], source_text: str) -> dict[str, Any]:
        if self._is_compact_command_decision(parsed):
            parsed = self._expand_compact_command_decision(parsed)
        if "decision_type" in parsed:
            decision = {
                "decision_type": parsed.get("decision_type") or "task_plan",
                "interaction_type": parsed.get("interaction_type") or "",
                "task_operation": parsed.get("task_operation") or "none",
                "slot_updates": parsed.get("slot_updates") if isinstance(parsed.get("slot_updates"), dict) else {},
                "slot_clears": parsed.get("slot_clears") if isinstance(parsed.get("slot_clears"), list) else [],
                "resume_dialogue_after_reply": bool(parsed.get("resume_dialogue_after_reply", False)),
                "actions": parsed.get("actions") if isinstance(parsed.get("actions"), list) else [],
                "memory_operation": parsed.get("memory_operation") or "none",
                "memory_fact": parsed.get("memory_fact") if isinstance(parsed.get("memory_fact"), dict) else None,
                "memory_fact_id": parsed.get("memory_fact_id"),
                "memory_query": parsed.get("memory_query") or "",
                "intent_analysis": parsed.get("intent_analysis") if isinstance(parsed.get("intent_analysis"), dict) else None,
                "reply": parsed.get("reply") or parsed.get("speech") or "",
                "task_groups": parsed.get("task_groups") or [],
                "ask_user": parsed.get("ask_user"),
                "confidence": float(parsed.get("confidence", 0.0) or 0.0),
                "user_text": parsed.get("user_text") or "",
            }
            for key in (
                "decision_wire_format",
                "compact_json_version",
                "compact_contract_error",
                "blocked_task_groups_from_compact_contract",
            ):
                if key in parsed:
                    decision[key] = parsed[key]
            return self._validate_decision(decision)
        mode = str(parsed.get("mode") or "")
        if mode == "tool_call":
            steps = []
            for task in parsed.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                skill = task.get("skill") or task.get("skill_name") or ""
                args = task.get("args") if isinstance(task.get("args"), dict) else {}
                action = task.get("action")
                arguments = dict(args)
                if action and "action" not in arguments:
                    arguments["action"] = action
                steps.append({"skill_name": skill, "arguments": arguments, "reason": "豆包实时语音决策"})
            return self._validate_decision(
                {
                    "decision_type": "task_plan",
                    "reply": parsed.get("speech") or parsed.get("reply") or "好的，我来处理。",
                    "task_groups": [
                        {
                            "title": parsed.get("title") or "语音任务",
                            "user_instruction": parsed.get("user_text") or source_text,
                            "slots": {},
                            "followups": [],
                            "steps": steps,
                        }
                    ],
                    "ask_user": None,
                    "confidence": 0.75,
                    "user_text": parsed.get("user_text") or source_text,
                }
            )
        if mode == "ask_user":
            question = str(parsed.get("speech") or parsed.get("question") or "")
            return {
                "decision_type": "ask_user",
                "reply": question,
                "task_groups": [],
                "ask_user": {"task_title": "需要补充信息", "question": question, "missing_slots": [], "optional_slots": [], "candidate_skills": []},
                "confidence": 0.7,
                "user_text": parsed.get("user_text") or source_text,
            }
        return self._answer_decision(str(parsed.get("speech") or parsed.get("reply") or source_text), "command")

    @staticmethod
    def _is_compact_command_decision(parsed: dict[str, Any]) -> bool:
        if "decision_type" in parsed:
            return False
        decision_type = str(parsed.get("type") or "").strip().lower()
        return bool(
            parsed.get("v") == 1
            or decision_type in {"tasks", "task", "task_plan", "ask", "ask_user", "answer", "reply", "noop"}
        )

    def _expand_compact_command_decision(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Expand compact wire JSON into the unchanged internal decision schema."""
        compact_type = str(parsed.get("type") or "").strip().lower()
        decision_type = {
            "tasks": "task_plan",
            "task": "task_plan",
            "task_plan": "task_plan",
            "ask": "ask_user",
            "ask_user": "ask_user",
            "answer": "answer",
            "reply": "answer",
            "noop": "noop",
        }.get(compact_type, "noop")
        user_text = str(parsed.get("text") or parsed.get("user_text") or "").strip()

        grouped: dict[str, dict[str, Any]] = {}
        for index, task in enumerate(parsed.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            skill = str(task.get("skill") or task.get("skill_name") or task.get("name") or "").strip()
            if not skill:
                continue
            group_key = str(task.get("group", 0))
            group = grouped.setdefault(
                group_key,
                {
                    "title": str(task.get("title") or parsed.get("title") or "语音任务"),
                    "user_instruction": str(task.get("instruction") or user_text),
                    "slots": dict(task.get("slots") or {}) if isinstance(task.get("slots"), dict) else {},
                    "followups": list(task.get("followups") or []) if isinstance(task.get("followups"), list) else [],
                    "steps": [],
                    "_first_index": index,
                },
            )
            raw_arguments = task.get("args") if isinstance(task.get("args"), dict) else task.get("arguments")
            arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
            action = task.get("action")
            if action is not None and "action" not in arguments:
                arguments["action"] = action
            raw_depends = task.get("depends") if isinstance(task.get("depends"), list) else task.get("depends_on_slots")
            depends_on_slots = raw_depends if isinstance(raw_depends, list) else []
            group["steps"].append(
                {
                    "skill_name": skill,
                    "arguments": arguments,
                    "depends_on_slots": [str(item) for item in depends_on_slots],
                    "reason": str(task.get("reason") or "豆包紧凑语音决策"),
                }
            )

        task_groups = sorted(grouped.values(), key=lambda item: int(item.pop("_first_index", 0)))
        raw_ask = parsed.get("ask")
        ask_user: dict[str, Any] | None = None
        if isinstance(raw_ask, str):
            raw_ask = {"question": raw_ask}
        if isinstance(raw_ask, dict):
            question = str(raw_ask.get("question") or parsed.get("reply") or "").strip()
            if question:
                raw_missing = raw_ask.get("missing") if isinstance(raw_ask.get("missing"), list) else raw_ask.get("missing_slots")
                raw_optional = raw_ask.get("optional") if isinstance(raw_ask.get("optional"), list) else raw_ask.get("optional_slots")
                raw_skills = raw_ask.get("skills") if isinstance(raw_ask.get("skills"), list) else raw_ask.get("candidate_skills")
                ask_user = {
                    "task_title": str(raw_ask.get("title") or parsed.get("title") or "需要补充信息"),
                    "question": question,
                    "missing_slots": [str(item) for item in raw_missing] if isinstance(raw_missing, list) else [],
                    "optional_slots": [str(item) for item in raw_optional] if isinstance(raw_optional, list) else [],
                    "candidate_skills": [str(item) for item in raw_skills] if isinstance(raw_skills, list) else [],
                }
                if not task_groups:
                    task_groups = [
                        {
                            "title": ask_user["task_title"],
                            "user_instruction": user_text,
                            "slots": dict(raw_ask.get("slots") or {}) if isinstance(raw_ask.get("slots"), dict) else {},
                            "followups": [],
                            "steps": [],
                        }
                    ]

        intent_analysis = self._expand_compact_intent(parsed.get("intent"), user_text)
        try:
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        expanded: dict[str, Any] = {
            "decision_type": decision_type,
            "interaction_type": parsed.get("interaction") or parsed.get("interaction_type") or (
                "command" if decision_type in {"task_plan", "ask_user"} else "conversation"
            ),
            "task_operation": parsed.get("operation") or parsed.get("task_operation") or "none",
            "slot_updates": (
                parsed.get("updates")
                if isinstance(parsed.get("updates"), dict)
                else parsed.get("slot_updates") if isinstance(parsed.get("slot_updates"), dict) else {}
            ),
            "slot_clears": (
                parsed.get("clears")
                if isinstance(parsed.get("clears"), list)
                else parsed.get("slot_clears") if isinstance(parsed.get("slot_clears"), list) else []
            ),
            "resume_dialogue_after_reply": bool(parsed.get("resume", parsed.get("resume_dialogue_after_reply", False))),
            "actions": parsed.get("actions") if isinstance(parsed.get("actions"), list) else [],
            "memory_operation": parsed.get("memory_op") or parsed.get("memory_operation") or "none",
            "memory_fact": (
                parsed.get("fact")
                if isinstance(parsed.get("fact"), dict)
                else parsed.get("memory_fact") if isinstance(parsed.get("memory_fact"), dict) else None
            ),
            "memory_fact_id": parsed.get("fact_id") or parsed.get("memory_fact_id"),
            "memory_query": str(parsed.get("query") or parsed.get("memory_query") or ""),
            "intent_analysis": intent_analysis,
            "reply": str(parsed.get("reply") or ""),
            "task_groups": task_groups,
            "ask_user": ask_user,
            "confidence": confidence,
            "user_text": user_text,
            "decision_wire_format": "compact_v1",
            "compact_json_version": 1,
        }
        if compact_type not in {"tasks", "task", "task_plan", "ask", "ask_user", "answer", "reply", "noop"}:
            expanded["compact_contract_error"] = "unknown_type"
        if decision_type == "ask_user" and ask_user is None:
            expanded.update(
                {
                    "decision_type": "noop",
                    "interaction_type": "ambiguous",
                    "reply": "",
                    "task_groups": [],
                    "compact_contract_error": "ask_without_question",
                }
            )
        if task_groups and any(group.get("steps") for group in task_groups):
            required_safety_fields = {"actionable", "authorization", "negated", "uncertain"}
            raw_intent = parsed.get("intent")
            missing = sorted(required_safety_fields - set(raw_intent)) if isinstance(raw_intent, dict) else sorted(required_safety_fields)
            malformed: list[str] = []
            if isinstance(raw_intent, dict):
                for field in ("actionable", "negated", "uncertain"):
                    if field in raw_intent and not isinstance(raw_intent.get(field), bool):
                        malformed.append(field)
                authorization = str(raw_intent.get("authorization") or "").strip().lower()
                if "authorization" in raw_intent and authorization not in {"explicit", "implied", "pragmatically_implied", "none"}:
                    malformed.append("authorization")
                speech_act = str(raw_intent.get("speech_act") or "").strip().lower()
                if (
                    speech_act == "implicit_request"
                    and raw_intent.get("actionable") is True
                    and authorization in {"implied", "pragmatically_implied"}
                    and not raw_intent.get("negated")
                    and not raw_intent.get("uncertain")
                ):
                    flat_steps = [
                        step
                        for group in task_groups
                        for step in group.get("steps") or []
                        if isinstance(step, dict)
                    ]
                    if len(flat_steps) != 1:
                        malformed.append("implicit_task_count")
                    else:
                        step = flat_steps[0]
                        intent_skill = str(raw_intent.get("skill") or raw_intent.get("target_skill") or "").strip()
                        intent_action = str(raw_intent.get("action") or raw_intent.get("target_action") or "").strip()
                        step_action = str((step.get("arguments") or {}).get("action") or "").strip()
                        if intent_skill != str(step.get("skill_name") or "").strip():
                            malformed.append("implicit_skill_mismatch")
                        if intent_action != step_action:
                            malformed.append("implicit_action_mismatch")
            if missing or malformed:
                expanded["blocked_task_groups_from_compact_contract"] = task_groups
                expanded["task_groups"] = []
                expanded["decision_type"] = "answer"
                expanded["interaction_type"] = "ambiguous"
                expanded["reply"] = ""
                details = []
                if missing:
                    details.append("missing=" + ",".join(missing))
                if malformed:
                    details.append("malformed=" + ",".join(sorted(set(malformed))))
                expanded["compact_contract_error"] = "intent_safety_contract:" + ";".join(details)
        return expanded

    @staticmethod
    def _expand_compact_intent(raw_intent: Any, user_text: str) -> dict[str, Any] | None:
        if not isinstance(raw_intent, dict):
            return None
        authorization = str(raw_intent.get("authorization") or "none").strip().lower()
        if authorization == "implied":
            authorization = "pragmatically_implied"
        try:
            confidence = float(raw_intent.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        raw_arguments = raw_intent.get("args") if isinstance(raw_intent.get("args"), dict) else raw_intent.get("arguments")
        return {
            "speech_act": raw_intent.get("speech_act") or "unclear",
            "literal_meaning": raw_intent.get("literal") or user_text,
            "implied_goal": raw_intent.get("goal") or "",
            "actionable": raw_intent.get("actionable"),
            "authorization": authorization,
            "negated": bool(raw_intent.get("negated", False)),
            "uncertain": bool(raw_intent.get("uncertain", False)),
            "target_skill": raw_intent.get("skill") or raw_intent.get("target_skill"),
            "target_action": raw_intent.get("action") or raw_intent.get("target_action"),
            "arguments": dict(raw_arguments) if isinstance(raw_arguments, dict) else {},
            "task_title": raw_intent.get("title") or "",
            "reason": raw_intent.get("reason") or "豆包紧凑语义分析",
            "confidence": confidence,
        }

    def _validate_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision = self._enforce_model_intent_safety(dict(decision))
        decision = self._materialize_model_intent_analysis(decision)
        disabled = self.registry.disabled_names()
        valid_groups = []
        dropped_arguments: list[dict[str, Any]] = []
        user_text = str(decision.get("user_text") or "")
        reply = str(decision.get("reply") or "")
        question = ""
        ask_user = decision.get("ask_user")
        if isinstance(ask_user, dict):
            question = str(ask_user.get("question") or "")
        if self._looks_like_robot_prompt_echo(user_text):
            return self._noop_decision("robot_prompt_echo", user_text or reply or question)
        for group in decision.get("task_groups") or []:
            if not isinstance(group, dict):
                continue
            group_text = " ".join(
                str(item)
                for item in (user_text, group.get("user_instruction"), group.get("title"))
                if item
            )
            steps = []
            for step in group.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                step = self._normalize_generated_step(step, group_text)
                skill = str(step.get("skill_name") or step.get("name") or "")
                ok, reason = self.registry.validate_step(skill)
                if not ok or skill in disabled:
                    return self._invalid_skill_decision(skill, reason)
                arguments, removed = self.registry.sanitize_model_arguments(skill, step.get("arguments") or {})
                if removed:
                    dropped_arguments.append({"skill_name": skill, "arguments": removed})
                steps.append(
                    {
                        "skill_name": skill,
                        "arguments": arguments,
                        "depends_on_slots": [str(item) for item in step.get("depends_on_slots") or []],
                        "reason": step.get("reason") or "豆包实时语音决策",
                    }
                )
            copied = dict(group)
            copied["steps"] = steps
            self._annotate_parallel_steps(copied["steps"])
            valid_groups.append(copied)
        if not user_text and not valid_groups and not isinstance(ask_user, dict) and self._looks_like_robot_prompt_echo(reply or question):
            return self._noop_decision("robot_prompt_echo", reply or question)
        decision["task_groups"] = valid_groups
        if dropped_arguments:
            decision["dropped_model_arguments"] = dropped_arguments
        if not decision.get("user_text") and valid_groups:
            decision["user_text"] = self._fallback_user_text_from_groups(valid_groups)
        # `reply` is robot output, never user intent evidence. A valid answer
        # must remain an answer; the planner may recover an executable command
        # later, but only from the authoritative ASR transcript.
        if not decision.get("task_groups") and not decision.get("ask_user") and decision.get("decision_type") == "task_plan":
            decision["decision_type"] = "answer"
        if decision.get("decision_type") == "noop":
            decision["reply"] = ""
            decision["task_groups"] = []
            decision["ask_user"] = None
        return decision

    @staticmethod
    def _enforce_model_intent_safety(decision: dict[str, Any]) -> dict[str, Any]:
        """Reject executable output that contradicts the model's own safety fields."""
        analysis = decision.get("intent_analysis")
        if not isinstance(analysis, dict):
            return decision
        authorization = str(analysis.get("authorization") or "").strip().lower()
        blocked = bool(
            analysis.get("negated")
            or analysis.get("uncertain")
            or analysis.get("actionable") is False
            or authorization == "none"
        )
        if not blocked or not decision.get("task_groups"):
            return decision
        decision["blocked_task_groups_from_model"] = decision.get("task_groups")
        decision["task_groups"] = []
        decision["ask_user"] = None
        decision["decision_type"] = "answer"
        if analysis.get("uncertain"):
            decision["interaction_type"] = "question"
        else:
            decision["interaction_type"] = "conversation"
        decision["intent_analysis_safety_blocked"] = True
        return decision

    def _materialize_model_intent_analysis(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Turn a model's semantic verdict into a validated task without text matching."""
        analysis = decision.get("intent_analysis")
        if not isinstance(analysis, dict):
            return decision
        ask_user = decision.get("ask_user")
        if isinstance(ask_user, dict):
            if decision.get("decision_wire_format") == "compact_v1":
                # Compact v1 explicitly chose `type=ask`; keep the turn
                # non-executable even if the model omitted the slot list.
                return decision
            unresolved = {
                str(item)
                for key in ("missing_slots", "optional_slots", "deferred_missing_slots", "deferred_optional_slots")
                for item in ask_user.get(key) or []
            }
            # A semantic target can repair a redundant "which action?" ask, but
            # it must never bypass genuinely required parameters such as a
            # destination, person, time, or exercise type.
            if unresolved and not unresolved.issubset({"action", "intent"}):
                return decision
        analysis = dict(analysis)
        decision["intent_analysis"] = analysis
        if not bool(analysis.get("actionable")) or bool(analysis.get("negated")) or bool(analysis.get("uncertain")):
            return decision
        authorization = str(analysis.get("authorization") or "").strip().lower()
        if authorization not in {"explicit", "pragmatically_implied"}:
            return decision
        try:
            confidence = float(analysis.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return decision
        threshold = float(self.voice_cfg.get("semantic_action_confidence_threshold", 0.78))
        if confidence < threshold:
            return decision

        skill_name = str(analysis.get("target_skill") or "").strip()
        action = str(analysis.get("target_action") or "").strip()
        ok, _ = self.registry.validate_step(skill_name)
        if not ok or skill_name in self.registry.disabled_names():
            return decision
        spec = self.registry.get(skill_name) or {}
        allowed_actions = {str(item) for item in spec.get("allowed_actions") or []}
        if not action or (allowed_actions and action not in allowed_actions):
            return decision

        arguments = dict(analysis.get("arguments") or {})
        arguments["action"] = action
        user_text = str(decision.get("user_text") or analysis.get("literal_meaning") or "").strip()
        matching_steps = [
            step
            for group in decision.get("task_groups") or []
            if isinstance(group, dict)
            for step in group.get("steps") or []
            if isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "") == skill_name
        ]
        all_steps = [
            step
            for group in decision.get("task_groups") or []
            if isinstance(group, dict)
            for step in group.get("steps") or []
            if isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "")
        ]
        if matching_steps and len(matching_steps) == len(all_steps):
            for step in matching_steps:
                merged = dict(step.get("arguments") or {})
                merged.update(arguments)
                step["arguments"] = merged
        elif not all_steps:
            decision["task_groups"] = [
                {
                    "title": str(analysis.get("task_title") or skill_name),
                    "user_instruction": user_text,
                    "slots": {"action": action},
                    "followups": [],
                    "steps": [
                        {
                            "skill_name": skill_name,
                            "arguments": arguments,
                            "depends_on_slots": [],
                            "reason": str(analysis.get("reason") or "大模型语义分析确认了用户的隐含执行意图"),
                        }
                    ],
                }
            ]
        else:
            return decision

        decision["ask_user"] = None
        decision["decision_type"] = "task_plan"
        decision["interaction_type"] = "command"
        decision["intent_analysis_materialized"] = True
        return decision

    @staticmethod
    def _fallback_user_text_from_groups(groups: list[dict[str, Any]]) -> str:
        for group in groups:
            if not isinstance(group, dict):
                continue
            for key in ("user_instruction", "title"):
                value = str(group.get(key) or "").strip()
                if value:
                    return value
        return ""

    def _normalize_generated_step(self, step: dict[str, Any], user_text: str) -> dict[str, Any]:
        copied = dict(step)
        raw_skill = str(copied.get("skill_name") or copied.get("name") or "").strip()
        arguments = dict(copied.get("arguments") or {})
        alias = self._movement_alias_skill(raw_skill, arguments, user_text)
        if alias:
            arguments.pop("direction", None)
            arguments.pop("skill", None)
            arguments.pop("skill_name", None)
            duration = self._coerce_duration_seconds(
                arguments.get("duration")
                or arguments.get("duration_seconds")
                or arguments.get("duration_sec")
                or arguments.get("seconds")
                or arguments.get("time")
            )
            if duration is None:
                duration_text = " ".join(
                    str(item)
                    for item in (
                        arguments.get("direction"),
                        arguments.get("action"),
                        arguments.get("mode"),
                        arguments.get("duration_text"),
                        user_text,
                    )
                    if item is not None
                )
                duration = self._duration_from_text(duration_text)
            if duration is not None:
                arguments["duration"] = duration
            copied["skill_name"] = alias
            copied["arguments"] = arguments
            copied["reason"] = copied.get("reason") or "用户要求短距离底盘移动"
        return copied

    def _movement_alias_skill(self, raw_skill: str, arguments: dict[str, Any], user_text: str) -> str:
        lowered = raw_skill.lower().strip()
        direction_text = " ".join(
            str(item)
            for item in (
                arguments.get("direction"),
                arguments.get("action"),
                arguments.get("mode"),
                user_text,
            )
            if item is not None
        ).lower()
        if lowered in {"move_forward", "move_backward", "move_left", "move_right"}:
            return lowered
        if lowered not in {"navigation_move", "base_move", "move", "cmd_vel", "robot_move", "chassis_move"}:
            return ""
        numeric_alias = self._movement_alias_from_numeric_args(arguments)
        if numeric_alias:
            return numeric_alias
        if any(token in direction_text for token in ("backward", "back", "后退", "向后", "往后")):
            return "move_backward"
        if any(token in direction_text for token in ("turn_left", "rotate_left", "left", "左移", "左转", "向左", "往左")):
            return "move_left"
        if any(token in direction_text for token in ("turn_right", "rotate_right", "right", "右移", "右转", "向右", "往右")):
            return "move_right"
        if any(token in direction_text for token in ("forward", "front", "ahead", "前进", "向前", "往前")):
            return "move_forward"
        return ""

    @staticmethod
    def _movement_alias_from_numeric_args(arguments: dict[str, Any]) -> str:
        for key in ("linear_x", "x", "vx", "speed_x"):
            try:
                value = float(arguments.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return "move_forward"
            if value < 0:
                return "move_backward"
        for key in ("angular_z", "yaw_rate", "wz"):
            try:
                value = float(arguments.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return "move_left"
            if value < 0:
                return "move_right"
        return ""

    @staticmethod
    def _coerce_duration_seconds(value: Any) -> float | None:
        if value is None:
            return None
        try:
            duration = float(value)
        except (TypeError, ValueError):
            text = str(value)
            match = re.search(r"(\d+(?:\.\d+)?)", text)
            if not match:
                return None
            duration = float(match.group(1))
        if duration <= 0:
            return None
        return min(duration, 30.0)

    @staticmethod
    def _duration_from_text(text: str) -> float | None:
        if not text:
            return None
        zh_digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|second)", text, re.IGNORECASE)
        if match:
            return min(float(match.group(1)), 30.0)
        match = re.search(r"([零一二两三四五六七八九十])\s*秒", text)
        if match:
            value = float(zh_digits.get(match.group(1), 0))
            return value or None
        return None

    @staticmethod
    def _invalid_skill_decision(skill: str, reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "invalid_skill",
            "invalid_skill": skill,
            "reason": reason,
            "decision_type": "noop",
            "reply": "这个动作暂时不可用，请换一种说法再试一次",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.0,
        }

    def _invalid_skill_from_text(self, text: str) -> dict[str, Any] | None:
        if not text or "unknown skill" not in text.lower():
            return None
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)", text)
        skill = match.group(1) if match else ""
        return self._invalid_skill_decision(skill, "unknown skill")

    def _annotate_parallel_steps(self, steps: list[dict[str, Any]]) -> None:
        group_index = 0
        index = 0
        while index < len(steps):
            window = []
            used_resources: set[str] = set()
            cursor = index
            while cursor < len(steps):
                step = steps[cursor]
                skill = str(step.get("skill_name") or "")
                resources = set(self.resources.resources_for_skill(skill, step.get("arguments") or {}))
                if not self._short_step_can_parallel(skill, resources):
                    break
                if used_resources & resources:
                    break
                window.append(step)
                used_resources.update(resources)
                cursor += 1
            if len(window) >= 2:
                group_index += 1
                group_name = f"doubao_short_{group_index}"
                for step in window:
                    arguments = dict(step.get("arguments") or {})
                    scheduler = dict(arguments.get("_scheduler") or {})
                    scheduler.update({"parallel_group": group_name, "can_parallel": True})
                    arguments["_scheduler"] = scheduler
                    step["arguments"] = arguments
                index = cursor
            else:
                index += 1

    def _short_step_can_parallel(self, skill_name: str, resources: set[str]) -> bool:
        if not resources:
            return True
        if resources & {"base", "navigation", "front_camera", "back_camera", "camera", "npu", "mic", "speaker"}:
            return False
        return skill_name in {"head_control", "projector_control", "reminder_schedule", "reminder_query", "reminder_cancel"}

    def _decision_ready(self, decision: dict[str, Any], mode: str) -> bool:
        if self._is_incomplete_json_decision(decision):
            return False
        if mode in {"resume", "followup"}:
            return bool(decision.get("ok", True))
        return bool(
            decision.get("ok", True)
            and (
                decision.get("ask_user")
                or decision.get("task_groups")
                or decision.get("decision_type") in {"answer", "noop"}
            )
        )

    def _empty_decision(self, error: str, mode: str) -> dict[str, Any]:
        if mode == "resume":
            return {"ok": False, "error": error, "text": ""}
        if mode == "followup":
            return {"ok": False, "error": error, "text": ""}
        return {"ok": False, "error": error, "decision_type": "noop", "reply": "我没有听清楚，请再说一遍", "task_groups": [], "ask_user": None, "confidence": 0.0}

    def _answer_decision(self, text: str, mode: str) -> dict[str, Any]:
        return {"decision_type": "answer", "reply": text, "task_groups": [], "ask_user": None, "confidence": 0.5, "user_text": text}

    def _noop_decision(self, reason: str, raw_text: str = "") -> dict[str, Any]:
        return {
            "decision_type": "noop",
            "reply": "",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.0,
            "user_text": "",
            "reason": reason,
            "raw_text": (raw_text or "")[-500:],
        }

    @staticmethod
    def _looks_like_robot_prompt_echo(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        if compact in {"我在", "我在。", "我在我在", "我没有听清楚，请再说一遍", "我没有听清楚请再说一遍"}:
            return True
        if compact.count("我在") >= 1 and len(compact) <= 6:
            return True
        if compact.startswith("我在") and len(compact) <= 14 and any(token in compact for token in ("什么事", "有什么事", "请说", "说吧")):
            return True
        if "unknownskill" in compact.lower() and "当前不可用" in compact:
            return True
        if "这个动作暂时不可用" in compact:
            return True
        return False

    @staticmethod
    def _looks_like_json_fragment(text: str) -> bool:
        stripped = (text or "").lstrip()
        return stripped.startswith("{") or stripped.startswith("[")

    @staticmethod
    def _is_incomplete_json_decision(decision: dict[str, Any]) -> bool:
        return bool(
            str(decision.get("error") or "") == "incomplete_json_decision"
            or decision.get("recovered_from_incomplete_json")
        )

    def _incomplete_json_decision(self, raw: str, mode: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": "incomplete_json_decision",
            "raw_text": (raw or "")[-500:],
        }
        if mode == "resume":
            payload.update({"text": "", "resume_action": "none"})
        elif mode == "followup":
            salvaged_text = (
                self._salvage_json_string_field(raw, "text")
                or self._salvage_json_string_field(raw, "answer")
                or self._salvage_json_string_field(raw, "user_text")
            )
            if salvaged_text:
                return self._plain_followup_text_decision(salvaged_text, raw_decision={"raw": raw})
            payload.update({"text": "", "task_groups": [], "ask_user": None})
        else:
            payload.update(
                {
                    "decision_type": "noop",
                    "reply": "我没有听清楚，请再说一遍",
                    "task_groups": [],
                    "ask_user": None,
                    "confidence": 0.0,
                }
            )
        return payload

    def _known_navigation_points(self) -> list[dict[str, Any]]:
        candidates = [
            self.config.get("robot_state", {}).get("navigation_points_path"),
            "/home/test/single_function/points/named_points.json",
        ]
        for item in candidates:
            if not item:
                continue
            path = Path(str(item))
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("points") or data.get("items") or []
        return []

    def _extract_text(self, body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        for key in ("content", "text"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _prefer_complete_transcript(previous: str, current: str) -> str:
        previous = str(previous or "").strip()
        current = str(current or "").strip()
        if not previous:
            return current
        if not current:
            return previous
        if previous in current or len(current) >= len(previous):
            return current
        return previous

    def _extract_asr_text(self, body: Any) -> tuple[str, bool]:
        if not isinstance(body, dict):
            return "", True
        results = body.get("results")
        if isinstance(results, list):
            best_text = ""
            best_is_interim = True
            for item in results:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("transcript") or "").strip()
                if not text:
                    continue
                is_interim = bool(item.get("is_interim", item.get("interim", False)))
                if not is_interim:
                    best_text = self._prefer_complete_transcript(best_text, text)
                    best_is_interim = False
                elif best_is_interim:
                    best_text = self._prefer_complete_transcript(best_text, text)
            return best_text, best_is_interim
        for key in ("text", "transcript", "utterance"):
            text = body.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip(), bool(body.get("is_interim", body.get("interim", False)))
        return "", True

    def _attach_authoritative_asr_text(self, decision: dict[str, Any], asr_text: str, mode: str) -> dict[str, Any]:
        transcript = str(asr_text or "").strip()
        if not transcript:
            return decision
        original = dict(decision or {})
        if mode == "followup":
            rich_followup = bool(
                original.get("interaction_type")
                or original.get("task_operation")
                or original.get("task_groups")
                or original.get("ask_user")
                or original.get("decision_type") in {"answer", "task_plan", "ask_user"}
            )
            if rich_followup:
                result = original
                result["user_text"] = transcript
                result["followup_text"] = transcript
                result["text"] = transcript
            else:
                result = self._plain_followup_text_decision(transcript, raw_decision=original)
        elif mode == "resume":
            result = {
                "ok": True,
                "error": "",
                "reply_type": "resume_confirmation",
                "text": transcript,
                # The orchestrator classifies this from the authoritative transcript.
                "resume_action": "",
            }
            if isinstance(original.get("command_decision"), dict):
                result["command_decision"] = original["command_decision"]
        else:
            result = original
            should_recover_from_asr = not result.get("ok", True) or (
                result.get("decision_type") == "noop" and not self._transcript_is_non_actionable(transcript)
            )
            if should_recover_from_asr:
                result = {
                    "ok": True,
                    "decision_type": "answer",
                    "reply": "",
                    "task_groups": [],
                    "ask_user": None,
                    "confidence": 0.0,
                    "reason": "asr_only_fallback",
                    "model_error": original.get("error"),
                }
                for key in (
                    "asr_final_fallback_deadline_reached",
                    "recovered_from_incomplete_json",
                    "incomplete_model_text",
                ):
                    if key in original:
                        result[key] = original[key]
            result["user_text"] = transcript
        result["asr_text"] = transcript
        result["authoritative_user_text"] = True
        result["asr_text_source"] = "doubao_event_451"
        return result

    @staticmethod
    def _transcript_is_non_actionable(text: str) -> bool:
        compact = re.sub(r"[\s，。,.？！!?、；;：:]+", "", str(text or ""))
        return compact in {
            "",
            "理想同学",
            "你好理想同学",
            "你好",
            "喂",
            "嗯",
            "啊",
            "哦",
        }

    def _contains_json_signal(self, text: str) -> bool:
        stripped = text.lstrip()
        return stripped.startswith("{") or bool(re.search(r"\{\s*\"(?:decision_type|mode|reply_type)\"\s*:", text))

    def _strip_json_fence(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        return stripped

    def _extract_json_object(self, text: str) -> str:
        start = text.find("{")
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text[start:], start=start):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return ""

    def _repair_incomplete_json(self, text: str) -> str:
        extracted = self._extract_json_object(text)
        if extracted:
            return extracted
        start = text.find("{")
        if start < 0:
            return ""
        candidate = text[start:].strip()
        opens = candidate.count("{") - candidate.count("}")
        brackets = candidate.count("[") - candidate.count("]")
        if brackets > 0:
            candidate += "]" * brackets
        if opens > 0:
            candidate += "}" * opens
        return candidate

    def _repair_task_groups_array_json(self, text: str) -> str:
        text = self._normalize_json_punctuation(text)
        start = text.find("{")
        if start < 0 or '"task_groups"' not in text or '"steps"' not in text:
            return ""
        candidate = text[start:].strip()
        if candidate.count("[") - candidate.count("]") != 1:
            return ""
        insert_at = self._find_top_level_field_after_task_group(candidate)
        if insert_at < 0:
            return ""
        repaired = candidate[:insert_at] + "]" + candidate[insert_at:]
        try:
            parsed = json.loads(repaired)
        except Exception:
            return ""
        if not isinstance(parsed, dict) or not isinstance(parsed.get("task_groups"), list):
            return ""
        return repaired

    def _repair_json_decision(self, text: str) -> str:
        text = self._normalize_json_punctuation(text)
        start = text.find("{")
        if start < 0:
            return ""
        candidate = text[start:].strip()
        extracted = self._extract_json_object(candidate)
        if extracted:
            candidate = extracted
        candidate = self._close_task_groups_before_top_level_fields(candidate)
        candidate = self._balance_json_suffix(candidate)
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            parsed = json.loads(candidate)
        except Exception:
            return ""
        return candidate if isinstance(parsed, dict) else ""

    @staticmethod
    def _normalize_json_punctuation(text: str) -> str:
        if not text or ("，" not in text and "：" not in text):
            return text
        output: list[str] = []
        in_string = False
        escape = False
        for char in text:
            if escape:
                output.append(char)
                escape = False
                continue
            if char == "\\":
                output.append(char)
                escape = True
                continue
            if char == '"':
                output.append(char)
                in_string = not in_string
                continue
            if not in_string and char == "，":
                output.append(",")
            elif not in_string and char == "：":
                output.append(":")
            else:
                output.append(char)
        return "".join(output)

    def _close_task_groups_before_top_level_fields(self, text: str) -> str:
        if '"task_groups"' not in text:
            return text
        output = text
        for field in ('"ask_user"', '"confidence"', '"user_text"', '"decision_type"', '"reply"'):
            index = self._find_top_level_field_start_after_unclosed_array(output, '"task_groups"', field)
            if index >= 0:
                output = output[:index] + "]" + output[index:]
                break
        return output

    @staticmethod
    def _balance_json_suffix(text: str) -> str:
        candidate = text.strip()
        curly = 0
        square = 0
        in_string = False
        escape = False
        for char in candidate:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                curly += 1
            elif char == "}":
                curly -= 1
            elif char == "[":
                square += 1
            elif char == "]":
                square -= 1
        if square > 0:
            candidate += "]" * square
        if curly > 0:
            candidate += "}" * curly
        return candidate

    @staticmethod
    def _find_top_level_field_start_after_unclosed_array(text: str, array_field: str, next_field: str) -> int:
        field_start = text.find(array_field)
        if field_start < 0:
            return -1
        array_start = text.find("[", field_start)
        if array_start < 0:
            return -1
        depth_curly = 0
        depth_square = 0
        in_string = False
        escape = False
        index = array_start
        while index < len(text):
            char = text[index]
            if escape:
                escape = False
                index += 1
                continue
            if char == "\\":
                escape = True
                index += 1
                continue
            if char == '"':
                in_string = not in_string
                index += 1
                continue
            if in_string:
                index += 1
                continue
            if char == "{":
                depth_curly += 1
            elif char == "}":
                depth_curly -= 1
            elif char == "[":
                depth_square += 1
            elif char == "]":
                depth_square -= 1
            elif char == "," and depth_curly == 0 and depth_square == 1:
                probe = text[index + 1 :].lstrip()
                if probe.startswith(next_field):
                    return index
            index += 1
        return -1

    @staticmethod
    def _find_top_level_field_after_task_group(text: str) -> int:
        start = text.find('"task_groups"')
        if start < 0:
            return -1
        array_start = text.find("[", start)
        if array_start < 0:
            return -1
        depth_curly = 0
        depth_square = 0
        in_string = False
        escape = False
        index = array_start
        while index < len(text):
            char = text[index]
            if escape:
                escape = False
                index += 1
                continue
            if char == "\\":
                escape = True
                index += 1
                continue
            if char == '"':
                in_string = not in_string
                index += 1
                continue
            if in_string:
                index += 1
                continue
            if char == "{":
                depth_curly += 1
            elif char == "}":
                depth_curly -= 1
            elif char == "[":
                depth_square += 1
            elif char == "]":
                depth_square -= 1
            elif char == "," and depth_curly == 0 and depth_square == 1:
                probe = text[index + 1 : index + 80].lstrip()
                if probe.startswith(('"confidence"', '"user_text"', '"ask_user"', '"decision_type"', '"reply"')):
                    return index
            index += 1
        return -1
