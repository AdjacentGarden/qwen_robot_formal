#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from new_project.doubao_realtime import DoubaoRealtimeSession
from new_project.planner import Planner
from new_project.resources import ResourceManager
from new_project.skill_registry import SkillRegistry


def steps_from(decision: dict) -> list[dict]:
    return [
        step
        for group in decision.get("task_groups") or []
        if isinstance(group, dict)
        for step in group.get("steps") or []
        if isinstance(step, dict)
    ]


def intent(
    skill: str | None,
    action: str | None,
    *,
    actionable: bool = True,
    authorization: str = "implied",
    negated: bool = False,
    uncertain: bool = False,
    args: dict | None = None,
) -> dict:
    return {
        "speech_act": "implicit_request" if authorization == "implied" else "explicit_command",
        "actionable": actionable,
        "authorization": authorization,
        "negated": negated,
        "uncertain": uncertain,
        "skill": skill,
        "action": action,
        "args": dict(args or {}),
        "confidence": 0.95,
    }


def compact_task(text: str, skill: str, action: str, args: dict | None = None) -> dict:
    return {
        "v": 1,
        "type": "tasks",
        "tasks": [{"skill": skill, "action": action, "args": dict(args or {}), "group": 0}],
        "intent": intent(skill, action, args=args),
        "confidence": 0.95,
        "text": text,
    }


