from __future__ import annotations

import unittest

from realtime_chat import delivered_fitness_completion


class FakeSpeaker:
    def __init__(self, event=None):
        self.event = event
        self.calls = []

    def delivered_event(self, **kwargs):
        self.calls.append(kwargs)
        return self.event


class FitnessCompletionSpeechTests(unittest.TestCase):
    def test_successful_fitness_uses_delivered_live_completion(self):
        event = {
            "turn_id": "4",
            "skill_name": "push_up",
            "kind": "complete",
            "text": "运动结束，你一共完成了三个俯卧撑。辛苦了，喝口水吧。",
        }
        speaker = FakeSpeaker(event)
        delivered = delivered_fitness_completion(
            speaker,
            tool_name="run_robot_scenario",
            arguments={"scenario": "push_up_companion"},
            result={"ok": True},
            turn_id=4,
        )
        self.assertEqual(delivered, event)
        self.assertEqual(speaker.calls[0]["skill_names"], {"push_up"})

    def test_failed_or_non_fitness_result_keeps_authoritative_final_speech(self):
        speaker = FakeSpeaker({"text": "不应使用"})
        self.assertIsNone(delivered_fitness_completion(
            speaker,
            tool_name="run_robot_scenario",
            arguments={"scenario": "push_up_companion"},
            result={"ok": False},
            turn_id=4,
        ))
        self.assertIsNone(delivered_fitness_completion(
            speaker,
            tool_name="run_robot_scenario",
            arguments={"scenario": "meeting_projection"},
            result={"ok": True},
            turn_id=4,
        ))
        self.assertEqual(speaker.calls, [])

    def test_all_fitness_scenarios_map_to_their_own_live_skill(self):
        expected = {
            "push_up_companion": "push_up",
            "pull_up_companion": "pull_up",
            "squat_companion": "squat",
        }
        for scenario, skill in expected.items():
            with self.subTest(scenario=scenario):
                speaker = FakeSpeaker({"text": "完成"})
                delivered_fitness_completion(
                    speaker,
                    tool_name="run_robot_scenario",
                    arguments={"scenario": scenario},
                    result={"ok": True},
                    turn_id="9",
                )
                self.assertEqual(speaker.calls[0]["skill_names"], {skill})


if __name__ == "__main__":
    unittest.main()
