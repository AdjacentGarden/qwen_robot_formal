#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import math
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

import websockets

try:
    from .map_codec import occupancy_to_png
except ImportError:  # Direct script execution used by robot_bridge.sh.
    from map_codec import occupancy_to_png


ROOT = Path(__file__).resolve().parent
PROJECT = Path(os.environ.get("ROBOT_PROJECT", str(ROOT.parents[1])))
RESIDENT_SOCKET = PROJECT / "robot_skills/runtime/resident/skills.sock"
VIDEO_GLOB = PROJECT / "robot_skills/runtime/exploration"
FITNESS_VIDEO_GLOB = PROJECT / "robot_skills/runtime/fitness"
APP_CONTROL_SOCKET = PROJECT / "runtime/app_control.sock"
APP_VOICE_RUNTIME = PROJECT / "runtime/app_voice"
MICROPHONE_STATE_FILE = PROJECT / "runtime/microphone_state.json"
MAX_APP_VOICE_BYTES = 4 * 1024 * 1024
TELEMETRY_INTERVAL_SEC = 0.25
LINK_HEARTBEAT_INTERVAL_SEC = 0.75
MAP_SEND_MIN_INTERVAL_SEC = 2.0
ASYNC_STATUS_TIMEOUT_SEC = 1.5
WEBSOCKET_SEND_TIMEOUT_SEC = 8.0


