from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WELCOME = ROOT / "robot_skills" / "welcome_projection"
PROJECTOR = ROOT / "robot_skills" / "projector_control"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def dry_run(script: Path, action: str, *extra: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(script), action, "--dry-run", "--json", *extra],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


class ProjectionMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.welcome = json.loads((WELCOME / "config.json").read_text(encoding="utf-8"))
        cls.projector = json.loads((PROJECTOR / "config.json").read_text(encoding="utf-8"))

    def test_welcome_source_matches_approved_three_second_asset(self) -> None:
        asset = WELCOME / "assets" / "welcome_home.mp4"
        sidecar = (WELCOME / "assets" / "welcome_home.sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(digest(asset), self.welcome["expected_sha256"])
        self.assertEqual(digest(asset), sidecar)

    def test_each_scene_is_pinned_to_distinct_media(self) -> None:
        paths = [
            self.welcome["container_video_path"],
            self.projector["fitness_video_container_path"],
            *self.projector["meeting_slide_container_paths"],
        ]
        hashes = [
            self.welcome["expected_sha256"],
            self.projector["fitness_video_sha256"],
            *self.projector["meeting_slide_sha256"],
        ]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_dry_run_reports_unambiguous_media_contract(self) -> None:
        welcome = dry_run(WELCOME / "run.py", "play", "--duration", "3")
        fitness = dry_run(PROJECTOR / "run.py", "fitness_video_on")
        meeting = dry_run(PROJECTOR / "run.py", "meeting_presentation_on")
        self.assertEqual(welcome["result"]["media_kind"], "welcome_home_video")
        self.assertEqual(fitness["result"]["media_kind"], "fitness_video")
        self.assertEqual(meeting["result"]["media_kind"], "meeting_slide_loop")
        self.assertEqual(fitness["result"]["media_paths"], ["/sdcard/Movies/exercise.mp4"])
        self.assertEqual(
            meeting["result"]["media_paths"],
            ["/sdcard/Pictures/test-1.jpg", "/sdcard/Pictures/test-2.jpg"],
        )

    def test_helpers_cannot_cross_route_media(self) -> None:
        welcome = (WELCOME / "system" / "robot-welcome-projection").read_text(encoding="utf-8")
        fitness = (PROJECTOR / "system" / "robot-start-exercise-projection").read_text(encoding="utf-8")
        meeting = (PROJECTOR / "system" / "robot-meeting-projection-v2").read_text(encoding="utf-8")
        self.assertIn("welcome_home.mp4", welcome)
        self.assertNotIn("exercise.mp4", welcome)
        self.assertNotIn("test-1.jpg", welcome)
        self.assertIn("exercise.mp4", fitness)
        self.assertNotIn("welcome_home.mp4", fitness)
        self.assertNotIn("test-1.jpg", fitness)
        self.assertIn("test-1.jpg", meeting)
        self.assertIn("test-2.jpg", meeting)
        self.assertNotIn("exercise.mp4", meeting)
        self.assertNotIn("welcome_home.mp4", meeting)

    def test_welcome_duration_starts_after_slow_player_startup(self) -> None:
        spec = importlib.util.spec_from_file_location("welcome_projection_run", WELCOME / "run.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def slow_start(*_args, **_kwargs):
            time.sleep(0.12)
            return subprocess.CompletedProcess([], 0, "", "")

        module.STOP_REQUESTED = False
        with (
            patch.object(module, "ensure_asset"),
            patch.object(module, "projector_light"),
            patch.object(module, "stop_content"),
            patch.object(module, "run", side_effect=slow_start),
        ):
            result = module.play({"helper_path": "/mock/player"}, 0.20)

        self.assertGreaterEqual(result["played_seconds"], 0.19)
        self.assertGreaterEqual(result["total_elapsed_seconds"], 0.30)
        self.assertGreater(result["total_elapsed_seconds"], result["played_seconds"])


if __name__ == "__main__":
    unittest.main()
