#!/usr/bin/env python3
"""Hardware-safe black-box integration test for motion_controller."""

import argparse
import math
import os
import signal
import subprocess
import sys
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from motion_controller.msg import NavGoal
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionServer, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool, Trigger


class Harness(Node):
    PREFIX = '/motion_controller_auto_test'

    def __init__(self, direction: float):
        super().__init__('motion_controller_auto_test_harness')
        self.direction = direction
        self.status = ''
        self.warning = ''
        self.conflict = False
        self.nav_status = ''
        self.wall_status = ''
        self.lidar_enabled = True
        self.outputs = []
        self.gated_scans = []
        self.received_goals = []
        self.wall_enable_requests = []
        self.results = []

        latched = QoSProfile(depth=10)
        latched.reliability = ReliabilityPolicy.RELIABLE
        latched.durability = DurabilityPolicy.TRANSIENT_LOCAL
        sensor_qos = QoSProfile(depth=10)
        sensor_qos.reliability = ReliabilityPolicy.BEST_EFFORT

        self.external_pub = self.create_publisher(Twist, f'{self.PREFIX}/external', 10)
        self.nav_pub = self.create_publisher(Twist, f'{self.PREFIX}/nav', 10)
        self.wall_pub = self.create_publisher(Twist, f'{self.PREFIX}/wall', 10)
        self.goal_pub = self.create_publisher(
            NavGoal, f'{self.PREFIX}/nav_goal_with_options', 10)
        self.raw_scan_pub = self.create_publisher(
            LaserScan, f'{self.PREFIX}/scan_raw', sensor_qos)
        self.aligned_pub = self.create_publisher(Bool, '/wall_alignment/aligned', 10)
        self.wall_node_status_pub = self.create_publisher(
            String, '/wall_alignment/status', 10)

        self.create_subscription(
            Twist, f'{self.PREFIX}/output', self._on_output, 10)
        self.create_subscription(
            LaserScan, f'{self.PREFIX}/scan_gated',
            lambda msg: self.gated_scans.append(msg.header.stamp.nanosec), sensor_qos)
        self.create_subscription(
            String, '/motion_controller_test/status',
            lambda msg: setattr(self, 'status', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller_test/warning',
            lambda msg: setattr(self, 'warning', msg.data), latched)
        self.create_subscription(
            Bool, '/motion_controller_test/control_conflict',
            lambda msg: setattr(self, 'conflict', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller_test/nav_goal_status',
            lambda msg: setattr(self, 'nav_status', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller_test/wall_alignment_status',
            lambda msg: setattr(self, 'wall_status', msg.data), latched)
        self.create_subscription(
            Bool, '/motion_controller_test/lidar_enabled',
            lambda msg: setattr(self, 'lidar_enabled', msg.data), latched)

        self.stop_client = self.create_client(Trigger, '/motion_controller_test/stop')
        self.align_client = self.create_client(Trigger, '/motion_controller_test/align_wall')
        self.lidar_client = self.create_client(
            SetBool, '/motion_controller_test/set_sensor_gate_enabled')
        self.wall_service = self.create_service(
            SetBool, '/wall_alignment/enable', self._wall_enable)
        self.nav_server = ActionServer(
            self, NavigateToPose, '/navigate_to_pose',
            execute_callback=self._execute_nav,
            goal_callback=lambda _: GoalResponse.ACCEPT)

    def _on_output(self, msg):
        self.outputs.append((time.monotonic(), msg.linear.x, msg.angular.z))

    def _wall_enable(self, request, response):
        self.wall_enable_requests.append(request.data)
        response.success = True
        response.message = 'fake wall service accepted request'
        return response

    def _execute_nav(self, goal_handle):
        pose = goal_handle.request.pose.pose
        yaw = 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)
        self.received_goals.append((pose.position.x, pose.position.y, yaw))
        goal_handle.succeed()
        return NavigateToPose.Result()

    @staticmethod
    def twist(x=0.0, z=0.0):
        msg = Twist()
        msg.linear.x = x
        msg.angular.z = z
        return msg

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def call(self, client, request, timeout=2.0):
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f'service unavailable: {client.srv_name}')
        future = client.call_async(request)
        if not self.wait_for(future.done, timeout):
            raise RuntimeError(f'service timeout: {client.srv_name}')
        return future.result()

    def stream(self, publisher, msg, seconds=0.45, hz=25.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            publisher.publish(msg)
            time.sleep(1.0 / hz)

    def check(self, name, condition, detail=''):
        passed = bool(condition)
        self.results.append((name, passed, detail))
        print(f'[{"PASS" if passed else "FAIL"}] {name}' +
              (f' — {detail}' if detail else ''), flush=True)

    def run_tests(self):
        self.check('startup_idle', self.wait_for(lambda: self.status == 'idle'), self.status)

        scan = LaserScan()
        scan.header.stamp.nanosec = 101
        self.raw_scan_pub.publish(scan)
        self.check('lidar_default_forwarding',
                   self.wait_for(lambda: 101 in self.gated_scans))
        req = SetBool.Request()
        req.data = False
        response = self.call(self.lidar_client, req)
        self.check('lidar_disable_idle', response.success and not self.lidar_enabled,
                   response.message)
        scan.header.stamp.nanosec = 102
        self.raw_scan_pub.publish(scan)
        time.sleep(0.25)
        self.check('lidar_blocks_scan', 102 not in self.gated_scans)
        req.data = True
        response = self.call(self.lidar_client, req)
        self.check('lidar_reenable', response.success and
                   self.wait_for(lambda: self.lidar_enabled), response.message)

        self.outputs.clear()
        self.stream(self.external_pub, self.twist(x=0.2), 0.6)
        peak = max((x for _, x, _ in self.outputs), default=0.0)
        self.check('external_velocity_forwarded_without_direction_flip', peak > 0.12,
                   f'peak_linear={peak:.3f}')
        req.data = False
        response = self.call(self.lidar_client, req)
        self.check('lidar_disable_rejected_during_motion', not response.success,
                   response.message)
        self.external_pub.publish(Twist())
        self.check('external_timeout_releases',
                   self.wait_for(lambda: self.status in ('released_external', 'idle'), 2.0),
                   self.status)

        self.outputs.clear()
        stop_stream = threading.Event()
        def external_stream():
            while not stop_stream.is_set():
                self.external_pub.publish(self.twist(x=0.18))
                time.sleep(0.04)
        thread = threading.Thread(target=external_stream)
        thread.start()
        self.wait_for(lambda: self.status == 'active_external')
        self.stream(self.nav_pub, self.twist(x=-0.2), 0.35)
        self.check('control_conflict_detected', self.conflict, self.warning)
        recent = [x for stamp, x, _ in self.outputs if stamp > time.monotonic() - 0.5]
        self.check('active_source_survives_conflict', bool(recent) and min(recent) >= -0.01)
        stop_stream.set()
        thread.join()
        self.external_pub.publish(Twist())
        self.nav_pub.publish(Twist())
        self.wait_for(lambda: not self.conflict and self.status == 'idle', 2.0)

        goal = NavGoal()
        goal.x, goal.y, goal.yaw = 0.1, 0.2, 1.0
        self.goal_pub.publish(goal)
        got_goal = self.wait_for(lambda: bool(self.received_goals), 3.0)
        expected = (self.direction * 0.1, self.direction * 0.2, self.direction * 1.0)
        actual = self.received_goals[-1] if got_goal else (math.nan,) * 3
        goal_ok = got_goal and all(abs(a - b) < 1e-5 for a, b in zip(actual, expected))
        self.check('ai_nav_goal_direction_transform', goal_ok,
                   f'expected={expected}, actual={actual}')
        self.check('nav_goal_success_feedback',
                   self.wait_for(lambda: self.nav_status == 'succeeded'), self.nav_status)

        invalid = NavGoal()
        invalid.x = math.nan
        self.goal_pub.publish(invalid)
        self.check('invalid_nav_goal_rejected', self.wait_for(
            lambda: self.nav_status == 'rejected_invalid_non_finite_goal'), self.nav_status)

        response = self.call(self.align_client, Trigger.Request())
        self.check('direct_wall_request_accepted', response.success, response.message)
        self.outputs.clear()
        self.stream(self.wall_pub, self.twist(z=0.3), 0.35)
        wall_peak = max((abs(z) for _, _, z in self.outputs), default=0.0)
        self.check('wall_velocity_forwarded', wall_peak > 0.25,
                   f'peak_angular={wall_peak:.3f}')
        aligned = Bool()
        aligned.data = True
        self.aligned_pub.publish(aligned)
        self.check('wall_success_feedback',
                   self.wait_for(lambda: self.wall_status == 'succeeded'), self.wall_status)

        wall_requests_before = len(self.wall_enable_requests)
        combined = NavGoal()
        combined.x = 0.05
        combined.align_to_wall = True
        self.conflict = False
        self.goal_pub.publish(combined)
        auto_wall_started = self.wait_for(
            lambda: len(self.wall_enable_requests) > wall_requests_before, 3.0)
        self.check('post_nav_wall_waits_then_starts',
                   auto_wall_started and not self.conflict, self.nav_status)
        aligned.data = True
        self.aligned_pub.publish(aligned)
        self.check('combined_nav_wall_success_feedback', self.wait_for(
            lambda: self.nav_status == 'succeeded;wall_aligned=true'), self.nav_status)

        self.outputs.clear()
        response = self.call(self.stop_client, Trigger.Request())
        zero_received = self.wait_for(lambda: any(
            abs(x) < 1e-9 and abs(z) < 1e-9 for _, x, z in self.outputs))
        self.check('stop_service_publishes_zero', response.success and zero_received,
                   f'{response.message}; status={self.status}')
        self.external_pub.publish(Twist())
        self.nav_pub.publish(Twist())
        self.wall_pub.publish(Twist())

        failures = [name for name, passed, _ in self.results if not passed]
        print(f'\nRESULT: {len(self.results) - len(failures)}/{len(self.results)} passed')
        if failures:
            print('FAILED: ' + ', '.join(failures))
        return not failures


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('sim', 'real'), default='sim')
    return parser.parse_args()


def main():
    args = parse_args()
    direction = 1.0 if args.mode == 'sim' else -1.0
    rclpy.init()
    harness = Harness(direction)
    if '/motion_controller' in harness.get_node_names():
        print('REFUSED: stop the normal motion_controller before isolated auto test.',
              file=sys.stderr)
        harness.destroy_node()
        rclpy.shutdown()
        return 2
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(harness)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    command = [
        'ros2', 'run', 'motion_controller', 'motion_controller_node', '--ros-args',
        '-r', '__node:=motion_controller_test',
        '-p', f'navigation_cmd_topic:={Harness.PREFIX}/nav',
        '-p', f'wall_alignment_cmd_topic:={Harness.PREFIX}/wall',
        '-p', f'external_cmd_topic:={Harness.PREFIX}/external',
        '-p', f'output_cmd_topic:={Harness.PREFIX}/output',
        '-p', f'nav_goal_with_options_topic:={Harness.PREFIX}/nav_goal_with_options',
        '-p', f'raw_scan_topic:={Harness.PREFIX}/scan_raw',
        '-p', f'gated_scan_topic:={Harness.PREFIX}/scan_gated',
        '-p', 'sensor_gate_recovery_required:=false',
        '-p', 'nav_goal_require_map_bounds:=false',
        '-p', f'direction_reverse:={direction}',
        '-p', 'switch_stop_duration:=0.10',
        '-p', 'input_timeout:=0.20',
    ]
    # ros2 run may spawn the executable below its CLI process. Own a dedicated
    # process group so failed tests cannot leave motion_controller_test behind.
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        passed = harness.run_tests()
        return 0 if passed else 1
    except Exception as exc:
        print(f'[FATAL] {exc}', file=sys.stderr)
        return 1
    finally:
        try:
            os.killpg(child.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2.0)
        executor.shutdown()
        harness.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
