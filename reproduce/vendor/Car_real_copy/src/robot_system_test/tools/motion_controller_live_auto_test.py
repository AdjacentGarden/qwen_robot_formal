#!/usr/bin/env python3
"""End-to-end AI control acceptance test against a running full robot stack."""

import argparse
from collections import deque
import math
import random
import sys
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from motion_controller.msg import NavGoal
from nav_msgs.msg import OccupancyGrid
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool, Trigger
from tf2_ros import Buffer, TransformException, TransformListener


TERMINAL_NAV_PREFIXES = (
    'succeeded', 'aborted', 'cancelled', 'rejected_', 'unknown_result',
    'failed_to_', 'wall_alignment_failed_', 'wall_alignment_cancelled',
)
TERMINAL_WALL_WORDS = ('succeeded', 'cancelled')


class LiveAcceptance(Node):
    def __init__(self, args):
        super().__init__('motion_controller_live_auto_test')
        self.args = args
        self.direction = 1.0 if args.mode == 'sim' else -1.0
        self.rng = random.Random(args.seed)
        self.map_msg = None
        self.manager_state = ''
        self.controller_status = ''
        self.warning = ''
        self.conflict = False
        self.conflict_events = []
        self.lidar_enabled = True
        self.nav_events = []
        self.wall_events = []
        self.results = []
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        latched = QoSProfile(depth=20)
        latched.reliability = ReliabilityPolicy.RELIABLE
        latched.durability = DurabilityPolicy.TRANSIENT_LOCAL
        map_qos = QoSProfile(depth=1)
        map_qos.reliability = ReliabilityPolicy.RELIABLE
        map_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel_external', 10)
        self.goal_pub = self.create_publisher(
            NavGoal, '/motion_controller/nav_goal_with_options', 10)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, map_qos)
        self.create_subscription(
            String, '/mapping_manager/state',
            lambda msg: setattr(self, 'manager_state', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller/status',
            lambda msg: setattr(self, 'controller_status', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller/warning', self._on_warning, latched)
        self.create_subscription(
            Bool, '/motion_controller/control_conflict',
            self._on_conflict, latched)
        self.create_subscription(
            Bool, '/motion_controller/lidar_enabled',
            lambda msg: setattr(self, 'lidar_enabled', msg.data), latched)
        self.create_subscription(
            String, '/motion_controller/nav_goal_status',
            lambda msg: self.nav_events.append((time.monotonic(), msg.data)), latched)
        self.create_subscription(
            String, '/motion_controller/wall_alignment_status',
            lambda msg: self.wall_events.append((time.monotonic(), msg.data)), latched)

        self.cancel_goal = self.create_client(
            Trigger, '/motion_controller/cancel_nav_goal')
        self.align_wall = self.create_client(Trigger, '/motion_controller/align_wall')
        self.cancel_align = self.create_client(
            Trigger, '/motion_controller/cancel_wall_alignment')
        self.stop_client = self.create_client(Trigger, '/motion_controller/stop')
        self.lidar_client = self.create_client(
            SetBool, '/motion_controller/set_sensor_gate_enabled')

    def _on_map(self, msg):
        if msg.info.width and msg.info.height and msg.info.resolution > 0.0:
            self.map_msg = msg

    def _on_warning(self, msg):
        self.warning = msg.data

    def _on_conflict(self, msg):
        self.conflict = msg.data
        self.conflict_events.append((time.monotonic(), msg.data))

    @staticmethod
    def twist(x=0.0, z=0.0):
        msg = Twist()
        msg.linear.x = x
        msg.angular.z = z
        return msg

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def call(self, client, request, timeout=3.0):
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f'service unavailable: {client.srv_name}')
        future = client.call_async(request)
        if not self.wait_for(future.done, timeout):
            raise RuntimeError(f'service timeout: {client.srv_name}')
        return future.result()

    def record(self, name, passed, detail=''):
        self.results.append((name, bool(passed), detail))
        print(f'[{"PASS" if passed else "FAIL"}] {name}' +
              (f' — {detail}' if detail else ''), flush=True)

    def robot_pose(self):
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_msg.header.frame_id or 'map', self.args.base_frame,
                rclpy.time.Time())
        except TransformException as exc:
            raise RuntimeError(f'map -> {self.args.base_frame} unavailable: {exc}') from exc
        t = transform.transform.translation
        q = transform.transform.rotation
        return t.x, t.y, 2.0 * math.atan2(q.z, q.w)

    def random_reachable_goal(self, min_distance=None, max_distance=None):
        grid = self.map_msg
        width, height = grid.info.width, grid.info.height
        resolution = grid.info.resolution
        origin_x = grid.info.origin.position.x
        origin_y = grid.info.origin.position.y
        robot_x, robot_y, _ = self.robot_pose()
        start_x = int((robot_x - origin_x) / resolution)
        start_y = int((robot_y - origin_y) / resolution)
        if not (0 <= start_x < width and 0 <= start_y < height):
            raise RuntimeError('robot pose is outside /map')
        free_threshold = self.args.free_threshold
        data = grid.data
        start = start_y * width + start_x
        if data[start] < 0 or data[start] > free_threshold:
            raise RuntimeError('robot is not in a free map cell')
        visited = bytearray(width * height)
        visited[start] = 1
        queue = deque([start])
        reachable = []
        while queue:
            index = queue.popleft()
            x, y = index % width, index // width
            reachable.append((x, y))
            for neighbor in (index - 1, index + 1, index - width, index + width):
                nx, ny = neighbor % width, neighbor // width
                if (neighbor < 0 or neighbor >= width * height or visited[neighbor]):
                    continue
                if abs(nx - x) + abs(ny - y) != 1:
                    continue
                if 0 <= data[neighbor] <= free_threshold:
                    visited[neighbor] = 1
                    queue.append(neighbor)
        min_d = self.args.min_goal_distance if min_distance is None else min_distance
        max_d = self.args.max_goal_distance if max_distance is None else max_distance
        clearance = max(1, int(math.ceil(self.args.clearance / resolution)))
        candidates = []
        for cell_x, cell_y in reachable:
            world_x = origin_x + (cell_x + 0.5) * resolution
            world_y = origin_y + (cell_y + 0.5) * resolution
            distance = math.hypot(world_x - robot_x, world_y - robot_y)
            if distance < min_d or distance > max_d:
                continue
            clear = True
            for yy in range(max(0, cell_y - clearance), min(height, cell_y + clearance + 1)):
                row = yy * width
                for xx in range(max(0, cell_x - clearance),
                                min(width, cell_x + clearance + 1)):
                    if data[row + xx] < 0 or data[row + xx] > free_threshold:
                        clear = False
                        break
                if not clear:
                    break
            if clear:
                candidates.append((world_x, world_y))
        if not candidates:
            raise RuntimeError(
                f'no reachable free goal with clearance={self.args.clearance}m '
                f'and distance={min_d}..{max_d}m')
        x, y = self.rng.choice(candidates)
        return x, y, self.rng.uniform(-math.pi, math.pi)

    def send_map_goal(self, target, align=False):
        x, y, yaw = target
        goal = NavGoal()
        goal.x = self.direction * x
        goal.y = self.direction * y
        goal.yaw = self.direction * yaw
        goal.align_to_wall = align
        start = len(self.nav_events)
        self.goal_pub.publish(goal)
        print(f'  map target=({x:.2f}, {y:.2f}, {yaw:.2f}), '
              f'AI payload=({goal.x:.2f}, {goal.y:.2f}, {goal.yaw:.2f}), '
              f'align={align}', flush=True)
        return start

    def nav_events_after(self, start):
        return [status for _, status in self.nav_events[start:]]

    def wait_nav_terminal(self, start, timeout=None):
        timeout = timeout or self.args.nav_timeout
        terminal = {'value': ''}
        def finished():
            for status in self.nav_events_after(start):
                if status.startswith(TERMINAL_NAV_PREFIXES):
                    terminal['value'] = status
                    return True
            return False
        self.wait_for(finished, timeout)
        return terminal['value']

    def publish_for(self, msg, duration, hz=20.0):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.cmd_pub.publish(msg)
            time.sleep(1.0 / hz)
        self.cmd_pub.publish(Twist())

    def run(self):
        print(f'LIVE E2E mode={self.args.mode}, seed={self.args.seed}', flush=True)
        ready = self.wait_for(
            lambda: self.map_msg is not None and self.controller_status in
            ('idle', 'active_navigation'), self.args.startup_timeout)
        self.record('full_stack_ready', ready,
                    f'manager={self.manager_state}, motion={self.controller_status}')
        if not ready:
            return False
        try:
            pose = self.robot_pose()
            self.record('map_tf_available', True,
                        f'pose=({pose[0]:.2f}, {pose[1]:.2f}, {pose[2]:.2f})')
        except RuntimeError as exc:
            self.record('map_tf_available', False, str(exc))
            return False

        request = SetBool.Request()
        request.data = False
        response = self.call(self.lidar_client, request)
        self.record('sensor_gate_off_idle', response.success and self.wait_for(
            lambda: not self.lidar_enabled, 2.0), response.message)
        request.data = True
        response = self.call(self.lidar_client, request)
        self.record('sensor_gate_on', response.success and self.wait_for(
            lambda: self.lidar_enabled, 2.0), response.message)
        time.sleep(self.args.lidar_recovery_time)

        for index in range(self.args.goal_count):
            target = self.random_reachable_goal()
            start = self.send_map_goal(target)
            terminal = self.wait_nav_terminal(start)
            self.record(f'random_navigation_{index + 1}', terminal == 'succeeded', terminal)
            if terminal != 'succeeded':
                break

        target = self.random_reachable_goal(
            min_distance=max(self.args.min_goal_distance, 0.8))
        start = self.send_map_goal(target)
        nav_moving = self.wait_for(
            lambda: self.controller_status == 'active_navigation', 10.0)
        conflict_start = len(self.conflict_events)
        if nav_moving:
            self.publish_for(self.twist(x=self.args.test_linear_speed), 0.4)
        conflict_seen = any(value for _, value in self.conflict_events[conflict_start:])
        self.record('external_request_rejected_during_navigation',
                    nav_moving and conflict_seen,
                    self.warning or self.controller_status)
        request.data = False
        response = self.call(self.lidar_client, request)
        self.record('sensor_gate_off_rejected_during_navigation', not response.success,
                    response.message)
        if response.success:
            request.data = True
            self.call(self.lidar_client, request)
            self.wait_for(lambda: self.lidar_enabled, 2.0)
        terminal = self.wait_nav_terminal(start)
        self.record('navigation_survives_external_conflict', terminal == 'succeeded', terminal)

        target = self.random_reachable_goal(
            min_distance=max(self.args.min_goal_distance, 0.8))
        start = self.send_map_goal(target)
        accepted = self.wait_for(lambda: any(
            status == 'accepted_by_nav2' or status.startswith('navigating;')
            for status in self.nav_events_after(start)), 5.0)
        time.sleep(0.5)
        response = self.call(self.cancel_goal, Trigger.Request())
        terminal = self.wait_nav_terminal(start, 8.0)
        self.record('navigation_cancel', accepted and response.success and
                    terminal == 'cancelled', f'{response.message}; {terminal}')
        self.wait_for(lambda: self.controller_status == 'idle', 3.0)

        self.publish_for(self.twist(x=self.args.test_linear_speed), 0.6)
        released = self.wait_for(lambda: self.controller_status in
                                 ('released_external', 'switching_zero_hold', 'idle'), 2.0)
        self.record('external_linear_command_and_release', released, self.controller_status)
        self.publish_for(self.twist(z=self.args.test_angular_speed), 0.5)
        released = self.wait_for(lambda: self.controller_status in
                                 ('released_external', 'switching_zero_hold', 'idle'), 2.0)
        self.record('external_angular_command_and_release', released, self.controller_status)

        stop_stream = threading.Event()
        def stream_external():
            while not stop_stream.is_set():
                self.cmd_pub.publish(self.twist(x=self.args.test_linear_speed))
                time.sleep(0.05)
        thread = threading.Thread(target=stream_external)
        thread.start()
        self.wait_for(lambda: self.controller_status == 'active_external', 2.0)
        response = self.call(self.stop_client, Trigger.Request())
        stop_stream.set()
        thread.join()
        self.cmd_pub.publish(Twist())
        self.record('stop_during_external_control', response.success, response.message)
        self.wait_for(lambda: self.controller_status == 'idle', 3.0)

        cancel_wall_start = len(self.wall_events)
        response = self.call(self.align_wall, Trigger.Request())
        time.sleep(0.3)
        cancel_response = self.call(self.cancel_align, Trigger.Request())
        cancelled = self.wait_for(lambda: any(
            status.endswith('cancelled') for _, status in
            self.wall_events[cancel_wall_start:]), 3.0)
        self.record('wall_alignment_cancel', response.success and
                    cancel_response.success and cancelled, cancel_response.message)
        self.wait_for(lambda: self.controller_status == 'idle', 3.0)

        wall_start = len(self.wall_events)
        response = self.call(self.align_wall, Trigger.Request())
        wall_terminal = {'value': ''}
        def wall_finished():
            for _, status in self.wall_events[wall_start:]:
                raw = status.removeprefix('running:')
                if raw in TERMINAL_WALL_WORDS or raw.startswith('failed_'):
                    wall_terminal['value'] = raw
                    return True
            return False
        completed = response.success and self.wait_for(wall_finished, 8.0)
        self.record('direct_wall_alignment_has_terminal_result', completed,
                    wall_terminal['value'] or response.message)
        self.wait_for(lambda: self.controller_status == 'idle', 3.0)

        target = self.random_reachable_goal()
        start = self.send_map_goal(target, align=True)
        terminal = self.wait_nav_terminal(start, self.args.nav_timeout + 10.0)
        combined_complete = terminal == 'succeeded;wall_aligned=true' or terminal.startswith(
            'wall_alignment_failed_')
        self.record('navigation_with_wall_alignment_completes', combined_complete, terminal)

        failures = [name for name, passed, _ in self.results if not passed]
        print(f'\nLIVE RESULT: {len(self.results) - len(failures)}/{len(self.results)} passed')
        if failures:
            print('FAILED: ' + ', '.join(failures))
        return not failures


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('sim', 'real'), default='sim')
    parser.add_argument('--confirm-real-motion', action='store_true')
    parser.add_argument('--seed', type=int, default=20260815)
    parser.add_argument('--goal-count', type=int, default=2)
    parser.add_argument('--min-goal-distance', type=float, default=0.6)
    parser.add_argument('--max-goal-distance', type=float, default=2.0)
    parser.add_argument('--clearance', type=float, default=0.35)
    parser.add_argument('--free-threshold', type=int, default=20)
    parser.add_argument('--test-linear-speed', type=float, default=0.06)
    parser.add_argument('--test-angular-speed', type=float, default=0.15)
    parser.add_argument('--base-frame', default='base_footprint')
    parser.add_argument('--startup-timeout', type=float, default=90.0)
    parser.add_argument('--nav-timeout', type=float, default=120.0)
    parser.add_argument('--lidar-recovery-time', type=float, default=1.0)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.mode == 'real' and not args.confirm_real_motion:
        print('REFUSED: real mode moves the robot. Add --confirm-real-motion after '
              'clearing the area and preparing the hardware emergency stop.', file=sys.stderr)
        return 2
    rclpy.init()
    node = LiveAcceptance(args)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    try:
        return 0 if node.run() else 1
    except Exception as exc:
        print(f'[FATAL] {exc}', file=sys.stderr)
        return 1
    finally:
        node.cmd_pub.publish(Twist())
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
