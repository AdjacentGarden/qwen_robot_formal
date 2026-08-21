from __future__ import annotations

import subprocess
import unittest

from new_project.executor import SkillExecutor


class ExecutorResultContractTests(unittest.TestCase):
    @staticmethod
    def step(skill: str):
        return type("Step", (), {"skill_name": skill})()

    def test_zero_exit_with_structured_false_is_failure(self):
        executor = SkillExecutor.__new__(SkillExecutor)
        executor._is_interrupt_requested = lambda: False
        executor.current_snapshot = lambda: {}
        executor._soft_failure_reason = lambda *_args: ""
        completed = subprocess.CompletedProcess(
            ["fake"],
            0,
            stdout='{"ok":false,"status":"no_face","result":{"reason":"no_detection"}}\n',
        )
        result = executor._single_function_result_from_completed(
            self.step("face_registration"),
            ["fake"],
            completed,
            [],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_detection")

    def test_pet_cli_always_requests_structured_json(self):
        executor = SkillExecutor.__new__(SkillExecutor)
        command = executor._arguments_to_cli(
            "pet_tracking",
            {"action": "find", "pet": "all", "camera": "/dev/video22"},
        )
        self.assertEqual(command[0], "find")
        self.assertIn("--json", command)


if __name__ == "__main__":
    unittest.main()
