from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from local_skills import SEQUENCE_TOOL_NAME, LocalSkillBridge
from scenario_engine import ScenarioCatalog, ScenarioExecutor


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scenarios" / "procedure_catalog.json"


class ScenarioCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ScenarioCatalog(CATALOG)

    def test_home_overlay_is_the_effective_fixed_scene(self):
        home = self.catalog.compile("homecoming_welcome", {})
        self.assertEqual([item["skill"] for item in home["steps"]], ["head_control", "welcome_projection", "head_control"])
        self.assertEqual(home["steps"][1]["arguments"]["duration"], 3)
        self.assertNotIn("navigation_goto", [item["skill"] for item in home["steps"]])
        pushup = self.catalog.compile("push_up_companion", {})
        self.assertEqual(pushup["steps"][0]["arguments"]["point"], "white_wall")
        self.assertEqual(pushup["steps"][2]["arguments"]["duration"], 30)
        self.assertEqual(pushup["steps"][2]["arguments"]["identity_policy"], "face_and_reid")

    def test_vague_scene_phrases_route_to_complete_procedures(self):
        cases = {
            "哈喽理想同学，我下班回来了": "homecoming_welcome",
            "哈喽理想同学": "homecoming_welcome",
            "来陪我运动吧": "push_up_companion",
            "我要开会了": "meeting_projection",
            "把会议投影关掉吧": "meeting_projection_stop",
            "豆豆该吃饭了，你去看看": "find_and_feed_doudou",
            "就在这里找找豆豆": "find_pet_here",
            "只去书房找豆豆": "find_pet_at",
            "客厅太暗了": "living_room_light_service",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), expected)

    def test_model_schema_hides_parallel_light_convenience_scene(self):
        schema = self.catalog.tool_schema["function"]["parameters"]["properties"]["scenario"]["enum"]
        self.assertNotIn("living_room_light_service", schema)
        self.assertEqual(self.catalog.match("客厅太暗了，帮我开灯"), "living_room_light_service")
        self.assertIsNone(self.catalog.match("先去客厅，到了以后把灯打开"))

    def test_scoped_negation_preserves_positive_pet_search(self):
        text = "不要喂豆豆，只去餐厅找一下它，然后告诉我结果"
        self.assertEqual(self.catalog.match(text), "find_pet_at")
        ok, reason = self.catalog.model_scenario_supported(
            "find_pet_at", text, allow_additional_intents=True
        )
        self.assertTrue(ok, reason)
        feed_ok, feed_reason = self.catalog.model_scenario_supported(
            "find_and_feed_doudou", text, allow_additional_intents=True
        )
        self.assertFalse(feed_ok)
        self.assertEqual(feed_reason, "negated_action")

    def test_scenario_alias_normalization_requires_matching_topic(self):
        self.assertEqual(
            self.catalog.normalize_scenario_name(
                "meeting_projection_at_location",
                "导航到书房以后开始会议投影",
            ),
            "meeting_projection",
        )
        self.assertEqual(
            self.catalog.normalize_scenario_name("meeting_projection_at_location", "今天天气怎么样"),
            "meeting_projection_at_location",
        )

    def test_natural_asr_and_homophone_variants_route_across_every_scene(self):
        cases = {
            "哈楼李想同学": "homecoming_welcome",
            "陪我做俯卧成": "push_up_companion",
            "我想练引体想上": "pull_up_companion",
            "陪我做深吨": "squat_companion",
            "帮我找一下豆都": "find_pet",
            "去书放找找豆都": "find_pet_at",
            "原地找一下够": "find_pet_here",
            "找找豆都然后给它喂饭": "find_and_feed_doudou",
            "陪我一起开个会": "meeting_projection",
            "把会易投影关掉": "meeting_projection_stop",
            "客听太按了帮我开灯": "living_room_light_service",
            "我想休息一会儿": "rest_lighting",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), expected)

    def test_model_semantics_extend_language_without_weakening_conflict_checks(self):
        accepted = (
            ("homecoming_welcome", "我下班到家啦"),
            ("push_up_companion", "来一组健身吧"),
            ("pull_up_companion", "去单杠练一组"),
            ("squat_companion", "来一组蹲起"),
            ("find_pet", "小狗跑哪去了，去瞧瞧"),
            ("find_pet_at", "到书房瞧瞧小狗"),
            ("find_pet_here", "在当前位置瞧瞧小狗"),
            ("meeting_projection", "咱们讨论一下工作方案"),
            ("meeting_projection_stop", "把PPT停下来"),
            ("rest_lighting", "我想歇会儿放松一下"),
            ("find_and_feed_doudou", "豆豆饿了，去看看它"),
            ("living_room_light_service", "客厅照明有点不够"),
        )
        for scenario, text in accepted:
            with self.subTest(scenario=scenario, text=text):
                matched = self.catalog.match(text)
                ok, reason = self.catalog.model_scenario_supported(scenario, text, matched=matched)
                self.assertTrue(ok, reason)

        rejected = (
            ("meeting_projection", "今天天气怎么样", "missing_topic_evidence"),
            ("rest_lighting", "不看了，我已经坐了一天了", "explicit_cancellation"),
            ("meeting_projection", "你会开会吗", "capability_question"),
            ("meeting_projection", "好的", "context_required"),
            ("rest_lighting", "陪我一起开个会", "local_conflict"),
        )
        for scenario, text, expected_reason in rejected:
            with self.subTest(scenario=scenario, text=text):
                matched = self.catalog.match(text)
                ok, reason = self.catalog.model_scenario_supported(scenario, text, matched=matched)
                self.assertFalse(ok)
                self.assertEqual(reason, expected_reason)

    def test_homecoming_accepts_common_wake_variants_and_asr_omission(self):
        accepted = (
            "哈喽理想同学",
            "哈啰理想同学",
            "哈罗理想同学",
            "Hello，理想同学。",
            "Hello，理想。",
            "hello 理想",
            "哈喽理想",
            "哈啰理想",
            "你好，理想同学",
            "理想同学，我回来了",
        )
        for text in accepted:
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), "homecoming_welcome")

    def test_generic_replay_language_never_becomes_homecoming(self):
        self.assertIsNone(self.catalog.match("不要打开投食器，先查天气，再播放音乐"))
        self.assertIsNone(self.catalog.match("再播放一首音乐"))
        self.assertTrue(self.catalog.explicit_homecoming_replay("再播放一次欢迎回家画面"))

    def test_leading_sequence_word_does_not_turn_feature_introduction_into_scene(self):
        text = "先介绍一下会议投影功能，再查询机器人当前位置"
        self.assertIsNone(self.catalog.match(text))
        ok, reason = self.catalog.model_scenario_supported(
            "meeting_projection", text, allow_additional_intents=True
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "informational_question")

    def test_homecoming_relaxation_does_not_turn_generic_speech_into_actions(self):
        rejected = (
            "Hello",
            "理想",
            "我有一个理想",
            "理想汽车",
            "Hello李想",
            "你好，我的理想是环游世界",
            "理想同学你有什么功能",
            "你能翻译Hello理想吗",
        )
        for text in rejected:
            with self.subTest(text=text):
                self.assertIsNone(self.catalog.match(text))

    def test_questions_and_negations_never_become_hardware_scenes(self):
        rejected = (
            "俯卧撑怎么做",
            "会议几点开始",
            "什么是会议投影",
            "你会找豆豆吗",
            "介绍一下书房会议投影",
            "不要做俯卧撑",
            "别找豆豆",
            "不要打开客厅灯",
            "不要开始会议投影",
            "别关闭会议投影",
            "不导航去书房",
        )
        for text in rejected:
            with self.subTest(text=text):
                self.assertIsNone(self.catalog.match(text))

    def test_reminder_content_and_scoped_negation_do_not_start_unrequested_scenes(self):
        for text in (
            "提醒我十分钟后开会",
            "帮我设个下午三点开会的提醒",
            "导航到客厅，但不要打开灯",
            "去客厅，灯不用开",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.catalog.match(text))
        self.assertEqual(self.catalog.match("我要开会了"), "meeting_projection")
        self.assertEqual(self.catalog.match("打开客厅灯"), "living_room_light_service")

    def test_room_name_alone_cannot_authorize_light_scene_in_multi_intent_text(self):
        text = "关闭会议投影然后导航到客厅"
        ok, reason = self.catalog.model_scenario_supported(
            "living_room_light_service",
            text,
            allow_additional_intents=True,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_topic_evidence")

        ok, reason = self.catalog.model_scenario_supported(
            "living_room_light_service",
            "先去客厅然后打开灯光",
            allow_additional_intents=True,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "ordered_lighting_requires_atomic_sequence")

    def test_projection_synonyms_and_scoped_negation(self):
        for text in ("把会议画面收起来，再去客厅", "投影先停掉，接着回客厅", "先关投影，然后喂豆豆"):
            with self.subTest(text=text):
                ok, reason = self.catalog.model_scenario_supported(
                    "meeting_projection_stop",
                    text,
                    allow_additional_intents=True,
                )
                self.assertTrue(ok, reason)

        self.assertIsNone(self.catalog.match("去书房，不要投影"))
        ok, reason = self.catalog.model_scenario_supported("meeting_projection", "去书房，不要投影")
        self.assertFalse(ok)
        self.assertEqual(reason, "negated_projection_start")

        accepted_requests = {
            "你能不能陪我做俯卧撑": "push_up_companion",
            "帮我看看豆豆在哪里": "find_pet",
            "不用身份识别，陪我做俯卧撑": "push_up_companion",
            "不看电影了，陪我运动吧": "push_up_companion",
        }
        for text, expected in accepted_requests.items():
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), expected)

    def test_protected_atomic_calls_are_forced_through_scenarios(self):
        self.assertEqual(self.catalog.protected_scenario("push_up", {"action": "run"}), "push_up_companion")
        self.assertEqual(
            self.catalog.protected_scenario("projector_control", {"action": "meeting_presentation_on"}),
            "meeting_projection",
        )
        self.assertEqual(
            self.catalog.protected_scenario("projector_control", {"action": "off"}),
            "meeting_projection_stop",
        )

    def test_compiled_plan_always_contains_outcome_group(self):
        for name in self.catalog.procedures:
            with self.subTest(name=name):
                arguments = {"point": "white_wall"} if name == "find_pet_at" else {}
                plan = self.catalog.compile(name, arguments)
                self.assertTrue(plan["steps"])
                self.assertEqual(plan["outcome_groups"][0]["procedure"], name)

    def test_every_procedure_uses_only_the_three_saved_points(self):
        allowed = {"origin", "white_wall", "study_projection"}
        for name in self.catalog.procedures:
            arguments = {"point": "white_wall"} if name == "find_pet_at" else {}
            with self.subTest(name=name):
                plan = self.catalog.compile(name, arguments)
                points = [
                    step["arguments"].get("point")
                    for step in plan["steps"]
                    if step["skill"] == "navigation_goto"
                ]
                self.assertTrue(all(point in allowed for point in points), points)

    def test_all_fitness_scenes_default_to_identity_and_reid(self):
        for name in ("push_up_companion", "pull_up_companion", "squat_companion"):
            with self.subTest(name=name):
                plan = self.catalog.compile(name, {})
                fitness = next(
                    step for step in plan["steps"]
                    if step["skill"] in {"push_up", "pull_up", "squat"}
                )
                self.assertEqual(fitness["arguments"]["identity_policy"], "face_and_reid")


class ScenarioExecutorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ScenarioCatalog(CATALOG)

    def test_scene_emits_start_and_stage_speech_without_changing_step_order(self):
        calls: list[tuple[str, dict]] = []
        events: list[dict] = []

        def invoke(skill, arguments):
            calls.append((skill, arguments))
            return {"ok": True, "validation_ok": True, "executed": True, "error": None}

        result = ScenarioExecutor(
            self.catalog,
            invoke,
            progress_callback=events.append,
        ).execute("meeting_projection", {})

        self.assertTrue(result["ok"])
        self.assertEqual(
            [item[0] for item in calls],
            ["navigation_goto", "head_control", "projector_control"],
        )
        self.assertEqual(events[0]["kind"], "acknowledgement")
        self.assertIn("先去书房", events[0]["text"])
        self.assertEqual([item["kind"] for item in events[1:]], ["progress", "progress"])
        self.assertIn("调整", events[1]["text"])
        self.assertIn("会议投影", events[2]["text"])

    def test_scene_announcement_can_be_suppressed_inside_a_larger_sequence(self):
        events: list[dict] = []
        result = ScenarioExecutor(
            self.catalog,
            lambda _skill, _arguments: {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "error": None,
            },
            progress_callback=events.append,
        ).execute("meeting_projection", {}, announce=False)

        self.assertTrue(result["ok"])
        self.assertNotIn("acknowledgement", [item["kind"] for item in events])
        self.assertEqual([item["kind"] for item in events], ["progress", "progress"])

    def test_dry_run_validates_complete_meeting_without_execution(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, arguments))
            return {"ok": False, "validation_ok": True, "executed": False, "error": "dry_run_only_not_executed"}

        result = ScenarioExecutor(self.catalog, invoke).execute("meeting_projection", {})
        self.assertTrue(result["validation_ok"])
        self.assertFalse(result["executed"])
        self.assertEqual([item[0] for item in calls], ["navigation_goto", "head_control", "projector_control"])
        self.assertTrue(result["outcome_groups"])
        self.assertIn("没有实际执行", result["spoken_summary"])

    def test_head_failure_skips_count_and_reports_real_stage(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, arguments))
            if skill == "head_control" and arguments.get("action") == "up":
                return {"ok": False, "validation_ok": False, "executed": False, "error": "HEAD_TARGET_TIMEOUT"}
            return {"ok": True, "validation_ok": True, "executed": True, "error": None}

        result = ScenarioExecutor(self.catalog, invoke).execute("push_up_companion", {})
        count = next(item for item in result["steps"] if item["id"] == "count")
        self.assertTrue(count["skipped"])
        self.assertNotIn("push_up", [item[0] for item in calls])
        self.assertEqual(result["outcome_groups"][0]["matched_outcome"], "preparation_failed")
        self.assertIn("抬头准备没有完成", result["spoken_summary"])

    def test_default_pet_search_visits_all_three_points_and_stops_when_found(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, dict(arguments)))
            found = skill == "pet_tracking" and arguments.get("action") == "find" and len(
                [item for item in calls if item[0] == "pet_tracking"]
            ) == 2
            return {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "structured_result": {"found": found},
            }

        result = ScenarioExecutor(self.catalog, invoke).execute("find_pet", {})
        points = [args["point"] for skill, args in calls if skill == "navigation_goto"]
        self.assertEqual(points, ["white_wall", "study_projection"])
        self.assertNotIn("origin", points)
        self.assertEqual(result["outcome_groups"][0]["matched_outcome"], "found_study")

    def test_anonymous_fitness_is_only_inferred_from_explicit_opt_out(self):
        self.assertEqual(
            self.catalog.infer_arguments("push_up_companion", "陪我运动，不用人脸识别和ReID"),
            {"identity_policy": "anonymous"},
        )
        self.assertEqual(self.catalog.infer_arguments("push_up_companion", "陪我运动"), {})

    def test_find_pet_at_infers_only_the_named_saved_point(self):
        for text, point in (("去书房找豆豆", "study_projection"), ("只去书房找一下豆豆", "study_projection"), ("去客厅找狗", "white_wall"), ("去餐厅看看豆豆", "origin")):
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), "find_pet_at")
                self.assertEqual(self.catalog.infer_arguments("find_pet_at", text)["point"], point)

    def test_meeting_projection_infers_stay_put_from_explicit_location_language(self):
        phrases = (
            "你就在原地头，然后开始播放会议内容吧",
            "就在当前位置抬头投影",
            "不要导航，直接播放会议PPT",
            "不用去书房，在这里开会",
        )
        for text in phrases:
            with self.subTest(text=text):
                self.assertEqual(self.catalog.match(text), "meeting_projection")
                self.assertTrue(
                    self.catalog.infer_arguments("meeting_projection", text)["stay_put"]
                )

    def test_meeting_projection_here_skips_navigation_but_keeps_protected_order(self):
        calls = []
        events = []

        def invoke(skill, arguments):
            calls.append((skill, dict(arguments)))
            return {"ok": True, "validation_ok": True, "executed": True}

        result = ScenarioExecutor(
            self.catalog,
            invoke,
            progress_callback=events.append,
        ).execute("meeting_projection", {"stay_put": True})

        self.assertTrue(result["ok"])
        self.assertEqual(
            [skill for skill, _arguments in calls],
            ["head_control", "projector_control"],
        )
        navigate = next(item for item in result["steps"] if item["id"] == "navigate")
        self.assertTrue(navigate["intentional_skip"])
        self.assertEqual(result["outcome_groups"][0]["matched_outcome"], "all_success_here")
        self.assertIn("当前位置", events[0]["text"])
        self.assertNotIn("书房", events[0]["text"])

    def test_default_meeting_projection_still_navigates_to_study(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, dict(arguments)))
            return {"ok": True, "validation_ok": True, "executed": True}

        result = ScenarioExecutor(self.catalog, invoke).execute("meeting_projection", {})
        self.assertTrue(result["ok"])
        self.assertEqual(
            [skill for skill, _arguments in calls],
            ["navigation_goto", "head_control", "projector_control"],
        )
        self.assertEqual(calls[0][1]["point"], "study_projection")

    def test_dry_run_validates_every_conditional_pet_branch(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, dict(arguments)))
            return {
                "ok": False,
                "validation_ok": True,
                "executed": False,
                "error": "dry_run_only_not_executed",
            }

        result = ScenarioExecutor(self.catalog, invoke).execute("find_and_feed_doudou", {})
        self.assertTrue(result["validation_ok"])
        self.assertFalse(result["executed"])
        self.assertEqual(len(calls), 7)
        self.assertEqual([skill for skill, _ in calls].count("navigation_goto"), 3)
        self.assertIn("feeder_control", [skill for skill, _ in calls])

    def test_successful_fitness_speaks_result_once_and_adds_one_care_reminder(self):
        def invoke(skill, arguments):
            result = {"ok": True, "validation_ok": True, "executed": True}
            if skill == "push_up":
                result["spoken_summary"] = "运动结束，你一共完成了九个俯卧撑。"
                result["structured_result"] = {"count": 9}
            return result

        result = ScenarioExecutor(self.catalog, invoke).execute("push_up_companion", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["spoken_summary"].count("九个俯卧撑"), 1)
        self.assertEqual(result["spoken_summary"].count("喝口水"), 1)
        self.assertNotIn("上传视频", result["spoken_summary"])

    def test_pet_not_found_never_feeds(self):
        calls = []

        def invoke(skill, arguments):
            calls.append((skill, dict(arguments)))
            payload = {"ok": True, "validation_ok": True, "executed": True}
            if skill == "pet_tracking":
                payload["structured_result"] = {"found": False}
            return payload

        result = ScenarioExecutor(self.catalog, invoke).execute("find_and_feed_doudou", {})
        self.assertNotIn("feeder_control", [skill for skill, _ in calls])
        self.assertEqual(result["outcome_groups"][0]["matched_outcome"], "not_found")
        self.assertIn("没有找到豆豆", result["spoken_summary"])

    def test_meeting_navigation_failure_prevents_head_and_projection(self):
        calls = []

        def invoke(skill, arguments):
            calls.append(skill)
            if skill == "navigation_goto":
                return {"ok": False, "validation_ok": False, "executed": False, "error": "NAV_FAILED"}
            return {"ok": True, "validation_ok": True, "executed": True}

        result = ScenarioExecutor(self.catalog, invoke).execute("meeting_projection", {})
        self.assertEqual(calls, ["navigation_goto"])
        self.assertEqual(result["outcome_groups"][0]["matched_outcome"], "navigation_failed")
        self.assertIn("没有抬头或开始投影", result["spoken_summary"])

    def test_every_homecoming_failure_stage_has_a_specific_outcome(self):
        cases = (
            (("head_control", "down"), "head_down_failed", "没有正常低头"),
            (("welcome_projection", "play"), "projection_failed", "欢迎画面没有正常播放"),
            (("head_control", "level"), "head_level_failed", "没有正常恢复平视"),
        )
        for failed, expected, phrase in cases:
            with self.subTest(failed=failed):
                def invoke(skill, arguments):
                    if (skill, arguments.get("action")) == failed:
                        return {"ok": False, "validation_ok": False, "executed": False, "error": "INJECTED"}
                    return {"ok": True, "validation_ok": True, "executed": True}

                result = ScenarioExecutor(self.catalog, invoke).execute("homecoming_welcome", {})
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)
                self.assertIn(phrase, result["spoken_summary"])

    def test_every_meeting_failure_stage_stops_or_reports_correctly(self):
        cases = (
            (("navigation_goto", "goto"), "navigation_failed"),
            (("head_control", "up"), "head_failed"),
            (("projector_control", "meeting_presentation_on"), "projection_failed"),
        )
        for failed, expected in cases:
            with self.subTest(failed=failed):
                calls = []

                def invoke(skill, arguments):
                    calls.append((skill, arguments.get("action")))
                    if calls[-1] == failed:
                        return {"ok": False, "validation_ok": False, "executed": False, "error": "INJECTED"}
                    return {"ok": True, "validation_ok": True, "executed": True}

                result = ScenarioExecutor(self.catalog, invoke).execute("meeting_projection", {})
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)
                if failed[0] != "projector_control":
                    self.assertNotIn(("projector_control", "meeting_presentation_on"), calls)

    def test_every_fitness_failure_stage_has_no_false_success(self):
        cases = (
            (("navigation_goto", "goto"), "navigation_failed"),
            (("head_control", "up"), "preparation_failed"),
            (("push_up", "run"), "count_failed"),
            (("projector_control", "off"), "cleanup_failed"),
            (("head_control", "level"), "cleanup_failed"),
        )
        for failed, expected in cases:
            with self.subTest(failed=failed):
                def invoke(skill, arguments):
                    if (skill, arguments.get("action")) == failed:
                        return {"ok": False, "validation_ok": False, "executed": False, "error": "INJECTED"}
                    return {"ok": True, "validation_ok": True, "executed": True}

                result = ScenarioExecutor(self.catalog, invoke).execute("push_up_companion", {})
                self.assertFalse(result["ok"])
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)
                self.assertNotIn("已经完成", result["spoken_summary"])

    def test_pet_found_at_each_point_stops_search_and_feeds_once(self):
        cases = ((1, "found_living_and_fed", 1), (2, "found_study_and_fed", 2), (3, "found_dining_and_fed", 3))
        for found_index, expected, navigation_count in cases:
            with self.subTest(found_index=found_index):
                searches = 0
                calls = []

                def invoke(skill, arguments):
                    nonlocal searches
                    calls.append((skill, dict(arguments)))
                    result = {"ok": True, "validation_ok": True, "executed": True}
                    if skill == "pet_tracking":
                        searches += 1
                        result["structured_result"] = {"found": searches == found_index}
                    return result

                result = ScenarioExecutor(self.catalog, invoke).execute("find_and_feed_doudou", {})
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)
                self.assertEqual(sum(skill == "navigation_goto" for skill, _ in calls), navigation_count)
                self.assertEqual(sum(skill == "feeder_control" for skill, _ in calls), 1)

    def test_each_feeder_failure_reason_is_reported_without_false_feeding(self):
        for reason, expected in (
            ("auth_expired_or_invalid", "feed_auth_failed"),
            ("device_offline", "feed_device_offline"),
            ("unknown", "feed_failed"),
        ):
            with self.subTest(reason=reason):
                def invoke(skill, arguments):
                    if skill == "pet_tracking":
                        return {"ok": True, "validation_ok": True, "executed": True, "structured_result": {"found": True}}
                    if skill == "feeder_control":
                        return {
                            "ok": False,
                            "validation_ok": False,
                            "executed": False,
                            "error": reason,
                            "structured_result": {"failure_reason": reason},
                        }
                    return {"ok": True, "validation_ok": True, "executed": True}

                result = ScenarioExecutor(self.catalog, invoke).execute("find_and_feed_doudou", {})
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)
                self.assertIn("没有投喂" if reason != "unknown" else "没有成功出粮", result["spoken_summary"])

    def test_all_rest_light_navigation_combinations_are_truthful(self):
        cases = ((False, False, "success"), (True, False, "light_failed"), (False, True, "navigation_failed"), (True, True, "both_failed"))
        for light_fails, navigation_fails, expected in cases:
            with self.subTest(light_fails=light_fails, navigation_fails=navigation_fails):
                def invoke(skill, arguments):
                    failed = (skill == "light_control" and light_fails) or (skill == "navigation_goto" and navigation_fails)
                    return {
                        "ok": not failed,
                        "validation_ok": not failed,
                        "executed": not failed,
                        "error": "INJECTED" if failed else None,
                    }

                result = ScenarioExecutor(self.catalog, invoke).execute("rest_lighting", {})
                self.assertEqual(result["outcome_groups"][0]["matched_outcome"], expected)


