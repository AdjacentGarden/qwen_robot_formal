#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from new_project.dialogue import RobotOrchestrator
from new_project.models import CommandSession


def main() -> int:
    project_root = Path(__file__).resolve().parent
    config_path = project_root / "config" / "hardware.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    config["_config_path"] = str(config_path)
    config["audio"]["voice_io_backend"] = "test_capture"
    config["audio"]["no_play"] = True

    with tempfile.TemporaryDirectory(prefix="query_speech_regression_") as runtime_dir:
        config["paths"]["runtime_dir"] = runtime_dir
        orchestrator = RobotOrchestrator(config)
        spoken: list[str] = []

        def capture(text: str) -> bool:
            spoken.append(str(text))
            return True

        orchestrator.audio.speak_text = capture  # type: ignore[method-assign]
        try:
            for query, expected_fragment in (("现在几点", "现在是"), ("今天天气怎么样", "当前"), ("天的天气怎么样", "当前")):
                spoken.clear()
                decision = orchestrator.planner.plan(query)
                decision.update({"ok": True, "user_text": query, "reply": "收到"})
                result = orchestrator.handle_voice_decision(decision, execute=True, dry_run=False)
                assert result["ok"] is True, result
                assert result["decision"].get("reply") == "", result
                assert result["decision"].get("task_start_ack_policy") == "final_summary_only", result
                assert len(spoken) == 1, (query, spoken)
                assert "收到" not in spoken[0], (query, spoken)
                assert expected_fragment in spoken[0], (query, spoken)

            spoken.clear()
            failure_query = "今天天气怎么样"
            failure_decision = orchestrator.planner.plan(failure_query)
            failure_decision.update({"ok": True, "user_text": failure_query, "reply": "收到"})
            original_execute = orchestrator.executor.execute_task_group

            def fail_realtime_query(task_group, dry_run=False):
                step = task_group.steps[0]
                step.status = "failed"
                step.error = "network_request_failed:internal_detail"
                step.result = {
                    "ok": False,
                    "error": step.error,
                    "parsed_json": {
                        "ok": False,
                        "status": "failed",
                        "skill": "realtime_information",
                        "message": "实时信息查询暂时失败，请稍后再试。",
                        "error": step.error,
                    },
                }
                task_group.status = "failed"
                task_group.result_summary = step.error
                return task_group

            orchestrator.executor.execute_task_group = fail_realtime_query  # type: ignore[method-assign]
            try:
                failure_result = orchestrator.handle_voice_decision(failure_decision, execute=True, dry_run=False)
            finally:
                orchestrator.executor.execute_task_group = original_execute  # type: ignore[method-assign]
            assert failure_result["decision"].get("reply") == "", failure_result
            assert spoken == ["实时信息查询暂时失败，请稍后再试。"], spoken
            failed_item = failure_result["execution"]["executed"][0]
            assert failed_item["status"] == "failed", failed_item
            assert failed_item["result_summary"] == spoken[0], failed_item
            assert "internal_detail" not in spoken[0], spoken

            action_decision = {
                "decision_type": "task_plan",
                "reply": "收到",
                "task_groups": [
                    {
                        "title": "抬头",
                        "user_instruction": "抬头",
                        "slots": {},
                        "steps": [{"skill_name": "head_control", "arguments": {"action": "up"}}],
                    }
                ],
                "ask_user": None,
            }
            session = CommandSession(session_type="regression")
            action_groups = orchestrator._task_groups_from_decision(session, action_decision)
            action_result = orchestrator._apply_task_start_ack_policy(action_decision, action_groups, session.session_id)
            assert action_result.get("reply") == "收到", action_result
            assert action_result.get("task_start_ack_policy") is None, action_result

            implicit_decision = {
                "decision_type": "task_plan",
                "reply": "要不要我帮你打开落地灯呢？",
                "intent_analysis": {
                    "actionable": True,
                    "authorization": "pragmatically_implied",
                    "negated": False,
                    "uncertain": False,
                    "target_skill": "light_control",
                    "target_action": "on",
                },
                "task_groups": [
                    {
                        "title": "改善照明",
                        "user_instruction": "房间光线偏暗",
                        "slots": {"action": "on"},
                        "steps": [{"skill_name": "light_control", "arguments": {"action": "on"}}],
                    }
                ],
                "ask_user": None,
            }
            implicit_session = CommandSession(session_type="regression")
            implicit_groups = orchestrator._task_groups_from_decision(implicit_session, implicit_decision)
            implicit_result = orchestrator._apply_task_start_ack_policy(
                implicit_decision,
                implicit_groups,
                implicit_session.session_id,
            )
            assert implicit_result.get("reply") == "", implicit_result
            assert implicit_result.get("suppressed_start_ack") == "要不要我帮你打开落地灯呢？", implicit_result
            assert implicit_result.get("task_start_ack_policy") == "final_summary_only", implicit_result
        finally:
            orchestrator.close()

    print("QUERY_SPEECH_REGRESSION_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
