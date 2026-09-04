from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from local_skills import (
    SCENARIO_TOOL_NAME,
    LocalSkillBridge,
    _explicit_feeder_task,
    _explicit_projector_task,
    _recover_explicit_sequence_tasks,
)
from scenario_engine import ScenarioCatalog, ScenarioError


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scenarios" / "procedure_catalog.json"
SPECS = ROOT / "robot_skills" / "config" / "skill_specs"


class ParameterizedIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = ScenarioCatalog(CATALOG)

    def active(self, normalized: dict) -> list[tuple[str, str, dict]]:
        plan = self.catalog.compile_intent(normalized)
        arguments = normalized["parameters"]
        return [
            (step["skill"], step["action"], step["arguments"])
            for step in plan["steps"]
            if self.catalog._step_enabled(step, arguments)
        ]

    def normalize(self, text: str, **model_arguments) -> dict:
        return self.catalog.normalize_intent(
            "meeting_projection", model_arguments, text
        )

    def test_default_meeting_template_is_unchanged(self) -> None:
        normalized = self.normalize("我要开会了")
        self.assertEqual(
            self.active(normalized),
            [
                ("navigation_goto", "goto", {"point": "study_projection"}),
                ("head_control", "up", {}),
                ("projector_control", "meeting_presentation_on", {}),
            ],
        )

    def test_all_existing_default_scene_step_order_is_preserved(self) -> None:
        expected = {
            "homecoming_welcome": ["head_control:down", "welcome_projection:play", "head_control:level"],
            "push_up_companion": ["navigation_goto:goto", "head_control:up", "push_up:run", "projector_control:off", "head_control:level"],
            "pull_up_companion": ["navigation_goto:goto", "head_control:up", "pull_up:run", "projector_control:off", "head_control:level"],
            "squat_companion": ["navigation_goto:goto", "head_control:up", "squat:run", "projector_control:off", "head_control:level"],
            "find_pet": ["navigation_goto:goto", "pet_tracking:find", "navigation_goto:goto", "pet_tracking:find", "navigation_goto:goto", "pet_tracking:find"],
            "find_pet_here": ["pet_tracking:find"],
            "find_and_feed_doudou": ["navigation_goto:goto", "pet_tracking:find", "navigation_goto:goto", "pet_tracking:find", "navigation_goto:goto", "pet_tracking:find", "feeder_control:feed"],
            "meeting_projection": ["navigation_goto:goto", "head_control:up", "projector_control:meeting_presentation_on"],
            "meeting_projection_stop": ["projector_control:off", "head_control:level"],
            "movie_projection": ["navigation_goto:goto", "head_control:up", "projector_control:on", "media_player:play_movie", "projector_control:off", "head_control:level"],
            "movie_projection_pause": ["media_player:pause"],
            "movie_projection_resume": ["media_player:resume"],
            "movie_projection_stop": ["media_player:stop", "projector_control:off", "head_control:level"],
            "living_room_light_service": ["light_control:on", "navigation_goto:goto", "environment_perception:inspect"],
            "rest_lighting": ["light_control:off", "navigation_goto:goto"],
        }
        for scenario, fingerprint in expected.items():
            with self.subTest(scenario=scenario):
                plan = self.catalog.compile(scenario, {})
                actual = [f"{item['skill']}:{item['action']}" for item in plan["steps"]]
                self.assertEqual(actual, fingerprint)

    def test_explicit_location_overrides_only_location(self) -> None:
        normalized = self.normalize("在客厅播放会议内容")
        self.assertEqual(normalized["parameters"]["point"], "white_wall")
        self.assertTrue(normalized["parameters"]["navigate"])
        self.assertEqual(self.active(normalized)[0][2]["point"], "white_wall")

    def test_current_location_removes_all_base_steps(self) -> None:
        normalized = self.normalize("就在原地开始会议投影")
        self.assertFalse(normalized["parameters"]["navigate"])
        self.assertTrue(normalized["constraints"]["forbid_base_motion"])
        self.assertEqual(
            [item[:2] for item in self.active(normalized)],
            [("head_control", "up"), ("projector_control", "meeting_presentation_on")],
        )

    def test_no_head_overrides_default_head_without_touching_projection(self) -> None:
        normalized = self.normalize("原地播放会议内容，不要抬头")
        self.assertEqual(normalized["parameters"]["head"], "keep")
        self.assertEqual(
            [item[:2] for item in self.active(normalized)],
            [("projector_control", "meeting_presentation_on")],
        )

    def test_model_invented_location_cannot_override_catalog_default(self) -> None:
        normalized = self.normalize("请投影会议内容", point="white_wall")
        self.assertEqual(normalized["parameters"]["point"], "study_projection")

    def test_transport_operations_can_never_compile_start_steps(self) -> None:
        for operation in ("pause", "resume", "stop"):
            with self.subTest(operation=operation):
                normalized = self.normalize(
                    "请投影会议内容", operation=operation
                )
                with self.assertRaisesRegex(ScenarioError, "transport_must_not_start"):
                    self.catalog.compile_intent(normalized)

    def test_transport_wording_normalizes_to_the_correct_operation(self) -> None:
        cases = {
            "暂停会议画面": "pause",
            "继续会议画面": "resume",
            "投影先停掉": "stop",
            "会议投影状态怎么样": "status",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                normalized = self.catalog.normalize_intent(
                    "meeting_projection_stop" if expected == "stop" else "meeting_projection",
                    {},
                    text,
                )
                self.assertEqual(normalized["operation"], expected)

    def test_bare_projector_does_not_guess_meeting_or_movie(self) -> None:
        self.assertIsNone(self.catalog.match("打开投影仪"))
        self.assertEqual(
            _explicit_projector_task("打开投影仪"),
            {"name": "projector_control", "arguments": {"action": "on"}},
        )

    def test_direct_feeding_never_expands_to_pet_patrol(self) -> None:
        for text, expected in (
            ("给豆豆喂十克", {"action": "feed", "grams": 10}),
            ("启动两份投食", {"action": "feed", "portions": 2}),
            ("不要去找豆豆，只投食十克", {"action": "feed", "grams": 10}),
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.catalog.match(text))
                self.assertEqual(_explicit_feeder_task(text)["arguments"], expected)

    def test_find_and_feed_requires_positive_search_semantics(self) -> None:
        self.assertEqual(
            self.catalog.match("帮我看看豆豆在干嘛，他该吃饭了"),
            "find_and_feed_doudou",
        )
        self.assertEqual(
            self.catalog.match("找到豆豆以后给它喂食"),
            "find_and_feed_doudou",
        )

    def test_compiler_rejects_base_resource_under_hard_constraint(self) -> None:
        normalized = self.catalog.normalize_intent(
            "meeting_projection", {}, "原地开始会议投影"
        )
        normalized["parameters"]["navigate"] = True
        normalized["parameters"]["stay_put"] = False
        with self.assertRaisesRegex(ScenarioError, "forbid_base_motion"):
            self.catalog.compile_intent(normalized)

    def test_wrong_model_meeting_transport_is_narrowed_before_execution(self) -> None:
        bridge = LocalSkillBridge(
            spec_dir=SPECS,
            scenario_catalog_path=CATALOG,
            execute=False,
            backend="subprocess",
        )
        with patch.object(
            bridge,
            "_invoke_atomic",
            return_value={"ok": False, "validation_ok": True, "executed": False},
        ) as invoke:
            bridge.invoke(
                SCENARIO_TOOL_NAME,
                {"scenario": "meeting_projection"},
                "暂停会议画面",
            )
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.args[0], "projector_control")
        self.assertEqual(invoke.call_args.args[1], {"action": "meeting_pause"})

    def test_wrong_model_find_and_feed_is_narrowed_to_feeder(self) -> None:
        bridge = LocalSkillBridge(
            spec_dir=SPECS,
            scenario_catalog_path=CATALOG,
            execute=False,
            backend="subprocess",
        )
        with patch.object(
            bridge,
            "_invoke_atomic",
            return_value={"ok": False, "validation_ok": True, "executed": False},
        ) as invoke:
            bridge.invoke(
                SCENARIO_TOOL_NAME,
                {"scenario": "find_and_feed_doudou"},
                "不要去找豆豆，只投食十克",
            )
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.args[0], "feeder_control")
        self.assertEqual(invoke.call_args.args[1], {"action": "feed", "grams": 10})

    def test_only_constraint_blocks_direct_atomic_bypass(self) -> None:
        bridge = LocalSkillBridge(
            spec_dir=SPECS,
            scenario_catalog_path=CATALOG,
            execute=False,
            backend="subprocess",
        )
        with patch.object(bridge, "_invoke_atomic") as invoke:
            result = bridge.invoke(
                "pet_tracking",
                {"action": "find"},
                "不要去找豆豆，只投食十克",
            )
        invoke.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertIn("only_constraint:pet_tracking", result["error"])

    def test_multi_command_order_actions_and_quantity_are_preserved(self) -> None:
        cases = {
            "先给豆豆投食两份，然后打开投影仪": [
                ("feeder_control", {"action": "feed", "portions": 2}),
                ("projector_control", {"action": "on"}),
            ],
            "先打开投影仪，然后暂停会议画面": [
                ("projector_control", {"action": "on"}),
                ("projector_control", {"action": "meeting_pause"}),
            ],
            "先抬头，然后低头，最后恢复平视": [
                ("head_control", {"action": "up"}),
                ("head_control", {"action": "down"}),
                ("head_control", {"action": "level"}),
            ],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                tasks = _recover_explicit_sequence_tasks(text, self.catalog)
                actual = [(item["name"], item["arguments"]) for item in tasks]
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
