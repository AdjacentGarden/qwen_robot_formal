#!/usr/bin/env python3
"""Hardware-free end-to-end simulations for the two exploration workflows."""

from __future__ import annotations

import contextlib
import io
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import autonomy_exploration
from autonomy_exploration import AutonomyEngine


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config" / "exploration.json"
REAL_MAP = Path("/home/test/project_0727_fixed_points_home_scenes/test_assets/maps/713_test.yaml")


class FakeCapture:
    def __init__(self, frame: np.ndarray):
        self.frame = frame

    def read(self):
        return True, self.frame.copy()


class FakeCamera:
    def __init__(self, frames: list[np.ndarray]):
        self.frames = list(frames)
        self.leases = 0

    def lease(self, *_args, **_kwargs):
        index = min(self.leases, len(self.frames) - 1)
        self.leases += 1
        return FakeCapture(self.frames[index])


class FakeRuntime:
    def __init__(self, navigation_statuses: list[str] | None = None):
        self.navigation_statuses = list(navigation_statuses or [])
        self.navigation_requests: list[dict] = []
        self.cancel_calls = 0
        self.stop_calls = 0

    def occupancy_grid_snapshot(self, **_kwargs):
        return {"available": False, "error": "simulation_uses_yaml"}

    def lookup_pose(self, *_args, **_kwargs):
        return {"available": False, "error": "simulation_pose_from_arguments"}

    def navigation_goal(self, request):
        self.navigation_requests.append(dict(request))
        status = self.navigation_statuses.pop(0) if self.navigation_statuses else "succeeded"
        return {"ok": status == "succeeded", "status": status, "simulated": True}

    def navigation_cancel_request(self):
        self.cancel_calls += 1
        return {"ok": True}

    def chassis_stop(self):
        self.stop_calls += 1
        return {"status": "stopped"}

    def wait_for_head_target(self, target_deg, **_kwargs):
        return {
            "ok": True,
            "available": True,
            "target_deg": float(target_deg),
            "roll_deg": float(target_deg),
            "roll_rate_dps": 0.0,
            "error_deg": 0.0,
            "at_target": True,
            "stable_sec": 1.0,
        }


class FakeOwner:
    def __init__(self, frames: list[np.ndarray], navigation_statuses: list[str] | None = None):
        self.runtime = FakeRuntime(navigation_statuses)
        self.camera = FakeCamera(frames)
        self.exploration_stop = threading.Event()
        self.head_calls: list[list[str]] = []
        self.generic_calls: list[tuple[str, list[str]]] = []

    def _head(self, argv):
        self.head_calls.append(list(argv))
        return 0

    def _generic(self, skill, argv):
        self.generic_calls.append((str(skill), list(argv)))
        return 0


def clear_center_frame() -> np.ndarray:
    frame = np.zeros((480, 640, 3), np.uint8)
    frame[180:300, 245:395] = 235
    return frame


def dark_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), np.uint8)


def run_json(callable_):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = callable_()
    payload = {}
    for line in reversed(output.getvalue().splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return code, payload


@unittest.skipUnless(REAL_MAP.exists(), "real copied map is unavailable")
class AutonomousProjectionSimulationTests(unittest.TestCase):
    def engine(self, frames):
        owner = FakeOwner(frames)
        engine = AutonomyEngine(owner, CONFIG)
        engine.config["projection"]["camera_settle_sec"] = 0.0
        return owner, engine

    @mock.patch.object(autonomy_exploration, "pucoding_projection_judge")
    def test_rejects_first_wall_then_projects_on_next_wall(self, vlm):
        vlm.side_effect = [
            {"ok": False, "confidence": 0.96, "provider": "simulated_vlm"},
            {"ok": True, "confidence": 0.96, "provider": "simulated_vlm"},
        ]
        owner, engine = self.engine([dark_frame(), clear_center_frame()])
        code, result = run_json(lambda: engine.projection_cli([
            "search_and_project", "--map-yaml", str(REAL_MAP),
            "--start-x", "0", "--start-y", "0", "--judge", "auto",
            "--allow-offline-execution", "--json",
        ]))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "projecting")
        self.assertEqual(len(owner.runtime.navigation_requests), 2)
        self.assertEqual(len(owner.generic_calls), 1)
        self.assertEqual(owner.generic_calls[0][0], "projector_control")
        self.assertTrue(any(
            call[0] == "angle"
            and "--angle" in call
            and call[call.index("--angle") + 1] == "190"
            for call in owner.head_calls
        ))
        self.assertEqual(vlm.call_count, 2)

    @mock.patch.object(autonomy_exploration, "pucoding_projection_judge")
    def test_all_unsuitable_walls_never_start_projector(self, vlm):
        vlm.return_value = {"ok": False, "confidence": 0.95, "provider": "simulated_vlm"}
        owner, engine = self.engine([dark_frame()])
        code, result = run_json(lambda: engine.projection_cli([
            "search_and_project", "--map-yaml", str(REAL_MAP),
            "--start-x", "0", "--start-y", "0", "--judge", "auto",
            "--allow-offline-execution", "--json",
        ]))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "no_suitable_projection_surface")
        self.assertFalse(result["suitable"])
        self.assertEqual(owner.generic_calls, [])
        self.assertGreater(vlm.call_count, 0)
        self.assertEqual(owner.head_calls[-1][0], "angle")
        self.assertEqual(
            owner.head_calls[-1][owner.head_calls[-1].index("--angle") + 1],
            "190",
        )

    @mock.patch.object(autonomy_exploration, "pucoding_projection_judge")
    def test_vlm_network_failure_is_not_reported_as_bad_wall(self, vlm):
        vlm.side_effect = RuntimeError("pucoding_judge_failed:URLError:offline")
        owner, engine = self.engine([clear_center_frame()])
        code, result = run_json(lambda: engine.projection_cli([
            "search_and_project", "--map-yaml", str(REAL_MAP),
            "--start-x", "0", "--start-y", "0", "--judge", "auto",
            "--allow-offline-execution", "--json",
        ]))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "projection_judge_unavailable")
        self.assertGreater(result["vlm_failures"], 0)
        self.assertEqual(owner.generic_calls, [])

    def test_live_execution_refuses_stale_fallback_map(self):
        owner, engine = self.engine([clear_center_frame()])
        code, result = run_json(lambda: engine.projection_cli([
            "search_and_project", "--map-yaml", str(REAL_MAP),
            "--start-x", "0", "--start-y", "0", "--judge", "local", "--json",
        ]))
        self.assertEqual(code, 1)
        self.assertIn("live_map_unavailable", result["error"])
        self.assertEqual(owner.runtime.navigation_requests, [])


