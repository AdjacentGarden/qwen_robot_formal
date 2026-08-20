#!/usr/bin/env python3
"""Persistent inference/ROS runtime exposed through a local Unix socket.

The service owns model contexts and a ROS node. Clients send frames or status
requests; no actuator command is implemented in this prototype.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import preload_runtime as preload


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "shared_runtime"
SOCKET_PATH = STATE_DIR / "inference.sock"
PID_FILE = STATE_DIR / "server.pid"
STATUS_FILE = STATE_DIR / "status.json"
MAX_HEADER = 65536
MAX_PAYLOAD = 8 * 1024 * 1024


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, "ts": round(time.time(), 3), **payload}, ensure_ascii=False, default=str), flush=True)


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = conn.recv(min(remaining, 1048576))
        if not chunk:
            raise ConnectionError(f"socket closed with {remaining} bytes remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_request(conn: socket.socket) -> tuple[dict[str, Any], bytes]:
    header_size = struct.unpack("!I", recv_exact(conn, 4))[0]
    if not 2 <= header_size <= MAX_HEADER:
        raise ValueError(f"invalid header size: {header_size}")
    header = json.loads(recv_exact(conn, header_size).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("request header must be an object")
    payload_size = int(header.get("payload_len", 0))
    if not 0 <= payload_size <= MAX_PAYLOAD:
        raise ValueError(f"invalid payload size: {payload_size}")
    return header, recv_exact(conn, payload_size) if payload_size else b""


def send_response(conn: socket.socket, response: dict[str, Any]) -> None:
    data = json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    conn.sendall(struct.pack("!I", len(data)) + data)


class SharedRuntime:
    def __init__(self):
        import cv2
        import mediapipe as mp
        import numpy as np
        import rclpy
        from geometry_msgs.msg import PoseStamped, Twist
        from nav_msgs.msg import OccupancyGrid, Odometry
        from nav2_msgs.action import ComputePathToPose, NavigateToPose
        from nav2_msgs.srv import ClearEntireCostmap
        from rclpy.action import ActionClient
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
            qos_profile_sensor_data,
        )
        from rclpy.time import Time
        from sensor_msgs.msg import Imu, LaserScan
        from std_msgs.msg import Float32, String
        from tf2_ros import Buffer, TransformListener

        self.cv2 = cv2
        self.np = np
        self.rclpy = rclpy
        self.Twist = Twist
        self.PoseStamped = PoseStamped
        self.ComputePathToPose = ComputePathToPose
        self.NavigateToPose = NavigateToPose
        self.ClearEntireCostmap = ClearEntireCostmap
        self.RosTime = Time
        self.stages: list[dict[str, Any]] = []
        self.model_lock = threading.Lock()
        self.move_lock = threading.Lock()
        self.navigation_lock = threading.Lock()
        self.move_cancel = threading.Event()
        self.navigation_cancel = threading.Event()
        self.active_goal_handle = None
        self.resource_status_provider = None
        self.map_lock = threading.RLock()
        self.latest_map: dict[str, Any] | None = None
        self.head_feedback_condition = threading.Condition()
        self.latest_head_roll: tuple[float, float] | None = None
        self.latest_head_roll_rate: tuple[float, float] | None = None
        self.navigation_health_condition = threading.Condition()
        self.navigation_inputs: dict[str, dict[str, float] | None] = {
            "scan": None,
            "imu": None,
            "odom": None,
        }
        self.navigation_input_sequences = {"scan": 0, "imu": 0, "odom": 0}
        self.navigation_stamp_advance_sequences = {"scan": 0, "imu": 0, "odom": 0}
        self.scan_sequence = 0
        self.latest_guard_status: dict[str, Any] | None = None

        self.retina = self._timed("retinaface", self._build_retina)
        self.face_common, self.facenet = self._timed("facenet", self._build_facenet)
        self.fitness_module, self.fitness_config = preload.load_fitness_config()
        self.yolo = self._timed("yolo", self._build_yolo)
        self.reid = self._timed("reid", self._build_reid)

        started = time.perf_counter()
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            enable_segmentation=False,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        # A first process call builds internal calculators and CPU delegates.
        self.pose.process(np.zeros((480, 640, 3), dtype=np.uint8))
        self.stages.append({"name": "mediapipe_pose", "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)})

        started = time.perf_counter()
        rclpy.init(args=None)
        self.ros_node = rclpy.create_node(f"v11_shared_runtime_{os.getpid()}")
        self.nav_client = ActionClient(self.ros_node, NavigateToPose, "navigate_to_pose")
        self.path_client = ActionClient(self.ros_node, ComputePathToPose, "compute_path_to_pose")
        self.clear_global_costmap_client = self.ros_node.create_client(
            ClearEntireCostmap,
            "/global_costmap/clear_entirely_global_costmap",
        )
        self.cmd_vel_pub = self.ros_node.create_publisher(Twist, "/cmd_vel", 10)
        map_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.map_subscription = self.ros_node.create_subscription(
            OccupancyGrid,
            "/map",
            self._on_map,
            map_qos,
        )
        self.head_roll_subscription = self.ros_node.create_subscription(
            Float32, "/head/roll_deg", self._on_head_roll, 10
        )
        self.head_roll_rate_subscription = self.ros_node.create_subscription(
            Float32, "/head/roll_rate_dps", self._on_head_roll_rate, 10
        )
        self.scan_health_subscription = self.ros_node.create_subscription(
            LaserScan, "/scan", self._on_navigation_scan, qos_profile_sensor_data
        )
        self.imu_health_subscription = self.ros_node.create_subscription(
            Imu, "/imu", self._on_navigation_imu, qos_profile_sensor_data
        )
        self.odom_health_subscription = self.ros_node.create_subscription(
            Odometry, "/odom", self._on_navigation_odom, qos_profile_sensor_data
        )
        self.guard_status_subscription = self.ros_node.create_subscription(
            String, "/head_lidar_guard/status", self._on_guard_status, 10
        )
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(
            self.tf_buffer,
            self.ros_node,
            spin_thread=False,
        )
        self.executor = MultiThreadedExecutor(num_threads=2)
        self.executor.add_node(self.ros_node)
        self.executor_thread = threading.Thread(target=self.executor.spin, name="shared_runtime_ros", daemon=True)
        self.executor_thread.start()
        self.stages.append({"name": "ros_node_action_client", "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)})

    def _on_map(self, message) -> None:
        """Keep one immutable-enough occupancy snapshot for exploration Skills."""

        info = message.info
        stamp = message.header.stamp
        snapshot = {
            "available": True,
            "width": int(info.width),
            "height": int(info.height),
            "resolution": float(info.resolution),
            "origin_x": float(info.origin.position.x),
            "origin_y": float(info.origin.position.y),
            "frame_id": str(message.header.frame_id or "map"),
            "stamp_sec": float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0,
            "received_monotonic": time.monotonic(),
            "source": "ros:/map",
            "data": self.np.asarray(message.data, dtype=self.np.int16).copy(),
        }
        with self.map_lock:
            self.latest_map = snapshot

    def _on_head_roll(self, message) -> None:
        with self.head_feedback_condition:
            self.latest_head_roll = (float(message.data), time.monotonic())
            self.head_feedback_condition.notify_all()

    def _on_head_roll_rate(self, message) -> None:
        with self.head_feedback_condition:
            self.latest_head_roll_rate = (float(message.data), time.monotonic())
            self.head_feedback_condition.notify_all()

    @staticmethod
    def _message_stamp_sec(message) -> float:
        stamp = message.header.stamp
        return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0

    def _note_navigation_input(self, name: str, message) -> None:
        stamp_sec = self._message_stamp_sec(message)
        valid = True
        diagnostics: dict[str, Any] = {}
        if name == "scan":
            ranges = list(getattr(message, "ranges", []) or [])
            range_min = float(getattr(message, "range_min", 0.0) or 0.0)
            range_max = float(getattr(message, "range_max", float("inf")) or float("inf"))
            finite_ranges = [
                float(value) for value in ranges
                if math.isfinite(float(value)) and range_min <= float(value) <= range_max
            ]
            minimum_finite = max(8, int(len(ranges) * 0.03)) if ranges else 8
            valid = len(finite_ranges) >= minimum_finite
            diagnostics = {
                "range_count": len(ranges),
                "finite_range_count": len(finite_ranges),
                "minimum_finite_range_count": minimum_finite,
            }
        elif name == "odom":
            pose = message.pose.pose
            twist = message.twist.twist
            values = (
                pose.position.x, pose.position.y, pose.position.z,
                pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w,
                twist.linear.x, twist.linear.y, twist.angular.z,
            )
            valid = all(math.isfinite(float(value)) for value in values)
        elif name == "imu":
            values = (
                message.orientation.x, message.orientation.y,
                message.orientation.z, message.orientation.w,
                message.angular_velocity.x, message.angular_velocity.y, message.angular_velocity.z,
                message.linear_acceleration.x, message.linear_acceleration.y, message.linear_acceleration.z,
            )
            valid = all(math.isfinite(float(value)) for value in values)
        with self.navigation_health_condition:
            previous = self.navigation_inputs.get(name)
            previous_stamp = None if previous is None else float(previous.get("stamp_sec", 0.0))
            self.navigation_input_sequences[name] = int(self.navigation_input_sequences.get(name, 0)) + 1
            stamp_advanced = previous_stamp is None or stamp_sec > previous_stamp + 1e-6
            if stamp_advanced:
                self.navigation_stamp_advance_sequences[name] = int(
                    self.navigation_stamp_advance_sequences.get(name, 0)
                ) + 1
            self.navigation_inputs[name] = {
                "received_monotonic": time.monotonic(),
                "stamp_sec": stamp_sec,
                "sequence": self.navigation_input_sequences[name],
                "stamp_advance_sequence": self.navigation_stamp_advance_sequences[name],
                "stamp_advanced": stamp_advanced,
                "valid": bool(valid),
                **diagnostics,
            }
            if name == "scan":
                self.scan_sequence += 1
            self.navigation_health_condition.notify_all()

    def _on_navigation_scan(self, message) -> None:
        self._note_navigation_input("scan", message)

    def _on_navigation_imu(self, message) -> None:
        self._note_navigation_input("imu", message)

    def _on_navigation_odom(self, message) -> None:
        self._note_navigation_input("odom", message)

    def _on_guard_status(self, message) -> None:
        try:
            payload = json.loads(str(message.data))
            if not isinstance(payload, dict):
                return
        except Exception:
            return
        with self.navigation_health_condition:
            self.latest_guard_status = {
                **payload,
                "received_monotonic": time.monotonic(),
            }
            self.navigation_health_condition.notify_all()

    def current_scan_sequence(self) -> int:
        with self.navigation_health_condition:
            return int(self.scan_sequence)

    def navigation_sequence_snapshot(self) -> dict[str, dict[str, int]]:
        with self.navigation_health_condition:
            return {
                "received": {key: int(value) for key, value in self.navigation_input_sequences.items()},
                "stamp_advanced": {
                    key: int(value) for key, value in self.navigation_stamp_advance_sequences.items()
                },
            }

    def wait_for_fresh_scan(
        self,
        *,
        after_sequence: int,
        timeout_sec: float = 2.0,
        maximum_age_sec: float = 0.5,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        with self.navigation_health_condition:
            while True:
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    return {
                        "ok": False,
                        "error": "fresh_scan_wait_cancelled",
                        "scan_sequence": int(self.scan_sequence),
                        "after_sequence": int(after_sequence),
                    }
                sample = self.navigation_inputs.get("scan")
                age = None if sample is None else max(0.0, now - float(sample["received_monotonic"]))
                guard = dict(self.latest_guard_status or {})
                guard_age = None if not guard else max(
                    0.0,
                    now - float(guard.get("received_monotonic", now)),
                )
                sequence_ok = self.scan_sequence > int(after_sequence)
                sample_ok = bool(sample and sample.get("valid", True) and sample.get("stamp_advanced"))
                guard_ok = (
                    bool(guard.get("live"))
                    and bool(guard.get("resume_confirmed"))
                    and bool(guard.get("stationary"))
                    and guard_age is not None
                    and guard_age <= 2.0
                )
                if (
                    sequence_ok
                    and sample_ok
                    and age is not None
                    and age <= max(0.05, float(maximum_age_sec))
                    and guard_ok
                ):
                    return {
                        "ok": True,
                        "scan_sequence": int(self.scan_sequence),
                        "scan_age_sec": age,
                        "scan_stamp_sec": float(sample.get("stamp_sec", 0.0)),
                        "guard_age_sec": guard_age,
                        "guard": guard,
                    }
                if now >= deadline:
                    return {
                        "ok": False,
                        "error": "fresh_scan_resume_timeout",
                        "scan_sequence": int(self.scan_sequence),
                        "after_sequence": int(after_sequence),
                        "scan_age_sec": age,
                        "scan_valid": bool(sample and sample.get("valid", True)),
                        "scan_stamp_advanced": bool(sample and sample.get("stamp_advanced")),
                        "guard_age_sec": guard_age,
                        "guard": guard,
                    }
                self.navigation_health_condition.wait(timeout=min(0.05, deadline - now))

    def navigation_health(self, maximum_age_sec: float = 0.75) -> dict[str, Any]:
        now = time.monotonic()
        now_ros_sec = self.ros_node.get_clock().now().nanoseconds / 1_000_000_000.0
        with self.navigation_health_condition:
            inputs = {
                name: None if sample is None else dict(sample)
                for name, sample in self.navigation_inputs.items()
            }
            guard = dict(self.latest_guard_status or {})
        ages = {
            name: None if sample is None else max(0.0, now - float(sample["received_monotonic"]))
            for name, sample in inputs.items()
        }
        stamp_ages = {
            name: None if sample is None else now_ros_sec - float(sample.get("stamp_sec", 0.0))
            for name, sample in inputs.items()
        }
        limit = max(0.1, float(maximum_age_sec))
        stale = [
            name for name, age in ages.items()
            if age is None
            or age > limit
            or stamp_ages[name] is None
            or stamp_ages[name] > limit
            or stamp_ages[name] < -0.10
        ]
        invalid = [
            name for name, sample in inputs.items()
            if sample is not None and not bool(sample.get("valid", True))
        ]
        guard_age = None
        if guard:
            guard_age = max(0.0, now - float(guard.get("received_monotonic", now)))
        guard_ok = bool(guard.get("live")) and bool(guard.get("resume_confirmed")) and guard_age is not None and guard_age <= 2.0
        stationary_ok = bool(guard.get("stationary"))
        pose = self.lookup_pose("map", "base_footprint")
        tf_ok = bool(pose.get("available")) and float(pose.get("age_sec", limit + 1.0)) <= limit
        errors = [f"{name}_missing_or_stale" for name in stale]
        errors.extend(f"{name}_invalid" for name in invalid)
        if not guard_ok:
            errors.append("lidar_guard_not_resumed")
        if not stationary_ok:
            errors.append("base_not_stationary")
        if not tf_ok:
            errors.append("map_tf_missing_or_stale")
        return {
            "ok": not errors,
            "errors": errors,
            "input_age_sec": ages,
            "input_stamp_age_sec": stamp_ages,
            "input_samples": inputs,
            "guard": guard,
            "guard_age_sec": guard_age,
            "base_stationary": stationary_ok,
            "pose": pose,
            "navigate_to_pose_ready": bool(self.nav_client.server_is_ready()),
        }

    def wait_for_navigation_health(
        self,
        *,
        timeout_sec: float = 5.0,
        stable_sec: float = 0.60,
        maximum_age_sec: float = 0.75,
        minimum_updates: int = 3,
        require_navigation_server: bool = True,
        maximum_pose_jump_m: float = 0.20,
        maximum_yaw_jump_rad: float = 0.35,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        stable_since: float | None = None
        baseline = self.navigation_sequence_snapshot()
        pose_samples: list[tuple[float, float, float]] = []
        last = self.navigation_health(maximum_age_sec)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return {
                    **last,
                    "ok": False,
                    "error": "navigation_health_cancelled",
                }
            last = self.navigation_health(maximum_age_sec)
            now = time.monotonic()
            current = self.navigation_sequence_snapshot()
            update_deltas = {
                name: int(current["received"].get(name, 0)) - int(baseline["received"].get(name, 0))
                for name in ("scan", "imu", "odom")
            }
            stamp_advance_deltas = {
                name: int(current["stamp_advanced"].get(name, 0))
                - int(baseline["stamp_advanced"].get(name, 0))
                for name in ("scan", "imu", "odom")
            }
            updates_ok = all(
                update_deltas[name] >= max(1, int(minimum_updates))
                and stamp_advance_deltas[name] >= max(1, int(minimum_updates))
                for name in ("scan", "imu", "odom")
            )
            server_ok = bool(last.get("navigate_to_pose_ready")) or not require_navigation_server
            pose = last.get("pose") or {}
            pose_ok = bool(pose.get("available"))
            if last.get("ok") and updates_ok and server_ok and pose_ok:
                sample = (float(pose["x"]), float(pose["y"]), float(pose["yaw"]))
                if pose_samples:
                    previous = pose_samples[-1]
                    translation_jump = math.hypot(sample[0] - previous[0], sample[1] - previous[1])
                    yaw_jump = abs((sample[2] - previous[2] + math.pi) % (2.0 * math.pi) - math.pi)
                    if (
                        translation_jump > max(0.01, float(maximum_pose_jump_m))
                        or yaw_jump > max(0.01, float(maximum_yaw_jump_rad))
                    ):
                        stable_since = None
                        pose_samples.clear()
                        last = {
                            **last,
                            "errors": [*(last.get("errors") or []), "map_pose_jump_detected"],
                            "pose_jump_m": translation_jump,
                            "yaw_jump_rad": yaw_jump,
                        }
                        with self.navigation_health_condition:
                            self.navigation_health_condition.wait(timeout=min(0.05, max(0.0, deadline - now)))
                        continue
                pose_samples.append(sample)
                stable_since = stable_since if stable_since is not None else now
                if now - stable_since >= max(0.0, float(stable_sec)):
                    return {
                        **last,
                        "ok": True,
                        "stable_sec": now - stable_since,
                        "update_deltas": update_deltas,
                        "stamp_advance_deltas": stamp_advance_deltas,
                        "pose_sample_count": len(pose_samples),
                    }
            else:
                stable_since = None
                pose_samples.clear()
            with self.navigation_health_condition:
                self.navigation_health_condition.wait(timeout=min(0.05, max(0.0, deadline - now)))
        return {
            **last,
            "ok": False,
            "error": "navigation_inputs_not_ready",
            "update_deltas": update_deltas,
            "stamp_advance_deltas": stamp_advance_deltas,
            "require_navigation_server": bool(require_navigation_server),
        }

    def wait_for_head_target(
        self,
        target_deg: float,
        *,
        tolerance_deg: float = 5.0,
        maximum_rate_dps: float = 2.0,
        maximum_roll_span_deg: float = 0.5,
        stable_sec: float = 0.25,
        timeout_sec: float = 15.0,
        maximum_feedback_age_sec: float = 0.25,
    ) -> dict[str, Any]:
        """Wait for fresh IMU feedback to remain at the requested head angle."""
        target = float(target_deg) % 360.0
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        stable_samples: list[tuple[float, float]] = []
        last_roll_sample_ts = -1.0
        last_result: dict[str, Any] = {"available": False, "error": "head_feedback_not_received"}
        with self.head_feedback_condition:
            while True:
                now = time.monotonic()
                roll_sample = self.latest_head_roll
                rate_sample = self.latest_head_roll_rate
                if roll_sample is not None:
                    roll, roll_ts = roll_sample
                    rate = None
                    rate_age = None
                    if rate_sample is not None:
                        rate, rate_ts = rate_sample
                        rate_age = max(0.0, now - rate_ts)
                    age = max(0.0, now - roll_ts)
                    error = (roll - target + 180.0) % 360.0 - 180.0
                    fresh = age <= max(0.05, float(maximum_feedback_age_sec))
                    at_target = fresh and abs(error) <= max(0.1, float(tolerance_deg))
                    last_result = {
                        "available": fresh,
                        "target_deg": target,
                        "roll_deg": roll,
                        "roll_rate_dps": rate,
                        "roll_rate_age_sec": rate_age,
                        "error_deg": error,
                        "feedback_age_sec": age,
                        "at_target": at_target,
                    }
                    if at_target:
                        if roll_ts != last_roll_sample_ts:
                            stable_samples.append((roll_ts, roll))
                            last_roll_sample_ts = roll_ts
                        window = max(0.0, float(stable_sec))
                        stable_samples = [sample for sample in stable_samples if sample[0] >= now - window]
                        if stable_samples:
                            stable_for = stable_samples[-1][0] - stable_samples[0][0]
                            roll_span = max(value for _, value in stable_samples) - min(value for _, value in stable_samples)
                            derived_rate = 0.0
                            if stable_for > 1e-3:
                                derived_rate = abs(stable_samples[-1][1] - stable_samples[0][1]) / stable_for
                            # The physical roll window is authoritative.  The
                            # auxiliary rate topic is retained as telemetry but is
                            # not a hard gate because it can be stale/noisy while
                            # the angle samples themselves are demonstrably stable.
                            if (
                                stable_for >= window * 0.9
                                and roll_span <= max(0.1, float(maximum_roll_span_deg))
                                and derived_rate <= max(0.1, float(maximum_rate_dps))
                            ):
                                return {
                                    **last_result,
                                    "ok": True,
                                    "stable_sec": stable_for,
                                    "stable_roll_span_deg": roll_span,
                                    "derived_roll_rate_dps": derived_rate,
                                }
                    else:
                        stable_samples.clear()
                if now >= deadline:
                    return {**last_result, "ok": False, "error": "head_target_timeout"}
                self.head_feedback_condition.wait(timeout=min(0.05, deadline - now))

    def occupancy_grid_snapshot(self, max_age_sec: float = 8.0) -> dict[str, Any]:
        with self.map_lock:
            snapshot = self.latest_map
            if snapshot is None:
                return {"available": False, "error": "map_not_received", "source": "ros:/map"}
            age = max(0.0, time.monotonic() - float(snapshot["received_monotonic"]))
            if age > max(0.1, float(max_age_sec)):
                return {"available": False, "error": f"map_stale:{age:.3f}s", "source": "ros:/map"}
            output = dict(snapshot)
            output["data"] = snapshot["data"].copy()
            output["age_sec"] = age
            return output

    def _timed(self, name: str, builder):
        started = time.perf_counter()
        value = builder()
        self.stages.append({"name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)})
        return value

    def _build_retina(self):
        module = preload.load_module("shared_runtime_retinaface", ROOT / "face_recognition" / "retinaface_rknn.py")
        model = ROOT / "face_recognition" / "assets" / "model" / "RetinaFace_resnet50_320_fp.rknn"
        return module.RetinaFaceRKNN(model)

    def _build_facenet(self):
        face_dir = str(ROOT / "face_recognition")
        if face_dir not in sys.path:
            sys.path.insert(0, face_dir)
        module = preload.load_module("shared_runtime_face_common", ROOT / "face_recognition" / "face_common.py")
        cfg = module.FaceConfig(ROOT / "face_recognition")
        return module, module.RKNNFaceEmbeddingModel(cfg.model_path)

    def _build_yolo(self):
        config = self.fitness_config
        models = config["models"]
        detector = config["detector"]
        return self.fitness_module.PersonDetector(
            models["person_detector"],
            models["person_detector_weight"],
            detector["confidence"],
            detector["npu_core_mask"],
            detector.get("device_id", "0002:21:00.0"),
        )

    def _build_reid(self):
        return self.fitness_module.RknnReID(
            self.fitness_config["models"]["person_reid"],
            self.fitness_config["reid"]["npu_core"],
        )

    def decode_frame(self, request: dict[str, Any], payload: bytes):
        shape = request.get("shape")
        if not isinstance(shape, list) or len(shape) != 3:
            raise ValueError("shape must be [height,width,channels]")
        height, width, channels = [int(v) for v in shape]
        if not (1 <= height <= 2160 and 1 <= width <= 3840 and channels == 3):
            raise ValueError(f"unsupported frame shape: {shape}")
        expected = height * width * channels
        if len(payload) != expected:
            raise ValueError(f"payload length {len(payload)} != {expected}")
        return self.np.frombuffer(payload, dtype=self.np.uint8).reshape((height, width, channels))

    def _publish_twist(self, linear_x: float, angular_z: float) -> None:
        message = self.Twist()
        message.linear.x = float(linear_x)
        message.angular.z = float(angular_z)
        self.cmd_vel_pub.publish(message)

    def _publish_stop(self, repetitions: int = 8) -> None:
        for _ in range(max(1, int(repetitions))):
            self._publish_twist(0.0, 0.0)
            time.sleep(0.04)

    def chassis_stop(self) -> dict[str, Any]:
        self.move_cancel.set()
        self._publish_stop()
        return {"status": "stopped", "subscribers": int(self.cmd_vel_pub.get_subscription_count())}

    def chassis_move(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.move_lock.acquire(blocking=False):
            raise RuntimeError("chassis_command_busy")
        requested_linear = float(request.get("linear_x", 0.0))
        requested_angular = float(request.get("angular_z", 0.0))
        requested_duration = float(request.get("duration", 0.5))
        linear = max(-0.25, min(0.25, requested_linear))
        angular = max(-0.60, min(0.60, requested_angular))
        duration = max(0.05, min(5.0, requested_duration))
        allow_no_subscriber = bool(request.get("allow_no_subscriber", False))
        self.move_cancel.clear()
        started = time.monotonic()
        try:
            discovery_deadline = time.monotonic() + min(3.0, max(0.0, float(request.get("discovery_timeout", 1.0))))
            subscribers = int(self.cmd_vel_pub.get_subscription_count())
            while subscribers <= 0 and time.monotonic() < discovery_deadline:
                time.sleep(0.05)
                subscribers = int(self.cmd_vel_pub.get_subscription_count())
            if subscribers <= 0 and not allow_no_subscriber:
                raise RuntimeError("cmd_vel_subscribers_0")
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline and not self.move_cancel.is_set():
                self._publish_twist(linear, angular)
                time.sleep(0.08)
            cancelled = self.move_cancel.is_set()
            return {
                "status": "cancelled" if cancelled else "completed",
                "linear_x": linear,
                "angular_z": angular,
                "duration_requested": duration,
                "duration_actual": round(time.monotonic() - started, 3),
                "subscribers": max(subscribers, int(self.cmd_vel_pub.get_subscription_count())),
                "limits": {"max_abs_linear": 0.25, "max_abs_angular": 0.60, "max_duration": 5.0},
            }
        finally:
            self._publish_stop()
            self.move_lock.release()

    def _wait_future(self, future, timeout: float, cancel_event: threading.Event | None = None) -> str:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not future.done():
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(0.02)
        return "done"

    def _resolve_navigation_goal(self, request: dict[str, Any]) -> dict[str, Any]:
        point_name = str(request.get("point", "")).strip()
        if point_name:
            points_path = ROOT / "points" / "named_points.json"
            points = json.loads(points_path.read_text(encoding="utf-8"))
            selected = None
            for key, value in points.items():
                tokens = [key, value.get("name"), value.get("display_name"), *(value.get("aliases") or [])]
                if any(point_name.lower() == str(token or "").strip().lower() for token in tokens):
                    selected = dict(value)
                    selected["point"] = key
                    break
            if selected is None:
                raise ValueError(f"unknown_navigation_point:{point_name}")
            if not bool(selected.get("configured", False)):
                raise ValueError(f"navigation_point_not_configured:{point_name}")
            goal = {
                "point": selected["point"],
                "display_name": selected.get("display_name", selected["point"]),
                "x": float(selected["x"]),
                "y": float(selected["y"]),
                "yaw": float(selected.get("yaw", 0.0)),
                "frame_id": str(selected.get("frame_id", "map")),
            }
        else:
            if request.get("x") is None or request.get("y") is None:
                raise ValueError("navigation requires point or x/y")
            goal = {
                "point": None,
                "display_name": "direct_pose",
                "x": float(request["x"]),
                "y": float(request["y"]),
                "yaw": float(request.get("yaw", 0.0)),
                "frame_id": str(request.get("frame_id", "map")),
            }
        if not (-50.0 <= goal["x"] <= 50.0 and -50.0 <= goal["y"] <= 50.0):
            raise ValueError("navigation coordinates outside safety bounds")
        return goal

    def navigation_cancel_request(self) -> dict[str, Any]:
        self.navigation_cancel.set()
        return {"status": "cancel_requested", "goal_active": self.active_goal_handle is not None}

    def preflight_navigation_path(
        self,
        goal: dict[str, Any],
        *,
        timeout_sec: float = 4.0,
    ) -> dict[str, Any]:
        """Compute a path without moving the base, using the resident ROS participant."""

        from action_msgs.msg import GoalStatus

        started = time.monotonic()
        timeout_sec = max(0.5, min(10.0, float(timeout_sec)))
        deadline = started + timeout_sec
        if not self.path_client.wait_for_server(timeout_sec=min(2.0, timeout_sec)):
            return {
                "ok": False,
                "error": "path_preflight_server_unavailable",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }

        pose = self.PoseStamped()
        pose.header.frame_id = str(goal["frame_id"])
        pose.header.stamp = self.ros_node.get_clock().now().to_msg()
        pose.pose.position.x = float(goal["x"])
        pose.pose.position.y = float(goal["y"])
        half = float(goal["yaw"]) / 2.0
        pose.pose.orientation.z = math.sin(half)
        pose.pose.orientation.w = math.cos(half)

        request = self.ComputePathToPose.Goal()
        request.goal = pose
        request.use_start = False
        request.planner_id = "GridBased"
        send_future = self.path_client.send_goal_async(request)
        response_timeout = max(0.1, deadline - time.monotonic())
        response_state = self._wait_future(send_future, response_timeout, self.navigation_cancel)
        if response_state != "done":
            return {
                "ok": False,
                "error": "path_preflight_cancelled" if response_state == "cancelled" else "path_preflight_goal_response_timeout",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            return {
                "ok": False,
                "error": "path_preflight_goal_rejected",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }

        result_future = goal_handle.get_result_async()
        result_timeout = max(0.1, deadline - time.monotonic())
        result_state = self._wait_future(result_future, result_timeout, self.navigation_cancel)
        if result_state != "done":
            with contextlib.suppress(Exception):
                goal_handle.cancel_goal_async()
            return {
                "ok": False,
                "error": "path_preflight_cancelled" if result_state == "cancelled" else "path_preflight_result_timeout",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        wrapped = result_future.result()
        status = int(getattr(wrapped, "status", -1))
        result = getattr(wrapped, "result", None)
        poses = list(getattr(getattr(result, "path", None), "poses", []) or [])
        if status != GoalStatus.STATUS_SUCCEEDED or not poses:
            return {
                "ok": False,
                "error": "navigation_no_valid_path",
                "status_code": status,
                "path_pose_count": len(poses),
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        return {
            "ok": True,
            "status_code": status,
            "path_pose_count": len(poses),
            "elapsed_sec": round(time.monotonic() - started, 3),
        }

    def clear_global_costmap_without_motion(self, *, timeout_sec: float = 3.0) -> dict[str, Any]:
        """Clear Nav2's global costmap without publishing a velocity command.

        Static map data is restored by the static layer.  Callers must wait for
        fresh level-head lidar observations before trusting a new path so a
        real obstacle is re-marked instead of being temporarily hidden.
        """

        started = time.monotonic()
        timeout_sec = max(0.5, min(8.0, float(timeout_sec)))
        client = self.clear_global_costmap_client
        if not client.wait_for_service(timeout_sec=min(2.0, timeout_sec)):
            return {
                "ok": False,
                "error": "global_costmap_clear_service_unavailable",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        future = client.call_async(self.ClearEntireCostmap.Request())
        state = self._wait_future(future, timeout_sec, self.navigation_cancel)
        if state != "done":
            return {
                "ok": False,
                "error": (
                    "global_costmap_clear_cancelled"
                    if state == "cancelled"
                    else "global_costmap_clear_timeout"
                ),
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        try:
            future.result()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"global_costmap_clear_failed:{type(exc).__name__}:{exc}",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }
        return {
            "ok": True,
            "motion_command_published": False,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }

    def preflight_navigation_path_with_recovery(
        self,
        goal: dict[str, Any],
        *,
        timeout_sec: float = 4.0,
        attempts: int = 3,
        retry_delay_sec: float = 0.75,
        clear_global_costmap: bool = True,
    ) -> dict[str, Any]:
        """Retry a transient no-path result, then safely refresh the costmap.

        This method never sends NavigateToPose and never publishes ``cmd_vel``.
        Only the specific ``navigation_no_valid_path`` result is recoverable;
        server, cancellation, TF and sensor failures remain fail-closed.
        """

        started = time.monotonic()
        # Three plans are the hard safety ceiling: initial, one replan after a
        # genuinely newer scan, then one final plan after the global costmap
        # refresh and the complete localization chain has stabilized.
        attempts = max(1, min(3, int(attempts)))
        retry_delay_sec = max(0.0, min(2.0, float(retry_delay_sec)))
        history: list[dict[str, Any]] = []
        clear_result: dict[str, Any] | None = None
        recovery_health: dict[str, Any] | None = None
        first_retry_scan: dict[str, Any] | None = None

        for index in range(attempts):
            if self.navigation_cancel.is_set():
                return {
                    "ok": False,
                    "error": "path_preflight_cancelled",
                    "attempt_count": len(history),
                    "history": history,
                    "first_retry_scan": first_retry_scan,
                    "total_elapsed_sec": round(time.monotonic() - started, 3),
                }
            result = self.preflight_navigation_path(goal, timeout_sec=timeout_sec)
            history.append({"attempt": index + 1, **dict(result)})
            if result.get("ok"):
                return {
                    **result,
                    "attempt_count": index + 1,
                    "recovered": index > 0,
                    "costmap_cleared": bool(clear_result and clear_result.get("ok")),
                    "clear_result": clear_result,
                    "first_retry_scan": first_retry_scan,
                    "recovery_health": recovery_health,
                    "history": history,
                    "total_elapsed_sec": round(time.monotonic() - started, 3),
                }
            if result.get("error") != "navigation_no_valid_path" or index + 1 >= attempts:
                break
            if self.navigation_cancel.is_set():
                return {
                    "ok": False,
                    "error": "path_preflight_cancelled",
                    "history": history,
                    "total_elapsed_sec": round(time.monotonic() - started, 3),
                }

            # First failure must be followed by a genuinely newer scan; a
            # fixed sleep is not evidence that the observation changed.  If
            # the second plan is still empty, clear only the global costmap and
            # require a stable, freshly advancing lidar/localization chain
            # before the final plan.  This lets real obstacles be re-marked.
            if index == 0:
                scan_sequence = self.current_scan_sequence()
                first_retry_scan = self.wait_for_fresh_scan(
                    after_sequence=scan_sequence,
                    timeout_sec=max(0.25, retry_delay_sec),
                    maximum_age_sec=0.60,
                    cancel_event=self.navigation_cancel,
                )
                if self.navigation_cancel.is_set():
                    return {
                        "ok": False,
                        "error": "path_preflight_cancelled",
                        "attempt_count": len(history),
                        "history": history,
                        "first_retry_scan": first_retry_scan,
                        "total_elapsed_sec": round(time.monotonic() - started, 3),
                    }
                if not first_retry_scan.get("ok"):
                    return {
                        "ok": False,
                        "error": "navigation_recovery_scan_failed",
                        "attempt_count": len(history),
                        "history": history,
                        "first_retry_scan": first_retry_scan,
                        "total_elapsed_sec": round(time.monotonic() - started, 3),
                    }
            elif not clear_global_costmap:
                return {
                    "ok": False,
                    "error": "navigation_recovery_costmap_refresh_disabled",
                    "attempt_count": len(history),
                    "history": history,
                    "first_retry_scan": first_retry_scan,
                    "total_elapsed_sec": round(time.monotonic() - started, 3),
                }
            elif clear_result is None:
                clear_result = self.clear_global_costmap_without_motion(timeout_sec=3.0)
                if not clear_result.get("ok"):
                    return {
                        "ok": False,
                        "error": clear_result.get("error") or "global_costmap_clear_failed",
                        "history": history,
                        "first_retry_scan": first_retry_scan,
                        "clear_result": clear_result,
                        "total_elapsed_sec": round(time.monotonic() - started, 3),
                    }
                recovery_health = self.wait_for_navigation_health(
                    timeout_sec=4.0,
                    stable_sec=1.25,
                    maximum_age_sec=0.60,
                    minimum_updates=5,
                    cancel_event=self.navigation_cancel,
                )
                if self.navigation_cancel.is_set():
                    return {
                        "ok": False,
                        "error": "path_preflight_cancelled",
                        "history": history,
                        "first_retry_scan": first_retry_scan,
                        "clear_result": clear_result,
                        "recovery_health": recovery_health,
                        "total_elapsed_sec": round(time.monotonic() - started, 3),
                    }
                if not recovery_health.get("ok"):
                    return {
                        "ok": False,
                        "error": "navigation_recovery_health_failed",
                        "history": history,
                        "first_retry_scan": first_retry_scan,
                        "clear_result": clear_result,
                        "recovery_health": recovery_health,
                        "total_elapsed_sec": round(time.monotonic() - started, 3),
                    }

        last = dict(history[-1]) if history else {"error": "navigation_path_preflight_failed"}
        return {
            **last,
            "ok": False,
            "attempt_count": len(history),
            "recovered": False,
            "costmap_cleared": bool(clear_result and clear_result.get("ok")),
            "clear_result": clear_result,
            "first_retry_scan": first_retry_scan,
            "recovery_health": recovery_health,
            "history": history,
            "total_elapsed_sec": round(time.monotonic() - started, 3),
        }

    def navigation_path_preflight_recovery_request(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Expose recovery diagnostics without disturbing an active base task."""

        if self.navigation_lock.locked() or self.move_lock.locked():
            return {
                "ok": False,
                "error": "navigation_recovery_base_busy",
                "movement_authorized": False,
            }
        goal = self._resolve_navigation_goal(request)
        return {
            "goal": goal,
            "movement_authorized": False,
            **self.preflight_navigation_path_with_recovery(
                goal,
                timeout_sec=float(request.get("timeout_sec", 4.0)),
                attempts=int(request.get("attempts", 3)),
                retry_delay_sec=float(request.get("retry_delay_sec", 0.75)),
                clear_global_costmap=bool(request.get("clear_global_costmap", True)),
            ),
        }

    def lookup_pose(self, parent_frame: str = "map", child_frame: str = "base_footprint") -> dict[str, Any]:
        """Read a fresh TF pose without starting another ROS process or node."""

        parent_frame = str(parent_frame or "map").strip()
        child_frame = str(child_frame or "base_footprint").strip()
        try:
            transform = self.tf_buffer.lookup_transform(parent_frame, child_frame, self.RosTime())
            translation = transform.transform.translation
            rotation = transform.transform.rotation
            yaw = math.atan2(
                2.0 * (float(rotation.w) * float(rotation.z) + float(rotation.x) * float(rotation.y)),
                1.0 - 2.0 * (float(rotation.y) ** 2 + float(rotation.z) ** 2),
            )
            stamp = transform.header.stamp
            stamp_sec = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
            now_sec = self.ros_node.get_clock().now().nanoseconds / 1_000_000_000.0
            if stamp_sec <= 0.0:
                raise RuntimeError("zero_tf_stamp")
            delta = now_sec - stamp_sec
            if delta < -0.1:
                raise RuntimeError(f"future_tf_stamp:{-delta:.6f}s")
            values = (float(translation.x), float(translation.y), float(translation.z), float(yaw))
            if not all(math.isfinite(value) for value in values):
                raise RuntimeError("non_finite_tf_pose")
            return {
                "available": True,
                "frame_id": parent_frame,
                "child_frame_id": child_frame,
                "x": values[0], "y": values[1], "z": values[2], "yaw": values[3],
                "tf_stamp_sec": stamp_sec,
                "age_sec": max(0.0, delta),
                "source": "resident_tf2",
            }
        except Exception as exc:
            return {
                "available": False,
                "frame_id": parent_frame,
                "child_frame_id": child_frame,
                "error": f"{type(exc).__name__}: {exc}",
                "source": "resident_tf2",
            }

    def current_pose(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return one non-blocking, read-only map pose from the resident TF buffer."""

        parent_frame = str(request.get("parent_frame", "map")).strip() or "map"
        child_frame = str(request.get("child_frame", "base_footprint")).strip() or "base_footprint"
        if parent_frame != "map":
            return {
                "available": False,
                "frame_id": parent_frame,
                "child_frame_id": child_frame,
                "error": "map_frame_required",
            }
        try:
            transform = self.tf_buffer.lookup_transform(
                parent_frame,
                child_frame,
                self.RosTime(),
            )
            translation = transform.transform.translation
            rotation = transform.transform.rotation
            yaw = math.atan2(
                2.0 * (
                    float(rotation.w) * float(rotation.z)
                    + float(rotation.x) * float(rotation.y)
                ),
                1.0
                - 2.0
                * (
                    float(rotation.y) * float(rotation.y)
                    + float(rotation.z) * float(rotation.z)
                ),
            )
            stamp = transform.header.stamp
            stamp_sec = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
            now_sec = self.ros_node.get_clock().now().nanoseconds / 1_000_000_000.0
            if stamp_sec <= 0.0:
                raise RuntimeError("zero_tf_stamp")
            clock_delta = now_sec - stamp_sec
            if clock_delta < -0.1:
                raise RuntimeError(
                    f"future_tf_stamp:{-clock_delta:.6f}s"
                )
            age_sec = max(0.0, clock_delta)
            resource_owners: dict[str, Any] = {}
            if callable(self.resource_status_provider):
                try:
                    resource_owners = dict(self.resource_status_provider() or {})
                except Exception:
                    resource_owners = {}
            base_busy = bool(
                self.move_lock.locked()
                or self.navigation_lock.locked()
                or "base" in resource_owners
            )
            values = (
                float(translation.x),
                float(translation.y),
                float(translation.z),
                float(yaw),
                float(age_sec),
            )
            if not all(math.isfinite(value) for value in values):
                raise RuntimeError("non_finite_tf_pose")
            return {
                "available": True,
                "frame_id": parent_frame,
                "child_frame_id": child_frame,
                "x": values[0],
                "y": values[1],
                "z": values[2],
                "yaw": values[3],
                "tf_stamp_sec": stamp_sec,
                "age_sec": values[4],
                "queried_monotonic": time.monotonic(),
                "source": "resident_tf2",
                "base_busy": base_busy,
                "base_owner": resource_owners.get("base"),
            }
        except Exception as exc:
            return {
                "available": False,
                "frame_id": parent_frame,
                "child_frame_id": child_frame,
                "error": f"{type(exc).__name__}: {exc}",
                "queried_monotonic": time.monotonic(),
                "source": "resident_tf2",
            }

    def navigation_goal(self, request: dict[str, Any]) -> dict[str, Any]:
        from action_msgs.msg import GoalStatus

        if not self.navigation_lock.acquire(blocking=False):
            raise RuntimeError("navigation_goal_busy")
        goal = self._resolve_navigation_goal(request)
        timeout = max(1.0, min(180.0, float(request.get("timeout", 120.0))))
        server_wait = max(0.1, min(15.0, float(request.get("server_wait_timeout", 5.0))))
        goal_response_timeout = max(0.5, min(15.0, float(request.get("goal_response_timeout", 8.0))))
        self.navigation_cancel.clear()
        self.active_goal_handle = None
        started = time.monotonic()
        feedback = {"count": 0, "last_distance_remaining": None, "last_ts": None}

        def on_feedback(message):
            feedback["count"] += 1
            feedback["last_ts"] = time.time()
            value = getattr(message.feedback, "distance_remaining", None)
            if value is not None:
                feedback["last_distance_remaining"] = float(value)

        try:
            preflight = self.wait_for_navigation_health(
                timeout_sec=server_wait,
                stable_sec=float(request.get("health_stable_sec", 0.80)),
                maximum_age_sec=float(request.get("health_maximum_age_sec", 0.60)),
                minimum_updates=int(request.get("health_minimum_updates", 4)),
                cancel_event=self.navigation_cancel,
            )
            if not preflight.get("ok"):
                return {
                    "status": "preflight_failed",
                    "goal": goal,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "preflight": preflight,
                }
            path_preflight = self.preflight_navigation_path_with_recovery(
                goal,
                timeout_sec=float(request.get("path_preflight_timeout", 4.0)),
                attempts=int(request.get("path_preflight_attempts", 3)),
                retry_delay_sec=float(request.get("path_preflight_retry_delay_sec", 0.75)),
                clear_global_costmap=bool(request.get("path_preflight_clear_global_costmap", True)),
            )
            if not path_preflight.get("ok"):
                return {
                    "status": "preflight_failed",
                    "goal": goal,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "preflight": preflight,
                    "path_preflight": path_preflight,
                }
            deadline = time.monotonic() + server_wait
            while not self.nav_client.server_is_ready() and time.monotonic() < deadline:
                if self.navigation_cancel.is_set():
                    return {"status": "cancelled_before_send", "goal": goal}
                time.sleep(0.05)
            if not self.nav_client.server_is_ready():
                raise RuntimeError("navigate_to_pose_server_unavailable")

            message = self.NavigateToPose.Goal()
            message.pose.header.frame_id = goal["frame_id"]
            message.pose.header.stamp = self.ros_node.get_clock().now().to_msg()
            message.pose.pose.position.x = goal["x"]
            message.pose.pose.position.y = goal["y"]
            half = goal["yaw"] / 2.0
            import math
            message.pose.pose.orientation.z = math.sin(half)
            message.pose.pose.orientation.w = math.cos(half)

            send_future = self.nav_client.send_goal_async(message, feedback_callback=on_feedback)
            response_state = self._wait_future(send_future, goal_response_timeout, self.navigation_cancel)
            if response_state != "done":
                raise RuntimeError("goal_response_cancelled" if response_state == "cancelled" else "goal_response_timeout")
            goal_handle = send_future.result()
            if goal_handle is None or not goal_handle.accepted:
                raise RuntimeError("navigation_goal_rejected")
            self.active_goal_handle = goal_handle
            result_future = goal_handle.get_result_async()
            remaining = max(0.1, timeout - (time.monotonic() - started))
            result_state = self._wait_future(result_future, remaining, self.navigation_cancel)
            if result_state != "done":
                cancel_future = goal_handle.cancel_goal_async()
                self._wait_future(cancel_future, 3.0)
                return {
                    "status": "cancelled" if result_state == "cancelled" else "timeout_cancelled",
                    "goal": goal,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "feedback": feedback,
                }
            wrapped = result_future.result()
            status_code = int(wrapped.status)
            names = {
                GoalStatus.STATUS_SUCCEEDED: "succeeded",
                GoalStatus.STATUS_ABORTED: "aborted",
                GoalStatus.STATUS_CANCELED: "canceled",
                GoalStatus.STATUS_CANCELING: "canceling",
            }
            return {
                "status": names.get(status_code, f"status_{status_code}"),
                "status_code": status_code,
                "goal": goal,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "feedback": feedback,
            }
        finally:
            self.active_goal_handle = None
            self.navigation_lock.release()

    def handle(self, request: dict[str, Any], payload: bytes) -> dict[str, Any]:
        op = str(request.get("op", "")).strip()
        if op in {"face_detect", "face_embed", "face_pipeline", "yolo", "reid", "pose"}:
            with self.model_lock:
                return self._handle_operation(request, payload)
        return self._handle_operation(request, payload)

    def _handle_operation(self, request: dict[str, Any], payload: bytes) -> dict[str, Any]:
        op = str(request.get("op", "")).strip()
        started = time.perf_counter()
        if op == "ping":
            result = {"pid": os.getpid(), "models": ["retinaface", "facenet", "yolo", "reid", "mediapipe_pose"], "ros_node": self.ros_node.get_name()}
        elif op == "ros_ready":
            result = {"navigate_to_pose_ready": bool(self.nav_client.server_is_ready())}
        elif op == "chassis_move":
            result = self.chassis_move(request)
        elif op == "chassis_stop":
            result = self.chassis_stop()
        elif op == "navigation_goal":
            result = self.navigation_goal(request)
        elif op == "navigation_health":
            result = self.wait_for_navigation_health(
                timeout_sec=float(request.get("timeout_sec", 5.0)),
                stable_sec=float(request.get("stable_sec", 0.80)),
                maximum_age_sec=float(request.get("maximum_age_sec", 0.60)),
                minimum_updates=int(request.get("minimum_updates", 4)),
                require_navigation_server=bool(request.get("require_navigation_server", True)),
            )
        elif op == "navigation_path_preflight":
            goal = self._resolve_navigation_goal(request)
            result = {
                "goal": goal,
                **self.preflight_navigation_path(
                    goal,
                    timeout_sec=float(request.get("timeout_sec", 4.0)),
                ),
            }
        elif op == "navigation_path_preflight_recovery":
            # Diagnostic/recovery-only operation: it may refresh the global
            # costmap but never sends NavigateToPose or publishes cmd_vel.
            result = self.navigation_path_preflight_recovery_request(request)
        elif op == "navigation_cancel":
            result = self.navigation_cancel_request()
        elif op == "current_pose":
            result = self.current_pose(request)
        elif op == "face_detect":
            frame = self.decode_frame(request, payload)
            detections = self.retina.detect(frame, score_threshold=float(request.get("threshold", 0.6)))
            result = {"detections": detections}
        elif op == "face_embed":
            frame = self.decode_frame(request, payload)
            feature = self.facenet.extract(frame)
            result = {"dimension": int(feature.size), "norm": float(self.np.linalg.norm(feature))}
            if request.get("return_vector"):
                result["vector"] = feature.tolist()
        elif op == "face_pipeline":
            frame = self.decode_frame(request, payload)
            detections = self.retina.detect(frame, score_threshold=float(request.get("threshold", 0.6)))
            faces = []
            for detection in detections[: int(request.get("max_faces", 8))]:
                aligned = self.face_common.align_face(frame, detection)
                feature = self.facenet.extract(aligned)
                item = {"detection": detection, "dimension": int(feature.size), "norm": float(self.np.linalg.norm(feature))}
                if request.get("return_vector"):
                    item["vector"] = feature.tolist()
                faces.append(item)
            result = {"faces": faces, "detection_count": len(detections)}
        elif op == "yolo":
            frame = self.decode_frame(request, payload)
            detections = self.yolo.detect(frame)
            result = {"detections": [{"confidence": float(v.confidence), "bbox": list(v.bbox)} for v in detections]}
        elif op == "reid":
            frame = self.decode_frame(request, payload)
            feature = self.reid.feature(frame)
            result = {"dimension": int(feature.size), "norm": float(self.np.linalg.norm(feature))}
            if request.get("return_vector"):
                result["vector"] = feature.tolist()
        elif op == "pose":
            frame = self.decode_frame(request, payload)
            rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
            pose_result = self.pose.process(rgb)
            landmarks = []
            if pose_result.pose_landmarks:
                landmarks = [
                    {"x": float(v.x), "y": float(v.y), "z": float(v.z), "visibility": float(v.visibility)}
                    for v in pose_result.pose_landmarks.landmark
                ]
            result = {"detected": bool(landmarks), "landmarks": landmarks if request.get("return_landmarks", True) else []}
        else:
            raise ValueError(f"unsupported operation: {op}")
        return {"ok": True, "op": op, "server_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3), "result": result}

    def spin_once(self) -> None:
        # ROS callbacks are handled by the persistent MultiThreadedExecutor.
        return None

    def close(self) -> None:
        self.move_cancel.set()
        self.navigation_cancel.set()
        try:
            self._publish_stop()
        except Exception:
            pass
        for obj in (getattr(self, "pose", None), getattr(self, "reid", None), getattr(self, "yolo", None), getattr(self, "facenet", None), getattr(self, "retina", None)):
            if obj is None:
                continue
            try:
                if hasattr(obj, "close"):
                    obj.close()
                elif hasattr(obj, "release"):
                    obj.release()
            except Exception as exc:
                emit("release_warning", object=type(obj).__name__, error=str(exc))
        try:
            if hasattr(self, "executor"):
                self.executor.shutdown(timeout_sec=2.0)
            if hasattr(self, "executor_thread"):
                self.executor_thread.join(timeout=2.5)
            if hasattr(self, "nav_client"):
                self.nav_client.destroy()
            if hasattr(self, "path_client"):
                self.path_client.destroy()
            if hasattr(self, "tf_listener"):
                self.tf_listener.unregister()
            if hasattr(self, "ros_node"):
                self.ros_node.destroy_node()
            if self.rclpy.ok():
                self.rclpy.shutdown()
        except Exception as exc:
            emit("ros_release_warning", error=str(exc))


def write_status(state: str, runtime: SharedRuntime | None, error: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    status = {
        "state": state,
        "pid": os.getpid(),
        "updated_at": time.time(),
        "socket": str(SOCKET_PATH),
        "stages": runtime.stages if runtime else [],
        "error": error,
        "safety": {
            "implements_limited_motion": True,
            "implements_navigation_goal": True,
            "controls_appliances": False,
            "motion_limits": {"max_abs_linear": 0.25, "max_abs_angular": 0.60, "max_duration_sec": 5.0},
        },
    }
    temp = STATUS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATUS_FILE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    write_status("loading", None)
    runtime = None
    server = None
    stopping = False
    workers: set[threading.Thread] = set()
    workers_lock = threading.Lock()

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        started = time.perf_counter()
        runtime = SharedRuntime()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(8)
        server.settimeout(0.05)
        write_status("ready", runtime)
        emit("shared_runtime_ready", pid=os.getpid(), socket=str(SOCKET_PATH), startup_ms=round((time.perf_counter() - started) * 1000.0, 3), stages=runtime.stages)

        def handle_connection(conn: socket.socket) -> None:
            try:
                with conn:
                    conn.settimeout(15.0)
                    request_started = time.perf_counter()
                    try:
                        request, payload = recv_request(conn)
                        response = runtime.handle(request, payload)
                        response["round_server_ms"] = round((time.perf_counter() - request_started) * 1000.0, 3)
                        send_response(conn, response)
                    except (BrokenPipeError, ConnectionError, socket.timeout) as exc:
                        emit("client_connection_warning", error=f"{type(exc).__name__}: {exc}")
                    except Exception as exc:
                        response = {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "round_server_ms": round((time.perf_counter() - request_started) * 1000.0, 3),
                        }
                        try:
                            send_response(conn, response)
                        except (BrokenPipeError, ConnectionError, socket.timeout):
                            pass
            finally:
                with workers_lock:
                    workers.discard(threading.current_thread())

        while not stopping:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            worker = threading.Thread(target=handle_connection, args=(conn,), name="shared_runtime_client", daemon=True)
            with workers_lock:
                workers.add(worker)
            worker.start()
        runtime.move_cancel.set()
        runtime.navigation_cancel.set()
        with workers_lock:
            pending = list(workers)
        for worker in pending:
            worker.join(timeout=5.0)
        write_status("stopping", runtime)
        return 0
    except Exception as exc:
        write_status("failed", runtime, f"{type(exc).__name__}: {exc}")
        emit("shared_runtime_failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        return 1
    finally:
        if server is not None:
            server.close()
        if runtime is not None:
            runtime.close()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        write_status("stopped", runtime)
        emit("shared_runtime_stopped")


if __name__ == "__main__":
    raise SystemExit(main())
