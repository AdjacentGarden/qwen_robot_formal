#!/usr/bin/env python3
"""End-to-end mock tests for strict boot ordering and failure containment."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


PROJECT = Path("/home/test/new_project_optimized_v11_navsafe")
SUPERVISOR = PROJECT / "startup" / "robot_stack_supervisor.py"
ORDER = ["base", "odometry", "navigation", "assistant", "emotion"]


def events(runtime: Path) -> list[dict]:
    return [json.loads(line) for line in (runtime / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def run(runtime: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SUPERVISOR), "--mock", "--once", "--runtime", str(runtime), *extra],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20.0,
        check=False,
    )


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="v6_startup_mock_", dir="/tmp"))
    report = {"ok": False, "hardware_started": False, "tests": []}
    try:
        success_dir = root / "success"
        completed = run(success_dir)
        success_events = events(success_dir)
        started = [item["stage"] for item in success_events if item["event"] == "started"]
        ready = [item["stage"] for item in success_events if item["event"] == "ready"]
        sequence = [
            (item["event"], item["stage"])
            for item in success_events
            if item["event"] in ("started", "ready")
        ]
        expected_sequence = [pair for name in ORDER for pair in (("started", name), ("ready", name))]
        success_ok = completed.returncode == 0 and started == ORDER and ready == ORDER and sequence == expected_sequence
        report["tests"].append({"name": "strict_success_order", "ok": success_ok, "returncode": completed.returncode, "sequence": sequence})

        failure_dir = root / "failure"
        failed = run(failure_dir, "--mock-fail", "navigation")
        failure_events = events(failure_dir)
        failed_started = [item["stage"] for item in failure_events if item["event"] == "started"]
        failure_ok = (
            failed.returncode == 1
            and failed_started == ["base", "odometry", "navigation"]
            and "assistant" not in failed_started
            and "emotion" not in failed_started
            and any(item["event"] == "failed" for item in failure_events)
        )
        report["tests"].append({"name": "failure_blocks_later_stages", "ok": failure_ok, "returncode": failed.returncode, "started": failed_started})

        lock_dir = root / "lock"
        first = subprocess.Popen(
            ["python3", str(SUPERVISOR), "--mock", "--runtime", str(lock_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (lock_dir / "supervisor.lock").exists() and (lock_dir / "mock_1_base.ready").exists():
                    break
                time.sleep(0.05)
            duplicate = run(lock_dir)
            lock_ok = duplicate.returncode == 2 and "already running" in duplicate.stderr
            report["tests"].append({"name": "duplicate_supervisor_blocked", "ok": lock_ok, "returncode": duplicate.returncode})
        finally:
            try:
                os.killpg(first.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            first.wait(timeout=10.0)

        report["ok"] = all(item["ok"] for item in report["tests"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
