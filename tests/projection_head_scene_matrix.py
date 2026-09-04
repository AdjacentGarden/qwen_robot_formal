#!/usr/bin/env python3
"""Pure-software projection/head regression matrix.

The executor is given a recording stub, so this test cannot reach ROS, the
resident skill socket, the step motor, navigation, or cmd_vel.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scenario_engine import ScenarioCatalog, ScenarioExecutor


CATALOG = ROOT / "scenarios" / "procedure_catalog.json"
ITERATIONS = 50


def run_scene(catalog: ScenarioCatalog, scenario: str, arguments: dict, failure: tuple[str, str] | None = None):
    calls: list[tuple[str, str]] = []

    def simulate(skill: str, call_arguments: dict):
        action = str(call_arguments.get("action") or "")
        calls.append((skill, action))
        if failure == (skill, action):
            return {
                "ok": False,
                "validation_ok": False,
                "executed": True,
                "error": "simulated_failure",
            }
        result = {"ok": True, "validation_ok": True, "executed": True, "error": None}
        if skill in {"push_up", "pull_up", "squat"}:
            result["spoken_summary"] = "模拟运动结束。"
        return result

    result = ScenarioExecutor(catalog, simulate).execute(scenario, arguments, announce=False)
    return result, calls


def main() -> None:
    catalog = ScenarioCatalog(CATALOG)
    cases = {
        "welcome_down_level": ("homecoming_welcome", {}, None, 0, 1),
        "push_up_up_level": ("push_up_companion", {}, None, 1, 1),
        "pull_up_up_level": ("pull_up_companion", {}, None, 1, 1),
        "squat_up_level": ("squat_companion", {}, None, 1, 1),
        "meeting_start_here": ("meeting_projection", {"navigate": False, "stay_put": True}, None, 1, 0),
        "meeting_stop": ("meeting_projection_stop", {}, None, 0, 1),
        "movie_start_here": ("movie_projection", {"stay_put": True}, None, 1, 0),
        "movie_stop": ("movie_projection_stop", {}, None, 0, 1),
        "meeting_stop_projector_failure": (
            "meeting_projection_stop", {}, ("projector_control", "off"), 0, 1
        ),
        "movie_player_failure_cleanup": (
            "movie_projection", {"stay_put": True}, ("media_player", "play_movie"), 1, 1
        ),
    }
    summary: dict[str, dict] = {}
    total = 0
    for label, (scenario, arguments, failure, expected_up, expected_level) in cases.items():
        observed = Counter()
        for _ in range(ITERATIONS):
            _result, calls = run_scene(catalog, scenario, arguments, failure)
            assert calls.count(("head_control", "up")) == expected_up, (label, calls)
            assert calls.count(("head_control", "level")) == expected_level, (label, calls)
            assert calls.count(("projector_control", "off")) <= 1, (label, calls)
            observed.update(calls)
            total += 1
        summary[label] = {
            "iterations": ITERATIONS,
            "expected_up_per_run": expected_up,
            "expected_level_per_run": expected_level,
            "aggregate_calls": {f"{skill}:{action}": count for (skill, action), count in observed.items()},
        }

    print(json.dumps({
        "ok": True,
        "hardware_execution": False,
        "total_runs": total,
        "cases": summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