@unittest.skipUnless(REAL_MAP.exists(), "real copied map is unavailable")
class PetMapSearchSimulationTests(unittest.TestCase):
    def engine(self):
        owner = FakeOwner([dark_frame()])
        return owner, AutonomyEngine(owner, CONFIG)

    def command(self, engine):
        return engine.pet_search_cli([
            "search", "--pet", "dog", "--map-yaml", str(REAL_MAP),
            "--start-x", "0", "--start-y", "0", "--max-viewpoints", "8",
            "--allow-offline-execution", "--json",
        ])

    def test_finds_doudou_at_first_rectangle_center(self):
        owner, engine = self.engine()
        with mock.patch.object(engine, "_scan_pet", return_value={"ok": True, "found": True, "status": "found"}) as scan:
            code, result = run_json(lambda: self.command(engine))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["scan_count"], 1)
        self.assertEqual(result["partition_mode"], "few_rectangular_regions")
        self.assertEqual(len(result["visited_regions"]), 1)
        scan.assert_called_once()
        self.assertGreaterEqual(owner.runtime.stop_calls, 1)

    def test_navigates_rectangle_centers_in_order_then_finds_doudou(self):
        owner, engine = self.engine()
        scans = [
            {"ok": True, "found": False, "status": "completed"},
            {"ok": True, "found": True, "status": "found"},
        ]
        with mock.patch.object(engine, "_scan_pet", side_effect=scans) as scan:
            code, result = run_json(lambda: self.command(engine))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["scan_count"], 2)
        self.assertEqual(len(result["visited_regions"]), 2)
        self.assertGreaterEqual(len(owner.runtime.navigation_requests), 1)
        self.assertEqual(scan.call_args_list[0].args[1], 1)
        self.assertEqual(scan.call_args_list[1].args[1], -1)

    def test_scans_every_rectangle_before_reporting_not_found(self):
        owner, engine = self.engine()
        completed = {"ok": True, "found": False, "status": "completed"}
        with mock.patch.object(engine, "_scan_pet", return_value=completed):
            code, result = run_json(lambda: self.command(engine))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "not_found")
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["scan_count"], result["region_count"])
        self.assertEqual(len(result["visited_regions"]), result["region_count"])
        self.assertEqual(result["failed_regions"], [])
        self.assertGreaterEqual(len(owner.runtime.navigation_requests), 1)

    def test_failed_region_navigation_never_claims_map_exhausted(self):
        owner = FakeOwner([dark_frame()], navigation_statuses=["failed", "succeeded", "succeeded", "succeeded", "succeeded"])
        engine = AutonomyEngine(owner, CONFIG)
        completed = {"ok": True, "found": False, "status": "completed"}
        with mock.patch.object(engine, "_scan_pet", return_value=completed):
            code, result = run_json(lambda: self.command(engine))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["exhausted"])
        self.assertGreaterEqual(len(result["failed_regions"]), 1)
        self.assertGreaterEqual(owner.runtime.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
