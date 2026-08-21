from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_skills import (
    RUNNER_RESULT_PREFIX,
    SEQUENCE_TOOL_NAME,
    LocalSkillBridge,
    _atomic_intent_supported,
    _recover_explicit_sequence_tasks,
    _repair_sequence_tasks,
    _normalized_schema,
    _tool_description,
    build_skill_start_speech,
    build_sequence_start_speech,
)
from skill_runner import (
    apply_realtime_execution_overrides,
    build_spoken_summary,
    extract_structured_result,
    is_read_only,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scenarios" / "procedure_catalog.json"
SPECS = ROOT / "robot_skills" / "config" / "skill_specs"


def write_spec(root: Path, name: str, **extra):
    value = {
        "name": name,
        "description_zh": "测试技能",
        "entrypoint": str(root / "run.sh"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "dry_run": {"type": "boolean|null"},
            },
            "required": [],
        },
        "allowed_actions": ["on", "off"],
        "resources": ["projector"],
        "side_effects": ["controls_projector"],
        **extra,
    }
    (root / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")


class LocalSkillTests(unittest.TestCase):
    @staticmethod
    def successful_result(name="test"):
        return {
            "ok": False,
            "validation_ok": True,
            "executed": False,
            "skill": name,
            "spoken_summary": "安全模拟校验通过，但本次操作没有实际执行。",
            "error": "dry_run_only_not_executed",
        }

    def production_bridge(self):
        return LocalSkillBridge(
            spec_dir=SPECS,
            scenario_catalog_path=CATALOG,
            execute=False,
            backend="subprocess",
        )

    def test_ordered_living_light_scene_is_repaired_to_atomic_sequence(self):
        bridge = self.production_bridge()
        with patch.object(
            bridge,
            "_invoke_atomic",
            side_effect=lambda name, *_args, **_kwargs: self.successful_result(name),
        ):
            result = bridge.invoke(
                "run_robot_scenario",
                {"scenario": "living_room_light_service"},
                "请先去客厅，到了以后把灯打开。",
            )
        self.assertEqual(result["skill"], SEQUENCE_TOOL_NAME)
        self.assertEqual([item["name"] for item in result["tasks"]], ["navigation_goto", "light_control"])
        self.assertEqual(result["tasks"][1]["arguments"]["action"], "on")

    def test_wrong_living_scene_is_repaired_without_changing_order_or_polarity(self):
        bridge = self.production_bridge()
        tasks = [
            {"name": "run_robot_scenario", "arguments": {"scenario": "living_room_light_service"}},
            {"name": "navigation_goto", "arguments": {"point": "study_projection"}},
        ]
        repaired = _repair_sequence_tasks(
            tasks,
            "先导航到书房，再把客厅灯关掉。",
            bridge.scenario_catalog,
        )
        self.assertEqual([item["name"] for item in repaired], ["navigation_goto", "light_control"])
        self.assertEqual(repaired[0]["arguments"]["point"], "study_projection")
        self.assertEqual(repaired[1]["arguments"]["action"], "off")

    def test_negated_light_scene_is_removed_but_other_tasks_remain(self):
        bridge = self.production_bridge()
        tasks = [
            {"name": "run_robot_scenario", "arguments": {"scenario": "living_room_light_service"}},
            {"name": "navigation_goto", "arguments": {"point": "white_wall"}},
            {"name": "media_player", "arguments": {"action": "play_music"}},
        ]
        repaired = _repair_sequence_tasks(
            tasks,
            "不要开灯，先导航到客厅，再播放音乐。",
            bridge.scenario_catalog,
        )
        self.assertEqual([item["name"] for item in repaired], ["navigation_goto", "media_player"])

    def test_malformed_sequence_recovers_only_explicit_positive_tasks(self):
        bridge = self.production_bridge()
        recovered = _recover_explicit_sequence_tasks(
            "关闭会议投影，但别移动，然后查询现在几点。",
            bridge.scenario_catalog,
        )
        self.assertEqual(
            [item["name"] for item in recovered],
            ["run_robot_scenario", "realtime_information"],
        )
        self.assertEqual(recovered[1]["arguments"]["action"], "current_time")

    def test_unknown_meeting_alias_is_normalized_and_duplicate_navigation_collapsed(self):
        bridge = self.production_bridge()
        repaired = _repair_sequence_tasks(
            [
                {"name": "navigation_goto", "arguments": {"point": "study_projection"}},
                {"name": "run_robot_scenario", "arguments": {"scenario": "meeting_projection_at_location"}},
            ],
            "导航到书房以后，就在到达的位置开始会议投影。",
            bridge.scenario_catalog,
        )
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["arguments"]["scenario"], "meeting_projection")
        self.assertEqual(repaired[0]["arguments"]["point"], "study_projection")

    def test_duration_between_record_verb_and_video_is_valid_evidence(self):
        self.assertTrue(
            _atomic_intent_supported(
                "front_camera_record", {}, "先用前摄拍照，再录一段五秒的视频", in_sequence=True
            )[0]
        )
        self.assertTrue(
            _atomic_intent_supported(
                "back_camera_record", {}, "先用后摄拍照，再用后摄录五秒视频", in_sequence=True
            )[0]
        )

    def test_short_location_query_and_homophone_destinations_are_supported(self):
        self.assertTrue(
            _atomic_intent_supported(
                "realtime_information", {"action": "location"}, "先查当前位置，再去书房"
            )[0]
        )
        self.assertTrue(
            _atomic_intent_supported(
                "navigation_goto", {"point": "study_projection"}, "先去书放，再开灯", in_sequence=True
            )[0]
        )

    def test_missing_model_call_can_be_recovered_only_from_explicit_intent(self):
        bridge = self.production_bridge()
        cases = {
            "我想去客厅的那个地方做俯卧撑": (
                "run_robot_scenario", {"scenario": "push_up_companion"}
            ),
            "导航去书": ("navigation_goto", {"point": "study_projection"}),
            "导航到书": ("navigation_goto", {"point": "study_projection"}),
            "导航到苏北": ("navigation_goto", {"point": "study_projection"}),
            "导航到克强": ("navigation_goto", {"point": "white_wall"}),
            "请你往前走": ("move_forward", {}),
        }
        for text, (name, arguments) in cases.items():
            with self.subTest(text=text):
                plan = bridge.recover_explicit_plan(text)
                self.assertIsNotNone(plan)
                self.assertEqual(plan["name"], name)
                self.assertEqual(plan["arguments"], arguments)

        for text in ("书挺好看的", "苏北好玩吗", "克强是谁", "请你导航"):
            with self.subTest(text=text):
                self.assertIsNone(bridge.recover_explicit_plan(text))

    def test_repeated_explicit_read_only_queries_are_locally_refreshed(self):
        bridge = self.production_bridge()
        cases = {
            "请告诉我现在有哪些导航点": ("navigation_list", {}),
            "机器人目前可以去哪些位置": ("navigation_list", {}),
            "我现在都有哪些提醒": ("reminder_query", {}),
            "查一下闹钟安排了哪些": ("reminder_query", {}),
            "今天星期几": ("realtime_information", {"action": "current_time"}),
            "今天会下雨吗": ("realtime_information", {"action": "weather"}),
        }
        for text, (name, arguments) in cases.items():
            with self.subTest(text=text):
                plan = bridge.recover_explicit_plan(text)
                self.assertIsNotNone(plan)
                self.assertEqual(plan["name"], name)
                self.assertEqual(plan["arguments"], arguments)

    def test_read_only_recovery_does_not_steal_navigation_or_general_questions(self):
        bridge = self.production_bridge()
        destination = bridge.recover_explicit_plan("导航到书房")
        self.assertIsNotNone(destination)
        self.assertEqual(destination["name"], "navigation_goto")
        for text in (
            "你会导航吗",
            "书房有哪些书",
            "公司有哪些地点",
            "手机在哪里",
            "提醒功能怎么用",
        ):
            with self.subTest(text=text):
                self.assertIsNone(bridge.recover_explicit_plan(text))

    def test_navigation_rejection_explains_destination_without_claiming_motion(self):
        bridge = self.production_bridge()
        result = bridge._invoke_atomic(
            "navigation_goto",
            {"point": "study_projection"},
            "请你导航",
        )
        self.assertFalse(result["executed"])
        self.assertIn("目的地", result["spoken_summary"])
        self.assertIn("没有移动", result["spoken_summary"])

    def test_model_navigation_alias_is_canonicalized_and_unknown_point_is_blocked(self):
        bridge = self.production_bridge()
        with patch.object(
            bridge,
            "_invoke_atomic",
            side_effect=lambda name, arguments, *_args, **_kwargs: {
                "name": name,
                "arguments": arguments,
            },
        ):
            repaired = bridge.invoke(
                "navigation_goto",
                {"point": "苏北"},
                "导航到苏北",
            )
        self.assertEqual(repaired["arguments"]["point"], "study_projection")

        supported, reason = _atomic_intent_supported(
            "navigation_goto",
            {"point": "深圳"},
            "导航到深圳",
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "navigation_destination_unknown")

    def test_local_recovery_never_turns_negated_hardware_into_an_action(self):
        bridge = self.production_bridge()
        for text in (
            "不要往前走",
            "别低头",
            "不用拍照",
            "不要导航到书房",
            "不要识别我",
        ):
            with self.subTest(text=text):
                self.assertIsNone(bridge.recover_explicit_plan(text))

        plan = bridge.recover_explicit_plan("不要开灯，导航到客厅")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["name"], "navigation_goto")
        self.assertEqual(plan["arguments"]["point"], "white_wall")
    def test_intent_evidence_blocks_association_without_requiring_fixed_phrases(self):
        supported, reason = _atomic_intent_supported(
            "light_control",
            {"action": "on", "room": "living_room"},
            "关闭投影然后导航到客厅",
            in_sequence=True,
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "missing_lighting_evidence")

        for text in ("把客厅灯打开", "屋里太暗了，调亮一点", "光线不足，帮我照亮些"):
            with self.subTest(text=text):
                self.assertTrue(
                    _atomic_intent_supported(
                        "light_control",
                        {"action": "on", "room": "living_room"},
                        text,
                        in_sequence=True,
                    )[0]
                )

        supported, reason = _atomic_intent_supported(
            "light_control",
            {"action": "off", "room": "living_room"},
            "我不想开灯，导航去客厅",
            in_sequence=True,
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "explicitly_negated_lighting")

    def test_atomic_semantics_accept_natural_paraphrases(self):
        accepted = (
            ("face_recognition", {}, "看看是不是还能认出我"),
            ("head_control", {"action": "up"}, "把视线往高处调整一点"),
            ("head_control", {"action": "level"}, "恢复正常角度，看正前方"),
            ("projector_control", {"action": "off"}, "墙上的内容先收起来吧"),
            ("media_player", {"action": "stop"}, "这个节目就到这里吧"),
            ("reminder_cancel", {}, "刚才那个提醒不用留了"),
            ("reminder_query", {}, "列一下我都安排了哪些提醒"),
            ("environment_perception", {}, "瞧瞧面前现在是什么情况"),
        )
        for name, arguments, text in accepted:
            with self.subTest(name=name, text=text):
                supported, reason = _atomic_intent_supported(name, arguments, text)
                self.assertTrue(supported, reason)

    def test_context_can_ground_only_low_risk_continuation_actions(self):
        supported, reason = _atomic_intent_supported(
            "media_player",
            {"action": "stop"},
            "这个先停了吧",
            prior_assistant_text="正在播放一段轻松音乐。",
        )
        self.assertTrue(supported, reason)

        supported, reason = _atomic_intent_supported(
            "projector_control",
            {"action": "off"},
            "这个先收起来",
            prior_assistant_text="会议内容已经投影好了。",
        )
        self.assertTrue(supported, reason)

        # Context never supplies permission or a destination for motion.
        supported, reason = _atomic_intent_supported(
            "navigation_goto",
            {"point": "study_projection"},
            "这个开始吧",
            prior_assistant_text="我可以导航到书房。",
        )
        self.assertFalse(supported)
        self.assertIn(reason, {"missing_navigation_goto_evidence", "navigation_destination_conflict"})

    def test_atomic_paraphrases_still_respect_negation_and_capability_questions(self):
        rejected = (
            ("projector_control", {"action": "off"}, "投影暂时别停"),
            ("head_control", {"action": "up"}, "先别把视线往上调"),
            ("reminder_cancel", {}, "不要删除刚才的提醒"),
            ("media_player", {"action": "stop"}, "你能不能自动停止节目"),
        )
        for name, arguments, text in rejected:
            with self.subTest(name=name, text=text):
                self.assertFalse(_atomic_intent_supported(name, arguments, text)[0])

    def test_visual_question_does_not_implicitly_authorize_camera(self):
        self.assertFalse(
            _atomic_intent_supported(
                "environment_perception",
                {"camera": "front", "purpose": "general"},
                "客厅有什么",
            )[0]
        )
        self.assertTrue(
            _atomic_intent_supported(
                "environment_perception",
                {"camera": "front", "purpose": "general"},
                "用摄像头看看面前有什么",
            )[0]
        )

    def test_intent_evidence_separates_company_chat_from_robot_location(self):
        self.assertFalse(
            _atomic_intent_supported(
                "realtime_information",
                {"action": "location"},
                "你知道理想公司吗",
            )[0]
        )
        for text in ("机器人现在在哪里", "你当前的位置是哪儿", "查一下本机定位"):
            with self.subTest(text=text):
                self.assertTrue(
                    _atomic_intent_supported(
                        "realtime_information",
                        {"action": "location"},
                        text,
                    )[0]
                )

    def test_sequence_navigation_requires_matching_spoken_destination(self):
        self.assertTrue(
            _atomic_intent_supported(
                "navigation_goto",
                {"point": "living_room"},
                "先关投影，再导航到客厅",
                in_sequence=True,
            )[0]
        )
        supported, reason = _atomic_intent_supported(
            "navigation_goto",
            {"point": "study_projection"},
            "先关投影，再导航到客厅",
            in_sequence=True,
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "navigation_destination_conflict")

    def test_sequence_tool_is_additive_and_keeps_existing_child_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "navigation_goto", resources=["base"])
            write_spec(root, "light_control", resources=["light"])
            bridge = LocalSkillBridge(spec_dir=root, scenario_catalog_path=None)
            functions = {item["function"]["name"]: item["function"] for item in bridge.tool_schemas}
            self.assertIn(SEQUENCE_TOOL_NAME, functions)
            self.assertIn("navigation_goto", functions)
            self.assertIn("light_control", functions)
            sequence_names = set(
                functions[SEQUENCE_TOOL_NAME]["parameters"]["properties"]["tasks"]["items"]
                ["properties"]["name"]["enum"]
            )
            self.assertEqual(sequence_names, {"navigation_goto", "light_control"})
            self.assertNotIn(SEQUENCE_TOOL_NAME, sequence_names)

    def test_sequence_executes_all_tasks_in_order_and_announces_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "navigation_goto", resources=["base"])
            write_spec(root, "light_control", resources=["light"])
            events: list[dict] = []
            bridge = LocalSkillBridge(
                spec_dir=root,
                scenario_catalog_path=None,
                event_callback=events.append,
            )
            calls: list[tuple[str, dict]] = []

            def invoke(name, arguments, user_text, *, announce=True):
                calls.append((name, arguments))
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "spoken_summary": "已到达客厅。" if name == "navigation_goto" else "客厅灯已经打开。",
                    "error": None,
                }

            with patch.object(bridge, "_invoke_atomic", side_effect=invoke):
                result = bridge.invoke(
                    SEQUENCE_TOOL_NAME,
                    {
                        "tasks": [
                            {"name": "navigation_goto", "arguments": {"point": "white_wall"}},
                            {"name": "light_control", "arguments": {"action": "on"}},
                        ]
                    },
                    "先导航到客厅，然后打开灯光",
                )

            self.assertTrue(result["ok"])
            self.assertEqual([item[0] for item in calls], ["navigation_goto", "light_control"])
            self.assertEqual(events[0]["kind"], "acknowledgement")
            self.assertEqual(len(events), 1)
            self.assertIn("顺序", events[0]["text"])
            self.assertIn("已到达客厅", result["spoken_summary"])
            self.assertIn("客厅灯已经打开", result["spoken_summary"])

    def test_sequence_stops_after_failure_unless_user_explicitly_requests_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "navigation_goto", resources=["base"])
            write_spec(root, "light_control", resources=["light"])
            bridge = LocalSkillBridge(spec_dir=root, scenario_catalog_path=None)
            tasks = [
                {"name": "navigation_goto", "arguments": {"point": "white_wall"}},
                {"name": "light_control", "arguments": {"action": "on"}},
            ]
            failed = {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "spoken_summary": "导航没有完成。",
                "error": "NAV_FAILED",
            }
            succeeded = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "spoken_summary": "灯已经打开。",
                "error": None,
            }
            with patch.object(bridge, "_invoke_atomic", side_effect=[failed, succeeded]) as invoke:
                stopped = bridge.invoke(SEQUENCE_TOOL_NAME, {"tasks": tasks}, "先去客厅再开灯")
                self.assertEqual(invoke.call_count, 1)
            self.assertTrue(stopped["tasks"][1]["skipped"])
            self.assertIn("后面的1项没有继续", stopped["spoken_summary"])

            with patch.object(bridge, "_invoke_atomic", side_effect=[failed, succeeded]) as invoke:
                forced_safe = bridge.invoke(
                    SEQUENCE_TOOL_NAME,
                    {"tasks": tasks, "failure_policy": "continue"},
                    "先去客厅然后开灯",
                )
                self.assertEqual(invoke.call_count, 1)
            self.assertEqual(forced_safe["failure_policy"], "stop")
            self.assertTrue(forced_safe["tasks"][1]["skipped"])

            with patch.object(bridge, "_invoke_atomic", side_effect=[failed, succeeded]) as invoke:
                continued = bridge.invoke(
                    SEQUENCE_TOOL_NAME,
                    {"tasks": tasks, "failure_policy": "continue"},
                    "先去客厅，不管是否成功都继续开灯",
                )
                self.assertEqual(invoke.call_count, 2)
            self.assertFalse(continued["ok"])
            self.assertFalse(continued["tasks"][1]["skipped"])

    def test_sequence_start_speech_never_claims_completion(self):
        text = build_sequence_start_speech(
            [
                {"name": "navigation_goto", "arguments": {"point": "study_projection"}},
                {"name": "feeder_control", "arguments": {"action": "feed"}},
            ]
        )
        self.assertTrue(any(marker in text for marker in ("顺序", "依次", "先")))
        self.assertNotIn("已经到达", text)
        self.assertNotIn("已经投食", text)
        self.assertLessEqual(len(text), 16)

    def test_navigation_start_and_arrival_wording_varies_without_changing_destination(self):
        starts = [
            build_skill_start_speech(
                "navigation_goto",
                {"point": "study_projection"},
                str(turn),
            )
            for turn in range(1, 5)
        ]
        self.assertEqual(len(set(starts)), 4)
        self.assertTrue(all("书房" in text for text in starts))

        class Policy:
            def step_summary(self, step, parsed):
                return ""

        executor = SimpleNamespace(speech_policy=Policy())
        step = SimpleNamespace(skill_name="navigation_goto", arguments={"point": "study_projection"})
        arrivals = [
            build_spoken_summary(executor, step, {"ok": True, "parsed_json": {"ok": True}})
            for _ in range(3)
        ]
        self.assertEqual(len(set(arrivals)), 3)
        self.assertTrue(all("书房" in text for text in arrivals))

    def test_status_and_reminder_summaries_are_human_and_action_accurate(self):
        class Policy:
            def step_summary(self, step, parsed):
                return "legacy internal summary"

        executor = SimpleNamespace(speech_policy=Policy())
        cases = (
            ("reminder_schedule", {"action": "schedule", "content": "喝水"}, "提醒"),
            ("navigation_list", {"action": "list"}, "原点、客厅白墙和书房投影点"),
            ("person_tracking", {"action": "check"}, "跟随"),
            ("push_up", {"action": "check"}, "计数"),
            ("projector_control", {"action": "status"}, "当前状态已经查到了"),
        )
        for skill, arguments, expected in cases:
            with self.subTest(skill=skill):
                step = SimpleNamespace(skill_name=skill, arguments=arguments)
                summary = build_spoken_summary(
                    executor,
                    step,
                    {"ok": True, "parsed_json": {"ok": True}},
                )
                self.assertIn(expected, summary)
                self.assertNotIn(skill, summary)

    @staticmethod
    def runner_success(name: str, *, spoken_summary: str = "", structured_result=None) -> dict:
        payload = {
            "ok": True,
            "skill": name,
            "arguments": {},
            "resources": ["base"] if name == "move_forward" else ["projector"],
            "message": "",
            "spoken_summary": spoken_summary,
            "structured_result": structured_result or {},
            "error": None,
        }
        return {
            "returncode": 0,
            "stdout": RUNNER_RESULT_PREFIX + json.dumps(payload) + "\n",
            "stderr": "",
            "error": None,
        }

    def test_all_enabled_specs_load_and_existing_disabled_spec_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "move_forward", resources=["base"])
            write_spec(root, "fan_control", side_effects=["disabled"])
            bridge = LocalSkillBridge(spec_dir=root, backend="subprocess")
            self.assertEqual(set(bridge.specs), {"move_forward"})
            self.assertEqual(bridge.unavailable, {"fan_control": "disabled_by_existing_spec"})

    def test_tool_schema_hides_internal_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "projector_control")
            bridge = LocalSkillBridge(spec_dir=root, backend="subprocess")
            properties = bridge.tool_schemas[0]["function"]["parameters"]["properties"]
            self.assertEqual(set(properties), {"action"})
            self.assertEqual(properties["action"]["enum"], ["on", "off"])

    def test_projector_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint = root / "run.sh"
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            write_spec(root, "projector_control")
            bridge = LocalSkillBridge(spec_dir=root, backend="subprocess")
            with patch.object(bridge, "_run", return_value=self.runner_success("projector_control")) as run:
                result = bridge.invoke("projector_control", {"action": "on", "dry_run": False})
            self.assertFalse(result["ok"])
            self.assertTrue(result["validation_ok"])
            self.assertFalse(result["executed"])
            self.assertFalse(result["device_state_changed"])
            self.assertEqual(result["mode"], "dry_run")
            self.assertIn("--dry-run", run.call_args.args[0])
            self.assertNotIn("dry_run", bridge.tool_schemas[0]["function"]["parameters"]["properties"])

    def test_base_skill_is_catalogued_but_still_dry_run_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "move_forward", resources=["base"])
            bridge = LocalSkillBridge(spec_dir=root, backend="subprocess")
            with patch.object(bridge, "_run", return_value=self.runner_success("move_forward")) as run:
                result = bridge.invoke("move_forward", {})
            self.assertFalse(result["ok"])
            self.assertTrue(result["validation_ok"])
            self.assertEqual(result["mode"], "dry_run")
            self.assertIn("--dry-run", run.call_args.args[0])

    def test_read_only_classification_does_not_include_motion(self):
        self.assertTrue(is_read_only("realtime_information", {"action": "current_time"}))
        self.assertTrue(is_read_only("light_control", {"action": "status"}))
        self.assertFalse(is_read_only("move_forward", {}))

    def test_realtime_meeting_projection_is_nonblocking(self):
        arguments = apply_realtime_execution_overrides(
            "projector_control",
            {"action": "meeting_presentation_on"},
        )
        self.assertIs(arguments["hold"], False)
        self.assertEqual(
            apply_realtime_execution_overrides("projector_control", {"action": "off"}),
            {"action": "off"},
        )

    def test_missing_realtime_and_pet_schemas_are_completed_in_bridge(self):
        realtime = _normalized_schema({"name": "realtime_information", "allowed_actions": []})
        pet = _normalized_schema({"name": "pet_tracking", "allowed_actions": []})
        self.assertIn("action", realtime["properties"])
        self.assertIn("current_time", realtime["properties"]["action"]["enum"])
        self.assertIn("query", realtime["properties"])
        self.assertIn("action", pet["properties"])
        self.assertIn("pet", pet["properties"])

    def test_navigation_list_and_goto_are_unambiguous(self):
        list_description = _tool_description({"name": "navigation_list"})
        goto_description = _tool_description({"name": "navigation_goto"})
        goto_schema = _normalized_schema(
            {
                "name": "navigation_goto",
                "allowed_actions": ["goto", "list"],
                "parameters": {"properties": {"action": {"type": "string"}}},
            }
        )
        self.assertIn("绝对禁止", list_description)
        self.assertIn("不得先调用", goto_description)
        self.assertEqual(goto_schema["properties"]["action"]["enum"], ["goto"])

    def test_concrete_runner_error_is_not_hidden_by_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "move_forward", resources=["base"])
            bridge = LocalSkillBridge(spec_dir=root, execute=True, backend="subprocess")
            payload = {
                "ok": False,
                "skill": "move_forward",
                "resources": ["base"],
                "message": "cmd_vel_subscribers_0",
                "error": "cmd_vel_subscribers_0",
            }
            completed = {
                "returncode": 5,
                "stdout": RUNNER_RESULT_PREFIX + json.dumps(payload) + "\n",
                "stderr": "",
                "error": "skill_runner_exit_5",
            }
            with patch.object(bridge, "_run", return_value=completed):
                result = bridge.invoke("move_forward", {"duration": 2})
            self.assertEqual(result["error"], "cmd_vel_subscribers_0")
            self.assertEqual(result["spoken_summary"], "cmd_vel_subscribers_0")

    def test_authoritative_structured_result_and_summary_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_spec(root, "face_recognition", resources=["camera", "face_db"])
            bridge = LocalSkillBridge(spec_dir=root, execute=True, backend="subprocess")
            structured = {
                "ok": True,
                "status": "matched",
                "result": {"status": "matched", "name": "zhangsan", "score": 0.837},
            }
            completed = self.runner_success(
                "face_recognition",
                spoken_summary="看起来是zhangsan。",
                structured_result=structured,
            )
            with patch.object(bridge, "_run", return_value=completed):
                result = bridge.invoke("face_recognition", {})
            self.assertTrue(result["ok"])
            self.assertTrue(result["executed"])
            self.assertTrue(result["result_is_authoritative"])
            self.assertEqual(result["structured_result"], structured)
            self.assertEqual(result["spoken_summary"], "看起来是zhangsan。")

    def test_runner_builds_face_summary_from_parsed_result(self):
        class Policy:
            def step_summary(self, step, parsed):
                face = parsed["result"]
                return f"看起来是{face['name']}。" if face["status"] == "matched" else ""

        executor = type("Executor", (), {"speech_policy": Policy()})()
        step = type("Step", (), {"skill_name": "face_recognition", "arguments": {}})()
        parsed = {
            "ok": True,
            "status": "matched",
            "result": {"status": "matched", "name": "zhangsan", "score": 0.837},
        }
        raw_result = {"ok": True, "parsed_json": parsed, "error": ""}
        self.assertEqual(extract_structured_result(raw_result), parsed)
        self.assertEqual(build_spoken_summary(executor, step, raw_result), "看起来是zhangsan。")

    def test_failed_result_does_not_use_success_policy_summary(self):
        class Policy:
            def step_summary(self, step, parsed):
                return "这句不应被使用"

        executor = type("Executor", (), {"speech_policy": Policy()})()
        step = type("Step", (), {"skill_name": "face_recognition", "arguments": {}})()
        raw_result = {"ok": False, "parsed_json": {"status": "error"}, "error": "camera_open_failed"}
        self.assertEqual(
            build_spoken_summary(executor, step, raw_result),
            "摄像头这次没能正常打开，所以没有继续。",
        )

    def test_oversized_structured_result_is_bounded(self):
        parsed = {
            "status": "matched",
            "result": {"status": "matched", "name": "zhangsan", "debug": "x" * 20000},
            "debug": "x" * 20000,
        }
        compact = extract_structured_result({"parsed_json": parsed})
        self.assertTrue(compact["_truncated"])
        self.assertEqual(compact["result"]["name"], "zhangsan")
        self.assertNotIn("debug", compact)


if __name__ == "__main__":
    unittest.main()