def load_microphone_state() -> Dict[str, Any]:
    enabled = True
    try:
        value = json.loads(MICROPHONE_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
            enabled = value["enabled"]
    except (OSError, ValueError, TypeError):
        pass
    return {
        "enabled": enabled,
        "accepting_local_voice": False,
        "app_voice_enabled": True,
    }


def save_microphone_state(enabled: bool) -> None:
    MICROPHONE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = MICROPHONE_STATE_FILE.with_name(
        f".{MICROPHONE_STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(
        json.dumps(
            {
                "enabled": bool(enabled),
                "updated_at": time.time(),
                "source": "android_app",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(MICROPHONE_STATE_FILE)


def compatibility_microphone_value(command_id: Any) -> Optional[bool]:
    """Decode the fallback carried through older relay servers.

    Old relays validate ``request_state`` but preserve the authenticated
    command id.  This keeps existing deployed relays usable until their server
    process is upgraded, without overloading any hardware command.
    """

    match = re.fullmatch(r"mic-set-([01])-[A-Za-z0-9_-]{8,80}", str(command_id or ""))
    if not match:
        return None
    return match.group(1) == "1"


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = conn.recv(size)
        if not chunk:
            raise ConnectionError("resident_runtime_closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def resident_skill(skill: str, argv: list[str], timeout: float = 30.0) -> Dict[str, Any]:
    payload = json.dumps({"op": "skill_run", "skill": skill, "argv": argv, "stream": False, "payload_len": 0}, ensure_ascii=False).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(RESIDENT_SOCKET))
        conn.sendall(struct.pack("!I", len(payload)) + payload)
        while True:
            size = struct.unpack("!I", recv_exact(conn, 4))[0]
            result = json.loads(recv_exact(conn, size).decode("utf-8"))
            if result.get("type") in {"stdout", "stderr"}:
                continue
            if result.get("type") == "final":
                result.pop("type", None)
            return result


class RosAdapter:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.pose: Optional[Dict[str, float]] = None
        self.map_message: Optional[Dict[str, Any]] = None
        self.map_signature = None
        self.goal_lock = threading.RLock()
        self.goal_handle = None
        self.cancel_requested = threading.Event()
        if dry_run:
            return
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import OccupancyGrid
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_srvs.srv import Trigger
        from tf2_ros import Buffer, TransformListener

        self.rclpy = rclpy
        self.Twist = Twist
        self.Trigger = Trigger
        rclpy.init(args=None)
        self.node = rclpy.create_node("android_robot_bridge")
        self.publisher = self.node.create_publisher(Twist, "/cmd_vel_external", 10)
        self.cancel_nav_client = self.node.create_client(
            Trigger, "/motion_controller/cancel_nav_goal"
        )
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL, history=HistoryPolicy.KEEP_LAST)
        self.node.create_subscription(OccupancyGrid, "/map", self._map_callback, qos)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node)
        self.thread = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True, name="android-bridge-ros")
        self.thread.start()

    def _map_callback(self, msg) -> None:
        signature = (msg.info.width, msg.info.height, msg.info.resolution, msg.header.stamp.sec, msg.header.stamp.nanosec)
        if signature == self.map_signature:
            return
        try:
            encoded, stats = occupancy_to_png(msg.data, msg.info.width, msg.info.height)
        except ValueError as exc:
            # Nav2/Cartographer can publish a transient 0x0 map while their
            # lifecycle nodes restart.  That sample is not an App map update
            # and must not terminate the long-lived ROS executor thread.
            self.map_signature = signature
            print(json.dumps({
                "event": "invalid_map_ignored",
                "width": int(msg.info.width),
                "height": int(msg.info.height),
                "cells": len(msg.data),
                "error": str(exc),
            }, ensure_ascii=False), flush=True)
            return
        self.map_signature = signature
        q = msg.info.origin.orientation
        origin_yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        self.map_message = {
            "type": "map", "image_base64": encoded,
            "meta": {
                "width": int(msg.info.width), "height": int(msg.info.height),
                "resolution": float(msg.info.resolution),
                "origin": {
                    "x": float(msg.info.origin.position.x),
                    "y": float(msg.info.origin.position.y),
                    "yaw": float(origin_yaw),
                },
                "frame_id": msg.header.frame_id or "map", **stats,
            },
        }

    def update_pose(self) -> None:
        if self.dry_run:
            return
        try:
            transform = self.tf_buffer.lookup_transform("map", "base_footprint", self.rclpy.time.Time())
            t, q = transform.transform.translation, transform.transform.rotation
            yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
            self.pose = {"x": float(t.x), "y": float(t.y), "yaw": float(yaw), "timestamp": time.time()}
        except Exception:
            pass

    def stop(self) -> None:
        if self.dry_run:
            return
        self.cancel_navigation()
        twist = self.Twist()
        for _ in range(3):
            self.publisher.publish(twist)
            time.sleep(0.03)

    def move(self, direction: str, duration: float, linear: float, angular: float) -> Dict[str, Any]:
        if self.dry_run:
            return {"status": "dry_run", "direction": direction, "duration": duration, "first_publish_ms": 0.0}
        raise RuntimeError("direct_motion_disabled_use_qwen_manager_adapter")

    def cancel_navigation(self) -> None:
        self.cancel_requested.set()
        if self.dry_run or not self.cancel_nav_client.service_is_ready():
            return
        with contextlib.suppress(Exception):
            self.cancel_nav_client.call_async(self.Trigger.Request())

    @staticmethod
    def _wait_future(future, timeout: float):
        event = threading.Event()
        future.add_done_callback(lambda _: event.set())
        if not event.wait(timeout):
            raise TimeoutError("future_timeout")
        return future.result()

    def navigate(self, x: float, y: float, yaw: float, timeout: float = 120.0) -> Dict[str, Any]:
        if self.dry_run:
            return {"status": "dry_run", "x": x, "y": y, "yaw": yaw}
        raise RuntimeError("direct_navigation_disabled_use_qwen_manager_adapter")


class ProgramController:
    """Start and stop the Qwen realtime resident while leaving the App bridge alive."""

    COMPONENT_NAMES = ("manager", "resident", "skill_host", "voice")

    def __init__(self, project: Path, dry_run: bool = False) -> None:
        self.project = project
        self.service_state_file = project / "runtime/resident_service/service_state.json"
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.process_patterns = {
            "manager": "mapping_navigation_manager.py",
            "resident": str(project / "robot_skills/resident_runtime_server.py"),
            "skill_host": str(project / "skill_host.py"),
            "voice": str(project / "realtime_chat.py"),
        }

    @staticmethod
    def _process_running(pattern: str) -> bool:
        return subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0

    @staticmethod
    def _pid_matches(pid: int, pattern: str) -> bool:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            return pattern in cmdline
        except OSError:
            return False

    @staticmethod
    def _qwen_voice_connected() -> bool:
        if not APP_CONTROL_SOCKET.is_socket():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(0.5)
                conn.connect(str(APP_CONTROL_SOCKET))
                conn.sendall(b'{"op":"status"}\n')
                raw = conn.makefile("rb").readline(1024 * 1024)
            value = json.loads(raw.decode("utf-8"))
            return bool(value.get("ok") and value.get("connected"))
        except Exception:
            return False

    def _manager_ready(self) -> bool:
        path = self.project / "runtime/robot_stack/health.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            manager = value.get("manager") or {}
            return bool(
                (value.get("ready") or {}).get("manager")
                and str(manager.get("state") or "").upper() == "NAVIGATION"
                and str(manager.get("sensor_gate_state") or "").lower() == "ready"
            )
        except Exception:
            return False

    def wait_for_voice_ready(self, timeout: float = 10.0) -> bool:
        """Wait through a short Qwen idle-session reconnect.

        The realtime provider rotates an otherwise healthy idle session after
        180 seconds. App voice may arrive in that brief window, so rejecting it
        as if the whole robot program were stopped produces a false failure.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self._qwen_voice_connected():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.20)

    def status(self) -> Dict[str, Any]:
        if self.dry_run:
            return {"state": "dry_run", "components": {name: True for name in self.COMPONENT_NAMES}}
        components = {name: self._process_running(pattern) for name, pattern in self.process_patterns.items()}
        components["manager"] = bool(components.get("manager") and self._manager_ready())
        # run.sh intentionally starts the voice process with a relative argv,
        # so an absolute pgrep pattern can report a false negative. The local
        # control socket is the authoritative readiness check used by the App.
        components["voice"] = self._qwen_voice_connected()
        service_transition = None
        try:
            service_state = json.loads(self.service_state_file.read_text(encoding="utf-8"))
            transition = str(service_state.get("state") or "")
            service_pid = int(service_state.get("pid") or 0)
            if transition in {"starting", "stopping"} and service_pid > 0:
                os.kill(service_pid, 0)
                if self._pid_matches(service_pid, str(self.project / "run.sh")):
                    service_transition = transition
        except (OSError, ValueError, TypeError):
            pass
        required = [components[name] for name in self.COMPONENT_NAMES]
        if service_transition is not None:
            state = service_transition
        elif all(required):
            state = "running"
        elif not any(components.values()):
            state = "stopped"
        else:
            state = "partial"
        return {"state": state, "components": components, "timestamp": time.time()}

    def _run(self, argv: list[str], timeout: float) -> Dict[str, Any]:
        completed = subprocess.run(
            argv,
            cwd=str(self.project),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout[-6000:]
        if completed.returncode != 0:
            raise RuntimeError(f"program_command_failed_{completed.returncode}: {output[-1200:]}")
        return {"returncode": completed.returncode, "output": output}

    def start(self) -> Dict[str, Any]:
        with self.lock:
            before = self.status()
            if self.dry_run:
                return {"status": "dry_run", "operation": "start", "program": before}
            if before["state"] == "running" and before["components"].get("voice"):
                return {"status": "already_running", "program": before}
            if before["state"] == "starting":
                return {"status": "already_starting", "program": before}
            if before["state"] == "stopping":
                raise RuntimeError("program_stop_in_progress")
            # A previous degraded start intentionally leaves voice available.
            # Stop that process before retrying the complete robot stack;
            # resident_service itself otherwise treats a live control socket as
            # already started and would never retry navigation.
            if before["state"] == "partial" and before["components"].get("voice"):
                self._run(["bash", str(self.project / "resident_service.sh"), "stop"], 120.0)
            try:
                # Keep the App request alive longer than resident_service's
                # complete two-attempt Manager state machine.  A shorter bridge
                # timeout could kill only the supervising shell mid-retry and
                # leave an otherwise recoverable startup looking incomplete.
                service = self._run(
                    ["bash", str(self.project / "resident_service.sh"), "start"],
                    780.0,
                )
            except Exception:
                # Always use the project's ownership-aware stop path after an
                # interrupted/failed start so the next button press begins
                # from a deterministic state.
                with contextlib.suppress(Exception):
                    self._run(
                        ["bash", str(self.project / "resident_service.sh"), "stop"],
                        120.0,
                    )
                raise
            program = self.status()
            components = program.get("components") or {}
            missing = [name for name in self.COMPONENT_NAMES if not components.get(name)]
            if program.get("state") != "running" or missing:
                # The App's start button means complete robot startup. Never
                # report success for a voice-only or otherwise partial stack.
                # Roll back survivors so the next press starts deterministically.
                with contextlib.suppress(Exception):
                    self._run(
                        ["bash", str(self.project / "resident_service.sh"), "stop"],
                        120.0,
                    )
                raise RuntimeError(
                    "program_incomplete_after_start: missing=" + ",".join(missing)
                )
            return {"status": "started", "program": program, "service": service}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            before = self.status()
            if self.dry_run:
                return {"status": "dry_run", "operation": "stop", "program": before}
            service = self._run(["bash", str(self.project / "resident_service.sh"), "stop"], 120.0)
            return {"status": "stopped", "program": self.status(), "service": service}


class Bridge:
    def __init__(self, servers: list[str], token: str, dry_run: bool) -> None:
        self.servers = list(dict.fromkeys(server.rstrip("/") for server in servers if server.strip()))
        if not self.servers:
            raise ValueError("at_least_one_relay_server_is_required")
        self.server = self.servers[0]
        self.token = token
        self.dry_run = dry_run
        self.ros = RosAdapter(dry_run=dry_run)
        self.program = ProgramController(PROJECT, dry_run=dry_run)
        self.program_state = self.program.status()
        self.last_map_signature = None
        self.uploaded = set()
        self.websocket = None
        self.task_state: Dict[str, Any] = {"active": False, "planning": False, "queued": 0}
        self.microphone_state: Dict[str, Any] = load_microphone_state()
        self.command_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.last_telemetry_monotonic = 0.0
        self.last_map_sent_monotonic = 0.0

    @staticmethod
    def qwen_control_request(request: Dict[str, Any], timeout: float = 150.0) -> Dict[str, Any]:
        if not APP_CONTROL_SOCKET.exists():
            raise RuntimeError("qwen_realtime_unavailable")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(APP_CONTROL_SOCKET))
            conn.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            response = bytearray()
            while b"\n" not in response:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 2 * 1024 * 1024:
                    raise RuntimeError("qwen_realtime_response_too_large")
        if not response:
            raise RuntimeError("qwen_realtime_empty_response")
        return json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))

    def qwen_realtime_status(self) -> Dict[str, Any]:
        try:
            result = self.qwen_control_request({"op": "status"}, timeout=0.7)
            if result.get("ok"):
                microphone = result.get("microphone")
                if isinstance(microphone, dict):
                    self.microphone_state = dict(microphone)
                return result
            return {"active": False, "planning": False, "queued": 0, "microphone": self.microphone_state, "error": result.get("error")}
        except Exception as exc:
            self.microphone_state = load_microphone_state()
            return {"active": False, "planning": False, "queued": 0, "microphone": self.microphone_state, "available": False, "error": f"{type(exc).__name__}: {exc}"}

    def set_microphone_enabled(self, enabled: bool) -> Dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("microphone_enabled_must_be_boolean")
        if self.dry_run:
            microphone = {
                "enabled": enabled,
                "accepting_local_voice": enabled,
                "app_voice_enabled": True,
            }
            self.microphone_state = microphone
            return {"status": "dry_run", "applied": False, "microphone": microphone}
        save_microphone_state(enabled)
        if not APP_CONTROL_SOCKET.is_socket():
            self.microphone_state = load_microphone_state()
            return {
                "status": "saved_for_next_start",
                "applied": False,
                "microphone": self.microphone_state,
            }
        try:
            result = self.qwen_control_request(
                {"op": "microphone_set", "enabled": enabled},
                timeout=5.0,
            )
        except (ConnectionError, OSError, TimeoutError) as exc:
            # A cleanly stopped voice process can leave its Unix socket inode
            # behind.  The preference is already persisted, so treat that
            # specific offline case as "apply on next start" instead of
            # showing a false failure in the App.  If voice is still running,
            # propagate the error: claiming an immediate mute would be unsafe.
            components = (self.program.status().get("components") or {})
            if components.get("voice"):
                raise
            self.microphone_state = load_microphone_state()
            return {
                "status": "saved_for_next_start",
                "applied": False,
                "microphone": self.microphone_state,
                "runtime_control": f"offline: {type(exc).__name__}",
            }
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "microphone_control_failed"))
        microphone = result.get("microphone")
        if not isinstance(microphone, dict):
            microphone = load_microphone_state()
        self.microphone_state = dict(microphone)
        return {
            "status": "enabled" if enabled else "disabled",
            "applied": True,
            "microphone": self.microphone_state,
        }

    @staticmethod
    def app_command_intent_text(command: Dict[str, Any]) -> str:
        """Describe an authenticated App control without bypassing intent guards.

        App buttons are explicit user actions, but the bridge previously sent
        all of them to the shared skill layer as the opaque text ``App 控制``.
        The speech-side semantic safety guard therefore rejected map
        navigation because the text contained no navigation evidence.  Keep
        the guard and provide a faithful description of the button the user
        actually pressed instead.
        """

        action = str(command.get("action") or "")
        if action == "navigate":
            return (
                "App 地图导航到坐标 "
                f"x={float(command['x']):.3f}, y={float(command['y']):.3f}, "
                f"yaw={float(command.get('yaw', 0.0)):.3f}"
            )
        if action == "light":
            return "App 按键打开灯光" if str(command.get("state")) == "on" else "App 按键关闭灯光"
        if action == "feed":
            return f"App 按键启动投食器，投食{int(command['grams'])}克"
        if action == "manual_move":
            labels = {
                "forward": "向前移动",
                "backward": "向后移动",
                "left": "向左转",
                "right": "向右转",
                "stop": "停止移动",
            }
            return "App 按键" + labels.get(str(command.get("direction") or ""), "控制机器人")
        return "App 控制"

    def execute_app_plan(self, command: Dict[str, Any]) -> Dict[str, Any]:
        action = str(command.get("action") or "")
        arguments: Dict[str, Any]
        if action == "manual_move":
            direction = str(command.get("direction") or "")
            if direction == "stop":
                result = self.qwen_control_request({"op": "cancel_all"}, timeout=12.0)
                self.ros.stop()
                return result
            skill = {"forward": "move_forward", "backward": "move_backward", "left": "move_left", "right": "move_right"}.get(direction)
            if not skill:
                raise ValueError("invalid_direction")
            step_action = "move" if direction in {"forward", "backward"} else "turn"
            arguments = {"duration": float(command["duration"])}
            if direction in {"forward", "backward"}:
                arguments["speed"] = float(command["linear_speed"])
            else:
                arguments["angular_speed"] = float(command["angular_speed"])
            busy_policy = "reject"
        elif action == "navigate":
            skill, step_action = "navigation_goto", "goto"
            arguments = {
                "x": float(command["x"]), "y": float(command["y"]),
                "yaw": float(command.get("yaw", 0.0)), "frame_id": "map", "timeout": 120.0,
            }
            busy_policy = "reject"
        elif action == "light":
            skill, step_action, arguments = "light_control", str(command["state"]), {}
            busy_policy = "queue"
        elif action == "feed":
            skill, step_action = "feeder_control", "feed"
            arguments = {"grams": int(command["grams"])}
            busy_policy = "queue"
        else:
            raise ValueError("unsupported_app_plan_action")
        result = self.qwen_control_request(
            {
                "op": "app_skill",
                "skill": skill,
                "arguments": {"action": step_action, **arguments},
                "busy_policy": busy_policy,
                "user_text": self.app_command_intent_text(command),
            },
            timeout=155.0,
        )
        if not result.get("ok"):
            error = str(result.get("error") or "")
            if not error and result.get("steps"):
                error = str(result["steps"][0].get("error") or "app_plan_failed")
            raise RuntimeError(error or "app_plan_failed")
        return result

    def execute_manual_move_fast(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Route App motion through the same Manager-aware Qwen adapter."""
        direction = str(command.get("direction") or "")
        if direction == "stop":
            self.ros.stop()
            def cancel_coordinator() -> None:
                with contextlib.suppress(Exception):
                    self.qwen_control_request({"op": "cancel_all"}, timeout=2.0)
            threading.Thread(target=cancel_coordinator, name="app-stop-cancel", daemon=True).start()
            return {"status": "stopped", "fast_path": True, "first_publish_ms": 0.0}
        result = self.execute_app_plan(command)
        result["fast_path"] = False
        result["transport"] = "qwen_car_real_copy_manager_adapter"
        return result

    def execute_app_voice(self, command: Dict[str, Any]) -> Dict[str, Any]:
        encoded = str(command.get("audio_base64") or "")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("invalid_voice_audio") from exc
        if not audio or len(audio) > MAX_APP_VOICE_BYTES:
            raise ValueError("invalid_voice_audio_size")
        if not APP_CONTROL_SOCKET.exists():
            raise RuntimeError("qwen_realtime_unavailable")
        APP_VOICE_RUNTIME.mkdir(parents=True, exist_ok=True)
        command_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(command.get("id") or "voice"))[:80]
        mime = str(command.get("mime_type") or "audio/webm").lower()
        suffix = ".ogg" if "ogg" in mime else ".wav" if "wav" in mime else ".webm"
        source_path = APP_VOICE_RUNTIME / f"{command_id}{suffix}"
        pcm_path = APP_VOICE_RUNTIME / f"{command_id}.pcm"
        source_path.write_bytes(audio)
        try:
            conversion = subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-probesize", "65536", "-analyzeduration", "2000000",
                    "-i", str(source_path),
                    "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", str(pcm_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=6.0,
                check=False,
            )
            if conversion.returncode != 0 or not pcm_path.is_file() or pcm_path.stat().st_size < 3200:
                raise RuntimeError(f"voice_audio_decode_failed:{conversion.stderr[-300:]}")
            request = {
                "op": "app_voice",
                "pcm_path": str(pcm_path),
                "sample_rate": 16000,
                "duration_ms": int(command.get("duration_ms") or 0),
                "source": "android_app",
            }
            result = self.qwen_control_request(request, timeout=150.0)
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "voice_command_failed"))
            return result
        finally:
            source_path.unlink(missing_ok=True)
            pcm_path.unlink(missing_ok=True)

    async def send(self, payload: Dict[str, Any]) -> None:
        websocket = self.websocket
        if websocket is not None:
            # Telemetry, heartbeats and command results are produced by separate
            # tasks. Serialize writes so one large map frame cannot corrupt or
            # race a small heartbeat frame on the same WebSocket.
            async with self.send_lock:
                if self.websocket is not websocket:
                    return
                await asyncio.wait_for(
                    websocket.send(json.dumps(payload, ensure_ascii=False)),
                    timeout=WEBSOCKET_SEND_TIMEOUT_SEC,
                )

    async def execute_command(self, command: Dict[str, Any]) -> None:
        command_id, action = command.get("id"), command.get("action")
        result_action = action
        try:
            compatibility_enabled = (
                compatibility_microphone_value(command_id)
                if action == "request_state"
                else None
            )
            if action == "microphone_set" or compatibility_enabled is not None:
                enabled = (
                    command.get("enabled")
                    if action == "microphone_set"
                    else compatibility_enabled
                )
                if not isinstance(enabled, bool):
                    raise ValueError("microphone_enabled_must_be_boolean")
                result = await asyncio.to_thread(self.set_microphone_enabled, enabled)
                result_action = "microphone_set"
            elif action == "program_start":
                self.program_state = {**self.program.status(), "state": "starting"}
                await self.send({"type": "program_status", "program": self.program_state, "timestamp": time.time()})
                result = await asyncio.to_thread(self.program.start)
                self.program_state = result["program"]
            elif action == "program_stop":
                self.program_state = {**self.program.status(), "state": "stopping"}
                await self.send({"type": "program_status", "program": self.program_state, "timestamp": time.time()})
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.qwen_control_request, {"op": "cancel_all"}, 12.0)
                await asyncio.to_thread(self.ros.stop)
                result = await asyncio.to_thread(self.program.stop)
                self.program_state = result["program"]
            elif action == "program_status":
                self.program_state = await asyncio.to_thread(self.program.status)
                result = {"status": "completed", "program": self.program_state}
            elif action == "manual_move":
                if self.program.status()["state"] != "running":
                    raise RuntimeError("robot_program_not_running")
                if self.dry_run:
                    result = {
                        "status": "dry_run", "direction": command["direction"],
                        "duration": command["duration"], "fast_path": True,
                        "first_publish_ms": 0.0,
                    }
                else:
                    async with self.command_lock:
                        result = await asyncio.to_thread(self.execute_manual_move_fast, command)
            elif action == "navigate":
                if self.program.status()["state"] != "running":
                    raise RuntimeError("robot_program_not_running")
                if self.dry_run:
                    result = {"status": "dry_run", "x": command["x"], "y": command["y"], "yaw": command.get("yaw", 0.0)}
                else:
                    async with self.command_lock:
                        result = await asyncio.to_thread(self.execute_app_plan, command)
            elif action == "stop":
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.qwen_control_request, {"op": "cancel_all"}, 12.0)
                await asyncio.to_thread(self.ros.stop)
                if not self.dry_run:
                    await asyncio.to_thread(resident_skill, "pet_map_search", ["stop", "--json"], 8.0)
                result = {"status": "stopped"}
            elif action == "light":
                if self.program.status()["state"] != "running":
                    raise RuntimeError("robot_program_not_running")
                result = {"status": "dry_run", "state": command["state"]} if self.dry_run else await asyncio.to_thread(self.execute_app_plan, command)
            elif action == "feed":
                if self.program.status()["state"] != "running":
                    raise RuntimeError("robot_program_not_running")
                result = {"status": "dry_run", "grams": command["grams"]} if self.dry_run else await asyncio.to_thread(self.execute_app_plan, command)
            elif action == "voice_audio":
                if self.dry_run:
                    result = {"status": "dry_run", "duration_ms": int(command.get("duration_ms") or 0)}
                else:
                    voice_ready = await asyncio.to_thread(self.program.wait_for_voice_ready, 10.0)
                    if not voice_ready:
                        raise RuntimeError("qwen_realtime_unavailable")
                    result = await asyncio.to_thread(self.execute_app_voice, command)
            elif action == "request_state":
                result = {"status": "ready"}
            else:
                raise ValueError("unsupported_action")
            await self.send({"type": "command_result", "id": command_id, "action": result_action, "ok": True, "result": result, "timestamp": time.time()})
        except Exception as exc:
            if action == "program_start":
                with contextlib.suppress(Exception):
                    self.program_state = await asyncio.to_thread(self.program.status)
                    await self.send({
                        "type": "program_status",
                        "program": self.program_state,
                        "timestamp": time.time(),
                    })
            await self.send({"type": "command_result", "id": command_id, "action": result_action, "ok": False, "error": f"{type(exc).__name__}: {exc}", "timestamp": time.time()})

    async def telemetry_loop(self) -> None:
        next_program_check = 0.0
        next_task_check = 0.0
        while True:
            errors: Dict[str, str] = {}
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.ros.update_pose),
                    timeout=ASYNC_STATUS_TIMEOUT_SEC,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors["pose"] = f"{type(exc).__name__}: {exc}"
            now = time.monotonic()
            if now >= next_program_check:
                try:
                    self.program_state = await asyncio.wait_for(
                        asyncio.to_thread(self.program.status),
                        timeout=ASYNC_STATUS_TIMEOUT_SEC,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors["program"] = f"{type(exc).__name__}: {exc}"
                next_program_check = now + 1.0
            if now >= next_task_check:
                try:
                    self.task_state = (
                        {"active": False, "planning": False, "queued": 0, "dry_run": True}
                        if self.dry_run else await asyncio.wait_for(
                            asyncio.to_thread(self.qwen_realtime_status),
                            timeout=ASYNC_STATUS_TIMEOUT_SEC,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors["task"] = f"{type(exc).__name__}: {exc}"
                next_task_check = now + 0.8
            microphone = self.task_state.get("microphone")
            if isinstance(microphone, dict):
                self.microphone_state = dict(microphone)
            await self.send({
                "type": "telemetry",
                "pose": self.ros.pose,
                "mode": "dry_run" if self.dry_run else "ready",
                "program": self.program_state,
                "task": self.task_state,
                "microphone": self.microphone_state,
                "status_errors": errors,
                "timestamp": time.time(),
            })
            self.last_telemetry_monotonic = time.monotonic()
            if (
                self.ros.map_message
                and self.ros.map_signature != self.last_map_signature
                and time.monotonic() - self.last_map_sent_monotonic >= MAP_SEND_MIN_INTERVAL_SEC
            ):
                await self.send(self.ros.map_message)
                self.last_map_signature = self.ros.map_signature
                self.last_map_sent_monotonic = time.monotonic()
            await asyncio.sleep(TELEMETRY_INTERVAL_SEC)

    async def link_heartbeat_loop(self) -> None:
        """Keep App connectivity observable even when a ROS/status probe is slow."""
        while True:
            now = time.monotonic()
            telemetry_age_ms = (
                round((now - self.last_telemetry_monotonic) * 1000.0, 1)
                if self.last_telemetry_monotonic
                else None
            )
            await self.send({
                "type": "link_heartbeat",
                "timestamp": time.time(),
                "program": self.program_state,
                "task": self.task_state,
                "microphone": self.microphone_state,
                "telemetry_age_ms": telemetry_age_ms,
            })
            await asyncio.sleep(LINK_HEARTBEAT_INTERVAL_SEC)

    def upload_manifest(self, manifest_path: Path) -> Dict[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        video = Path(manifest["video_path"])
        if not video.is_file():
            raise FileNotFoundError(video)
        request = urllib.request.Request(
            self.server + "/api/robot/videos", data=video.read_bytes(), method="POST",
            headers={
                "Content-Type": "video/mp4", "X-Robot-Token": self.token,
                "X-Video-Name": video.name,
                "X-Video-Title": urllib.parse.quote(str(manifest.get("title", "豆豆找到了"))),
                "X-Video-Time": str(manifest.get("created_at", time.time())),
                "X-Video-Duration": str(manifest.get("duration_sec", 5.0)),
                "X-Video-Pose": json.dumps(self.ros.pose, ensure_ascii=False),
                "X-Video-Category": str(manifest.get("category", "pet")),
                "X-Video-Exercise": str(manifest.get("exercise", "")),
                "X-Video-Exercise-Label": urllib.parse.quote(str(manifest.get("exercise_label", ""))),
                "X-Video-Count": str(int(manifest.get("count", 0) or 0)),
                "X-Video-Identity": urllib.parse.quote(str(manifest.get("identity", ""))),
                "X-Video-Session-State": str(manifest.get("session_state", "completed")),
            },
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
        manifest["status"] = "uploaded"
        manifest["uploaded_at"] = time.time()
        manifest["server_video_id"] = result.get("video", {}).get("id")
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)
        return result

    async def upload_loop(self) -> None:
        while True:
            if not self.dry_run:
                manifests = []
                if VIDEO_GLOB.exists():
                    manifests.extend(VIDEO_GLOB.glob("pet_search_*/video_ready.json"))
                if FITNESS_VIDEO_GLOB.exists():
                    manifests.extend(FITNESS_VIDEO_GLOB.glob("*/*.app_video.json"))
                for manifest in sorted(manifests):
                    key = str(manifest)
                    if key in self.uploaded:
                        continue
                    try:
                        current = json.loads(manifest.read_text(encoding="utf-8"))
                        if current.get("status") == "uploaded": self.uploaded.add(key); continue
                        result = await asyncio.to_thread(self.upload_manifest, manifest)
                        self.uploaded.add(key)
                        category = str(current.get("category") or "pet")
                        await self.send({
                            "type": "event",
                            "event": "fitness_video_uploaded" if category == "fitness" else "pet_video_uploaded",
                            "video": result.get("video"),
                            "timestamp": time.time(),
                        })
                    except Exception:
                        pass
            await asyncio.sleep(1.0)

    async def connected(self, websocket) -> None:
        self.websocket = websocket
        await self.send({"type": "hello", "robot": {"name": "理想机器人", "project": PROJECT.name, "dry_run": self.dry_run}, "program": self.program_state})
        self.last_telemetry_monotonic = 0.0
        command_tasks = set()

        async def receive_loop() -> None:
            async for raw in websocket:
                command = json.loads(raw)
                task = asyncio.create_task(self.execute_command(command))
                command_tasks.add(task)
                task.add_done_callback(command_tasks.discard)

        receive = asyncio.create_task(receive_loop(), name="app-bridge-receive")
        telemetry = asyncio.create_task(self.telemetry_loop(), name="app-bridge-telemetry")
        heartbeat = asyncio.create_task(self.link_heartbeat_loop(), name="app-bridge-heartbeat")
        uploads = asyncio.create_task(self.upload_loop(), name="app-bridge-uploads")
        service_tasks = {receive, telemetry, heartbeat, uploads}
        try:
            done, _ = await asyncio.wait(service_tasks, return_when=asyncio.FIRST_COMPLETED)
            stopped = next(iter(done))
            if stopped.cancelled():
                raise asyncio.CancelledError
            error = stopped.exception()
            if error is not None:
                raise error
            raise ConnectionError(f"bridge_task_stopped:{stopped.get_name()}")
        finally:
            for task in service_tasks:
                task.cancel()
            await asyncio.gather(*service_tasks, return_exceptions=True)
            for task in command_tasks:
                task.cancel()
            await asyncio.gather(*command_tasks, return_exceptions=True)
            self.websocket = None

    async def run(self) -> None:
        delay = 0.5
        while True:
            for server in self.servers:
                self.server = server
                uri = server.replace("http://", "ws://").replace("https://", "wss://") + "/ws/robot?token=" + self.token
                established = False
                try:
                    async with websockets.connect(
                        uri,
                        open_timeout=4,
                        close_timeout=2,
                        ping_interval=10,
                        ping_timeout=25,
                        max_size=16 * 1024 * 1024,
                        max_queue=64,
                    ) as websocket:
                        established = True
                        delay = 0.5
                        print(json.dumps({"event": "bridge_connected", "server": server}, ensure_ascii=False), flush=True)
                        await self.connected(websocket)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(json.dumps({
                        "event": "bridge_reconnect",
                        "server": server,
                        "error": f"{type(exc).__name__}: {exc}",
                        "next_server": True,
                    }, ensure_ascii=False), flush=True)
                    # A link that was healthy moments ago is more likely to
                    # recover than an unreachable LAN-only backup. Retry it
                    # first instead of paying the backup's connection timeout.
                    if established:
                        break
            await asyncio.sleep(delay)
            delay = min(10.0, delay * 1.7)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="", help="single relay URL (backward compatible)")
    parser.add_argument("--servers", default="", help="comma-separated relay URLs")
    parser.add_argument("--token", default=os.environ.get("ROBOT_BRIDGE_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("ROBOT_BRIDGE_TOKEN is required")
    configured = (
        args.server
        or args.servers
        or os.environ.get("ROBOT_RELAY_URLS", "")
        or os.environ.get("ROBOT_RELAY_URL", "")
        or "http://100.125.188.94:8765,http://10.249.188.197:8765"
    )
    servers = [item.strip() for item in configured.split(",") if item.strip()]
    asyncio.run(Bridge(servers, args.token, args.dry_run).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
