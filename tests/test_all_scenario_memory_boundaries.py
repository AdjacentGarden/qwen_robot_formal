from __future__ import annotations

import unittest
from pathlib import Path

from intent_policy import enforce_turn_tool_policy, normalize_user_intent
from scenario_engine import ScenarioCatalog


ROOT = Path(__file__).resolve().parents[1]


CASES = {
    "homecoming_welcome": {
        "execute": ("哈喽理想同学", "再播放一次欢迎回家"),
        "history": ("上次欢迎回家画面播放成功了吗？", "刚才欢迎画面的结果怎么样？"),
    },
    "living_room_light_service": {
        "execute": ("天黑了，帮我打开客厅的灯", "去客厅帮我开灯"),
        "history": ("上次打开客厅灯成功了吗？", "刚才客厅灯光操作的结果怎么样？"),
    },
    "push_up_companion": {
        "execute": ("陪我做俯卧撑", "帮我数一下俯卧撑"),
        "history": ("上次俯卧撑做了几个？", "刚才俯卧撑计数结果是多少？"),
    },
    "pull_up_companion": {
        "execute": ("陪我做引体向上", "开始做引体向上"),
        "history": ("上次引体向上做了几个？", "刚才引体计数成功了吗？"),
    },
    "squat_companion": {
        "execute": ("陪我做深蹲", "帮我数深蹲"),
        "history": ("上次深蹲做了几个？", "刚才深蹲计数结果怎么样？"),
    },
    "find_pet": {
        "execute": ("找一下豆豆", "看看豆豆在哪里"),
        "history": ("上次找豆豆找到了吗？", "刚才寻找宠物的结果怎么样？"),
    },
    "find_pet_at": {
        "execute": ("去书房找找豆豆", "只在客厅看看狗"),
        "history": ("上次去书房找豆豆找到了吗？", "刚才在客厅寻找宠物的结果怎么样？"),
    },
    "find_pet_here": {
        "execute": ("就在这里找找豆豆", "原地找一下狗"),
        "history": ("上次原地找豆豆找到了吗？", "刚才在这里找狗的结果怎么样？"),
    },
    "find_and_feed_doudou": {
        "execute": ("找到豆豆以后给它喂饭", "豆豆该吃东西了，去看看它"),
        "history": ("上次找到豆豆并喂食成功了吗？", "刚才给豆豆投食的结果怎么样？"),
    },
    "meeting_projection": {
        "execute": ("开始会议投影", "帮我把会议内容投出来"),
        "history": ("上次会议投影成功了吗？", "刚才播放会议内容的结果怎么样？"),
    },
    "meeting_projection_stop": {
        "execute": ("结束会议投影", "把会议投影关掉"),
        "history": ("上次结束会议投影成功了吗？", "刚才关闭会议画面的结果怎么样？"),
    },
    "movie_projection": {
        "execute": ("我想看电影", "播放一部电影"),
        "history": ("上次电影投影成功了吗？", "刚才播放电影的结果怎么样？"),
    },
    "movie_projection_pause": {
        "execute": ("暂停电影", "电影先停一下"),
        "history": ("上次暂停电影成功了吗？", "刚才电影暂停的结果怎么样？"),
    },
    "movie_projection_resume": {
        "execute": ("继续播放电影", "恢复电影播放"),
        "history": ("上次继续播放电影成功了吗？", "刚才恢复电影的结果怎么样？"),
    },
    "movie_projection_stop": {
        "execute": ("结束电影播放", "把电影关掉"),
        "history": ("上次结束电影播放成功了吗？", "刚才关闭电影投影的结果怎么样？"),
    },
    "rest_lighting": {
        "execute": ("我想休息一会", "有点累帮我调整灯光"),
        "history": ("上次休息时调整灯光成功了吗？", "刚才休息场景的灯光结果怎么样？"),
    },
}


class EveryScenarioMemoryBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = ScenarioCatalog(ROOT / "scenarios" / "procedure_catalog.json")

    def test_matrix_covers_every_formal_scenario(self) -> None:
        self.assertEqual(set(CASES), set(self.catalog.procedures))
        for scenario, cases in CASES.items():
            self.assertGreaterEqual(len(cases["execute"]), 2, scenario)
            self.assertGreaterEqual(len(cases["history"]), 2, scenario)

    def test_two_current_instructions_per_scenario_still_compile(self) -> None:
        for scenario, cases in CASES.items():
            for text in cases["execute"]:
                with self.subTest(scenario=scenario, text=text):
                    self.assertNotEqual(normalize_user_intent(text)["domain"], "memory")
                    self.assertEqual(self.catalog.match(text), scenario)
                    normalized = self.catalog.normalize_intent(scenario, {}, text)
                    plan = self.catalog.compile_intent(normalized)
                    self.assertTrue(plan.get("steps"))

    def test_two_history_instructions_per_scenario_are_read_only(self) -> None:
        for scenario, cases in CASES.items():
            for text in cases["history"]:
                with self.subTest(scenario=scenario, text=text):
                    intent = normalize_user_intent(text)
                    self.assertEqual(intent["domain"], "memory")
                    self.assertEqual(intent["constraints"]["allowed_tools"], ["memory_query"])
                    self.assertIsNone(self.catalog.match(text))
                    supported, reason = self.catalog.model_scenario_supported(scenario, text)
                    self.assertFalse(supported)
                    self.assertEqual(reason, "retrospective_query_must_use_memory")
                    name, arguments, policy_reason = enforce_turn_tool_policy(
                        intent,
                        "run_robot_scenario",
                        {"scenario": scenario},
                    )
                    self.assertEqual(name, "memory_query")
                    self.assertEqual(arguments["scope"], "command_history")
                    self.assertEqual(policy_reason, "retrospective_query_forced_read_only")


if __name__ == "__main__":
    unittest.main()
