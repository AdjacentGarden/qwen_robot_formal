#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from new_project.planner import Planner
from new_project.skill_registry import SkillRegistry


def main() -> int:
    config_path = Path(__file__).resolve().parent / "config" / "hardware.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["_config_path"] = str(config_path)
    registry = SkillRegistry(config)
    planner = Planner(config, registry)

    cases = {
        "今天天气怎么样？": "weather",
        "深圳今天天气怎么样？": "weather",
        "今天，今天的交通状况如何？": "traffic",
        "深圳现在堵不堵？": "traffic",
        "附近有什么好玩的？": "nearby",
        "我在哪？": "location",
        "现在几点？": "current_time",
    }
    for query, expected_action in cases.items():
        decision = planner.plan(query)
        steps = [
            step
            for group in decision.get("task_groups") or []
            for step in group.get("steps") or []
        ]
        assert len(steps) == 1, (query, decision)
        assert decision.get("reply") == "", (query, decision)
        step = steps[0]
        assert step.get("skill_name") == "realtime_information", (query, step)
        assert (step.get("arguments") or {}).get("action") == expected_action, (query, step)

        model_answer = {
            "decision_type": "answer",
            "interaction_type": "conversation",
            "reply": "我无法查询实时信息。",
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.8,
        }
        recovered = planner._postprocess_decision(model_answer, query, authoritative_user_text=True)
        recovered_steps = [
            item
            for group in recovered.get("task_groups") or []
            for item in group.get("steps") or []
        ]
        assert recovered_steps and recovered_steps[0].get("skill_name") == "realtime_information", (query, recovered)
        assert recovered.get("reply") == "", (query, recovered)

    pet = planner.plan("小狗在哪里？")
    pet_skills = {
        step.get("skill_name")
        for group in pet.get("task_groups") or []
        for step in group.get("steps") or []
    }
    assert "realtime_information" not in pet_skills, pet
    # Implicit pet-location meaning is now decided by the realtime model's
    # structured intent_analysis and covered by implicit_intent_regression_check.
    # The deterministic query router only has to prove it never mistakes this
    # for the robot's own realtime location query.
    assert "pet_tracking" not in pet_skills, pet

    assert registry.should_speak_start_ack("realtime_information") is False
    assert registry.should_speak_start_ack("pet_tracking") is True

    print("REALTIME_INFORMATION_REGRESSION_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
