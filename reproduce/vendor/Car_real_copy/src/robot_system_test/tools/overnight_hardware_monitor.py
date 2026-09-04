#!/usr/bin/env python3
"""Observer-only overnight monitor for lidar, IMU and Cartographer localization."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import glob
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Deque, Dict, Optional, Tuple

import rclpy
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener

from rplidar_runtime_monitor import (
    EventLog,
    cpu_snapshot,
    device_owners,
    read_baud_once,
    read_cmdline,
    uart_counters,
)


def finite(values: Any) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def stamp_seconds(message: Any) -> Optional[float]:
    header = getattr(message, "header", None)
    if header is None:
        return None
    return float(header.stamp.sec) + float(header.stamp.nanosec) * 1e-9


class StreamStats:
    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self.arrivals: Deque[float] = deque()
        self.total = 0
        self.invalid = 0
        self.last_arrival: Optional[float] = None
        self.last_stamp: Optional[float] = None
        self.max_gap_s = 0.0
        self.stamp_regressions = 0
        self.extra: Dict[str, Any] = {}

    def add(self, valid: bool, stamp: Optional[float], **extra: Any) -> None:
        now = time.monotonic()
        if self.last_arrival is not None:
            self.max_gap_s = max(self.max_gap_s, now - self.last_arrival)
        if stamp is not None and self.last_stamp is not None and stamp < self.last_stamp:
            self.stamp_regressions += 1
        self.last_arrival = now
        self.last_stamp = stamp
        self.arrivals.append(now)
        cutoff = now - self.window_seconds
        while self.arrivals and self.arrivals[0] < cutoff:
            self.arrivals.popleft()
        self.total += 1
        if not valid:
            self.invalid += 1
        self.extra = extra

    def snapshot(self, now: float) -> Dict[str, Any]:
        rate = None
        if len(self.arrivals) >= 2:
            span = self.arrivals[-1] - self.arrivals[0]
            if span > 0:
                rate = (len(self.arrivals) - 1) / span
        return {
            "total": self.total,
            "invalid": self.invalid,
            "invalid_ratio": round(self.invalid / self.total, 6) if self.total else None,
            "rate_hz": round(rate, 3) if rate is not None else None,
            "age_s": round(now - self.last_arrival, 3) if self.last_arrival else None,
            "max_gap_s": round(self.max_gap_s, 6),
            "stamp_regressions": self.stamp_regressions,
            **self.extra,
        }


def yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def angle_delta(a: float, b: float) -> float:
    return math.atan2(math.sin(a - b), math.cos(a - b))


class RosHealthMonitor(Node):
    def __init__(self, log: EventLog) -> None:
        super().__init__("overnight_hardware_monitor")
        self.log = log
        self.lock = threading.Lock()
        self.streams = {
            "/scan": StreamStats(),
            "/scan_gated": StreamStats(),
            "/imu/raw": StreamStats(),
            "/imu": StreamStats(),
            "/imu_cartographer_gated": StreamStats(),
        }
        self.manager_state = ""
        self.manager_event = ""
        self.sensor_gate_state = ""
        self.tf_buffer = Buffer(cache_time=Duration(seconds=30.0))
        self.tf_listener = TransformListener(self.tf_buffer, self, spin_thread=False)
        self.tf_baseline: Optional[Tuple[float, float, float]] = None
        self.tf_last: Optional[Tuple[float, float, float]] = None
        self.max_drift_m = 0.0
        self.max_drift_yaw_deg = 0.0
        self.max_jump_m = 0.0
        self.max_jump_yaw_deg = 0.0

        self.create_subscription(
            LaserScan, "/scan", lambda msg: self._scan("/scan", msg),
            qos_profile_sensor_data)
        self.create_subscription(
            LaserScan, "/scan_gated", lambda msg: self._scan("/scan_gated", msg),
            qos_profile_sensor_data)
        self.create_subscription(
            Imu, "/imu/raw", lambda msg: self._imu("/imu/raw", msg),
            qos_profile_sensor_data)
        self.create_subscription(
            Imu, "/imu", lambda msg: self._imu("/imu", msg),
            qos_profile_sensor_data)
        self.create_subscription(
            Imu, "/imu_cartographer_gated",
            lambda msg: self._imu("/imu_cartographer_gated", msg),
            qos_profile_sensor_data)
        self.create_subscription(
            String, "/mapping_manager/state", self._manager_state, 10)
        self.create_subscription(
            String, "/mapping_manager/event", self._manager_event, 50)
        self.create_subscription(
            String, "/motion_controller/sensor_gate_state", self._gate_state, 10)

    def _scan(self, topic: str, message: LaserScan) -> None:
        usable = sum(1 for value in message.ranges if math.isfinite(value))
        valid = bool(message.ranges) and usable > 0 and finite([
            message.angle_min, message.angle_max, message.angle_increment,
            message.range_min, message.range_max, message.scan_time,
        ])
        with self.lock:
            self.streams[topic].add(
                valid, stamp_seconds(message), points=len(message.ranges),
                finite_points=usable, scan_time=round(float(message.scan_time), 6))

    def _imu(self, topic: str, message: Imu) -> None:
        gyro = (
            message.angular_velocity.x, message.angular_velocity.y,
            message.angular_velocity.z)
        accel = (
            message.linear_acceleration.x, message.linear_acceleration.y,
            message.linear_acceleration.z)
        valid = finite(gyro + accel)
        with self.lock:
            self.streams[topic].add(
                valid, stamp_seconds(message),
                gyro_norm=round(math.sqrt(sum(value * value for value in gyro)), 6),
                accel_norm=round(math.sqrt(sum(value * value for value in accel)), 6))

    def _manager_state(self, message: String) -> None:
        value = message.data.strip()
        with self.lock:
            changed = value != self.manager_state
            self.manager_state = value
        if changed:
            self.log.write("manager_state", value=value)

    def _manager_event(self, message: String) -> None:
        value = message.data.strip()
        with self.lock:
            self.manager_event = value
        self.log.write("manager_event", value=value)

    def _gate_state(self, message: String) -> None:
        value = message.data.strip()
        with self.lock:
            changed = value != self.sensor_gate_state
            self.sensor_gate_state = value
        if changed:
            self.log.write("sensor_gate_state", value=value)

    def stream_snapshot(self) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        with self.lock:
            return {name: stats.snapshot(now) for name, stats in self.streams.items()}

    def state_snapshot(self) -> Dict[str, str]:
        with self.lock:
            return {
                "manager_state": self.manager_state,
                "manager_event": self.manager_event,
                "sensor_gate_state": self.sensor_gate_state,
            }

    def localization_snapshot(self) -> Dict[str, Any]:
        try:
            transform = self.tf_buffer.lookup_transform(
                "map", "base_footprint", Time(), timeout=Duration(seconds=0.1))
        except TransformException as exc:
            return {"available": False, "error": str(exc)}
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        pose = (
            float(translation.x), float(translation.y),
            yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w))
        with self.lock:
            if self.tf_baseline is None:
                self.tf_baseline = pose
                self.log.write(
                    "localization_baseline", x=pose[0], y=pose[1],
                    yaw_deg=math.degrees(pose[2]))
            baseline = self.tf_baseline
            drift_m = math.hypot(pose[0] - baseline[0], pose[1] - baseline[1])
            drift_yaw = abs(math.degrees(angle_delta(pose[2], baseline[2])))
            jump_m = 0.0
            jump_yaw = 0.0
            if self.tf_last is not None:
                jump_m = math.hypot(pose[0] - self.tf_last[0], pose[1] - self.tf_last[1])
                jump_yaw = abs(math.degrees(angle_delta(pose[2], self.tf_last[2])))
            self.tf_last = pose
            self.max_drift_m = max(self.max_drift_m, drift_m)
            self.max_drift_yaw_deg = max(self.max_drift_yaw_deg, drift_yaw)
            self.max_jump_m = max(self.max_jump_m, jump_m)
            self.max_jump_yaw_deg = max(self.max_jump_yaw_deg, jump_yaw)
        stamp = float(transform.header.stamp.sec) + float(transform.header.stamp.nanosec) * 1e-9
        now_ros = self.get_clock().now().nanoseconds * 1e-9
        return {
            "available": True,
            "x": round(pose[0], 6), "y": round(pose[1], 6),
            "yaw_deg": round(math.degrees(pose[2]), 6),
            "stamp_age_s": round(max(0.0, now_ros - stamp), 6),
            "drift_m": round(drift_m, 6),
            "drift_yaw_deg": round(drift_yaw, 6),
            "jump_m": round(jump_m, 6),
            "jump_yaw_deg": round(jump_yaw, 6),
        }

    def localization_summary(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "baseline": self.tf_baseline,
                "max_drift_m": round(self.max_drift_m, 6),
                "max_drift_yaw_deg": round(self.max_drift_yaw_deg, 6),
                "max_jump_m": round(self.max_jump_m, 6),
                "max_jump_yaw_deg": round(self.max_jump_yaw_deg, 6),
            }


PROCESS_PATTERNS = {
    "manager": "mapping_navigation_manager.py",
    "rplidar": "/rplidar_node",
    "imu": "/imu_cartographer_publisher",
    "cartographer": "/cartographer_node",
}


class ProcessSampler:
    def __init__(self) -> None:
        self.previous: Dict[int, Tuple[int, float]] = {}
        self.clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        self.page_size = os.sysconf("SC_PAGE_SIZE")

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        result: Dict[str, Any] = {name: [] for name in PROCESS_PATTERNS}
        current: Dict[int, Tuple[int, float]] = {}
        for path in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                pid = int(path.split("/")[2])
                cmdline = read_cmdline(pid)
                matched = [name for name, pattern in PROCESS_PATTERNS.items() if pattern in cmdline]
                if not matched:
                    continue
                stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                fields = stat_text[stat_text.rfind(")") + 2:].split()
                ticks = int(fields[11]) + int(fields[12])
                rss_mb = int(fields[21]) * self.page_size / (1024.0 * 1024.0)
                cpu = None
                if pid in self.previous:
                    old_ticks, old_time = self.previous[pid]
                    elapsed = now - old_time
                    if elapsed > 0:
                        cpu = 100.0 * (ticks - old_ticks) / self.clock_ticks / elapsed
                current[pid] = (ticks, now)
                item = {
                    "pid": pid, "cpu_percent": round(cpu, 2) if cpu is not None else None,
                    "rss_mb": round(rss_mb, 2), "cmdline": cmdline,
                }
                for name in matched:
                    result[name].append(item)
            except (OSError, ValueError, IndexError):
                continue
        self.previous = current
        return result


class AlertTracker:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.active: Dict[str, bool] = {}
        self.counts: Dict[str, int] = {}

    def set(self, name: str, active: bool, **details: Any) -> None:
        previous = self.active.get(name, False)
        if active and not previous:
            self.counts[name] = self.counts.get(name, 0) + 1
            self.log.write("alert_start", name=name, **details)
            print(f"ALERT {name}: {details}", flush=True)
        elif previous and not active:
            self.log.write("alert_clear", name=name, **details)
            print(f"CLEAR {name}", flush=True)
        self.active[name] = active


def kernel_driver() -> Optional[str]:
    path = "/sys/bus/i2c/devices/4-006a/driver"
    return os.path.realpath(path) if os.path.islink(path) else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="logs/overnight_hardware_monitor")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="seconds to run; zero runs until stopped")
    parser.add_argument("--heartbeat", type=float, default=5.0)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--serial-index", type=int, default=8)
    parser.add_argument("--serial-port", default="/dev/ttyS8")
    parser.add_argument("--i2c-port", default="/dev/i2c-4")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration < 0 or args.heartbeat <= 0 or args.poll <= 0:
        raise SystemExit("duration must be non-negative and intervals must be positive")
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%z")
    run_dir = Path(args.output_dir).expanduser().resolve() / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    log = EventLog(run_dir / "events.jsonl")
    (run_dir / "pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
    print(f"Run directory: {run_dir}", flush=True)

    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda _s, _f: stop_event.set())
    signal.signal(signal.SIGTERM, lambda _s, _f: stop_event.set())
    rclpy.init(args=None)
    node = RosHealthMonitor(log)
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, name="ros-spin", daemon=True)
    spin_thread.start()

    sampler = ProcessSampler()
    alerts = AlertTracker(log)
    previous_cpu = None
    previous_uart = uart_counters(args.serial_index)
    uart_faults = 0
    baud_captured = False
    last_uart_fault_log = 0.0
    started = time.monotonic()
    next_heartbeat = started
    stack_seen = False
    stack_started: Optional[float] = None
    cartographer_seen = False
    last_summary: Dict[str, Any] = {}
    log.write(
        "start", argv=sys.argv, pid=os.getpid(), hostname=os.uname().nodename,
        kernel=os.uname().release, observer_only=True,
        serial_port=args.serial_port, i2c_port=args.i2c_port)

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                break
            current_uart = uart_counters(args.serial_index)
            uart_delta = {key: current_uart[key] - previous_uart[key] for key in current_uart}
            if any(uart_delta[key] for key in ("fe", "pe", "brk", "oe")):
                uart_faults += 1
                if now - last_uart_fault_log >= 1.0:
                    values: Dict[str, Any] = {
                        "uart": current_uart, "delta": uart_delta,
                        "owners": device_owners(args.serial_port),
                    }
                    if not baud_captured:
                        values["baud"] = read_baud_once(args.serial_port)
                        baud_captured = True
                    log.write("uart_error", **values)
                    last_uart_fault_log = now
            previous_uart = current_uart

            if now >= next_heartbeat:
                streams = node.stream_snapshot()
                state = node.state_snapshot()
                localization = node.localization_snapshot()
                processes = sampler.snapshot()
                system, previous_cpu = cpu_snapshot(previous_cpu)
                driver = kernel_driver()
                manager_active = bool(processes["manager"])
                if manager_active and stack_started is None:
                    stack_started = now
                    log.write("manager_detected", processes=processes["manager"])
                stack_seen = stack_seen or manager_active
                cartographer_seen = cartographer_seen or bool(processes["cartographer"])
                active_age = now - stack_started if stack_started is not None else 0.0

                alerts.set("imu_kernel_driver_bound", driver is not None, driver=driver)
                alerts.set("manager_disappeared", stack_seen and not manager_active)
                alerts.set(
                    "rplidar_process_missing",
                    stack_seen and active_age > 15 and not processes["rplidar"])
                alerts.set(
                    "imu_process_missing",
                    stack_seen and active_age > 15 and not processes["imu"])
                alerts.set(
                    "cartographer_process_missing",
                    cartographer_seen and not processes["cartographer"])
                scan = streams["/scan"]
                imu_raw = streams["/imu/raw"]
                alerts.set(
                    "scan_never_seen", manager_active and active_age > 15
                    and scan["total"] == 0)
                alerts.set(
                    "imu_raw_never_seen", manager_active and active_age > 15
                    and imu_raw["total"] == 0)
                alerts.set(
                    "cartographer_never_seen", manager_active and active_age > 120
                    and not cartographer_seen)
                alerts.set(
                    "scan_stale", stack_seen and scan["total"] > 0
                    and scan["age_s"] is not None and scan["age_s"] > 0.5,
                    stream=scan)
                alerts.set(
                    "imu_raw_stale", stack_seen and imu_raw["total"] > 0
                    and imu_raw["age_s"] is not None and imu_raw["age_s"] > 0.25,
                    stream=imu_raw)
                alerts.set(
                    "localization_tf_missing", cartographer_seen and not localization["available"],
                    localization=localization)
                alerts.set(
                    "localization_jump",
                    localization.get("jump_m", 0.0) > 0.20
                    or localization.get("jump_yaw_deg", 0.0) > 10.0,
                    localization=localization)
                alerts.set(
                    "stationary_localization_drift",
                    localization.get("drift_m", 0.0) > 0.50
                    or localization.get("drift_yaw_deg", 0.0) > 20.0,
                    localization=localization)

                last_summary = {
                    "streams": streams, "state": state,
                    "localization": localization, "processes": processes,
                    "system": system, "uart": current_uart,
                    "uart_delta": uart_delta, "uart_fault_events": uart_faults,
                    "imu_kernel_driver": driver,
                    "serial_owners": device_owners(args.serial_port),
                    "i2c_owners": device_owners(args.i2c_port),
                    "alerts_active": sorted(name for name, value in alerts.active.items() if value),
                }
                log.write("heartbeat", **last_summary)
                print(
                    f"OK {datetime.now().astimezone().isoformat(timespec='seconds')} "
                    f"manager={manager_active} scan={scan['rate_hz']}Hz "
                    f"imu={imu_raw['rate_hz']}Hz tf={localization.get('available')} "
                    f"drift={localization.get('drift_m')}m cpu={system['cpu_usage_percent']}%",
                    flush=True)
                next_heartbeat = now + args.heartbeat
            stop_event.wait(args.poll)
    except Exception as exc:
        log.write("fatal_error", error=repr(exc))
        raise
    finally:
        stop_event.set()
        executor.shutdown(timeout_sec=2.0)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=2.0)
        summary = {
            "stopped_at": datetime.now().astimezone().isoformat(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "alert_counts": alerts.counts,
            "alerts_active_at_stop": sorted(
                name for name, value in alerts.active.items() if value),
            "localization": node.localization_summary(),
            "last_heartbeat": last_summary,
            "uart_fault_events": uart_faults,
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log.write("stop", summary=summary)
        log.close()
        print(f"Stopped. Summary: {run_dir / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
