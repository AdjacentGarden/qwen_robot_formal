#!/usr/bin/env python3
"""Persistent, read-only ROS health monitor used during robot-stack startup.

The monitor never publishes, sends an action goal, changes lifecycle state, or
opens a hardware device.  One long-lived DDS participant replaces the former
loop that spawned many ``ros2`` CLI processes and occasionally misdiagnosed a
healthy graph while discovery was still converging.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict


FRESH_SAMPLE_SECONDS = 2.5
REQUIRED_STABLE_SAMPLES = 3


def evaluate_readiness(snapshot: Dict[str, Any], now: float) -> Dict[str, Any]:
    topics = snapshot.get("topics") or {}

    def fresh(name: str) -> bool:
        seen = topics.get(name)
        return isinstance(seen, (int, float)) and 0.0 <= now - float(seen) <= FRESH_SAMPLE_SECONDS

    lifecycle = snapshot.get("lifecycle") or {}
    actions = snapshot.get("actions") or {}
    manager = snapshot.get("manager") or {}
    base = bool(snapshot.get("cmd_vel_subscribers", 0)) and fresh("scan") and fresh("imu")
    odometry = fresh("odom")
    navigation = (
        lifecycle.get("map_server") == "active"
        and lifecycle.get("planner_server") == "active"
        and lifecycle.get("bt_navigator") == "active"
        and bool(snapshot.get("map_received"))
        and bool(actions.get("compute_path_to_pose"))
        and bool(actions.get("navigate_to_pose"))
        and bool(snapshot.get("tf_ready"))
    )
    manager_ready = (
        str(manager.get("state") or "").upper() == "NAVIGATION"
        and manager.get("sensor_gate_enabled") is True
        and str(manager.get("sensor_gate_state") or "").lower() == "ready"
        and not bool(manager.get("control_conflict"))
        and base
        and odometry
        and navigation
    )
    return {
        "base": base,
        "odometry": odometry,
        "navigation": navigation,
        "manager": manager_ready,
    }


class HealthMonitor:
    def __init__(self, run_dir: Path) -> None:
        import rclpy
        from lifecycle_msgs.srv import GetState
        from nav2_msgs.action import ComputePathToPose, NavigateToPose
        from nav_msgs.msg import OccupancyGrid, Odometry
        from rclpy.action import ActionClient
        from rclpy.duration import Duration
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Imu, LaserScan
        from std_msgs.msg import Bool, String, UInt32
        from tf2_ros import Buffer, TransformListener

        self.rclpy = rclpy
        self.GetState = GetState
        self.Duration = Duration
        self.Executor = SingleThreadedExecutor
        self.run_dir = run_dir
        self.state_file = run_dir / "health.json"
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.snapshot: Dict[str, Any] = {
            "topics": {},
            "lifecycle": {
                "map_server": "unknown",
                "planner_server": "unknown",
                "bt_navigator": "unknown",
            },
            "actions": {},
            "map_received": False,
            "tf_ready": False,
            "cmd_vel_subscribers": 0,
            "manager": {
                "state": "not_running",
                "event": "",
                "attempt": 0,
                "sensor_gate_enabled": None,
                "sensor_gate_state": "unknown",
                "control_conflict": False,
                "controller_status": "unknown",
                "controller_warning": "",
            },
        }
        self.stable_counts = {"base": 0, "odometry": 0, "navigation": 0, "manager": 0}
        self.pending_lifecycle: Dict[str, Any] = {}
        self.last_lifecycle_request = 0.0

        rclpy.init(args=None)
        self.node = rclpy.create_node("qwen_robot_startup_health_monitor")
        sensor_qos = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
        )
        map_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.node.create_subscription(LaserScan, "/scan_raw", lambda _msg: self.mark_topic("scan_raw"), sensor_qos)
        self.node.create_subscription(LaserScan, "/scan", lambda _msg: self.mark_topic("scan"), sensor_qos)
        self.node.create_subscription(Imu, "/imu", lambda _msg: self.mark_topic("imu"), sensor_qos)
        self.node.create_subscription(Odometry, "/odom", lambda _msg: self.mark_topic("odom"), 10)
        self.node.create_subscription(OccupancyGrid, "/map", self.mark_map, map_qos)
        status_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
        )
        for message_type, topic, key in (
            (String, "/mapping_manager/state", "state"),
            (String, "/mapping_manager/event", "event"),
            (UInt32, "/mapping_manager/attempt", "attempt"),
            (Bool, "/motion_controller/lidar_enabled", "sensor_gate_enabled"),
            (String, "/motion_controller/sensor_gate_state", "sensor_gate_state"),
            (Bool, "/motion_controller/control_conflict", "control_conflict"),
            (String, "/motion_controller/status", "controller_status"),
            (String, "/motion_controller/warning", "controller_warning"),
        ):
            self.node.create_subscription(
                message_type,
                topic,
                lambda message, manager_key=key: self.mark_manager(manager_key, message),
                status_qos,
            )
        self.lifecycle_clients = {
            name: self.node.create_client(GetState, f"/{name}/get_state")
            for name in ("map_server", "planner_server", "bt_navigator")
        }
        self.action_clients = {
            "compute_path_to_pose": ActionClient(self.node, ComputePathToPose, "/compute_path_to_pose"),
            "navigate_to_pose": ActionClient(self.node, NavigateToPose, "/navigate_to_pose"),
        }
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node)
        self.node.create_timer(0.20, self.sample)

    def mark_topic(self, name: str) -> None:
        with self.lock:
            self.snapshot["topics"][name] = time.monotonic()

    def mark_map(self, _message: Any) -> None:
        with self.lock:
            self.snapshot["map_received"] = True
            self.snapshot["topics"]["map"] = time.monotonic()

    def mark_manager(self, key: str, message: Any) -> None:
        value = getattr(message, "data", message)
        if key == "state":
            value = str(value or "not_running").strip().upper()
        elif key in {"event", "sensor_gate_state", "controller_status", "controller_warning"}:
            value = str(value or "").strip()
        elif key in {"sensor_gate_enabled", "control_conflict"}:
            value = bool(value)
        elif key == "attempt":
            value = int(value)
        with self.lock:
            self.snapshot["manager"][key] = value

    def request_lifecycle_states(self, now: float) -> None:
        if now - self.last_lifecycle_request < 0.75:
            return
        self.last_lifecycle_request = now
        for name, client in self.lifecycle_clients.items():
            pending = self.pending_lifecycle.get(name)
            if pending is not None and not pending.done():
                continue
            if not client.service_is_ready():
                with self.lock:
                    self.snapshot["lifecycle"][name] = "unknown"
                continue
            future = client.call_async(self.GetState.Request())
            self.pending_lifecycle[name] = future

            def completed(result_future: Any, node_name: str = name) -> None:
                try:
                    label = str(result_future.result().current_state.label or "unknown").lower()
                except Exception:
                    label = "unknown"
                with self.lock:
                    self.snapshot["lifecycle"][node_name] = label

            future.add_done_callback(completed)

    def sample(self) -> None:
        now = time.monotonic()
        self.request_lifecycle_states(now)
        with self.lock:
            manager_publishers = int(self.node.count_publishers("/mapping_manager/state"))
            if manager_publishers <= 0:
                self.snapshot["manager"]["state"] = "not_running"
            self.snapshot["cmd_vel_subscribers"] = int(
                self.node.count_subscribers("/cmd_vel_external")
            )
            for name, client in self.action_clients.items():
                self.snapshot["actions"][name] = bool(client.server_is_ready())
            self.snapshot["tf_ready"] = any(
                self.tf_buffer.can_transform(
                    "map", base_frame, self.rclpy.time.Time(), timeout=self.Duration(seconds=0.0)
                )
                for base_frame in ("base_footprint", "base_link")
            )
            raw_ready = evaluate_readiness(self.snapshot, now)
            for component, ready in raw_ready.items():
                self.stable_counts[component] = self.stable_counts[component] + 1 if ready else 0
            ready = {
                component: count >= REQUIRED_STABLE_SAMPLES
                for component, count in self.stable_counts.items()
            }
            output = {
                "monitor_pid": os.getpid(),
                "updated_at": time.time(),
                "raw_ready": raw_ready,
                "stable_counts": dict(self.stable_counts),
                "ready": ready,
                "topics_age_sec": {
                    name: round(max(0.0, now - float(seen)), 3)
                    for name, seen in self.snapshot["topics"].items()
                },
                "lifecycle": dict(self.snapshot["lifecycle"]),
                "actions": dict(self.snapshot["actions"]),
                "map_received": bool(self.snapshot["map_received"]),
                "tf_ready": bool(self.snapshot["tf_ready"]),
                "cmd_vel_subscribers": int(self.snapshot["cmd_vel_subscribers"]),
                "manager": dict(self.snapshot["manager"]),
                "manager_publishers": manager_publishers,
            }
        self.atomic_write(output)

    def atomic_write(self, value: Dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(f".{self.state_file.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.state_file)

    def run(self) -> int:
        executor = self.Executor()
        executor.add_node(self.node)
        try:
            while not self.stop_event.is_set() and self.rclpy.ok():
                executor.spin_once(timeout_sec=0.20)
        finally:
            executor.remove_node(self.node)
            self.node.destroy_node()
            if self.rclpy.ok():
                self.rclpy.shutdown()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    monitor = HealthMonitor(args.run_dir)

    def stop(_signum: int, _frame: Any) -> None:
        monitor.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
