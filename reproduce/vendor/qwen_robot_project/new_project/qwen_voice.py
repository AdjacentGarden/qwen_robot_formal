from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .doubao_realtime import DoubaoRealtimeSession
from .local_tts import LocalMatchaTTS
from .persistent_asr import PersistentZipformerASR
from .planner import Planner


QWEN_PERSONA_AND_ROUTING = """
You are the semantic decision core of a real household robot, not a chat-only
assistant. Call the supplied submit tool exactly once. Put the JSON object that
the system schema requests in the tool arguments; do not emit prose outside the
tool call.

Understand intent from the whole utterance and its pragmatic meaning. An
indirect but clear real-world need can authorize the single safe action that
naturally resolves it; do not depend on phrase or keyword matching. Preserve
negation, uncertainty, questions, hypotheticals, and quoted speech. Never turn
those into hardware actions. If ASR appears to have clipped one leading
syllable, recover the intended meaning only when the remaining utterance and
available capabilities make it unambiguous; otherwise ask one concise question.

For task plans, omit ask and set reply to an empty string: execution results
own the spoken response, so never say generic acknowledgements such as 收到,
好的, 正在处理, or 马上. ask is exclusively for type=ask when genuinely
required information is missing; it is forbidden for type=tasks. For conversation,
clarification, and errors, reply in concise, warm, everyday Chinese. Sound like
a considerate household companion: specific, natural, and grounded, with at
most two short sentences. Do not mention JSON, models, prompts, tools, skills,
ASR, APIs, internal state, or implementation details. Never claim that a device
action or realtime lookup succeeded before its actual result exists.

Realtime time, weather, location, nearby-place, and traffic questions are
executable information-retrieval tasks even though their speech act is a
question. For them, use type=tasks, speech_act=question, actionable=true,
authorization=explicit, and realtime_information. Put the user's original
query in args.query. When no location is spoken, leave location absent so the
configured home location is used. This includes current or forecast weather
questions; never answer with "让我查一下" without creating that task.

A question asking where a visible household target such as the dog is now is a
request for the robot to locate it, so use pet_tracking rather than promising
to search without a task. A clipped ASR fragment whose only plausible recovery
is a realtime query may be reconstructed semantically; for example, 气怎么样
after wake-up should be treated as a clipped 天气怎么样, not as casual chat.

Distinguish prohibition from an opposite command. 别开灯 means do not turn the
light on and authorizes no device action; 关灯 explicitly authorizes off. A
hypothetical or conditional instruction that the runtime cannot evaluate must
not execute unconditionally: use speech_act=hypothetical, actionable=false,
authorization=none, uncertain=true, and no tasks. /no_think
""".strip()


def _command_tool() -> dict[str, Any]:
    task_item = {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "args": {"type": "object"},
            "action": {"type": ["string", "null"]},
            "group": {"type": ["string", "integer"]},
            "title": {"type": "string"},
            "instruction": {"type": "string"},
            "slots": {"type": "object"},
            "followups": {"type": "array"},
            "depends": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["skill", "args", "group", "reason"],
    }
    intent = {
        "type": "object",
        "properties": {
            "speech_act": {
                "type": "string",
                "enum": [
                    "explicit_command",
                    "implicit_request",
                    "question",
                    "hypothetical",
                    "conversation",
                    "social",
                    "unclear",
                ],
            },
            "actionable": {"type": "boolean"},
            "authorization": {
                "type": "string",
                "enum": ["explicit", "implied", "pragmatically_implied", "none"],
            },
            "negated": {"type": "boolean"},
            "uncertain": {"type": "boolean"},
            "skill": {"type": ["string", "null"]},
            "action": {"type": ["string", "null"]},
            "args": {"type": "object"},
            "literal": {"type": "string"},
            "goal": {"type": "string"},
            "title": {"type": "string"},
            "reason": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["speech_act", "actionable", "authorization", "negated", "uncertain"],
    }
    ask = {
        "type": ["object", "null"],
        "properties": {
            "title": {"type": "string"},
            "question": {"type": "string"},
            "missing": {"type": "array", "items": {"type": "string"}},
            "optional": {"type": "array", "items": {"type": "string"}},
            "skills": {"type": "array", "items": {"type": "string"}},
            "slots": {"type": "object"},
        },
    }
    return {
        "type": "function",
        "function": {
            "name": "submit_robot_decision",
            "description": "Submit one complete compact household-robot decision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["tasks", "ask", "answer", "noop"],
                    },
                    "text": {"type": "string"},
                    "reply": {"type": "string"},
                    "tasks": {"type": "array", "items": task_item},
                    "ask": ask,
                    "intent": intent,
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "interaction": {"type": "string"},
                    "operation": {"type": "string"},
                    "updates": {"type": "object"},
                    "clears": {"type": "array", "items": {"type": "string"}},
                    "resume": {"type": "boolean"},
                    "actions": {"type": "array"},
                    "memory_op": {"type": "string"},
                    "fact": {"type": ["object", "null"]},
                    "fact_id": {"type": ["string", "null"]},
                    "query": {"type": "string"},
                },
                "required": ["type", "text", "intent", "confidence"],
            },
        },
    }


