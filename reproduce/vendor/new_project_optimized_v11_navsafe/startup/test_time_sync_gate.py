#!/usr/bin/env python3
"""Hardware-free regression tests for the V8 boot time synchronization gate."""

from __future__ import annotations

import json

from wait_for_time_sync import wait_for_stable_sync


class FakeClock:
    def __init__(self, *, jump_at: float | None = None, jump_seconds: float = 0.0):
        self.now = 0.0
        self.offset = 1000.0
        self.jump_at = jump_at
        self.jump_seconds = jump_seconds
        self.jumped = False

    def monotonic(self) -> float:
        return self.now

    def wall_time(self) -> float:
        return self.now + self.offset

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.jump_at is not None and not self.jumped and self.now >= self.jump_at:
            self.offset += self.jump_seconds
            self.jumped = True


def run_case(name: str, clock: FakeClock, probe, **options) -> dict:
    ok, reason, elapsed = wait_for_stable_sync(
        probe,
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
        sleep=clock.sleep,
        timeout_seconds=options.get("timeout_seconds", 1.0),
        stable_seconds=options.get("stable_seconds", 0.3),
        poll_seconds=0.1,
        max_clock_step_seconds=0.05,
    )
    return {"name": name, "ok": ok, "reason": reason, "elapsed": round(elapsed, 3)}


def main() -> int:
    stable_clock = FakeClock()
    stable = run_case("stable_sync_passes", stable_clock, lambda: True)
    stable["passed"] = stable["ok"] and stable["elapsed"] >= 0.3

    missing_clock = FakeClock()
    missing = run_case(
        "missing_sync_times_out",
        missing_clock,
        lambda: False,
        timeout_seconds=0.4,
    )
    missing["passed"] = not missing["ok"] and missing["reason"] == "waiting_for_ntp"

    jump_clock = FakeClock(jump_at=0.2, jump_seconds=285.0)
    jumped = run_case("clock_step_resets_stability_window", jump_clock, lambda: True)
    jumped["passed"] = jumped["ok"] and jump_clock.jumped and jumped["elapsed"] >= 0.5

    delayed_clock = FakeClock()
    delayed = run_case("delayed_sync_then_passes", delayed_clock, lambda: delayed_clock.now >= 0.3)
    delayed["passed"] = delayed["ok"] and delayed["elapsed"] >= 0.6

    tests = [stable, missing, jumped, delayed]
    report = {"ok": all(item["passed"] for item in tests), "hardware_started": False, "tests": tests}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
