#!/usr/bin/env python3
"""Repeatedly launch the real hardware stack and measure ROS stream health."""

import argparse
import csv
import datetime
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import Twist
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from ros_robot_controller_msgs.msg import MotorsState
from sensor_msgs.msg import Imu, JointState, LaserScan, Range

from robot_system_test.stats import StreamStats, evaluate, finite


MESSAGE_TYPES = {
    'sensor_msgs/msg/LaserScan': LaserScan,
    'sensor_msgs/msg/Imu': Imu,
    'sensor_msgs/msg/Range': Range,
    'sensor_msgs/msg/JointState': JointState,
    'ros_robot_controller_msgs/msg/MotorsState': MotorsState,
}


def message_stamp(message):
    header = getattr(message, 'header', None)
    if header is None:
        return None
    return float(header.stamp.sec) + float(header.stamp.nanosec) * 1e-9


def valid_message(message):
    if isinstance(message, LaserScan):
        usable = [value for value in message.ranges if math.isfinite(value)]
        return bool(message.ranges) and bool(usable) and finite([
            message.angle_min, message.angle_max, message.angle_increment,
            message.range_min, message.range_max,
        ])
    if isinstance(message, Imu):
        values = [
            message.angular_velocity.x, message.angular_velocity.y,
            message.angular_velocity.z, message.linear_acceleration.x,
            message.linear_acceleration.y, message.linear_acceleration.z,
        ]
        quaternion = message.orientation
        if quaternion_covariance_available(message):
            values += [quaternion.x, quaternion.y, quaternion.z, quaternion.w]
            norm = sum(value * value for value in
                       [quaternion.x, quaternion.y, quaternion.z, quaternion.w])
            if not 0.5 <= norm <= 1.5:
                return False
        return finite(values)
    if isinstance(message, Range):
        return (math.isfinite(message.range) and message.min_range <= message.range
                < message.max_range)
    if isinstance(message, JointState):
        return bool(message.name) and finite(message.position + message.velocity + message.effort)
    if isinstance(message, MotorsState):
        # Message layouts changed across controller revisions. Recursively reject NaN/Inf
        # while accepting an empty motor list as a valid controller heartbeat.
        return finite_numeric_fields(message)
    return True


def quaternion_covariance_available(message):
    covariance = message.orientation_covariance
    return not (len(covariance) > 0 and covariance[0] < 0.0)


def finite_numeric_fields(message):
    for field_name in message.get_fields_and_field_types():
        value = getattr(message, field_name)
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, float) and not math.isfinite(item):
                    return False
                if hasattr(item, 'get_fields_and_field_types') and not finite_numeric_fields(item):
                    return False
    return True


class HardwareMonitor(Node):
    def __init__(self, topic_config, publish_zero):
        super().__init__('hardware_stability_monitor')
        self._lock = threading.Lock()
        self.stats = {key: StreamStats() for key in topic_config}
        self.seen = set()
        self._monitor_subscriptions = []
        for key, config in topic_config.items():
            message_type = MESSAGE_TYPES.get(config['type'])
            if message_type is None:
                raise ValueError('Unsupported message type: ' + config['type'])
            callback = lambda message, stream=key: self._receive(stream, message)
            self._monitor_subscriptions.append(
                self.create_subscription(message_type, config['name'], callback, 50))
        self.zero_publisher = None
        self.zero_timer = None
        if publish_zero:
            self.zero_publisher = self.create_publisher(Twist, '/cmd_vel', 10)
            self.zero_timer = self.create_timer(0.1, lambda: self.zero_publisher.publish(Twist()))

    def _receive(self, stream, message):
        now = time.monotonic()
        with self._lock:
            self.seen.add(stream)
            self.stats[stream].add(now, valid_message(message), message_stamp(message))

    def reset(self):
        with self._lock:
            self.stats = {key: StreamStats() for key in self.stats}

    def missing_streams(self):
        with self._lock:
            return sorted(set(self.stats) - self.seen)

    def snapshot(self, duration):
        with self._lock:
            return {key: value.result(duration) for key, value in self.stats.items()}


def stop_process(process, timeout):
    """Stop ros2 launch and also clean children after the launch parent exits."""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2.0)
    # ros2 launch may exit before its children. Reap/kill any survivors in its
    # original process group so the next iteration cannot duplicate publishers.
    for sig, wait_sec in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        time.sleep(wait_sec)
    return process.returncode


