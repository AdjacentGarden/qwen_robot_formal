from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from intent_policy import enforce_turn_tool_policy, normalize_user_intent
from local_skills import _atomic_intent_supported
from memory_store import MemoryStore
from scenario_engine import ScenarioCatalog


ROOT = Path(__file__).resolve().parents[1]


class MemoryIntentPolicyTests(unittest.TestCase):
    def test_retrospective_queries_are_read_only(self) -> None:
        cases = (
            "我上一轮做了多少个俯卧撑？",
            "上次俯卧撑一共数了几个？",
            "刚才运动计数的结果是什么？",
            "上一条让你执行的指令是什么？",
            "往前数两轮，我让你做了什么？",
            "最近前两轮我让你执行了哪些任务？",
            "最开始让你执行的指令是什么？",
            "从最早开始数，第二条指令是什么？",
            "今天我让你执行过哪些指令？",
            "昨天我几点叫你做过运动？",
            "前天执行过什么任务？",
            "你记不记得我上一次让你做过什么？",
            "帮我查一下刚才完成的俯卧撑结果。",
            "之前那次深蹲完成了多少个？",
            "我上一轮的运动计数是多少？",
            "上次找豆豆找到了吗？",
            "刚才寻找豆豆的结果怎么样？",
            "之前给豆豆喂食成功了吗？",
            "上一次会议投影成功了吗？",
            "刚才会议投影为什么失败？",
            "之前会议画面结束了吗？",
            "上次导航到书房成功了吗？",
            "刚才导航为什么失败？",
            "上一次电影播放到哪里了？",
            "刚才音乐结束了吗？",
            "之前抬头成功了吗？",
            "刚才恢复平视了吗？",
            "上次客厅关灯成功了吗？",
        )
        for text in cases:
            with self.subTest(text=text):
                intent = normalize_user_intent(text)
                self.assertEqual(intent["domain"], "memory")
                self.assertEqual(intent["operation"], "query")
                self.assertTrue(intent["constraints"]["forbid_hardware"])
                self.assertEqual(intent["constraints"]["allowed_tools"], ["memory_query"])

    def test_present_and_prospective_requests_are_not_blocked(self) -> None:
        cases = (
            "现在陪我做俯卧撑。",
            "继续上次没做完的俯卧撑。",
            "把刚才的运动重新做一遍。",
            "上次那组运动再来一组。",
            "请开始三十秒俯卧撑计数。",
            "今天我应该做几个俯卧撑比较合适？",
            "我想知道每天做多少个俯卧撑合适。",
            "帮我数一下俯卧撑。",
            "我准备开始运动了。",
            "再做一次刚才的运动。",
            "导航到书房。",
            "抬头然后恢复平视。",
            "重新开始会议投影。",
            "把上次的会议投影再播放一次。",
            "继续刚才寻找豆豆的任务。",
            "把上次找狗流程再来一次。",
            "重新导航到书房。",
            "继续播放刚才的电影。",
            "再抬一次头。",
            "重新关闭客厅灯。",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertNotEqual(normalize_user_intent(text)["domain"], "memory")

    def test_query_parameters_are_structured(self) -> None:
        matrix = (
            ("上一条指令是什么", "latest", {}),
            ("往前数两轮的指令是什么", "offset", {"offset": 1}),
            ("最近前两轮有哪些任务", "recent", {"limit": 2}),
            ("最开始让我执行的指令是什么", "first", {}),
            ("第二条指令是什么", "ordinal", {"position": 2}),
            ("昨天让我执行过哪些指令", "time_range", {"date_period": "yesterday"}),
            ("上一次俯卧撑做了几个", "search", {"query": "俯卧撑", "limit": 1}),
            ("上次找豆豆找到了吗", "search", {"query": "豆豆", "limit": 1}),
            ("上一次会议投影成功了吗", "search", {"query": "会议", "limit": 1}),
            ("刚才导航到书房成功了吗", "search", {"query": "导航", "limit": 1}),
        )
        for text, query_type, expected in matrix:
            with self.subTest(text=text):
                arguments = normalize_user_intent(text)["parameters"]
                self.assertEqual(arguments["scope"], "command_history")
                self.assertEqual(arguments["query_type"], query_type)
                for key, value in expected.items():
                    self.assertEqual(arguments[key], value)

    def test_final_policy_rewrites_wrong_hardware_call(self) -> None:
        intent = normalize_user_intent("我上一轮做了多少个俯卧撑？")
        name, arguments, reason = enforce_turn_tool_policy(
            intent,
            "run_robot_scenario",
            {"scenario": "push_up_companion"},
        )
        self.assertEqual(name, "memory_query")
        self.assertEqual(arguments["query_type"], "search")
        self.assertEqual(arguments["query"], "俯卧撑")
        self.assertEqual(reason, "retrospective_query_forced_read_only")

    def test_runtime_dispatch_boundary_never_reaches_hardware_bridge(self) -> None:
        try:
            from realtime_chat import JsonLogger, RealtimeConversation, parser as runtime_parser
        except ModuleNotFoundError as exc:
            self.skipTest(f"partial local snapshot: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = runtime_parser().parse_args(["--no-local-skills", "--no-reconnect"])
            args.memory_dir = root / "memory"
            client = RealtimeConversation(args, "unused-offline-key", JsonLogger(root / "events.jsonl"))
            client.user_turn_id = 1
            text = "我上一轮做了多少个俯卧撑？"
            client.committed_user_turns[1] = {
                "text": text,
                "normalized_intent": normalize_user_intent(text),
            }
            result = asyncio.run(
                client.handle_function_call(
                    {
                        "call_id": "wrong-model-scene-call",
                        "name": "run_robot_scenario",
                        "arguments": '{"scenario":"push_up_companion"}',
                        "_synthetic_local": True,
                    },
                    turn_id=1,
                    user_text=text,
                )
            )
            self.assertEqual(result["skill"], "memory_query")
            self.assertEqual(result["mode"], "local_memory")
            self.assertFalse(result["device_state_changed"])
            self.assertIsNone(client.skill_bridge)

    def test_atomic_and_scene_guards_reject_reexecution(self) -> None:
        text = "我上一轮做了多少个俯卧撑？"
        supported, reason = _atomic_intent_supported("push_up", {"action": "run"}, text)
        self.assertFalse(supported)
        self.assertEqual(reason, "retrospective_query_must_use_memory")
        catalog = ScenarioCatalog(ROOT / "scenarios" / "procedure_catalog.json")
        self.assertIsNone(catalog.match(text))
        supported, reason = catalog.model_scenario_supported("push_up_companion", text)
        self.assertFalse(supported)
        self.assertEqual(reason, "retrospective_query_must_use_memory")

    def test_existing_scene_routes_are_unchanged(self) -> None:
        catalog = ScenarioCatalog(ROOT / "scenarios" / "procedure_catalog.json")
        expected = {
            "陪我做俯卧撑": "push_up_companion",
            "我想做运动了": "push_up_companion",
            "我要开会了": "meeting_projection",
            "请结束会议投影": "meeting_projection_stop",
            "帮我看看豆豆在哪": "find_pet",
            "哈喽理想同学": "homecoming_welcome",
        }
        for text, scenario in expected.items():
            with self.subTest(text=text):
                self.assertEqual(catalog.match(text), scenario)

    def test_latest_matching_exercise_result_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory))
            rows = (
                ("陪我做俯卧撑", "run_robot_scenario", {"scenario": "push_up_companion"}, "完成了9个俯卧撑"),
                ("导航到书房", "navigation_goto", {"point": "study_projection"}, "已到达书房"),
            )
            for turn, (text, skill, arguments, summary) in enumerate(rows, 1):
                store.record_command(
                    user_text=text,
                    session_id="intent-policy-test",
                    turn_id=turn,
                    skill=skill,
                    arguments=arguments,
                    result={"ok": True, "executed": True, "spoken_summary": summary},
                    received_at=1000 + turn,
                )
            query = normalize_user_intent("我上一次俯卧撑做了几个？")["parameters"]
            result = store.invoke("memory_query", query)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["commands"]), 1)
            self.assertEqual(result["commands"][0]["text"], "陪我做俯卧撑")
            self.assertIn("9个", result["commands"][0]["calls"][0]["summary"])

    def test_meeting_pet_and_navigation_history_use_matching_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory))
            rows = (
                ("我要开会了", "run_robot_scenario", {"scenario": "meeting_projection"}, "会议投影已经完成"),
                ("帮我看看豆豆在哪", "run_robot_scenario", {"scenario": "find_pet"}, "已经找到豆豆"),
                ("导航到书房", "navigation_goto", {"point": "study_projection"}, "导航已经到达书房"),
                ("播放一首音乐", "media_player", {"action": "play_music"}, "音乐已经开始播放"),
            )
            for turn, (text, skill, arguments, summary) in enumerate(rows, 1):
                store.record_command(
                    user_text=text,
                    session_id="multi-domain-memory-test",
                    turn_id=turn,
                    skill=skill,
                    arguments=arguments,
                    result={"ok": True, "executed": True, "spoken_summary": summary},
                    received_at=2000 + turn,
                )
            matrix = (
                ("上一次会议投影成功了吗？", "我要开会了"),
                ("上次找豆豆找到了吗？", "帮我看看豆豆在哪"),
                ("刚才导航到书房成功了吗？", "导航到书房"),
                ("上一次音乐播放成功了吗？", "播放一首音乐"),
            )
            for text, expected in matrix:
                with self.subTest(text=text):
                    result = store.invoke("memory_query", normalize_user_intent(text)["parameters"])
                    self.assertEqual(result["commands"][0]["text"], expected)


if __name__ == "__main__":
    unittest.main()
