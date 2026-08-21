#!/usr/bin/env python3
"""V11 robot stack supervisor.

Production stages run in separate user-systemd cgroups.  Stopping a stage
therefore removes every descendant, including ROS launch children that detach
from the original process group.  Mock mode stays hardware-free for tests.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT = Path("/home/test/new_project_optimized_v11_navsafe")
CAR = Path("/home/test/Car_real_copy_v11_navsafe")
RUNTIME = PROJECT / "runtime" / "startup"
TRANSPORT_ENV = PROJECT / "startup" / "ros_transport_env.sh"
NAV2_HEALTH = PROJECT / "startup" / "nav2_health_check.py"
UNIT_PREFIX = "v11-navsafe"
ROS_ENV = (
    f"source {TRANSPORT_ENV} && "
    "source /opt/ros/humble/setup.bash && "
    f"source {CAR}/install/setup.bash"
)


@dataclass(frozen=True)
class Stage:
    name: str
    command: str
    ready: str
    timeout: float
    existing_pattern: str = ""
    cleanup: str = ""
    ready_check_timeout: float = 4.0
    stable_successes: int = 2
    start_attempts: int = 1


PRODUCTION_STAGES = (
    Stage(
        "base",
        f"{ROS_ENV} && exec ros2 launch robot_bringup real_robot_base.launch.py",
        f"{ROS_ENV} && ros2 node list --no-daemon 2>/dev/null | grep -Fxq /ros_robot_controller",
        60.0,
        "ros2 launch robot_bringup real_robot_base.launch.py",
    ),
    Stage(
        "odometry",
        f"{ROS_ENV} && exec ros2 launch robot_bringup real_robot_odometry.launch.py",
        f"{ROS_ENV} && nodes=$(ros2 node list --no-daemon 2>/dev/null) && "
        "grep -Fxq /odom_publisher <<<\"$nodes\" && grep -Fxq /ekf_filter_node <<<\"$nodes\"",
        45.0,
        "ros2 launch robot_bringup real_robot_odometry.launch.py",
    ),
    Stage(
        "navigation",
        f"{ROS_ENV} && exec ros2 launch robot_bringup real_robot_nav.launch.py use_rviz:=false",
        f"{ROS_ENV} && python3 {NAV2_HEALTH} --timeout 4.0",
        100.0,
        "ros2 launch robot_bringup real_robot_nav.launch.py",
        ready_check_timeout=7.0,
        stable_successes=3,
        start_attempts=2,
    ),
    Stage(
        "assistant",
        f"cd {PROJECT} && {ROS_ENV} && exec -a v11_navsafe_robot_daemon "
        "python3 -u -m new_project.cli daemon --execute --max-followups 3 --replace-existing-daemon",
        f"grep -Fq '\"daemon\": \"started\"' {RUNTIME}/assistant.log",
        45.0,
        "v11_navsafe_robot_daemon",
        stable_successes=1,
    ),
    Stage(
        "emotion",
        "cd /home/test && bash /home/test/mpv_emotion.sh || true; "
        "pid=$(cat /home/test/mpv_pid.pid 2>/dev/null); test -n \"$pid\" || exit 2; "
        "while kill -0 \"$pid\" 2>/dev/null; do sleep 2; done; exit 3",
        "pid=$(cat /home/test/mpv_pid.pid 2>/dev/null) && kill -0 \"$pid\" 2>/dev/null",
        15.0,
        "Gaoxinglong.mp4",
        "pid=$(cat /home/test/mpv_pid.pid 2>/dev/null) && kill \"$pid\" 2>/dev/null || true",
        stable_successes=1,
    ),
)


class SupervisorError(RuntimeError):
    pass


@dataclass
class StageHandle:
    stage: Stage
    unit: str = ""
    process: subprocess.Popen | None = None


class Supervisor:
    def __init__(self, stages: tuple[Stage, ...], runtime: Path, *, once: bool = False, mock: bool = False):
        self.stages = stages
        self.runtime = runtime
        self.once = once
        self.mock = mock
        self.children: list[StageHandle] = []
        self.stopping = False
        runtime.mkdir(parents=True, exist_ok=True)
        self.event_path = runtime / "events.jsonl"

    def event(self, event: str, stage: str = "", **extra) -> None:
        payload = {"ts": time.time(), "event": event, "stage": stage, **extra}
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def shell_ok(command: str, timeout: float = 4.0) -> bool:
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def process_exists(pattern: str) -> bool:
        if not pattern:
            return False
        own_pid = os.getpid()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == own_pid:
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            except (OSError, PermissionError):
                continue
            if pattern in command:
                return True
        return False

    @staticmethod
    def unit_name(stage: Stage) -> str:
        return f"{UNIT_PREFIX}-{stage.name}.service"

    @staticmethod
    def unit_state(unit: str) -> str:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
            text=True,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
        value = (result.stdout or "").strip()
        return value or "not-found"

    @staticmethod
    def unit_pid(unit: str) -> int:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
            text=True,
            capture_output=True,
            timeout=3.0,
            check=False,
        )
        with contextlib.suppress(ValueError):
            return int((result.stdout or "0").strip())
        return 0

    def stop_unit(self, stage: Stage, *, verify: bool = True) -> None:
        unit = self.unit_name(stage)
        if stage.cleanup:
            with contextlib.suppress(Exception):
                subprocess.run(["bash", "-lc", stage.cleanup], check=False, timeout=5.0)
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
            check=False,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.unit_state(unit) in {"inactive", "failed", "not-found"}:
                break
            time.sleep(0.1)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if verify and stage.existing_pattern:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and self.process_exists(stage.existing_pattern):
                time.sleep(0.1)
            if self.process_exists(stage.existing_pattern):
                raise SupervisorError(f"{stage.name} cgroup stopped but matching processes remain")

    def start_systemd_unit(self, stage: Stage) -> StageHandle:
        unit = self.unit_name(stage)
        log_path = self.runtime / f"{stage.name}.log"
        log_path.write_text("", encoding="utf-8")
        self.stop_unit(stage, verify=False)
        if self.process_exists(stage.existing_pattern):
            raise SupervisorError(f"existing {stage.name} process detected; refusing duplicate hardware ownership")
        command = [
            "systemd-run",
            "--user",
            f"--unit={unit.removesuffix('.service')}",
            "--collect",
            "--property=Type=simple",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=8s",
            "--property=Restart=no",
            f"--property=StandardOutput=append:{log_path}",
            f"--property=StandardError=append:{log_path}",
            "--",
            "/bin/bash",
            "-lc",
            stage.command,
        ]
        result = subprocess.run(command, text=True, capture_output=True, timeout=10.0, check=False)
        if result.returncode != 0:
            raise SupervisorError(f"failed to create {unit}: {(result.stderr or result.stdout).strip()}")
        handle = StageHandle(stage=stage, unit=unit)
        self.children.append(handle)
        self.event("started", stage.name, unit=unit, pid=self.unit_pid(unit))
        return handle

    def start_mock_process(self, stage: Stage) -> StageHandle:
        log = (self.runtime / f"{stage.name}.log").open("wb", buffering=0)
        process = subprocess.Popen(
            ["bash", "-lc", stage.command],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        handle = StageHandle(stage=stage, process=process)
        self.children.append(handle)
        self.event("started", stage.name, pid=process.pid)
        return handle

    def handle_running(self, handle: StageHandle) -> bool:
        if handle.process is not None:
            return handle.process.poll() is None
        return self.unit_state(handle.unit) in {"active", "activating", "reloading"}

    def wait_ready(self, handle: StageHandle) -> None:
        stage = handle.stage
        deadline = time.monotonic() + stage.timeout
        consecutive = 0
        checks = 0
        while time.monotonic() < deadline:
            if not self.handle_running(handle):
                raise SupervisorError(f"{stage.name} exited before ready")
            checks += 1
            if self.shell_ok(stage.ready, timeout=stage.ready_check_timeout):
                consecutive += 1
                self.event("readiness_sample", stage.name, ok=True, consecutive=consecutive, checks=checks)
                if consecutive >= max(1, stage.stable_successes):
                    self.event(
                        "ready",
                        stage.name,
                        unit=handle.unit,
                        pid=self.unit_pid(handle.unit) if handle.unit else handle.process.pid,
                        consecutive_successes=consecutive,
                        checks=checks,
                    )
                    return
            else:
                if consecutive:
                    self.event("readiness_sample", stage.name, ok=False, consecutive=0, checks=checks)
                consecutive = 0
            time.sleep(0.3)
        raise SupervisorError(f"{stage.name} readiness timeout after {stage.timeout:.1f}s")

    def start_one(self, stage: Stage) -> None:
        attempts = max(1, stage.start_attempts if not self.mock else 1)
        last_error = ""
        for attempt in range(1, attempts + 1):
            handle = self.start_mock_process(stage) if self.mock else self.start_systemd_unit(stage)
            try:
                self.wait_ready(handle)
                return
            except SupervisorError as exc:
                last_error = str(exc)
                self.event("start_attempt_failed", stage.name, attempt=attempt, error=last_error)
                self.stop_handle(handle)
                with contextlib.suppress(ValueError):
                    self.children.remove(handle)
                if attempt < attempts:
                    self.event("clean_restart", stage.name, next_attempt=attempt + 1)
                    time.sleep(1.0)
        raise SupervisorError(last_error or f"{stage.name} failed to start")

    def stop_handle(self, handle: StageHandle) -> None:
        stage = handle.stage
        if handle.process is not None:
            if handle.process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(handle.process.pid, signal.SIGINT)
                try:
                    handle.process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(handle.process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        handle.process.wait(timeout=1.0)
            self.event("stopped", stage.name, returncode=handle.process.poll())
            return
        self.stop_unit(stage)
        self.event("stopped", stage.name, unit=handle.unit, state=self.unit_state(handle.unit))

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        errors: list[str] = []
        for handle in reversed(self.children):
            try:
                self.stop_handle(handle)
            except Exception as exc:
                errors.append(f"{handle.stage.name}: {exc}")
                self.event("stop_error", handle.stage.name, error=str(exc))
        if errors:
            self.event("cleanup_incomplete", errors=errors)

    def run(self) -> int:
        self.event("supervisor_start", mode="mock" if self.mock else "systemd_cgroup")
        try:
            for stage in self.stages:
                self.start_one(stage)
            self.event("all_ready")
            if self.once:
                return 0
            unhealthy_since: dict[str, float] = {}
            while True:
                for handle in self.children:
                    if self.handle_running(handle):
                        unhealthy_since.pop(handle.stage.name, None)
                        continue
                    first = unhealthy_since.setdefault(handle.stage.name, time.monotonic())
                    # A controlled `systemctl --user restart` may briefly cross
                    # inactive.  Give it a bounded recovery window.
                    if time.monotonic() - first > 12.0:
                        raise SupervisorError(f"{handle.stage.name} stopped unexpectedly")
                time.sleep(0.5)
        except SupervisorError as exc:
            self.event("failed", error=str(exc))
            print(f"startup failed: {exc}", file=sys.stderr)
            return 1
        finally:
            self.stop()


def mock_stages(runtime: Path, fail: str) -> tuple[Stage, ...]:
    result = []
    for index, name in enumerate(("base", "odometry", "navigation", "assistant", "emotion"), start=1):
        ready = runtime / f"mock_{index}_{name}.ready"
        mode = "fail" if name == fail else "ready"
        command = f"exec python3 {PROJECT}/startup/mock_stage.py --name {name} --ready-file {ready} --mode {mode}"
        result.append(Stage(name, command, f"test -f {ready}", 3.0, stable_successes=1))
    return tuple(result)


def acquire_lock(runtime: Path):
    runtime.mkdir(parents=True, exist_ok=True)
    handle = (runtime / "supervisor.lock").open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SupervisorError("another V11 startup supervisor is already running") from exc
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--mock-fail", choices=("", "base", "odometry", "navigation", "assistant", "emotion"), default="")
    parser.add_argument("--runtime", default=str(RUNTIME))
    parser.add_argument("--once", action="store_true", help="exit after all stages become ready (tests only)")
    args = parser.parse_args()
    runtime = Path(args.runtime)
    try:
        lock = acquire_lock(runtime)
    except SupervisorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    stages = mock_stages(runtime, args.mock_fail) if args.mock else PRODUCTION_STAGES
    supervisor = Supervisor(stages, runtime, once=args.once, mock=args.mock)

    def stop_handler(signum, _frame):
        with contextlib.suppress(TypeError, ValueError):
            signal_name = signal.Signals(signum).name
        if "signal_name" not in locals():
            signal_name = str(signum)
        supervisor.event("shutdown_signal_received", signal=signal_name, signal_number=int(signum), pid=os.getpid())
        supervisor.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        return supervisor.run()
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