async def assert_stream_parses_once(voice: DoubaoRealtimeSession, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = [{"event": 550, "body": {"content": char}} for char in raw]

        async def recv(self) -> dict:
            await asyncio.sleep(0)
            return self.messages.pop(0)

    class FakeClient:
        def __init__(self) -> None:
            self.ws = FakeWebSocket()
            self.audio_frames: list[bytes] = []
            self.audio_bytes = 0
            self.audio_send_error = None

    async def sent() -> bool:
        return True

    args = argparse.Namespace(
        listen_timeout=1.0,
        seconds=1.0,
        silence_tail_sec=0.1,
        first_response_timeout=5.0,
        decision_timeout_after_input=2.0,
        model_text_first_timeout=2.0,
        asr_command_fallback_timeout=2.0,
        fast_tool_json=True,
        _turn_timing={"turn_started_at": time.monotonic(), "counters": {}},
    )
    original_loader = voice._load_modules
    voice._load_modules = lambda: {"parse_response": lambda message: message}
    try:
        send_task = asyncio.create_task(sent())
        await asyncio.sleep(0)
        decision = await voice._receive_decision(FakeClient(), args, send_task, "command")
    finally:
        voice._load_modules = original_loader
    counters = args._turn_timing["counters"]
    assert decision["decision_wire_format"] == "compact_v1", decision
    assert counters["primary.json_parse_attempts"] == 1, counters
    assert counters["primary.complete_json_boundaries"] == 1, counters
    assert counters["primary.incomplete_json_fragments_skipped"] == len(raw) - 1, counters


def main() -> int:
    root = Path(__file__).resolve().parent
    config_path = root / "config" / "hardware.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["_config_path"] = str(config_path)
    registry = SkillRegistry(config)
    voice = DoubaoRealtimeSession(config, registry, ResourceManager(config))
    planner = Planner(config, registry)

    assert config["voice_decision"]["compact_decision_json"] is True
    assert config["voice_decision"]["compact_decision_version"] == 1
    prompt = voice._system_prompt("command", {})
    assert "Compact decision wire format v1" in prompt
    assert "phrase lookup tables" in prompt
    assert len(prompt) < config["voice_decision"]["system_prompt_max_chars"]
    for skill_name in registry.names():
        assert f'"name": "{skill_name}"' in prompt, (skill_name, "compact_prompt_truncated")
    voice.voice_cfg["compact_decision_json"] = False
    assert "Required decision schema" in voice._system_prompt("command", {})
    voice.voice_cfg["compact_decision_json"] = True
    voice.voice_cfg["compact_decision_version"] = 999
    assert "Required decision schema" in voice._system_prompt("command", {})
    voice.voice_cfg["compact_decision_version"] = 1

    light_raw = compact_task("这里看东西有些费眼", "light_control", "on")
    light = voice._parse_decision(json.dumps(light_raw, ensure_ascii=False), "command")
    assert light["decision_wire_format"] == "compact_v1", light
    assert light["decision_type"] == "task_plan", light
    assert light["intent_analysis"]["authorization"] == "pragmatically_implied", light
    assert [(step["skill_name"], step["arguments"].get("action")) for step in steps_from(light)] == [
        ("light_control", "on")
    ], light

    feeder_raw = compact_task("小狗像是该吃点东西了", "feeder_control", "feed", {"grams": 20, "pet": "dog"})
    feeder = voice._parse_decision(json.dumps(feeder_raw, ensure_ascii=False), "command")
    assert steps_from(feeder)[0]["arguments"] == {"action": "feed", "grams": 20}, feeder
    assert feeder["dropped_model_arguments"] == [{"skill_name": "feeder_control", "arguments": ["pet"]}], feeder

    negative_raw = compact_task("房间并不暗，不要开灯", "light_control", "on")
    negative_raw["intent"] = intent(
        "light_control", "on", actionable=False, authorization="none", negated=True
    )
    negative = voice._parse_decision(json.dumps(negative_raw, ensure_ascii=False), "command")
    assert not steps_from(negative), negative
    assert negative.get("intent_analysis_safety_blocked") is True, negative

    unsafe_missing = compact_task("开灯", "light_control", "on")
    unsafe_missing["intent"] = {"actionable": True}
    blocked = voice._parse_decision(json.dumps(unsafe_missing, ensure_ascii=False), "command")
    assert not steps_from(blocked), blocked
    assert blocked.get("compact_contract_error", "").startswith("intent_safety_contract:"), blocked
    blocked["asr_text"] = "开灯"
    assert voice._should_adjudicate_semantics(blocked, "command") is True

    mismatched = compact_task("小狗好像饿了", "feeder_control", "feed")
    mismatched["intent"]["skill"] = "light_control"
    mismatch_blocked = voice._parse_decision(json.dumps(mismatched, ensure_ascii=False), "command")
    assert not steps_from(mismatch_blocked), mismatch_blocked
    assert "implicit_skill_mismatch" in mismatch_blocked.get("compact_contract_error", ""), mismatch_blocked

    multi_raw = {
        "v": 1,
        "type": "tasks",
        "tasks": [
            {"skill": "light_control", "action": "on", "group": 0},
            {"skill": "feeder_control", "action": "feed", "group": 1},
        ],
        "intent": intent(None, None, authorization="explicit"),
        "confidence": 0.94,
        "text": "打开灯，然后给小狗投食",
    }
    multi = voice._parse_decision(json.dumps(multi_raw, ensure_ascii=False), "command")
    assert len(multi["task_groups"]) == 2, multi
    assert [step["skill_name"] for step in steps_from(multi)] == ["light_control", "feeder_control"], multi

    ask_raw = {
        "v": 1,
        "type": "ask",
        "ask": {"question": "你想去哪个位置？", "title": "导航", "missing": ["point"], "skills": ["navigation_goto"]},
        "intent": intent("navigation_goto", "goto", authorization="explicit"),
        "confidence": 0.9,
        "text": "带我过去",
    }
    ask = voice._parse_decision(json.dumps(ask_raw, ensure_ascii=False), "command")
    assert ask["decision_type"] == "ask_user", ask
    assert ask["ask_user"]["missing_slots"] == ["point"], ask
    assert ask["task_groups"] and not steps_from(ask), ask
    optional_ask_raw = dict(ask_raw)
    optional_ask_raw["ask"] = {
        "question": "需要同时打开投影吗？",
        "title": "运动",
        "optional": ["projector"],
        "skills": ["squat"],
    }
    optional_ask = voice._parse_decision(json.dumps(optional_ask_raw, ensure_ascii=False), "command")
    assert optional_ask["decision_type"] == "ask_user" and optional_ask["ask_user"]["optional_slots"] == ["projector"], optional_ask
    no_slot_ask_raw = dict(ask_raw)
    no_slot_ask_raw["ask"] = {"question": "你能再具体说一下吗？", "title": "澄清"}
    no_slot_ask = voice._parse_decision(json.dumps(no_slot_ask_raw, ensure_ascii=False), "command")
    assert no_slot_ask["decision_type"] == "ask_user" and not steps_from(no_slot_ask), no_slot_ask

    answer_raw = {
        "v": 1,
        "type": "answer",
        "interaction": "conversation",
        "intent": intent(None, None, actionable=False, authorization="none"),
        "reply": "你好，很高兴见到你。",
        "confidence": 0.95,
        "text": "你好",
    }
    answer = voice._parse_decision(json.dumps(answer_raw, ensure_ascii=False), "command")
    assert answer["decision_type"] == "answer" and not steps_from(answer), answer

    memory_raw = dict(answer_raw, memory_op="remember", fact={"kind": "pet_name", "value": "豆豆"})
    memory = voice._parse_decision(json.dumps(memory_raw, ensure_ascii=False), "command")
    assert memory["memory_operation"] == "remember" and memory["memory_fact"]["value"] == "豆豆", memory

    meeting_raw = compact_task("投影会议内容", "projector_control", "meeting_presentation_on")
    meeting_raw["intent"]["authorization"] = "explicit"
    meeting = voice._parse_decision(json.dumps(meeting_raw, ensure_ascii=False), "command")
    planned = planner._postprocess_decision(meeting, "投影会议内容", authoritative_user_text=True)
    assert [step["skill_name"] for step in steps_from(planned)] == [
        "navigation_goto",
        "head_control",
        "environment_perception",
        "projector_control",
    ], planned

    legacy_raw = {
        "decision_type": "task_plan",
        "interaction_type": "command",
        "task_operation": "none",
        "reply": "",
        "task_groups": [
            {
                "title": "灯光控制",
                "user_instruction": "开灯",
                "slots": {},
                "followups": [],
                "steps": [{"skill_name": "light_control", "arguments": {"action": "on"}}],
            }
        ],
        "ask_user": None,
        "confidence": 0.9,
        "user_text": "开灯",
        "intent_analysis": {
            "speech_act": "explicit_command",
            "literal_meaning": "开灯",
            "implied_goal": "打开灯光",
            "actionable": True,
            "authorization": "explicit",
            "negated": False,
            "uncertain": False,
            "target_skill": "light_control",
            "target_action": "on",
            "arguments": {},
            "task_title": "灯光控制",
            "reason": "用户明确要求打开灯光",
            "confidence": 0.9,
        },
    }
    legacy = voice._parse_decision(json.dumps(legacy_raw, ensure_ascii=False), "command")
    assert steps_from(legacy)[0]["skill_name"] == "light_control", legacy
    assert "decision_wire_format" not in legacy, legacy

    nested = '{"v":1,"type":"answer","reply":"包含 { 花括号 } 和 \\"引号\\"","intent":{"actionable":false,"authorization":"none","negated":false,"uncertain":false},"confidence":1,"text":"测试"}'
    for end in range(1, len(nested)):
        assert voice._extract_json_object(nested[:end]) == "", end
    assert voice._extract_json_object(nested) == nested

    compact_chars = len(json.dumps(light_raw, ensure_ascii=False, separators=(",", ":")))
    legacy_chars = len(json.dumps(legacy_raw, ensure_ascii=False, separators=(",", ":")))
    assert compact_chars < legacy_chars, (compact_chars, legacy_chars)
    asyncio.run(assert_stream_parses_once(voice, light_raw))

    print(
        "COMPACT_JSON_REGRESSION_CHECK_OK",
        json.dumps(
            {
                "prompt_chars": len(prompt),
                "compact_sample_chars": compact_chars,
                "legacy_sample_chars": legacy_chars,
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
