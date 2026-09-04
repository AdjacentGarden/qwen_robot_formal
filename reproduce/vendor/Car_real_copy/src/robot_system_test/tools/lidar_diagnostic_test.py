#!/usr/bin/env python3
"""Classify real-robot lidar failures as acquisition or software-gate faults."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import statistics
import time

import rclpy
from rcl_interfaces.msg import Log
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, String


@dataclass
class ScanStats:
    received: int = 0
    first_rx: float | None = None
    last_rx: float | None = None
    intervals: list[float] = field(default_factory=list)
    stamp_ns: list[int] = field(default_factory=list)
    valid_points: list[int] = field(default_factory=list)
    total_points: list[int] = field(default_factory=list)
    scan_times: list[float] = field(default_factory=list)
    bad_geometry: int = 0
    non_monotonic_stamps: int = 0

    def add(self, msg: LaserScan, now: float) -> None:
        if self.last_rx is not None:
            self.intervals.append(now - self.last_rx)
        self.first_rx = now if self.first_rx is None else self.first_rx
        self.last_rx = now
        self.received += 1
        stamp = int(msg.header.stamp.sec) * 1_000_000_000 + int(msg.header.stamp.nanosec)
        if self.stamp_ns and stamp <= self.stamp_ns[-1]:
            self.non_monotonic_stamps += 1
        self.stamp_ns.append(stamp)
        total = len(msg.ranges)
        valid = sum(
            1 for value in msg.ranges
            if math.isfinite(value) and msg.range_min <= value <= msg.range_max)
        self.total_points.append(total)
        self.valid_points.append(valid)
        self.scan_times.append(float(msg.scan_time))
        expected = 0
        if msg.angle_increment > 0.0 and msg.angle_max >= msg.angle_min:
            expected = round((msg.angle_max - msg.angle_min) / msg.angle_increment) + 1
        if total < 2 or expected <= 0 or abs(total - expected) > 2:
            self.bad_geometry += 1

    def report(self, duration: float, now: float, max_gap: float, min_valid: int) -> dict:
        hz = self.received / duration if duration > 0.0 else 0.0
        gaps = [value for value in self.intervals if value > max_gap]
        terminal_silence = None if self.last_rx is None else now - self.last_rx
        terminal_timeout = terminal_silence is not None and terminal_silence > max_gap
        low_valid = sum(value < min_valid for value in self.valid_points)
        zero_duration = sum(value <= 0.0 for value in self.scan_times)
        return {
            'received': self.received,
            'average_hz': hz,
            'median_interval_s': statistics.median(self.intervals) if self.intervals else None,
            'max_interval_s': max(self.intervals) if self.intervals else None,
            'gaps_over_limit': len(gaps) + int(terminal_timeout),
            'largest_gap_s': max(gaps + ([terminal_silence] if terminal_timeout else [0.0])),
            'terminal_silence_s': terminal_silence,
            'valid_points_min': min(self.valid_points) if self.valid_points else None,
            'valid_points_median': statistics.median(self.valid_points) if self.valid_points else None,
            'low_valid_frames': low_valid,
            'zero_scan_time_frames': zero_duration,
            'bad_geometry_frames': self.bad_geometry,
            'non_monotonic_stamp_frames': self.non_monotonic_stamps,
        }


class LidarDiagnostic(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__('lidar_diagnostic_test')
        self.args = args
        self.started = time.monotonic()
        self.raw = ScanStats()
        self.gated = ScanStats()
        self.gate_enabled: bool | None = None
        self.gate_state = 'unknown'
        self.gate_events: list[dict] = []
        self.gated_frames_while_disabled = 0
        self.quality_events: list[dict] = []
        self.driver_errors: list[str] = []
        self.publisher_counts: dict[str, int] = {}
        self.last_progress = self.started

        self.create_subscription(
            LaserScan, args.raw_topic,
            lambda msg: self.raw.add(msg, time.monotonic()), qos_profile_sensor_data)
        self.create_subscription(
            LaserScan, args.gated_topic,
            self._gated_scan, qos_profile_sensor_data)

        status_qos = QoSProfile(depth=10)
        status_qos.reliability = ReliabilityPolicy.RELIABLE
        status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(Bool, args.gate_enabled_topic, self._gate_enabled, status_qos)
        self.create_subscription(String, args.gate_state_topic, self._gate_state, status_qos)
        self.create_subscription(String, args.quality_topic, self._quality, 10)
        self.create_subscription(Log, '/rosout', self._rosout, 100)

    def _event(self, value) -> dict:
        return {'elapsed_s': round(time.monotonic() - self.started, 3), 'value': value}

    def _gate_enabled(self, msg: Bool) -> None:
        self.gate_enabled = bool(msg.data)
        self.gate_events.append(self._event({'enabled': self.gate_enabled}))

    def _gated_scan(self, msg: LaserScan) -> None:
        self.gated.add(msg, time.monotonic())
        if self.gate_enabled is False:
            self.gated_frames_while_disabled += 1

    def _gate_state(self, msg: String) -> None:
        self.gate_state = msg.data.strip()
        self.gate_events.append(self._event({'state': self.gate_state}))

    def _quality(self, msg: String) -> None:
        self.quality_events.append(self._event(msg.data.strip()))

    def _rosout(self, msg: Log) -> None:
        text = msg.msg.strip()
        name = msg.name.lower()
        if ('rplidar' in name or 'lidar_quality' in text.lower()) and any(
                token in text.lower() for token in
                ('error', 'failed', 'timeout', 'rejected', 'restart', 'health')):
            if text not in self.driver_errors:
                self.driver_errors.append(text)

    def refresh_graph(self) -> None:
        for topic in (self.args.raw_topic, self.args.gated_topic):
            self.publisher_counts[topic] = len(self.get_publishers_info_by_topic(topic))

    def progress(self) -> None:
        now = time.monotonic()
        if now - self.last_progress < self.args.progress_period:
            return
        self.last_progress = now
        raw_age = math.inf if self.raw.last_rx is None else now - self.raw.last_rx
        gated_age = math.inf if self.gated.last_rx is None else now - self.gated.last_rx
        print(
            f'[lidar-test {now - self.started:6.1f}s] raw={self.raw.received} '
            f'age={raw_age:.3f}s gated={self.gated.received} age={gated_age:.3f}s '
            f'gate={self.gate_enabled}/{self.gate_state}', flush=True)

    def evaluate(self) -> tuple[int, dict]:
        now = time.monotonic()
        elapsed = max(0.001, now - self.started)
        self.refresh_graph()
        raw = self.raw.report(elapsed, now, self.args.max_gap, self.args.min_valid_points)
        gated = self.gated.report(elapsed, now, self.args.max_gap, self.args.min_valid_points)
        quality_bad = any(
            any(word in str(item['value']).upper() for word in ('RESTART', 'REJECT', 'ERROR'))
            for item in self.quality_events)
        raw_bad = (
            raw['average_hz'] < self.args.min_hz
            or raw['gaps_over_limit'] > 0
            or raw['low_valid_frames'] > 0
            or raw['zero_scan_time_frames'] > 0
            or raw['bad_geometry_frames'] > 0
            or raw['non_monotonic_stamp_frames'] > 0
            or quality_bad
            or bool(self.driver_errors))
        gate_expected = not self.args.raw_only and self.gate_enabled is not False
        gated_bad = gate_expected and (
            gated['average_hz'] < self.args.min_hz
            or gated['gaps_over_limit'] > 0
            or (self.raw.received > 0 and gated['received'] < self.raw.received * 0.90))

        if self.publisher_counts.get(self.args.raw_topic, 0) == 0:
            verdict = 'INCONCLUSIVE_NO_RAW_PUBLISHER'
            explanation = '没有检测到原始 scan publisher；驱动可能未启动或启动文件错误。'
            exit_code = 2
        elif raw_bad:
            verdict = 'HARDWARE_OR_DRIVER_ACQUISITION_FAULT'
            explanation = (
                '故障已经出现在原始 /scan 或 RPLidar 驱动质量层，优先检查雷达供电、'
                '串口线/接口、电机转速、串口占用和 RPLidar SDK 驱动。不是 sensor gate 下游丢包。')
            exit_code = 1
        elif gated_bad:
            verdict = 'SOFTWARE_SENSOR_GATE_FAULT'
            explanation = (
                '原始 /scan 健康，但 /scan_gated 在 gate 应开启时丢帧或断流；'
                '检查 motion_controller、gate 状态和 QoS。')
            exit_code = 1
        elif not self.args.raw_only and self.gated_frames_while_disabled > 0:
            verdict = 'SOFTWARE_GATE_LEAK'
            explanation = 'sensor gate 已关闭，但仍收到 gated scan。'
            exit_code = 1
        else:
            verdict = 'LIDAR_PIPELINE_HEALTHY'
            explanation = (
                '测试窗口内原始 scan 正常。' if self.args.raw_only else
                '测试窗口内原始 scan 与门控转发均正常；若 Cartographer 仍报错，'
                '应继续检查 gated IMU/odom 时间戳、TF 或 Cartographer。')
            exit_code = 0

        report = {
            'verdict': verdict,
            'explanation': explanation,
            'duration_s': elapsed,
            'raw_only': self.args.raw_only,
            'thresholds': {
                'min_hz': self.args.min_hz,
                'max_gap_s': self.args.max_gap,
                'min_valid_points': self.args.min_valid_points,
            },
            'publishers': self.publisher_counts,
            'gate': {'enabled': self.gate_enabled, 'state': self.gate_state,
                     'gated_frames_while_disabled': self.gated_frames_while_disabled,
                     'events': self.gate_events},
            'raw_scan': raw,
            'gated_scan': gated,
            'quality_events': self.quality_events,
            'driver_errors': self.driver_errors,
        }
        return exit_code, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='RPLidar hardware/software isolation test')
    parser.add_argument('--duration', type=float, default=180.0)
    parser.add_argument('--raw-topic', default='/scan')
    parser.add_argument('--gated-topic', default='/scan_gated')
    parser.add_argument('--quality-topic', default='/rplidar/quality_status')
    parser.add_argument('--gate-enabled-topic', default='/motion_controller/lidar_enabled')
    parser.add_argument('--gate-state-topic', default='/motion_controller/sensor_gate_state')
    parser.add_argument('--min-hz', type=float, default=8.0)
    parser.add_argument('--max-gap', type=float, default=0.35)
    parser.add_argument('--min-valid-points', type=int, default=60)
    parser.add_argument('--progress-period', type=float, default=5.0)
    parser.add_argument('--output', default='')
    parser.add_argument(
        '--raw-only', action='store_true',
        help='只判断原始 scan；用于仅启动 RPLidar 驱动的隔离测试')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0 or args.min_hz <= 0 or args.max_gap <= 0:
        raise SystemExit('duration/min-hz/max-gap 必须大于 0')
    rclpy.init(args=None)
    node = LidarDiagnostic(args)
    deadline = time.monotonic() + args.duration
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            node.progress()
        exit_code, report = node.evaluate()
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print('\n' + text, flush=True)
        if args.output:
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text + '\n', encoding='utf-8')
            print(f'报告已保存：{output}', flush=True)
        return exit_code
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
