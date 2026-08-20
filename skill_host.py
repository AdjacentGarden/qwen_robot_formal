#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Callable

from skill_runner import RunnerRuntime


MAX_REQUEST_BYTES = 1024 * 1024


class SkillHost:
    def __init__(
        self,
        *,
        robot_project: Path,
        single_function_dir: Path,
        fallback_single_function_dir: Path,
        runtime_dir: Path,
        resident_socket: Path,
    ) -> None:
        self.started_at = time.time()
        self.request_count = 0
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_skills: dict[str, str] = {}
        self.execute_runtime = RunnerRuntime(
            robot_project,
            single_function_dir=single_function_dir,
            runtime_dir=runtime_dir,
            resident_socket=resident_socket,
        )
        # Dry-run uses the legacy standalone scripts, which validate without
        # requiring the resident camera/model owners to be started.
        self.dry_runtime = RunnerRuntime(
            robot_project,
            single_function_dir=fallback_single_function_dir,
            runtime_dir=runtime_dir,
        )

    def handle(
        self,
        request: dict[str, Any],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        request_id = str(request.get("request_id") or "")[:128]
        operation = str(request.get("op") or "invoke")
        if operation == "ping":
            with self._state_lock:
                active = sorted(self._active_skills.values())
            return {
                "ok": True,
                "request_id": request_id,
                "state": "ready",
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "request_count": self.request_count,
                "resident_socket": str(self.execute_runtime.resident_socket or ""),
                "active_skills": active,
            }
        if operation == "cancel_active":
            return self._cancel_active(request_id)
        if operation != "invoke":
            return {"ok": False, "request_id": request_id, "error": f"unsupported_operation:{operation}"}
        skill = str(request.get("skill") or "")
        arguments = request.get("arguments")
        if not skill or not isinstance(arguments, dict):
            return {"ok": False, "request_id": request_id, "error": "invalid_invoke_request"}
        dry_run = bool(request.get("dry_run", True))
        # Serializing at this layer keeps shared executor state deterministic;
        # the lower resident runtime still owns fine-grained hardware locks.
        with self._lock:
            self.request_count += 1
            runtime = self.dry_runtime if dry_run else self.execute_runtime
            with self._state_lock:
                self._active_skills[request_id] = skill
            try:
                result = runtime.execute(
                    skill,
                    arguments,
                    dry_run=dry_run,
                    user_text=str(request.get("user_text") or "")[:1000],
                    event_callback=event_callback,
                )
            finally:
                with self._state_lock:
                    self._active_skills.pop(request_id, None)
        return {"request_id": request_id, **result}

    def _cancel_active(self, request_id: str) -> dict[str, Any]:
        """Bypass the normal execution lock to stop a long resident task.

        The resident runtime already marks these stop operations as priority
        controls.  This host endpoint only reaches that owner; it never opens
        ROS, a serial port, or a second camera.
        """

        with self._state_lock:
            active = sorted(set(self._active_skills.values()))
        stop_targets = {
            "push_up": ("push_up", {"action": "stop"}),
            "pull_up": ("pull_up", {"action": "stop"}),
            "squat": ("squat", {"action": "stop"}),
            "person_tracking": ("person_tracking", {"action": "stop"}),
            "pet_tracking": ("pet_tracking", {"action": "stop"}),
            "navigation_goto": ("navigation_goto", {"action": "stop"}),
        }
        results = []
        for active_skill in active:
            target = stop_targets.get(active_skill)
            if target is None:
                continue
            skill, arguments = target
            result = self.execute_runtime.execute(
                skill,
                arguments,
                dry_run=False,
                user_text="runtime_priority_cancel",
            )
            results.append({"active_skill": active_skill, "result": result})
        return {
            "ok": all(bool(item["result"].get("ok")) for item in results),
            "request_id": request_id,
            "state": "cancel_requested" if results else "nothing_to_cancel",
            "active_skills": active,
            "results": results,
        }


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request_too_large")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request_must_be_object")
            def stream_event(event: dict[str, Any]) -> None:
                envelope = {"type": "skill_event", "event": event}
                self.wfile.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()

            response = self.server.owner.handle(request, stream_event)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
        final = {"type": "final", **response}
        self.wfile.write(json.dumps(final, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent local SkillHost for Qwen Realtime")
    root = Path(__file__).resolve().parent
    parser.add_argument("--socket", type=Path, default=root / "runtime" / "skill_host.sock")
    parser.add_argument("--robot-project", type=Path, default=Path("/home/test/qwen_robot_project"))
    parser.add_argument("--single-function-dir", type=Path, default=root / "robot_skills")
    parser.add_argument("--fallback-single-function-dir", type=Path, default=Path("/home/test/single_function"))
    parser.add_argument("--runtime-dir", type=Path, default=root / "runtime" / "robot_executor")
    parser.add_argument("--resident-socket", type=Path, default=root / "robot_skills" / "runtime" / "resident" / "skills.sock")
    args = parser.parse_args()

    args.socket.parent.mkdir(parents=True, exist_ok=True)
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    if args.socket.exists() or args.socket.is_socket():
        args.socket.unlink()
    owner = SkillHost(
        robot_project=args.robot_project,
        single_function_dir=args.single_function_dir,
        fallback_single_function_dir=args.fallback_single_function_dir,
        runtime_dir=args.runtime_dir,
        resident_socket=args.resident_socket,
    )
    server = UnixServer(str(args.socket), RequestHandler)
    server.owner = owner  # type: ignore[attr-defined]
    os.chmod(args.socket, 0o600)

    def stop(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"event": "skill_host_ready", "pid": os.getpid(), "socket": str(args.socket)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        args.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
