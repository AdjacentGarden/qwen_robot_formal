#!/usr/bin/env python3
"""Measure App-start and direct-terminal-start reliability without motion commands.

The App path connects to the same relay endpoint as the Android application and
sends ``program_start``/``program_stop``.  The terminal path starts the exact
``bash run.sh --execute-skills`` command.  A round is successful only when the
Manager, resident skill runtime, skill host, realtime voice process, navigation
readiness, and Qwen connection are all ready.

This file never publishes cmd_vel, sends a navigation goal, or invokes a robot
skill.  It only starts, observes, and stops the project runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import websockets


PROJECT = Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test")
RUN_SH = PROJECT / "run.sh"
RESIDENT_SERVICE = PROJECT / "resident_service.sh"
ROBOT_STACK = PROJECT / "robot_stack.sh"
CONFIG_JS = PROJECT / "android_app/web/config.js"
APP_CONTROL_SOCKET = PROJECT / "runtime/app_control.sock"
HEALTH_FILE = PROJECT / "runtime/robot_stack/health.json"
SERVICE_PID_FILE = PROJECT / "runtime/resident_service/service.pid"
RESULT_ROOT = PROJECT / "runtime/startup_reliability"
LOCK_FILE = RESULT_ROOT / "test.lock"

COMPONENT_PATTERNS = {
    "manager": "mapping_navigation_manager.py",
    "resident": str(PROJECT / "robot_skills/resident_runtime_server.py"),
    "skill_host": str(PROJECT / "skill_host.py"),
}


@dataclass
class RoundResult:
    mode: str
    round: int
    success: bool
    elapsed_sec: float
    cycle_elapsed_sec: float
    started_at: str
    ack_sec: float | None = None
    endpoint: str = ""
    error: str = ""
    final_state: str = ""
    runtime_mode: str = ""
    components: dict[str, bool] | None = None
    capabilities: dict[str, Any] | None = None
    log_file: str = ""


def run(argv: list[str], timeout: float, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=str(PROJECT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode:
        raise RuntimeError(
            f"command_failed_{completed.returncode}: {' '.join(argv)}: {completed.stdout[-1200:]}"
        )
    return completed


def process_running(pattern: str) -> bool:
    return subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def project_voice_running() -> bool:
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            cmdline = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
            if "realtime_chat.py" not in cmdline:
                continue
            if (item / "cwd").resolve() == PROJECT or str(PROJECT) in cmdline:
                return True
        except OSError:
            continue
    return False


def qwen_control_status() -> dict[str, Any]:
    if not APP_CONTROL_SOCKET.is_socket():
        return {}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1.0)
            conn.connect(str(APP_CONTROL_SOCKET))
            conn.sendall(b'{"op":"status"}\n')
            raw = conn.makefile("rb").readline(1024 * 1024)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def health_status() -> dict[str, Any]:
    try:
        value = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        age = max(0.0, time.time() - float(value.get("updated_at") or 0.0))
        manager = value.get("manager") or {}
        ready = bool(
            age <= 3.0
            and int(value.get("manager_publishers") or 0) == 1
            and str(manager.get("state") or "").upper() == "NAVIGATION"
            and not bool(manager.get("control_conflict"))
        )
        navigation_ready = bool(
            ready
            and manager.get("sensor_gate_enabled") is True
            and str(manager.get("sensor_gate_state") or "").lower() == "ready"
            and (value.get("ready") or {}).get("manager")
        )
        return {
            "operational": ready,
            "navigation_ready": navigation_ready,
            "sensor_gate_state": manager.get("sensor_gate_state"),
            "state": manager.get("state"),
            "age_sec": round(age, 3),
        }
    except Exception as exc:
        return {"operational": False, "navigation_ready": False, "error": str(exc)}


def local_program_status() -> dict[str, Any]:
    health = health_status()
    voice_control = qwen_control_status()
    components = {
        "manager": process_running(COMPONENT_PATTERNS["manager"]) and bool(health["operational"]),
        "resident": process_running(COMPONENT_PATTERNS["resident"]),
        "skill_host": process_running(COMPONENT_PATTERNS["skill_host"]),
        "voice": project_voice_running() and APP_CONTROL_SOCKET.is_socket(),
    }
    capabilities = {
        "navigation_ready": bool(health["navigation_ready"]),
        "voice_ready": bool(voice_control.get("ok") and voice_control.get("connected")),
        "accepting_local_voice": voice_control.get("accepting_local_voice"),
        "microphone_enabled": voice_control.get("enabled"),
        "sensor_gate_state": health.get("sensor_gate_state"),
    }
    if all(components.values()):
        state = "running"
    elif not any(components.values()):
        state = "stopped"
    else:
        state = "partial"
    return {
        "state": state,
        "components": components,
        "capabilities": capabilities,
        "runtime_mode": "navigation_ready" if capabilities["navigation_ready"] else "not_ready",
        "health": health,
    }


def complete_ready(status: dict[str, Any]) -> bool:
    components = status.get("components") or {}
    capabilities = status.get("capabilities") or {}
    return bool(
        status.get("state") == "running"
        and all(components.get(name) for name in ("manager", "resident", "skill_host", "voice"))
        and capabilities.get("navigation_ready")
        and capabilities.get("voice_ready")
    )


def wait_local_ready(process: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"terminal_process_exited_{process.returncode}")
        last = local_program_status()
        if complete_ready(last):
            return last
        time.sleep(0.5)
    raise TimeoutError(f"terminal_ready_timeout: {json.dumps(last, ensure_ascii=False)}")


def wait_local_stopped(timeout: float = 150.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = local_program_status()
        if last.get("state") == "stopped":
            return last
        time.sleep(0.5)
    raise TimeoutError(f"runtime_stop_timeout: {json.dumps(last, ensure_ascii=False)}")


def service_owned() -> bool:
    try:
        pid = int(SERVICE_PID_FILE.read_text(encoding="utf-8").strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        return str(RUN_SH) in cmdline
    except (OSError, ValueError):
        return False


def stop_service_owned_runtime() -> None:
    if service_owned() or local_program_status().get("state") == "stopped":
        run(["bash", str(RESIDENT_SERVICE), "stop"], 150.0, check=True)
        wait_local_stopped()
        return
    raise RuntimeError("refusing_to_stop_unowned_direct_runtime")


def stop_owned_terminal(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=150.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10.0)
            raise RuntimeError("terminal_runtime_required_sigkill")
    wait_local_stopped()


def load_app_config() -> tuple[list[str], str]:
    text = CONFIG_JS.read_text(encoding="utf-8")
    token_match = re.search(r'token\s*:\s*"([^"]+)"', text)
    bases_match = re.search(r"serverBases\s*:\s*\[(.*?)\]", text, re.S)
    if not token_match or not bases_match:
        raise RuntimeError("app_config_missing")
    endpoints = re.findall(r'"(https?://[^"]+)"', bases_match.group(1))
    if not endpoints:
        raise RuntimeError("app_server_endpoints_missing")
    return endpoints, token_match.group(1)


class AppClient:
    def __init__(self, endpoints: list[str], token: str) -> None:
        self.endpoints = endpoints
        self.token = token
        self.endpoint = ""
        self.ws: Any = None

    async def connect(self) -> str:
        errors = []
        for endpoint in self.endpoints:
            uri = re.sub(r"^http", "ws", endpoint.rstrip("/")) + "/ws/app?token=" + quote(self.token)
            try:
                self.ws = await websockets.connect(
                    uri,
                    open_timeout=12,
                    close_timeout=3,
                    ping_interval=10,
                    ping_timeout=10,
                    max_size=4 * 1024 * 1024,
                )
                self.endpoint = endpoint
                await asyncio.wait_for(self.ws.recv(), timeout=10)
                return endpoint
            except Exception as exc:
                errors.append(f"{endpoint}:{type(exc).__name__}:{exc}")
                if self.ws is not None:
                    await self.ws.close()
                self.ws = None
        raise RuntimeError("app_relay_connect_failed: " + " | ".join(errors))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def command(self, action: str, timeout: float) -> tuple[dict[str, Any], float | None]:
        if self.ws is None:
            await self.connect()
        command_id = f"startup_test_{action}_{uuid.uuid4().hex[:12]}"
        sent = time.monotonic()
        await self.ws.send(json.dumps({"id": command_id, "action": action}))
        ack_sec: float | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            message = json.loads(raw)
            if str(message.get("id") or "") != command_id:
                continue
            if message.get("type") == "command_ack":
                if not message.get("ok"):
                    raise RuntimeError(f"app_command_rejected: {message.get('error')}")
                ack_sec = round(time.monotonic() - sent, 3)
            elif message.get("type") == "command_result":
                if not message.get("ok"):
                    raise RuntimeError(f"app_command_failed: {message.get('error')}")
                return dict(message.get("result") or {}), ack_sec
        raise TimeoutError(f"app_{action}_timeout")


async def app_round(index: int, timeout: float) -> RoundResult:
    endpoints, token = load_app_config()
    client = AppClient(endpoints, token)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.monotonic()
    ack_sec = None
    status: dict[str, Any] = {}
    error = ""
    success = False
    endpoint = ""
    startup_elapsed = 0.0
    try:
        endpoint = await client.connect()
        result, ack_sec = await client.command("program_start", timeout)
        status = dict(result.get("program") or {})
        if not complete_ready(status):
            raise RuntimeError(f"app_incomplete_start: {json.dumps(status, ensure_ascii=False)}")
        success = True
        startup_elapsed = time.monotonic() - started
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        startup_elapsed = time.monotonic() - started
    finally:
        try:
            if client.ws is None:
                await client.connect()
            await client.command("program_stop", 180.0)
            await asyncio.to_thread(wait_local_stopped)
        except Exception as exc:
            success = False
            error = (error + " | " if error else "") + f"cleanup:{type(exc).__name__}:{exc}"
            try:
                await asyncio.to_thread(stop_service_owned_runtime)
            except Exception as cleanup_exc:
                error += f" | fallback_cleanup:{type(cleanup_exc).__name__}:{cleanup_exc}"
        await client.close()
    return RoundResult(
        mode="app",
        round=index,
        success=success,
        elapsed_sec=round(startup_elapsed, 3),
        cycle_elapsed_sec=round(time.monotonic() - started, 3),
        started_at=started_at,
        ack_sec=ack_sec,
        endpoint=endpoint,
        error=error,
        final_state=str(status.get("state") or ""),
        runtime_mode=str(status.get("runtime_mode") or ""),
        components=status.get("components"),
        capabilities=status.get("capabilities"),
    )


def terminal_round(index: int, timeout: float, run_dir: Path) -> RoundResult:
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    status: dict[str, Any] = {}
    error = ""
    success = False
    log_file = run_dir / f"terminal_round_{index:02d}.log"
    startup_elapsed = 0.0
    try:
        if local_program_status().get("state") != "stopped":
            raise RuntimeError("runtime_not_stopped_before_terminal_round")
        with log_file.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(
                ["bash", str(RUN_SH), "--execute-skills"],
                cwd=str(PROJECT),
                text=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            status = wait_local_ready(process, timeout)
            success = True
            startup_elapsed = time.monotonic() - started
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if log_file.is_file():
            tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
            error += " | log_tail=" + tail.replace("\n", "\\n")
        startup_elapsed = time.monotonic() - started
    finally:
        try:
            stop_owned_terminal(process)
        except Exception as exc:
            success = False
            error = (error + " | " if error else "") + f"cleanup:{type(exc).__name__}:{exc}"
    return RoundResult(
        mode="terminal",
        round=index,
        success=success,
        elapsed_sec=round(startup_elapsed, 3),
        cycle_elapsed_sec=round(time.monotonic() - started, 3),
        started_at=started_at,
        error=error,
        final_state=str(status.get("state") or ""),
        runtime_mode=str(status.get("runtime_mode") or ""),
        components=status.get("components"),
        capabilities=status.get("capabilities"),
        log_file=str(log_file),
    )


def save_report(run_dir: Path, results: list[RoundResult], complete: bool) -> dict[str, Any]:
    modes: dict[str, Any] = {}
    for mode in ("app", "terminal"):
        selected = [item for item in results if item.mode == mode]
        passed = sum(item.success for item in selected)
        modes[mode] = {
            "rounds_completed": len(selected),
            "successes": passed,
            "failures": len(selected) - passed,
            "success_rate_percent": round(100.0 * passed / len(selected), 2) if selected else None,
            "average_elapsed_sec": round(
                sum(item.elapsed_sec for item in selected) / len(selected), 3
            ) if selected else None,
        }
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "complete": complete,
        "safety": {
            "motion_commands_sent": 0,
            "navigation_goals_sent": 0,
            "robot_skills_invoked": 0,
        },
        "summary": modes,
        "rounds": [asdict(item) for item in results],
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (run_dir / "rounds.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "mode", "round", "success", "elapsed_sec", "ack_sec", "started_at",
                "cycle_elapsed_sec", "endpoint", "final_state", "runtime_mode", "error", "log_file",
            ],
        )
        writer.writeheader()
        for item in results:
            value = asdict(item)
            writer.writerow({key: value.get(key) for key in writer.fieldnames})
    (RESULT_ROOT / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def validate_only() -> None:
    missing = [str(path) for path in (PROJECT, RUN_SH, RESIDENT_SERVICE, ROBOT_STACK, CONFIG_JS) if not path.exists()]
    if missing:
        raise RuntimeError("missing_dependencies: " + ",".join(missing))
    endpoints, token = load_app_config()
    if not token or len(token) < 16:
        raise RuntimeError("invalid_app_token")
    if not all(endpoint.startswith(("http://", "https://")) for endpoint in endpoints):
        raise RuntimeError("invalid_app_endpoint")
    print(json.dumps({"ok": True, "endpoints": endpoints, "motion_commands": 0}, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-rounds", type=int, default=20)
    parser.add_argument("--terminal-rounds", type=int, default=20)
    parser.add_argument("--startup-timeout", type=float, default=900.0)
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.validate_only:
        validate_only()
        return 0
    if not 1 <= args.app_rounds <= 100 or not 1 <= args.terminal_rounds <= 100:
        raise SystemExit("round count must be between 1 and 100")
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    lock_stream = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another startup reliability test is already running")
    validate_only()
    if local_program_status().get("state") != "stopped":
        stop_service_owned_runtime()
    if not subprocess.run(
        ["systemctl", "is-active", "--quiet", "ideal-robot-app-bridge.service"],
        check=False,
    ).returncode == 0:
        raise SystemExit("App bridge systemd service is not active")

    run_dir = RESULT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    results: list[RoundResult] = []
    complete = False
    try:
        for index in range(1, args.app_rounds + 1):
            item = asyncio.run(app_round(index, args.startup_timeout))
            results.append(item)
            report = save_report(run_dir, results, False)
            print(
                f"[app {index}/{args.app_rounds}] {'PASS' if item.success else 'FAIL'} "
                f"{item.elapsed_sec:.3f}s rate={report['summary']['app']['success_rate_percent']}%",
                flush=True,
            )
            time.sleep(args.settle_seconds)
        for index in range(1, args.terminal_rounds + 1):
            item = terminal_round(index, args.startup_timeout, run_dir)
            results.append(item)
            report = save_report(run_dir, results, False)
            print(
                f"[terminal {index}/{args.terminal_rounds}] {'PASS' if item.success else 'FAIL'} "
                f"{item.elapsed_sec:.3f}s rate={report['summary']['terminal']['success_rate_percent']}%",
                flush=True,
            )
            time.sleep(args.settle_seconds)
        complete = True
    except KeyboardInterrupt:
        print("interrupted; partial report preserved", file=sys.stderr)
    finally:
        report = save_report(run_dir, results, complete)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
        print(f"report={run_dir / 'report.json'}", flush=True)
    expected = args.app_rounds + args.terminal_rounds
    return 0 if complete and len(results) == expected and all(item.success for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