def _followup_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_followup_decision",
            "description": "Submit the user's answer or operation for the pending robot task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "user_text": {"type": "string"},
                    "decision_type": {"type": "string"},
                    "interaction_type": {"type": "string"},
                    "task_operation": {"type": "string"},
                    "slot_updates": {"type": "object"},
                    "slot_clears": {"type": "array", "items": {"type": "string"}},
                    "resume_dialogue_after_reply": {"type": "boolean"},
                    "reply": {"type": "string"},
                    "task_groups": {"type": "array"},
                    "ask_user": {"type": ["object", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["text", "task_operation", "slot_updates", "slot_clears"],
            },
        },
    }


def _resume_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_resume_decision",
            "description": "Submit whether to resume, cancel, or replace an interrupted task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reply_type": {"type": "string"},
                    "text": {"type": "string"},
                    "resume_action": {"type": "string"},
                    "command_decision": {"type": ["object", "null"]},
                },
                "required": ["reply_type", "text", "resume_action"],
            },
        },
    }


class QwenVoiceSession(DoubaoRealtimeSession):
    """Local VAD/ASR + ModelScope Qwen tool calling + local offline TTS.

    ModelScope currently exposes no hosted Qwen Omni/audio provider for the
    supplied token.  This backend therefore keeps the audio edge local and uses
    the lowest-latency available Qwen endpoint for semantic decisions.  The
    class preserves the original realtime-session interface so a true Qwen
    Realtime transport can replace it later without touching orchestration.
    """

    def __init__(self, config: dict[str, Any], registry: Any, resources: Any):
        super().__init__(config, registry, resources)
        qwen = self.voice_cfg.get("qwen", {}) if isinstance(self.voice_cfg.get("qwen"), dict) else {}
        self.backend_name = "qwen_modelscope_hybrid"
        self.base_url = str(qwen.get("base_url", "https://api-inference.modelscope.cn/v1")).rstrip("/")
        self.model = str(qwen.get("model", "Qwen/Qwen3.5-35B-A3B"))
        self.fallback_models = [
            str(item)
            for item in qwen.get("fallback_models", [])
            if str(item).strip() and str(item).strip() != self.model
        ]
        self.secret_env_file = Path(
            qwen.get("secret_env_file", "/home/test/.config/qwen_robot/modelscope.env")
        )
        self.request_connect_timeout = float(qwen.get("connect_timeout_seconds", 8.0))
        self.request_read_timeout = float(qwen.get("read_timeout_seconds", 30.0))
        self.max_tokens = int(qwen.get("max_decision_tokens", 1000))
        self.temperature = float(qwen.get("temperature", 0.1))
        self.top_p = float(qwen.get("top_p", 0.8))
        self.early_return_on_valid_tool_args = bool(qwen.get("early_return_on_valid_tool_args", True))
        self.api_key = self._load_api_key()
        self._http: Any | None = None
        self._http_lock = threading.RLock()
        self._api_warmup_result: dict[str, Any] | None = None
        self._recorder_modules: dict[str, Any] | None = None
        self._local_planner = Planner(config, registry)
        self.asr = PersistentZipformerASR(config, resources)
        self.tts = LocalMatchaTTS(config, resources)
        # Start heavyweight local preparation immediately.  cmd_daemon may
        # choose to wait for it, but construction itself remains non-blocking.
        self.asr.start_warmup()
        self.tts.start_warmup()

    def warmup(self) -> dict[str, Any]:
        started = time.monotonic()
        asr_result = self.asr.warmup()
        tts_result = self.tts.warmup()
        if self._api_warmup_result is None:
            self._api_warmup_result = self._warmup_api()
        api_result = dict(self._api_warmup_result)
        return {
            "ok": bool(asr_result.get("ok") and tts_result.get("ok") and api_result.get("ok")),
            "backend": self.backend_name,
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "asr": asr_result,
            "tts": tts_result,
            "modelscope": api_result,
        }

    def prepare_once(self, mode: str = "command", context: dict[str, Any] | None = None) -> dict[str, Any]:
        asr_result = self.asr.warmup()
        tts_result = self.tts.warmup()
        ok = bool(asr_result.get("ok") and tts_result.get("ok") and self.api_key)
        return {
            "ok": ok,
            "backend": self.backend_name,
            "mode": mode,
            "prepared": ok,
            "asr": asr_result,
            "tts": tts_result,
            "modelscope_session_ready": bool(self.api_key),
        }

    def decide_once(
        self,
        seconds: float | None = None,
        mode: str = "command",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._thread_lock:
            args = self._voice_args(seconds=seconds, mode=mode)
            self._timing_counter(args, "backend", self.backend_name)
            try:
                self._timing_mark(args, "primary.modules_load_started_at")
                modules = self._load_recorder_modules()
                self._timing_mark(args, "primary.modules_loaded_at")
                self._timing_mark(args, "primary.mic_wait_started_at")
                with self.resources.acquire(["mic"]):
                    self._timing_mark(args, "primary.mic_acquired_at")
                    self._timing_mark(args, "primary.recording_started_at")
                    pcm = asyncio.run(modules["record_one_turn_pcm"](args))
                    self._timing_mark(args, "primary.recording_finished_at")
                if not pcm:
                    return self._attach_timing(
                        self._empty_decision("no_valid_speech_detected", mode),
                        args,
                    )
                self._timing_counter(args, "primary.captured_audio_bytes", len(pcm))
                self._timing_counter(args, "primary.captured_audio_frames", max(1, len(pcm) // 640))
                wav_path = Path(self.audio.get("command_wav_path", ""))
                if wav_path:
                    modules["write_pcm_wav"](wav_path, pcm, int(args.input_rate))
                self._timing_mark(args, "primary.asr_started_at")
                transcript, asr_meta = self.asr.transcribe_pcm(pcm, sample_rate=int(args.input_rate))
                self._timing_mark(args, "primary.asr_finished_at")
                self._timing_counter(args, "primary.asr_final_chars", len(transcript))
                self._timing_counter(args, "primary.asr_model_reused", int(bool(asr_meta.get("model_reused"))))
                if not transcript:
                    return self._attach_timing(self._empty_decision("asr_empty", mode), args)
                self._timing_mark(args, "primary.prompt_build_started_at")
                prompt = self._system_prompt(mode=mode, context=context or {})
                prompt = prompt + "\n\n" + QWEN_PERSONA_AND_ROUTING
                self._timing_mark(args, "primary.prompt_ready_at")
                self._timing_counter(args, "primary.prompt_chars", len(prompt))
                decision = self._request_decision(transcript, prompt, mode, args)
                decision = dict(decision or {})
                decision["user_text"] = transcript
                decision["asr_text"] = transcript
                decision["authoritative_user_text"] = True
                decision["asr_text_source"] = "persistent_zipformer_rknn"
                decision["decision_backend"] = self.backend_name
                decision["asr_metadata"] = asr_meta
                return self._attach_timing(decision, args)
            except Exception as exc:
                fallback_text = ""
                try:
                    fallback_text = str(locals().get("transcript") or "")
                    if fallback_text:
                        planned = self._local_planner.plan(fallback_text, history=[], session_context={})
                        planned = dict(planned or {})
                        planned.update(
                            {
                                "ok": True,
                                "user_text": fallback_text,
                                "asr_text": fallback_text,
                                "authoritative_user_text": True,
                                "asr_text_source": "persistent_zipformer_rknn",
                                "decision_backend": "local_planner_outage_fallback",
                                "model_error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        return self._attach_timing(planned, args)
                except Exception:
                    pass
                return self._attach_timing(
                    self._empty_decision(f"qwen_voice_error: {type(exc).__name__}: {exc}", mode),
                    args,
                )

    def decide_text(
        self,
        text: str,
        mode: str = "command",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Diagnostic entry point that exercises Qwen without microphone or ASR."""
        args = self._voice_args(mode=mode)
        self._timing_mark(args, "primary.prompt_build_started_at")
        prompt = self._system_prompt(mode=mode, context=context or {}) + "\n\n" + QWEN_PERSONA_AND_ROUTING
        self._timing_mark(args, "primary.prompt_ready_at")
        self._timing_counter(args, "primary.prompt_chars", len(prompt))
        decision = self._request_decision(str(text).strip(), prompt, mode, args)
        decision = dict(decision or {})
        decision.update(
            {
                "user_text": str(text).strip(),
                "asr_text": str(text).strip(),
                "authoritative_user_text": True,
                "asr_text_source": "diagnostic_text",
                "decision_backend": self.backend_name,
            }
        )
        return self._attach_timing(decision, args)

    def speak_text(self, text: str) -> bool:
        ok, metadata = self.tts.speak(text)
        if bool(self.voice_cfg.get("detailed_timing_log", True)):
            print("VOICE_TTS_TIMING:" + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), flush=True)
        return bool(ok)

    def close(self) -> None:
        self.asr.close()
        with self._http_lock:
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
                self._http = None

    def _request_decision(
        self,
        transcript: str,
        prompt: str,
        mode: str,
        args: Any,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("MODELSCOPE_API_KEY is not configured")
        tool = _resume_tool() if mode == "resume" else _followup_tool() if mode == "followup" else _command_tool()
        expected_name = str(tool["function"]["name"])
        last_error = ""
        for model_index, model in enumerate([self.model, *self.fallback_models]):
            self._timing_counter(args, "primary.model_attempt", model_index + 1)
            self._timing_counter(args, "primary.model_name_chars", len(model))
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": transcript},
                ],
                "tools": [tool],
                "tool_choice": "auto",
                "stream": True,
                "enable_thinking": False,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "max_tokens": self.max_tokens,
            }
            try:
                self._timing_mark(args, "primary.model_request_started_at")
                parsed, raw_meta = self._stream_tool_call(payload, expected_name, args)
                self._timing_mark(args, "primary.model_request_finished_at")
                self._timing_counter(args, "primary.model_stream_events", raw_meta.get("events", 0))
                self._timing_counter(args, "primary.model_reasoning_chars", raw_meta.get("reasoning_chars", 0))
                self._timing_counter(args, "primary.model_content_chars", raw_meta.get("content_chars", 0))
                self._timing_counter(args, "primary.tool_argument_chars", raw_meta.get("argument_chars", 0))
                if isinstance(parsed, dict):
                    decision = self._parse_decision(json.dumps(parsed, ensure_ascii=False), mode)
                    decision["model"] = model
                    decision["modelscope_stream"] = raw_meta
                    return decision
                content = str(raw_meta.get("content") or "").strip()
                if content:
                    decision = self._parse_decision(content, mode)
                    decision["model"] = model
                    decision["modelscope_stream"] = raw_meta
                    return decision
                last_error = str(raw_meta.get("error") or "missing_tool_call")
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(last_error or "ModelScope Qwen returned no decision")

    def _stream_tool_call(
        self,
        payload: dict[str, Any],
        expected_name: str,
        args: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        session = self._http_session()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        response = session.post(
            self.base_url + "/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=(self.request_connect_timeout, self.request_read_timeout),
        )
        events = 0
        reasoning_chars = 0
        content_parts: list[str] = []
        tool_names: dict[int, str] = {}
        tool_arguments: dict[int, str] = {}
        parsed_arguments: dict[str, Any] | None = None
        error = ""
        try:
            if not response.ok:
                raise RuntimeError(f"ModelScope HTTP {response.status_code}: {response.text[:500]}")
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                events += 1
                self._timing_mark(args, "primary.first_model_event_at", first=True)
                line = raw_line.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                for choice in choices:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str):
                        reasoning_chars += len(reasoning)
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        content_parts.append(content)
                        self._timing_mark(args, "primary.first_model_content_at", first=True)
                    for tool_call in delta.get("tool_calls") or []:
                        index = int(tool_call.get("index", 0) or 0)
                        function = tool_call.get("function") or {}
                        name_piece = function.get("name")
                        if isinstance(name_piece, str) and name_piece:
                            tool_names[index] = tool_names.get(index, "") + name_piece
                            self._timing_mark(args, "primary.first_tool_name_at", first=True)
                        argument_piece = function.get("arguments")
                        if isinstance(argument_piece, str) and argument_piece:
                            tool_arguments[index] = tool_arguments.get(index, "") + argument_piece
                            candidate_name = tool_names.get(index, "")
                            if candidate_name == expected_name:
                                try:
                                    candidate = json.loads(tool_arguments[index])
                                except json.JSONDecodeError:
                                    candidate = None
                                if isinstance(candidate, dict):
                                    parsed_arguments = candidate
                                    self._timing_mark(args, "primary.tool_arguments_ready_at", first=True)
                                    if self.early_return_on_valid_tool_args:
                                        break
                    if parsed_arguments is not None and self.early_return_on_valid_tool_args:
                        break
                if parsed_arguments is not None and self.early_return_on_valid_tool_args:
                    break
        finally:
            response.close()
        if parsed_arguments is None:
            for index, value in tool_arguments.items():
                if tool_names.get(index) != expected_name:
                    continue
                try:
                    candidate = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    parsed_arguments = candidate
                    break
        content = "".join(content_parts)
        return parsed_arguments, {
            "events": events,
            "reasoning_chars": reasoning_chars,
            "content_chars": len(content),
            "argument_chars": sum(len(value) for value in tool_arguments.values()),
            "tool_names": list(tool_names.values()),
            "content": content[-1000:],
            "error": error,
            "early_return": bool(parsed_arguments is not None and self.early_return_on_valid_tool_args),
        }

    def _warmup_api(self) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "error": "MODELSCOPE_API_KEY is not configured"}
        started = time.monotonic()
        try:
            response = self._http_session().get(
                self.base_url + "/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=(self.request_connect_timeout, self.request_read_timeout),
            )
            if not response.ok:
                return {
                    "ok": False,
                    "status": response.status_code,
                    "error": response.text[:300],
                    "elapsed_seconds": round(time.monotonic() - started, 4),
                }
            data = response.json()
            available = {
                str(item.get("id") or "")
                for item in data.get("data", [])
                if isinstance(item, dict)
            }
            return {
                "ok": self.model in available,
                "model": self.model,
                "model_available": self.model in available,
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }

    def _http_session(self) -> Any:
        with self._http_lock:
            if self._http is None:
                import requests

                session = requests.Session()
                session.headers.update({"User-Agent": "qwen-household-robot/1.0"})
                self._http = session
            return self._http

    def _load_recorder_modules(self) -> dict[str, Any]:
        if self._recorder_modules is not None:
            return self._recorder_modules
        self_program_dir = Path(self.config["paths"]["self_program_dir"])
        if str(self_program_dir) not in sys.path:
            sys.path.insert(0, str(self_program_dir))
        from vocal_stream_llm import record_one_turn_pcm, write_pcm_wav

        self._recorder_modules = {
            "record_one_turn_pcm": record_one_turn_pcm,
            "write_pcm_wav": write_pcm_wav,
        }
        return self._recorder_modules

    def _load_api_key(self) -> str:
        existing = str(os.environ.get("MODELSCOPE_API_KEY") or os.environ.get("MODELSCOPE_SDK_TOKEN") or "").strip()
        if existing:
            return existing
        try:
            for raw_line in self.secret_env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in {"MODELSCOPE_API_KEY", "MODELSCOPE_SDK_TOKEN"}:
                    return value.strip().strip("\"").strip("'")
        except FileNotFoundError:
            return ""
        return ""

    def _timing_report(self, args: Any) -> dict[str, Any]:
        report = super()._timing_report(args)
        timing = getattr(args, "_turn_timing", {})
        pairs = {
            "qwen_recording": ("primary.recording_started_at", "primary.recording_finished_at"),
            "qwen_asr": ("primary.asr_started_at", "primary.asr_finished_at"),
            "qwen_model_request": ("primary.model_request_started_at", "primary.model_request_finished_at"),
            "qwen_request_to_first_event": ("primary.model_request_started_at", "primary.first_model_event_at"),
            "qwen_request_to_tool_name": ("primary.model_request_started_at", "primary.first_tool_name_at"),
            "qwen_request_to_valid_arguments": ("primary.model_request_started_at", "primary.tool_arguments_ready_at"),
        }
        durations = report.setdefault("durations_ms", {})
        for label, (start, end) in pairs.items():
            value = self._duration_ms(timing, start, end)
            if value is not None:
                durations[label] = value
        report["backend"] = self.backend_name
        return report
