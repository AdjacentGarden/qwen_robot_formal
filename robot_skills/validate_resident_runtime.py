#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "runtime" / "validation"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

CASES = {
    "back_camera_capture": ["--dry-run"],
    "back_camera_record": ["--dry-run"],
    "camera_capture": ["--dry-run"],
    "camera_record": ["--dry-run"],
    "environment_perception": ["--dry-run"],
    "face_recognition": ["--dry-run"],
    "face_registration": ["--dry-run", "--name", "resident_validation"],
    "fan_control": [],
    "feeder_control": ["check", "--dry-run"],
    "front_camera_capture": ["--dry-run"],
    "front_camera_record": ["--dry-run"],
    "head_control": ["up", "--dry-run"],
    "light_control": ["check", "--dry-run"],
    "move_backward": ["--speed", "0", "--duration", "0.1", "--allow-no-subscriber"],
    "move_forward": ["--speed", "0", "--duration", "0.1", "--allow-no-subscriber"],
    "move_left": ["--angular-speed", "0", "--duration", "0.1", "--allow-no-subscriber"],
    "move_right": ["--angular-speed", "0", "--duration", "0.1", "--allow-no-subscriber"],
    "navigation_goto": ["0", "0", "0", "--dry-run"],
    "navigation_list": [],
    "person_tracking": ["check", "--json"],
    "pet_tracking": ["track", "--dry-run", "--json"],
    "projector_control": ["status", "--dry-run"],
    "pull_up": ["check"],
    "push_up": ["check"],
    "realtime_information": ["--action", "current_time", "--dry-run"],
    "reminder_cancel": ["--help"],
    "reminder_query": ["--help"],
    "reminder_schedule": ["--help"],
    "squat": ["check"],
}


def run(skill: str, args: list[str]) -> dict:
    command = ["bash", str(ROOT / skill / "run.sh"), *args]
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, timeout=30)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # 69 is the resident protocol's explicit "configured but disabled" code.
    expected = {69} if skill == "fan_control" else {0}
    return {
        "skill": skill,
        "command": command,
        "exit_code": completed.returncode,
        "expected_exit_codes": sorted(expected),
        "passed": completed.returncode in expected,
        "elapsed_ms": round(elapsed_ms, 3),
        "stdout_tail": completed.stdout[-1500:],
        "stderr_tail": completed.stderr[-1000:],
    }


def main() -> int:
    rows = [run(skill, args) for skill, args in CASES.items()]
    report = {
        "ok": all(row["passed"] for row in rows),
        "created_at": time.time(),
        "root": str(ROOT),
        "skill_count": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "latency_ms": {
            "minimum": round(min(row["elapsed_ms"] for row in rows), 3),
            "maximum": round(max(row["elapsed_ms"] for row in rows), 3),
            "mean": round(sum(row["elapsed_ms"] for row in rows) / len(rows), 3),
        },
        "rows": rows,
    }
    target = REPORT_DIR / "latest.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("ok", "skill_count", "passed", "failed", "latency_ms")}, ensure_ascii=False, indent=2))
    if not report["ok"]:
        for row in rows:
            if not row["passed"]:
                print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