def wait_for_streams(monitor, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False, 'launch_exited'
        if not monitor.missing_streams():
            return True, ''
        time.sleep(0.1)
    return False, 'startup_timeout'


def run_iteration(index, args, config, monitor, output_dir):
    launch_log = output_dir / ('iteration_%03d_launch.log' % index)
    command = ['ros2', 'launch', args.launch_package, args.launch_file] + args.launch_arg
    started_at = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    with monitor._lock:
        monitor.seen.clear()
    monitor.reset()
    with launch_log.open('w', encoding='utf-8') as log_file:
        launch_started = time.monotonic()
        process = subprocess.Popen(
            command, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True, text=True)
        ready, startup_error = wait_for_streams(
            monitor, process, float(config['startup_timeout_sec']))
        startup_wait_sec = time.monotonic() - launch_started
        if ready:
            monitor.reset()
            sample_started = time.monotonic()
            deadline = sample_started + float(config['sample_duration_sec'])
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.1)
            sample_duration = max(0.001, time.monotonic() - sample_started)
            process_died = process.poll() is not None
            streams = monitor.snapshot(sample_duration)
        else:
            sample_duration = 0.0
            process_died = process.poll() is not None
            streams = monitor.snapshot(max(0.001, float(config['startup_timeout_sec'])))

        nodes = set(monitor.get_node_names_and_namespaces())
        actual_nodes = {namespace.rstrip('/') + '/' + name if namespace != '/'
                        else '/' + name for name, namespace in nodes}
        missing_nodes = sorted(set(config.get('expected_nodes', [])) - actual_nodes)
        return_code = stop_process(process, float(config['shutdown_timeout_sec']))

    failures = []
    if startup_error:
        failures.append(startup_error)
    if process_died:
        failures.append('launch_exited_during_sample')
    if missing_nodes:
        failures.append('missing_nodes')
    for key, result in streams.items():
        result['failures'] = evaluate(result, config['topics'][key])
        if result['failures']:
            failures.append(key + ':' + ','.join(result['failures']))
    return {
        'iteration': index,
        'passed': not failures,
        'started_at': started_at,
        'startup_wait_sec': startup_wait_sec,
        'sample_duration_sec': sample_duration,
        'launch_return_code': return_code,
        'missing_nodes': missing_nodes,
        'missing_topics_at_startup': monitor.missing_streams() if not ready else [],
        'failures': failures,
        'streams': streams,
        'launch_log': str(launch_log),
    }


def write_reports(results, output_dir, invocation, config):
    passed = sum(1 for result in results if result['passed'])
    report = {
        'passed': passed == len(results),
        'iterations_passed': passed,
        'iterations_total': len(results),
        'invocation': invocation,
        'config': config,
        'iterations': results,
    }
    (output_dir / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output_dir / 'summary.csv').open('w', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(['iteration', 'passed', 'failures', 'launch_log'])
        for result in results:
            writer.writerow([result['iteration'], result['passed'],
                             ';'.join(result['failures']), result['launch_log']])
    return report


def parse_args(argv=None):
    default_config = Path(get_package_share_directory('robot_system_test')) / 'config' / 'hardware_stability.yaml'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=10)
    parser.add_argument('--config', default=str(default_config))
    parser.add_argument('--output-dir', default='hardware_test_results')
    parser.add_argument('--launch-package', default='robot_bringup')
    parser.add_argument('--launch-file', default='real_robot_base.launch.py')
    parser.add_argument('--launch-arg', action='append', default=[],
                        help='Forward one launch argument, e.g. lidar_serial_port:=/dev/ttyS8')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.iterations < 1:
        raise SystemExit('--iterations must be at least 1')
    with Path(args.config).open(encoding='utf-8') as config_file:
        config = yaml.safe_load(config_file)
    timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    output_dir = Path(args.output_dir).expanduser().resolve() / timestamp
    output_dir.mkdir(parents=True, exist_ok=False)
    rclpy.init(args=None)
    monitor = HardwareMonitor(config['topics'], config.get('zero_cmd_vel_during_test', True))
    executor = SingleThreadedExecutor()
    executor.add_node(monitor)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    results = []
    try:
        for index in range(1, args.iterations + 1):
            print('[%d/%d] starting hardware stack' % (index, args.iterations), flush=True)
            result = run_iteration(index, args, config, monitor, output_dir)
            results.append(result)
            print('[%d/%d] %s: %s' % (
                index, args.iterations, 'PASS' if result['passed'] else 'FAIL',
                ', '.join(result['failures']) or 'all checks passed'), flush=True)
            if index < args.iterations:
                time.sleep(float(config['cooldown_sec']))
    except KeyboardInterrupt:
        print('Interrupted; writing partial report.', file=sys.stderr)
    finally:
        executor.shutdown()
        monitor.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=2.0)
    invocation = ['hardware_stability_test'] + (argv if argv is not None else sys.argv[1:])
    report = write_reports(results, output_dir, invocation, config)
    print('Report: ' + str(output_dir / 'report.json'))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
