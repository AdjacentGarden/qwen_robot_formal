#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from new_project.doubao_realtime import DoubaoRealtimeSession
from new_project.executor import SkillExecutor
from new_project.models import TaskStep
from new_project.planner import Planner
from new_project.resources import ResourceManager
from new_project.skill_registry import SkillRegistry


def steps_from(decision: dict) -> list[dict]:
    return [
        step
        for group in decision.get("task_groups") or []
        for step in group.get("steps") or []
        if isinstance(step, dict)
    ]


def semantic_decision(
    text: str,
    skill: str | None,
    action: str | None,
    *,
    actionable: bool = True,
    authorization: str = "pragmatically_implied",
    negated: bool = False,
    uncertain: bool = False,
    confidence: float = 0.94,
    arguments: dict | None = None,
) -> dict:
    return {
        "decision_type": "answer",
        "interaction_type": "conversation",
        "reply": "",
        "task_groups": [],
        "ask_user": None,
        "confidence": confidence,
        "user_text": text,
        "intent_analysis": {
            "speech_act": "implicit_request" if actionable else "question",
            "literal_meaning": text,
            "implied_goal": "由大模型根据整句语义推断的期望状态" if actionable else "",
            "actionable": actionable,
            "authorization": authorization,
            "negated": negated,
            "uncertain": uncertain,
            "target_skill": skill,
            "target_action": action,
            "arguments": dict(arguments or {}),
            "task_title": "语义推断任务" if actionable else "",
            "reason": "大模型完成语用推理后确认了隐含执行意图",
            "confidence": confidence,
        },
    }


def assert_model_route(
    voice: DoubaoRealtimeSession,
    planner: Planner,
    text: str,
    skill: str,
    action: str,
    arguments: dict | None = None,
) -> None:
    normalized = voice._normalize_command_decision(
        semantic_decision(text, skill, action, arguments=arguments),
        text,
    )
    normalized.update(
        {
            "asr_text": text,
            "asr_text_source": "regression",
            "authoritative_user_text": True,
            "semantic_adjudication_completed": True,
        }
    )
    decision = planner._postprocess_decision(normalized, text, authoritative_user_text=True)
    matching = [step for step in steps_from(decision) if step.get("skill_name") == skill]
    assert len(matching) == 1, (text, decision)
    assert (matching[0].get("arguments") or {}).get("action") == action, (text, matching[0])
    assert decision.get("ask_user") is None, (text, decision)
    assert decision.get("intent_analysis_materialized") is True, (text, decision)


