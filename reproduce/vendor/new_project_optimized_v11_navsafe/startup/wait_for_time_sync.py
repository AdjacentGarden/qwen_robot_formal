#!/usr/bin/env python3
"""Block robot startup until this boot has a stable, NTP-synchronized clock."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Callable


DEFAULT_SYNC_MARKER = Path("/run/systemd/timesync/synchronized")
DEFAULT_STATE_FILE = Path("/home/test/new_project_optimized_v11_navsafe/runtime/startup/time_sync_gate.json")


def timedatectl_reports_synchronized(command: str = "/usr/bin/timedatectl") -> bool:
    try:
        completed = subprocess.run(
            [command, "show", "--property=NTPSynchronized", "--value"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "yes"


def wait_for_stable_sync(
    synchronized: Callable[[], bool],
    *,
    timeout_seconds: float,
    stable_seconds: float,
    poll_seconds: float,
    max_clock_step_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str, float], None] | None = None,
) -> tuple[bool, str, float]:
    """Wait with a monotonic deadline and reject wall-clock steps while settling."""

    started = monotonic()
    deadline = started + max(0.0, timeout_seconds)
    stable_since: float | None = None
    previous_offset: float | None = None
    last_report = started - 10.0
    reason = "waiting_for_ntp"

    while True:
        now = monotonic()
        if synchronized():
            offset = wall_time() - now
            if previous_offset is None:
                stable_since = now
                reason = "settling_after_ntp"
            elif abs(offset - previous_offset) > max_clock_step_seconds:
                stable_since = now
                reason = "clock_step_detected"
            previous_offset = offset
            if stable_since is not None and now - stable_since >= stable_seconds:
                return True, "synchronized_and_stable", now - started
        else:
            stable_since = None
            previous_offset = None
            reason = "waiting_for_ntp"

        if progress is not None and now - last_report >= 10.0:
            progress(reason, now - started)
            last_report = now
        if now >= deadline:
            return False, reason, now - started
        sleep(max(0.01, min(poll_seconds, deadline - now)))


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--stable-seconds", type=float, default=3.0)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--max-clock-step", type=float, default=0.10)
    parser.add_argument("--sync-marker", type=Path, default=DEFAULT_SYNC_MARKER)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--timedatectl", default="/usr/bin/timedatectl")
    parser.add_argument("--skip-timedatectl", action="store_true", help="test-only: trust the marker file")
    args = parser.parse_args()

    def synchronized() -> bool:
        if not args.sync_marker.exists():
            return False
        return args.skip_timedatectl or timedatectl_reports_synchronized(args.timedatectl)

    def progress(reason: str, elapsed: float) -> None:
        print(f"time-sync-gate: {reason}; monotonic_elapsed={elapsed:.1f}s", flush=True)

    ok, reason, elapsed = wait_for_stable_sync(
        synchronized,
        timeout_seconds=args.timeout,
        stable_seconds=max(0.0, args.stable_seconds),
        poll_seconds=max(0.01, args.poll_seconds),
        max_clock_step_seconds=max(0.0, args.max_clock_step),
        progress=progress,
    )
    payload = {
        "ok": ok,
        "status": reason if ok else "timeout",
        "last_reason": reason,
        "elapsed_monotonic_seconds": round(elapsed, 3),
        "sync_marker": str(args.sync_marker),
        "sync_marker_exists": args.sync_marker.exists(),
        "timedatectl_synchronized": None
        if args.skip_timedatectl
        else timedatectl_reports_synchronized(args.timedatectl),
        "stable_seconds_required": args.stable_seconds,
        "checked_wall_time": time.time(),
    }
    write_state(args.state_file, payload)
    if ok:
        print(f"time-sync-gate: synchronized and stable for {args.stable_seconds:.1f}s", flush=True)
        return 0
    print(
        f"time-sync-gate: NTP did not become stable within {args.timeout:.1f}s; blocking ROS startup",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