class ScenarioBridgeBoundaryTests(unittest.TestCase):
    def make_bridge(self, root: Path) -> LocalSkillBridge:
        for name in (
            "navigation_goto",
            "head_control",
            "projector_control",
            "push_up",
            "light_control",
            "reminder_schedule",
        ):
            spec = {
                "name": name,
                "description_zh": name,
                "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "point": {"type": "string"}}},
                "allowed_actions": [],
                "resources": [],
            }
            (root / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
        return LocalSkillBridge(spec_dir=root, scenario_catalog_path=CATALOG)

    def test_protected_atomic_tools_are_not_exposed_to_model(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            names = [item["function"]["name"] for item in bridge.tool_schemas]
            self.assertIn("run_robot_scenario", names)
            self.assertIn("navigation_goto", names)
            self.assertNotIn("projector_control", names)
            self.assertNotIn("push_up", names)

    def test_sequence_keeps_a_protected_scene_whole_then_runs_next_atomic_task(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "scenario": "meeting_projection",
                "spoken_summary": "会议投影已经准备好了。",
            }
            light_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "skill": "light_control",
                "spoken_summary": "灯光已经打开。",
            }
            with (
                patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as scene,
                patch.object(bridge, "_invoke_atomic", return_value=light_result) as atomic,
            ):
                result = bridge.invoke(
                    SEQUENCE_TOOL_NAME,
                    {
                        "tasks": [
                            {
                                "name": "run_robot_scenario",
                                "arguments": {"scenario": "meeting_projection"},
                            },
                            {"name": "light_control", "arguments": {"action": "on"}},
                        ]
                    },
                    "先帮我准备会议投影，然后打开灯光",
                    "turn-sequence",
                )

            self.assertTrue(result["ok"])
            scene.assert_called_once_with("meeting_projection", {}, announce=False)
            atomic.assert_called_once_with(
                "light_control",
                {"action": "on"},
                "先帮我准备会议投影，然后打开灯光",
                announce=False,
            )
            self.assertEqual([item["name"] for item in result["tasks"]], ["run_robot_scenario", "light_control"])

    def test_unrelated_topic_words_cannot_replace_the_requested_atomic_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            reminder_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "skill": "reminder_schedule",
                "spoken_summary": "提醒已经设好。",
            }
            navigation_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "skill": "navigation_goto",
                "spoken_summary": "已经到达客厅。",
            }
            with (
                patch.object(bridge.scenario_executor, "execute") as scene,
                patch.object(
                    bridge,
                    "_invoke_atomic",
                    side_effect=[reminder_result, navigation_result],
                ) as atomic,
            ):
                reminder = bridge.invoke(
                    "reminder_schedule",
                    {"content": "开会", "delay_seconds": 600},
                    "提醒我十分钟后开会",
                )
                navigation = bridge.invoke(
                    "navigation_goto",
                    {"point": "white_wall"},
                    "导航到客厅，但不要打开灯",
                )

            scene.assert_not_called()
            self.assertEqual(atomic.call_count, 2)
            self.assertEqual(reminder["skill"], "reminder_schedule")
            self.assertEqual(navigation["skill"], "navigation_goto")

    def test_scene_transcript_overrides_atomic_plan_and_deduplicates_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True, "validation_ok": True, "executed": True,
                "scenario": "meeting_projection", "spoken_summary": "已投影好会议内容。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                first = bridge.invoke("navigation_goto", {"point": "study_projection"}, "我要开会了", "turn-1")
                second = bridge.invoke("head_control", {"action": "up"}, "我要开会了", "turn-1")
            execute.assert_called_once_with("meeting_projection", {})
            self.assertEqual(first["scenario"], "meeting_projection")
            self.assertTrue(second["deduplicated"])

    def test_homecoming_runs_once_but_explicit_replay_runs_again(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "scenario": "homecoming_welcome",
                "spoken_summary": "欢迎画面播放好了。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                first = bridge.invoke("navigation_goto", {}, "哈喽理想同学", "turn-1")
                second = bridge.invoke("navigation_goto", {}, "哈喽理想同学", "turn-2")
                replay = bridge.invoke("navigation_goto", {}, "再播放一次欢迎画面", "turn-3")
            self.assertEqual(first["scenario"], "homecoming_welcome")
            self.assertEqual(second["mode"], "greeting_only")
            self.assertFalse(second["device_state_changed"])
            self.assertEqual(replay["scenario"], "homecoming_welcome")
            self.assertEqual(execute.call_count, 2)

    def test_model_cannot_invent_a_scene_not_supported_by_the_utterance(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            result = bridge.invoke(
                "run_robot_scenario",
                {"scenario": "rest_lighting"},
                "不看了，我已经坐了一天了",
                "turn-off-script",
            )
            self.assertFalse(result["executed"])
            self.assertEqual(result["mode"], "intent_rejected")
            self.assertEqual(result["routing_reason"], "explicit_cancellation")

    def test_explicit_stay_put_transcript_overrides_model_default_false(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "scenario": "meeting_projection",
                "spoken_summary": "已经在当前位置投影好了。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                result = bridge.invoke(
                    "run_robot_scenario",
                    {"scenario": "meeting_projection", "stay_put": False},
                    "不要导航，就在这里抬头播放会议内容",
                    "turn-stay-put",
                )
            self.assertTrue(result["executed"])
            execute.assert_called_once_with("meeting_projection", {"stay_put": True})

    def test_qwen_semantic_scene_choice_is_accepted_with_local_topic_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "scenario": "meeting_projection",
                "spoken_summary": "已经投影好会议内容了。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                result = bridge.invoke(
                    "run_robot_scenario",
                    {"scenario": "meeting_projection"},
                    "咱们讨论一下工作方案",
                    "turn-semantic",
                )
            self.assertTrue(result["executed"])
            self.assertEqual(
                result["routing"]["source"],
                "qwen_semantic_with_local_evidence",
            )
            execute.assert_called_once_with("meeting_projection", {})

    def test_phonetic_scene_overrides_partial_atomic_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "scenario": "meeting_projection",
                "spoken_summary": "已经投影好会议内容了。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                first = bridge.invoke(
                    "navigation_goto",
                    {"point": "study_projection"},
                    "陪我一起开个会",
                    "turn-phonetic",
                )
                second = bridge.invoke(
                    "head_control",
                    {"action": "up"},
                    "陪我一起开个会",
                    "turn-phonetic",
                )
            self.assertEqual(first["scenario"], "meeting_projection")
            self.assertTrue(second["deduplicated"])
            execute.assert_called_once_with("meeting_projection", {})

    def test_short_affirmation_can_use_only_the_previous_assistant_offer(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.make_bridge(Path(directory))
            scene_result = {
                "ok": True, "validation_ok": True, "executed": True,
                "scenario": "push_up_companion", "spoken_summary": "运动完成。",
            }
            with patch.object(bridge.scenario_executor, "execute", return_value=scene_result) as execute:
                accepted = bridge.invoke(
                    "run_robot_scenario",
                    {"scenario": "push_up_companion"},
                    "好的",
                    "turn-yes",
                    "那我陪你运动一下吧？",
                )
                rejected = bridge.invoke(
                    "run_robot_scenario",
                    {"scenario": "meeting_projection"},
                    "好的",
                    "turn-wrong",
                    "那我陪你运动一下吧？",
                )
            self.assertTrue(accepted["executed"])
            self.assertEqual(rejected["mode"], "intent_rejected")
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