def main() -> int:
    project_root = Path(__file__).resolve().parent
    config_path = project_root / "config" / "hardware.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["_config_path"] = str(config_path)
    registry = SkillRegistry(config)
    planner = Planner(config, registry)
    voice = DoubaoRealtimeSession(config, registry, ResourceManager(config))

    # The text varies freely; routing is driven by the model's structured
    # semantic verdict, not by matching any of these surface forms locally.
    for text in (
        "我觉得灯光有一点暗。",
        "觉得房间的灯光有一点暗。",
        "在这儿看字眼睛有些吃力。",
        "这个氛围未免也太昏沉了。",
    ):
        assert_model_route(voice, planner, text, "light_control", "on")

    for text in ("小狗不见了", "小狗现在在哪里？", "半天没瞧见那只小家伙了"):
        assert_model_route(
            voice,
            planner,
            text,
            "pet_tracking",
            "find_route",
            {"pet": "dog", "search_strategy": "current_then_known_points", "track_after_found": True},
        )

    for text in ("小狗有一点饿了", "那孩子肚子空空的，该照顾一下了"):
        assert_model_route(voice, planner, text, "feeder_control", "feed")

    feeder_with_semantic_noise = semantic_decision(
        "小狗有一点饿了",
        "feeder_control",
        "feed",
        arguments={"pet": "小狗", "grams": 20},
    )
    sanitized_feeder = voice._normalize_command_decision(feeder_with_semantic_noise, feeder_with_semantic_noise["user_text"])
    feeder_arguments = (steps_from(sanitized_feeder)[0].get("arguments") or {})
    assert feeder_arguments == {"action": "feed", "grams": 20}, sanitized_feeder
    assert sanitized_feeder.get("dropped_model_arguments") == [
        {"skill_name": "feeder_control", "arguments": ["pet"]}
    ], sanitized_feeder

    # A model answer or action-only clarification is repaired from the model's
    # own semantic fields. The runtime never inspects words such as 暗 or 饿.
    wrong_action = semantic_decision("灯光让人看着费劲", "light_control", "on")
    wrong_action["decision_type"] = "ask_user"
    wrong_action["task_groups"] = [
        {
            "title": "落地灯控制",
            "user_instruction": wrong_action["user_text"],
            "slots": {},
            "followups": [],
            "steps": [{"skill_name": "light_control", "arguments": {"action": "status"}, "reason": "初次输出错误"}],
        }
    ]
    wrong_action["ask_user"] = {
        "task_title": "落地灯控制",
        "question": "要执行什么动作？",
        "missing_slots": ["action"],
        "optional_slots": [],
        "candidate_skills": ["light_control"],
    }
    corrected = voice._normalize_command_decision(wrong_action, wrong_action["user_text"])
    assert corrected["decision_type"] == "task_plan", corrected
    assert corrected["ask_user"] is None, corrected
    assert (steps_from(corrected)[0].get("arguments") or {}).get("action") == "on", corrected

    negative_analyses = (
        semantic_decision("房间并不暗，不要改变灯光", "light_control", "on", actionable=False, negated=True),
        semantic_decision("小狗是不是饿了？", "feeder_control", "feed", actionable=False, uncertain=True),
        semantic_decision("假设宠物走丢了该怎么办", "pet_tracking", "find_route", actionable=False),
        semantic_decision("这是一句闲聊", "light_control", "on", confidence=0.4),
    )
    for raw in negative_analyses:
        normalized = voice._normalize_command_decision(raw, raw["user_text"])
        assert not steps_from(normalized), normalized

    contradictory_negative = semantic_decision(
        "模型已经判定用户拒绝候选动作",
        "light_control",
        "off",
        actionable=True,
        negated=True,
    )
    contradictory_negative["decision_type"] = "task_plan"
    contradictory_negative["task_groups"] = [
        {
            "title": "错误的模型步骤",
            "user_instruction": contradictory_negative["user_text"],
            "slots": {},
            "followups": [],
            "steps": [{"skill_name": "light_control", "arguments": {"action": "off"}}],
        }
    ]
    blocked = voice._normalize_command_decision(contradictory_negative, contradictory_negative["user_text"])
    assert blocked.get("intent_analysis_safety_blocked") is True, blocked
    assert not steps_from(blocked), blocked

    invalid_action = semantic_decision("语义动作不在技能契约里", "light_control", "feed")
    assert not steps_from(voice._normalize_command_decision(invalid_action, invalid_action["user_text"]))

    # The deterministic fallback is deliberately not allowed to execute these
    # implicit statements. They must pass through model semantic analysis.
    for text in ("房间的灯光有一点暗。", "小狗有一点饿了", "小狗不见了"):
        assert not steps_from(planner._local_fallback_plan(text)), text

    primary_answer = {
        "decision_type": "answer",
        "reply": "",
        "task_groups": [],
        "ask_user": None,
        "asr_text": "一种从未列举过的间接说法",
        "intent_analysis": {"actionable": False},
    }
    assert voice._should_adjudicate_semantics(primary_answer, "command") is True
    primary_answer["semantic_adjudication_completed"] = True
    assert voice._should_adjudicate_semantics(primary_answer, "command") is False
    partial = '{"decision_type":"task_plan","task_groups":['
    final_json = '{"decision_type":"answer","task_groups":[],"ask_user":null}'
    assert voice._best_model_text([partial], [final_json]) == final_json
    repaired_fragment = voice._parse_decision('{"decision_type":"task_plan",', "command")
    assert repaired_fragment.get("recovered_from_incomplete_json") is True
    assert voice._decision_ready(repaired_fragment, "command") is False

    prompt_specs = registry.compact_specs_for_prompt()
    assert all("trigger_phrases_zh" not in spec for spec in prompt_specs)
    primary_prompt = voice._system_prompt("command", {})
    assert "phrase lookup tables" in primary_prompt
    assert "trigger_phrases_zh" not in primary_prompt
    assert "房间有点暗" not in primary_prompt
    assert "小狗有点饿了" not in primary_prompt
    adjudication_prompt = voice._semantic_adjudication_prompt("原始语音转写")
    assert "second-pass semantic adjudication" in adjudication_prompt
    assert "原始语音转写" in adjudication_prompt
    for skill_name in registry.names():
        token = f'"name": "{skill_name}"'
        assert token in primary_prompt, (skill_name, "primary_prompt_truncated")
        assert token in adjudication_prompt, (skill_name, "adjudication_prompt_truncated")
    assert len(primary_prompt) < config["voice_decision"]["system_prompt_max_chars"]
    assert len(adjudication_prompt) < config["voice_decision"]["system_prompt_max_chars"]

    voice_cfg = config["voice_decision"]
    assert voice_cfg["semantic_adjudication_on_unresolved"] is True
    assert voice_cfg["semantic_action_confidence_threshold"] >= 0.75
    assert voice_cfg["asr_command_fallback_timeout_seconds"] >= 6.0

    assert config["execution"]["pet_tracking"]["base_speed"] == 1.0
    executor = SkillExecutor.__new__(SkillExecutor)
    executor.config = config
    pet_step = TaskStep(skill_name="pet_tracking", arguments={"action": "track", "base_speed": 0.2})
    executor._normalize_step_arguments(pet_step)
    assert pet_step.arguments["base_speed"] == 1.0, pet_step.arguments

    print("MODEL_SEMANTIC_INTENT_REGRESSION_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
