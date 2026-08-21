from __future__ import annotations

import unittest

from new_project.speech_policy import SpeechPolicy


class TrackingSpeechPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SpeechPolicy()

    @staticmethod
    def step(skill: str, **arguments):
        return type("Step", (), {"skill_name": skill, "arguments": arguments})()

    def test_pet_find_does_not_claim_tracking_or_video(self):
        summary = self.policy.step_summary(
            self.step("pet_tracking", action="find", pet="all"),
            {"ok": True, "mode": "find", "pet": "all", "found": True, "state": "found", "video_path": None},
        )
        self.assertEqual(summary, "已经找到宠物。")

    def test_pet_not_found_is_explicit(self):
        summary = self.policy.step_summary(
            self.step("pet_tracking", action="find", pet="dog"),
            {"ok": True, "mode": "find", "pet": "dog", "found": False, "state": "not_found"},
        )
        self.assertEqual(summary, "这次没有找到小狗。")

    def test_person_find_does_not_claim_following(self):
        summary = self.policy.step_summary(
            self.step("person_tracking", action="find"),
            {"ok": True, "mode": "find"},
        )
        self.assertEqual(summary, "人员查找已经结束。")


if __name__ == "__main__":
    unittest.main()
