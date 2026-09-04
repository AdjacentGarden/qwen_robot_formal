#!/usr/bin/env python3
"""Persistent mapping-to-navigation mission process manager.

This node deliberately lives outside every child launch process.  Each child is
started in its own POSIX process group so a mapping shutdown cannot terminate the
manager (or unrelated hardware processes).
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

import rclpy
from action_msgs.msg import GoalStatusArray
from controller_manager_msgs.srv import ListControllers
from geometry_msgs.msg import Twist
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import Bool, String, UInt32
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener


NAV2_NODES = ('/controller_server', '/planner_server', '/bt_navigator')
NAV2_ALL_NODES = (
    '/controller_server', '/planner_server', '/behavior_server',
    '/bt_navigator', '/velocity_smoother',
    '/lifecycle_manager_navigation',
)
MAPPING_NODES = (
    '/cartographer_node',
    '/cartographer_occupancy_grid_node',
    *NAV2_ALL_NODES,
    '/frontier_explorer',
    '/save_map_on_exploration_complete',
    '/map_saver',
    '/lifecycle_manager_map_saver',
)
ODOMETRY_NODES = ('/rf2o_laser_odometry', '/ekf_filter_node')
MOTION_CONTROL_NODES = ('/motion_controller',)


class ManagedProcess:
    """A launch process with an independently signalable process group."""

    def __init__(self, name: str, command: Iterable[str], log_dir: Path):
        self.name = name
        self.command = list(command)
        self.log_dir = log_dir
        self.process: Optional[subprocess.Popen] = None
        self._log_file = None
        self.log_path: Optional[Path] = None
        self._process_groups = set()

    def start(self) -> None:
        if self.is_running():
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        self.log_path = self.log_dir / f'{self.name}-{stamp}.log'
        self._log_file = self.log_path.open('a', encoding='utf-8')
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self._process_groups = {self.process.pid}

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def returncode(self) -> Optional[int]:
        return None if self.process is None else self.process.poll()

    def stop(self, interrupt_s: float = 10.0, terminate_s: float = 5.0) -> None:
        process = self.process
        if process is None:
            return
        # Some launch actions create another process group. Capture only groups
        # descended from our launch process before its parent/child links vanish.
        self._process_groups.update(self._descendant_process_groups(process.pid))
        self._signal_groups_and_wait(signal.SIGINT, interrupt_s)
        self._signal_groups_and_wait(signal.SIGTERM, terminate_s)
        self._signal_groups_and_wait(signal.SIGKILL, 2.0)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self.process = None

    @staticmethod
    def _descendant_process_groups(root_pid: int) -> set:
        parent_by_pid = {}
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text(encoding='utf-8')
                closing_paren = stat.rfind(')')
                pid = int(stat[:stat.find(' ')])
                fields_after_name = stat[closing_paren + 2:].split()
                parent_by_pid[pid] = int(fields_after_name[1])
            except (OSError, ValueError, IndexError):
                continue
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, parent in parent_by_pid.items():
                if parent in descendants and pid not in descendants:
                    descendants.add(pid)
                    changed = True
        groups = set()
        own_group = os.getpgrp()
        for pid in descendants:
            try:
                group = os.getpgid(pid)
                if group != own_group:
                    groups.add(group)
            except ProcessLookupError:
                pass
        return groups

    @staticmethod
    def _group_exists(group: int) -> bool:
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _signal_groups_and_wait(self, sig: signal.Signals, timeout: float) -> None:
        active = {group for group in self._process_groups if self._group_exists(group)}
        for group in active:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while active and time.monotonic() < deadline:
            if self.process is not None:
                self.process.poll()  # Reap the launch group leader if it exited.
            active = {group for group in active if self._group_exists(group)}
            if active:
                time.sleep(0.05)
        self._process_groups = active


class MapValidator:
    """Validate source/install map artifacts produced by the current attempt."""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def validate(self, attempt_started_wall: float) -> Tuple[bool, str]:
        source_pb = self.workspace / 'src/robot_bringup/map/map.pbstream'
        install_pb = self.workspace / 'install/robot_bringup/share/robot_bringup/map/map.pbstream'
        source_yaml = self.workspace / 'src/car_nav2/maps/exploration_map.yaml'
        source_pgm = self.workspace / 'src/car_nav2/maps/exploration_map.pgm'
        install_yaml = self.workspace / 'install/car_nav2/share/car_nav2/maps/exploration_map.yaml'
        install_pgm = self.workspace / 'install/car_nav2/share/car_nav2/maps/exploration_map.pgm'
        limits = {
            source_pb: 10 * 1024, install_pb: 10 * 1024,
            source_yaml: 50, install_yaml: 50,
            source_pgm: 1024, install_pgm: 1024,
        }
        errors = []
        for path, minimum in limits.items():
            if not path.is_file():
                errors.append(f'missing {path}')
            elif path.stat().st_size <= minimum:
                errors.append(f'too small ({path.stat().st_size} bytes) {path}')
            elif path.stat().st_mtime + 1.0 < attempt_started_wall:
                errors.append(f'not updated by this attempt {path}')
        if errors:
            return False, '; '.join(errors)

        for left, right in ((source_pb, install_pb), (source_yaml, install_yaml),
                            (source_pgm, install_pgm)):
            if self._sha256(left) != self._sha256(right):
                errors.append(f'source/install mismatch: {left.name}')

        try:
            image_line = next(
                line for line in source_yaml.read_text(encoding='utf-8').splitlines()
                if line.strip().startswith('image:'))
            image_value = image_line.split(':', 1)[1].strip().strip('"\'')
            referenced = Path(image_value)
            if not referenced.is_absolute():
                referenced = source_yaml.parent / referenced
            if not referenced.is_file():
                errors.append(f'YAML image does not exist: {referenced}')
        except (OSError, StopIteration, ValueError) as exc:
            errors.append(f'invalid map YAML: {exc}')
        return not errors, '; '.join(errors) if errors else 'map artifacts valid'

    def validate_existing_for_navigation(self) -> Tuple[bool, str]:
        """Check the pbstream actually loaded by the installed localization launch."""
        source_pb = self.workspace / 'src/robot_bringup/map/map.pbstream'
        install_pb = (
            self.workspace /
            'install/robot_bringup/share/robot_bringup/map/map.pbstream'
        )
        minimum_size = 10 * 1024
        if not install_pb.is_file():
            return False, f'installed localization map is missing: {install_pb}'
        try:
            size = install_pb.stat().st_size
        except OSError as exc:
            return False, f'cannot inspect installed localization map {install_pb}: {exc}'
        if size <= minimum_size:
            return False, (
                f'installed localization map is too small ({size} bytes): {install_pb}'
            )

        detail = f'existing localization map found: {install_pb} ({size} bytes)'
        # Source/install mismatch does not prevent this run because localization
        # loads the installed file. Record it so a later rebuild does not silently
        # replace the working installed map with a different source artifact.
        if not source_pb.is_file():
            detail += f'; warning: source map is missing: {source_pb}'
        else:
            try:
                if self._sha256(source_pb) != self._sha256(install_pb):
                    detail += '; warning: source/install map hashes differ'
            except OSError as exc:
                detail += f'; warning: source map comparison failed: {exc}'
        return True, detail


class MappingNavigationManager(Node):
    def __init__(self) -> None:
        super().__init__('mapping_navigation_manager')
        self._declare_parameters()
        self.workspace = Path(self.get_parameter('workspace_root').value).expanduser().resolve()
        self.log_dir = self.workspace / self.get_parameter('log_directory').value
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._event_log = (self.log_dir / 'mapping_manager.log').open(
            'a', encoding='utf-8', buffering=1)

        status_qos = QoSProfile(depth=1)
        status_qos.reliability = ReliabilityPolicy.RELIABLE
        status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.state_pub = self.create_publisher(String, '/mapping_manager/state', status_qos)
        self.event_pub = self.create_publisher(String, '/mapping_manager/event', status_qos)
        self.attempt_pub = self.create_publisher(UInt32, '/mapping_manager/attempt', status_qos)
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.motion_controller_status = ''
        self.motion_controller_conflict = False
        self.motion_controller_warning = ''
        self.motion_controller_status_seen = 0.0
        self.lidar_restart_started: Optional[float] = None
        self.lidar_restart_deadline: Optional[float] = None
        self.sensor_gate_enabled: Optional[bool] = None
        self.sensor_gate_state = 'unknown'
        self.sensor_gate_stable_since: Optional[float] = None
        self.shutdown_requested = False
        self.shutdown_thread: Optional[threading.Thread] = None
        self.localization_observation_started: Optional[float] = None
        self.localization_tf_anchor: Optional[Tuple[float, float, float]] = None
        self.localization_last_tf: Optional[Tuple[float, float, float]] = None
        self.localization_last_detail = 'not observed'
        self.last_localization_diagnostic = 0.0

        # Subscribe before any child starts so the short-lived saver result cannot be missed.
        self.result_sub = self.create_subscription(
            String, '/mapping/save_result', self._on_mapping_result, 10)
        self.create_subscription(
            String, '/motion_controller/status',
            self._on_motion_controller_status, status_qos)
        self.create_subscription(
            Bool, '/motion_controller/control_conflict',
            self._on_motion_controller_conflict, status_qos)
        self.create_subscription(
            String, '/motion_controller/warning',
            self._on_motion_controller_warning, status_qos)
        self.create_subscription(
            Bool, '/motion_controller/lidar_enabled',
            self._on_sensor_gate_enabled, status_qos)
        self.create_subscription(
            String, '/motion_controller/sensor_gate_state',
            self._on_sensor_gate_state, status_qos)
        self.create_subscription(
            String, '/rplidar/quality_status',
            self._on_lidar_quality_status, 10)
        self.create_service(
            Trigger, '/mapping_manager/shutdown', self._on_shutdown_request)
        sensor_qos = QoSProfile(depth=5)
        sensor_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        self.create_subscription(LaserScan, '/scan', lambda _: self._seen('scan'), sensor_qos)
        self.create_subscription(Imu, '/imu', lambda _: self._seen('imu'), sensor_qos)
        self.create_subscription(
            LaserScan, '/scan_gated', lambda _: self._seen('scan_gated'), sensor_qos)
        self.create_subscription(
            Imu, '/imu_cartographer_gated',
            lambda _: self._seen('imu_gated'), sensor_qos)
        self.create_subscription(Odometry, '/odom', lambda _: self._seen('odom'), 10)
        self.create_subscription(
            Odometry, '/odom_rf2o', lambda _: self._seen('rf2o'), sensor_qos)
        self.create_subscription(OccupancyGrid, '/map', self._on_map, 10)
        # Keeps action status discovery warm on distributions where server graph updates lag.
        self.create_subscription(GoalStatusArray, '/navigate_to_pose/_action/status',
                                 lambda _: None, 10)

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.nav_action = ActionClient(self, NavigateToPose, '/navigate_to_pose')
        self.lifecycle_generation = 0
        self.lifecycle_clients = {
            name: self.create_client(GetState, f'{name}/get_state') for name in NAV2_NODES
        }
        self.lifecycle_states: Dict[str, int] = {}
        self.lifecycle_pending = set()
        # ros2_control exists only in simulation.  Keep the client available so the
        # same manager can supervise both environments, but never require it on hardware.
        self.controller_client = self.create_client(
            ListControllers, '/controller_manager/list_controllers')
        self.controller_active = False
        self.controller_pending = False
        self.controller_states: Dict[str, str] = {}
        self.controller_query_error = ''
        self.controller_status_seen = 0.0
        self.last_base_diagnostic = 0.0

        use_sim = str(self.get_parameter('use_sim_time').value).lower()
        self.is_sim = use_sim == 'true'
        self.controller_name = (
            str(self.get_parameter('sim_controller_name').value)
            if self.is_sim else ''
        )
        direction_reverse = '1.0' if self.is_sim else '-1.0'
        rf2o = str(self.get_parameter('use_rf2o_in_ekf').value).lower()
        rviz = str(self.get_parameter('use_rviz').value).lower()
        common = [f'use_sim_time:={use_sim}']

        if self.is_sim:
            self.BASE_NODES = (
                '/controller_manager',
                '/robot_state_publisher',
                '/gazebo',
            )
            base_launch = 'sim_robot_no_autonav.launch.py'
            base_extra_args = ['enable_odometry:=false']
        else:
            # Real hardware has no ros2_control/controller_manager.  The chassis
            # itself is validated from its serial device and /motor_speed publisher.
            self.BASE_NODES = ('/robot_state_publisher',)
            base_launch = 'real_robot_base.launch.py'
            base_extra_args = []
        self.processes = {
            'base': ManagedProcess('base', [
                'ros2', 'launch', 'robot_bringup', base_launch,
                *common, *base_extra_args,
            ], self.log_dir),
            'odometry': ManagedProcess('odometry', [
                'ros2', 'launch', 'robot_bringup', 'rf2o_ekf.launch.py',
                *common, f'use_rf2o_in_ekf:={rf2o}',
            ], self.log_dir),
            'motion_control': ManagedProcess('motion_control', [
                'ros2', 'launch', 'motion_controller', 'motion_controller.launch.py',
                *common, 'initial_mode:=navigation',
                f'direction_reverse:={direction_reverse}',
            ], self.log_dir),
            'mapping': ManagedProcess('mapping', [
                'ros2', 'launch', 'robot_bringup', 'real_robot_automap.launch.py',
                *common, f'use_rviz:={rviz}', 'enable_auto_navigation:=false',
            ], self.log_dir),
            'navigation_map': ManagedProcess('navigation_map', [
                'ros2', 'launch', 'car_nav2', 'car_nav2.launch.py',
                *common, 'autostart:=true',
                f'params_dir:={self.workspace / "install/car_nav2/share/car_nav2/param_map"}',
            ], self.log_dir),
            'saver': ManagedProcess('saver', [
                'ros2', 'launch', 'robot_bringup',
                'save_map_on_exploration_complete.launch.py', *common,
            ], self.log_dir),
            'explorer': ManagedProcess('explorer', [
                'ros2', 'launch', 'frontier_exploration_ros2',
                'frontier_explorer.launch.py', *common,
            ], self.log_dir),
            'localization': ManagedProcess('localization', [
                'ros2', 'launch', 'robot_bringup', 'real_robot_nav.launch.py',
                *common, f'use_rviz:={rviz}', 'enable_auto_navigation:=false',
                'enable_motion_controller:=false',
            ], self.log_dir),
            'navigation': ManagedProcess('navigation', [
                'ros2', 'launch', 'car_nav2', 'car_nav2.launch.py',
                *common, 'autostart:=true',
                f'params_dir:={self.workspace / "install/car_nav2/share/car_nav2/param"}',
            ], self.log_dir),
        }
        self.validator = MapValidator(self.workspace)
        self.run_full_pipeline = True
        self.last_seen: Dict[str, float] = {}
        self.state = 'BOOT'
        self.state_since = time.monotonic()
        self.stable_since: Optional[float] = None
        self.attempt = 0
        self.rebuilds = 0
        self.mapping_nav2_restarts = 0
        self.nav2_restarts = 0
        self.attempt_started_wall = 0.0
        self.pending_result: Optional[str] = None
        self.result_locked = False
        self.transition_kind = ''
        self.shutting_down = False
        self._publish_attempt()
        self._set_state('BOOT', 'Persistent mission manager started')
        # BOOT must run before Gazebo has created /clock, so drive the manager's
        # state machine from a steady wall clock even when use_sim_time is true.
        self.timer = self.create_timer(
            0.5, self._tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def _declare_parameters(self) -> None:
        prefix = Path(__file__).resolve()
        # Source run and symlink-install both normally have /workspace/src in the path.
        workspace = next((p.parent for p in prefix.parents if p.name == 'src'), Path.cwd())
        # The real map is recorded with the robot facing the negative map-X
        # direction at its docking/start pose. Simulation maps use +X.
        use_sim_time = bool(self.get_parameter('use_sim_time').value)
        defaults = {
            'workspace_root': str(workspace), 'log_directory': 'logs',
            'use_sim_time': False, 'use_rviz': False, 'use_rf2o_in_ekf': True,
            # true forces mapping; false uses an existing installed map when available.
            'init': False,
            'start_base': True, 'restart_odometry_on_transition': True,
            'base_timeout_s': 90.0, 'mapping_timeout_s': 120.0,
            'auxiliary_timeout_s': 30.0,
            'localization_timeout_s': 90.0, 'nav2_timeout_s': 120.0,
            'nav2_retry_settle_s': 5.0, 'max_nav2_restarts': 1,
            'stable_duration_s': 4.0, 'mission_timeout_s': 1800.0,
            'scan_stale_s': 2.0, 'imu_stale_s': 2.0,
            'odom_stale_s': 2.0, 'map_stale_s': 10.0,
            'lidar_restart_timeout_s': 10.0,
            # motion_controller has already enforced 3 seconds; this second,
            # independent observation keeps normal total recovery near 4 seconds.
            'sensor_gate_recovery_stable_s': 1.0,
            'sensor_gate_scan_stale_s': 0.25,
            'sensor_gate_imu_stale_s': 0.25,
            'sensor_gate_odom_stale_s': 0.5,
            'sensor_gate_tf_stale_s': 0.5,
            'base_diagnostic_period_s': 5.0,
            'startup_graph_settle_s': 2.0, 'cleanup_timeout_s': 30.0,
            'localization_observation_s': 10.0,
            'localization_start_x': 0.0, 'localization_start_y': 0.0,
            'localization_start_yaw': 0.0 if use_sim_time else math.pi,
            'localization_start_position_tolerance_m': 0.35,
            'localization_start_yaw_tolerance_deg': 15.0,
            'localization_reset_odom_position_tolerance_m': 0.30,
            'localization_reset_odom_yaw_tolerance_deg': 15.0,
            'localization_tf_stability_position_m': 0.15,
            'localization_tf_stability_yaw_deg': 5.0,
            # Low-rate localization on the real robot can legitimately apply a
            # sizeable map correction between manager samples.  Treat larger
            # corrections as diagnostics, not a mission-stopping condition.
            'localization_tf_jump_position_m': 3.0,
            'localization_tf_jump_yaw_deg': 20.0,
            'max_rebuilds': 0,
            # Simulation uses ros2_control; real hardware does not.
            'sim_controller_name': 'robot_diff_drive_controller',
            # Real chassis readiness: device path + ROS publisher presence.
            'chassis_serial_port': '/dev/ttyS0',
            'motor_speed_topic': '/motor_speed',
        }
        for name, value in defaults.items():
            # rclpy's time source declares use_sim_time during Node construction.
            if not self.has_parameter(name):
                self.declare_parameter(name, value)

    def _seen(self, topic: str) -> None:
        self.last_seen[topic] = time.monotonic()

    def _on_map(self, _msg: OccupancyGrid) -> None:
        self._seen('map')

    def _start_sensor_gate_recovery(self, reason: str) -> None:
        now = time.monotonic()
        timeout = max(
            15.0, float(self.get_parameter('lidar_restart_timeout_s').value))
        self.lidar_restart_started = now
        self.lidar_restart_deadline = now + timeout
        self.sensor_gate_stable_since = None
        self._publish_zero_velocity()
        self._log_event(f'SENSOR_GATE_RECOVERING: {reason}; timeout={timeout:.1f}s')

    def _on_sensor_gate_enabled(self, msg: Bool) -> None:
        enabled = bool(msg.data)
        previous = self.sensor_gate_enabled
        self.sensor_gate_enabled = enabled
        if not enabled:
            self.lidar_restart_started = None
            self.lidar_restart_deadline = None
            self.sensor_gate_stable_since = None
            self._publish_zero_velocity()
            self._log_event('SENSOR_GATE_DISABLED: mission paused and velocity forced to zero')
        elif previous is not True:
            self._start_sensor_gate_recovery('gate enabled')

    def _on_sensor_gate_state(self, msg: String) -> None:
        state = msg.data.strip().lower()
        if not state or state == self.sensor_gate_state:
            return
        previous = self.sensor_gate_state
        self.sensor_gate_state = state
        self._log_event(f'SENSOR_GATE_STATE: {previous} -> {state}')
        if state == 'recovering' and self.lidar_restart_started is None:
            self._start_sensor_gate_recovery('controller reported recovering')
        elif state in ('disabled', 'stopping'):
            self.sensor_gate_stable_since = None

    def _on_lidar_quality_status(self, msg: String) -> None:
        status = msg.data.strip()
        if status.startswith('RESTARTING'):
            self._start_sensor_gate_recovery(f'lidar driver {status}')
        elif status.startswith('RECOVERED'):
            self._log_event(f'LIDAR_DRIVER_RECOVERY_NOTICE: {status}')

    def _on_shutdown_request(self, _request: Trigger.Request,
                             response: Trigger.Response) -> Trigger.Response:
        if not self.shutdown_requested:
            self.shutdown_requested = True
            self._log_event('External shutdown accepted; stopping complete mission')
            # Never call rclpy.shutdown() from an executor callback: waiting for
            # the executor from its own thread can deadlock after all children
            # have already stopped. A worker lets this callback return its
            # service response before cleanup and context shutdown begin.
            self.shutdown_thread = threading.Thread(
                target=self._finish_external_shutdown,
                name='mapping-manager-shutdown', daemon=True)
            self.shutdown_thread.start()
        response.success = True
        response.message = 'Manager shutdown accepted'
        return response

    def _finish_external_shutdown(self) -> None:
        time.sleep(0.1)
        self.stop_all()
        if rclpy.ok(context=self.context):
            rclpy.shutdown(context=self.context)

    def _handle_lidar_restart(self) -> bool:
        """Pause until the controller and independently observed inputs are stable."""
        cartographer_active_states = {
            'WAIT_MAPPING_READY', 'WAIT_SAVER', 'WAIT_EXPLORER', 'MAPPING',
            'WAIT_LOCALIZATION_READY', 'WAIT_NAVIGATION_READY',
            'WAIT_NAVIGATION_STOPPED', 'NAVIGATION',
        }
        if self.state not in cartographer_active_states:
            # Do not wait for /map before the manager has launched Cartographer.
            self.lidar_restart_started = None
            self.lidar_restart_deadline = None
            self.sensor_gate_stable_since = None
            return False
        if self.sensor_gate_enabled is False:
            self._publish_zero_velocity()
            return True
        if (self.sensor_gate_enabled is True
                and self.sensor_gate_state != 'ready'
                and self.lidar_restart_started is None):
            self._start_sensor_gate_recovery(
                f'active mission observed gate state {self.sensor_gate_state}')
        if self.lidar_restart_deadline is None or self.lidar_restart_started is None:
            return False

        self._publish_zero_velocity()
        now = time.monotonic()
        scan_limit = float(self.get_parameter('sensor_gate_scan_stale_s').value)
        imu_limit = float(self.get_parameter('sensor_gate_imu_stale_s').value)
        odom_limit = float(self.get_parameter('sensor_gate_odom_stale_s').value)
        map_seen_after_restart = self.last_seen.get('map', 0.0) > self.lidar_restart_started
        locally_ready = (
            self.sensor_gate_state == 'ready'
            and self._fresh('scan_gated', scan_limit)
            and self._fresh('imu_gated', imu_limit)
            and self._fresh('rf2o', odom_limit)
            and self._fresh('odom', odom_limit)
            and map_seen_after_restart
            and self._tf_fresh('map', 'base_footprint',
                               float(self.get_parameter('sensor_gate_tf_stale_s').value))
        )
        if locally_ready:
            if self.sensor_gate_stable_since is None:
                self.sensor_gate_stable_since = now
        else:
            self.sensor_gate_stable_since = None
        stable_s = float(self.get_parameter('sensor_gate_recovery_stable_s').value)
        if (self.sensor_gate_stable_since is not None
                and now - self.sensor_gate_stable_since >= stable_s):
            elapsed = time.monotonic() - self.lidar_restart_started
            self._log_event(
                f'SENSOR_GATE_RECOVERED: controller ready and scan/RF2O/odom/TF/map '
                f'stable for {stable_s:.1f}s after {elapsed:.2f}s')
            self.lidar_restart_started = None
            self.lidar_restart_deadline = None
            self.sensor_gate_stable_since = None
            return False

        if now >= self.lidar_restart_deadline:
            timeout = now - self.lidar_restart_started
            self.lidar_restart_started = None
            self.lidar_restart_deadline = None
            self._safe_stop(
                f'SENSOR_GATE_RECOVERY_TIMEOUT after {timeout:.1f}s: '
                f'state={self.sensor_gate_state}, '
                f'scan_age={self._age_detail("scan_gated")}, '
                f'imu_age={self._age_detail("imu_gated")}, '
                f'rf2o_age={self._age_detail("rf2o")}, '
                f'odom_age={self._age_detail("odom")}, '
                f'map_after_restart={map_seen_after_restart}')
            return True

        return True

    def _on_motion_controller_status(self, msg: String) -> None:
        status = msg.data.strip()
        self.motion_controller_status_seen = time.monotonic()
        if not status or status == self.motion_controller_status:
            return
        previous = self.motion_controller_status or 'unknown'
        self.motion_controller_status = status
        self._log_event(
            f'motion_controller feedback: {previous} -> {status}; '
            f'conflict={self.motion_controller_conflict}')

    def _on_motion_controller_conflict(self, msg: Bool) -> None:
        conflict = bool(msg.data)
        if conflict == self.motion_controller_conflict:
            return
        self.motion_controller_conflict = conflict
        if conflict:
            self._log_event('motion_controller feedback: command request conflict detected')
        else:
            self._log_event('motion_controller feedback: command-source conflict cleared')

    def _on_motion_controller_warning(self, msg: String) -> None:
        warning = msg.data.strip()
        if warning == self.motion_controller_warning:
            return
        self.motion_controller_warning = warning
        if warning:
            text = f'motion_controller WARN: {warning}'
            self.get_logger().warning(text)
            self._log_event(text)

    def _publish_attempt(self) -> None:
        msg = UInt32()
        msg.data = self.attempt
        self.attempt_pub.publish(msg)

    def _log_event(self, text: str) -> None:
        line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {self.state}: {text}'
        if not self._event_log.closed:
            self._event_log.write(line + '\n')
        # SIGINT may invalidate the rclpy context before spin raises. Logging and
        # publishing must never prevent process cleanup in that situation.
        if rclpy.ok(context=self.context):
            try:
                msg = String()
                msg.data = text
                self.event_pub.publish(msg)
                self.get_logger().info(text)
            except Exception:
                pass

    def _set_state(self, state: str, event: str = '') -> None:
        self.state = state
        self.state_since = time.monotonic()
        self.stable_since = None
        if state == 'WAIT_LOCALIZATION_READY':
            self._reset_localization_gate()
        msg = String()
        msg.data = state
        self.state_pub.publish(msg)
        if event:
            self._log_event(event)

    def _timed_out(self, parameter: str) -> bool:
        return time.monotonic() - self.state_since > float(self.get_parameter(parameter).value)

    def _node_names(self) -> set:
        return set(self._node_name_counts())

    def _node_name_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for name, namespace in self.get_node_names_and_namespaces():
            full_name = (namespace.rstrip('/') + '/' + name).replace('//', '/')
            counts[full_name] = counts.get(full_name, 0) + 1
        return counts

    def _tf_ready(self, parent: str, child: str) -> bool:
        try:
            return self.tf_buffer.can_transform(
                parent, child, rclpy.time.Time(), timeout=Duration(seconds=0.0))
        except Exception:
            return False

    def _tf_fresh(self, parent: str, child: str, max_age_s: float) -> bool:
        try:
            transform = self.tf_buffer.lookup_transform(
                parent, child, rclpy.time.Time(), timeout=Duration(seconds=0.0))
            stamp_ns = (
                int(transform.header.stamp.sec) * 1_000_000_000
                + int(transform.header.stamp.nanosec))
            if stamp_ns <= 0:
                return False
            age_s = max(0.0, (self.get_clock().now().nanoseconds - stamp_ns) / 1e9)
            return age_s <= max_age_s
        except Exception:
            return False

    @staticmethod
    def _angle_error(a: float, b: float) -> float:
        return abs(math.atan2(math.sin(a - b), math.cos(a - b)))

    def _tf_pose(self, parent: str, child: str) -> Optional[Tuple[float, float, float]]:
        try:
            transform = self.tf_buffer.lookup_transform(
                parent, child, rclpy.time.Time(), timeout=Duration(seconds=0.0))
        except Exception:
            return None
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
        )
        return translation.x, translation.y, yaw

    def _reset_localization_gate(self) -> None:
        self.localization_observation_started = None
        self.localization_tf_anchor = None
        self.localization_last_tf = None
        self.localization_last_detail = 'waiting for localization transforms'

    def _log_localization_readiness_if_due(self) -> None:
        now = time.monotonic()
        period = max(
            0.5, float(self.get_parameter('base_diagnostic_period_s').value))
        if now - self.last_localization_diagnostic >= period:
            self.last_localization_diagnostic = now
            self._log_event(
                f'Localization readiness pending: {self.localization_last_detail}')

    def _localization_gate_ready(self) -> bool:
        """Validate localization at the mandatory stationary mapping start pose."""
        if not self._localization_ready():
            self._reset_localization_gate()
            return False
        map_base = self._tf_pose('map', 'base_footprint')
        odom_base = self._tf_pose('odom', 'base_footprint')
        map_odom = self._tf_pose('map', 'odom')
        if map_base is None or odom_base is None or map_odom is None:
            self._reset_localization_gate()
            return False

        expected = (
            float(self.get_parameter('localization_start_x').value),
            float(self.get_parameter('localization_start_y').value),
            float(self.get_parameter('localization_start_yaw').value),
        )
        start_position_error = math.hypot(
            map_base[0] - expected[0], map_base[1] - expected[1])
        start_yaw_error = self._angle_error(map_base[2], expected[2])
        odom_position_error = math.hypot(odom_base[0], odom_base[1])
        odom_yaw_error = self._angle_error(odom_base[2], 0.0)
        start_ok = (
            start_position_error <= float(self.get_parameter(
                'localization_start_position_tolerance_m').value)
            and math.degrees(start_yaw_error) <= float(self.get_parameter(
                'localization_start_yaw_tolerance_deg').value)
        )
        odom_ok = (
            odom_position_error <= float(self.get_parameter(
                'localization_reset_odom_position_tolerance_m').value)
            and math.degrees(odom_yaw_error) <= float(self.get_parameter(
                'localization_reset_odom_yaw_tolerance_deg').value)
        )

        now = time.monotonic()
        if not start_ok or not odom_ok:
            self.localization_observation_started = None
            self.localization_tf_anchor = None
            stable_position = float('inf')
            stable_yaw = float('inf')
        else:
            if self.localization_tf_anchor is None:
                self.localization_tf_anchor = map_odom
                self.localization_observation_started = now
            stable_position = math.hypot(
                map_odom[0] - self.localization_tf_anchor[0],
                map_odom[1] - self.localization_tf_anchor[1])
            stable_yaw = self._angle_error(map_odom[2], self.localization_tf_anchor[2])
            if (stable_position > float(self.get_parameter(
                    'localization_tf_stability_position_m').value)
                    or math.degrees(stable_yaw) > float(self.get_parameter(
                        'localization_tf_stability_yaw_deg').value)):
                self.localization_tf_anchor = map_odom
                self.localization_observation_started = now

        observed = 0.0 if self.localization_observation_started is None else (
            now - self.localization_observation_started)
        self.localization_last_tf = map_odom
        self.localization_last_detail = (
            f'start_error={start_position_error:.3f}m/'
            f'{math.degrees(start_yaw_error):.1f}deg, '
            f'odom_reset_error={odom_position_error:.3f}m/'
            f'{math.degrees(odom_yaw_error):.1f}deg, '
            f'map_to_odom_stability={stable_position:.3f}m/'
            f'{math.degrees(stable_yaw):.1f}deg, observed={observed:.1f}s/'
            f'{float(self.get_parameter("localization_observation_s").value):.1f}s')
        return start_ok and odom_ok and observed >= float(
            self.get_parameter('localization_observation_s').value)

    def _localization_tf_jump(self) -> Optional[str]:
        current = self._tf_pose('map', 'odom')
        previous = self.localization_last_tf
        if current is None:
            # Topic/TF freshness watchdogs handle sustained loss. Do not stop a
            # moving robot because one non-blocking lookup raced a TF update.
            return None
        self.localization_last_tf = current
        if previous is None:
            return None
        position = math.hypot(current[0] - previous[0], current[1] - previous[1])
        yaw = math.degrees(self._angle_error(current[2], previous[2]))
        if (position > float(self.get_parameter(
                'localization_tf_jump_position_m').value)
                or yaw > float(self.get_parameter(
                    'localization_tf_jump_yaw_deg').value)):
            return f'map->odom jumped {position:.3f}m/{yaw:.1f}deg'
        return None

    def _fresh(self, topic: str, max_age: float) -> bool:
        return time.monotonic() - self.last_seen.get(topic, 0.0) <= max_age

    def _request_controller_state(self) -> None:
        # The real chassis is a serial driver, not a ros2_control controller.
        if not self.is_sim:
            self.controller_active = True
            self.controller_pending = False
            self.controller_states = {}
            self.controller_query_error = ''
            return
        if self.controller_pending or not self.controller_client.service_is_ready():
            return
        self.controller_pending = True
        future = self.controller_client.call_async(ListControllers.Request())
        future.add_done_callback(self._controller_response)

    def _controller_response(self, future) -> None:
        self.controller_pending = False
        try:
            name = self.controller_name
            controllers = future.result().controller
            self.controller_states = {
                controller.name: controller.state for controller in controllers
            }
            self.controller_active = any(
                controller.name == name and controller.state == 'active'
                for controller in controllers)
            self.controller_query_error = ''
            self.controller_status_seen = time.monotonic()
        except Exception as exc:
            self.controller_active = False
            self.controller_states = {}
            self.controller_query_error = str(exc)
            self.get_logger().warning(f'Controller state query failed: {exc}')

    def _request_lifecycle_states(self) -> None:
        generation = self.lifecycle_generation
        for name, client in self.lifecycle_clients.items():
            if name in self.lifecycle_pending or not client.service_is_ready():
                continue
            self.lifecycle_pending.add(name)
            future = client.call_async(GetState.Request())
            future.add_done_callback(
                lambda fut, node_name=name, request_generation=generation:
                self._lifecycle_response(node_name, fut, request_generation))

    def _lifecycle_response(self, name: str, future, generation: int) -> None:
        if generation != self.lifecycle_generation:
            return
        self.lifecycle_pending.discard(name)
        try:
            self.lifecycle_states[name] = future.result().current_state.id
        except Exception:
            self.lifecycle_states.pop(name, None)

    def _reset_lifecycle_observation(self) -> None:
        # Service clients created for the mapping Nav2 can retain old DDS
        # endpoints after those same-named nodes are restarted. Recreate the
        # clients and ignore late callbacks from the previous generation.
        self.lifecycle_generation += 1
        for client in self.lifecycle_clients.values():
            try:
                self.destroy_client(client)
            except Exception:
                pass
        self.lifecycle_clients = {
            name: self.create_client(GetState, f'{name}/get_state') for name in NAV2_NODES
        }
        self.lifecycle_states.clear()
        self.lifecycle_pending.clear()

    def _real_chassis_ready(self) -> bool:
        """Check the serial chassis driver without depending on ros2_control.

        /motor_speed publisher existence is used instead of message freshness because a
        stationary chassis may legitimately publish zero slowly or only on updates.
        """
        serial_port = Path(str(self.get_parameter('chassis_serial_port').value))
        motor_speed_topic = str(self.get_parameter('motor_speed_topic').value)
        try:
            motor_speed_publisher = bool(
                self.get_publishers_info_by_topic(motor_speed_topic))
        except Exception:
            motor_speed_publisher = False
        return serial_port.exists() and motor_speed_publisher

    def _base_ready(self) -> bool:
        self._request_controller_state()
        nodes = self._node_names()

        common_ready = (
            self._fresh('scan', float(self.get_parameter('scan_stale_s').value))
            and self._fresh('imu', float(self.get_parameter('imu_stale_s').value))
            and self._fresh('odom', float(self.get_parameter('odom_stale_s').value))
            and self._tf_ready('odom', 'base_footprint')
            and self._tf_ready('base_footprint', 'laser_link')
            and '/rf2o_laser_odometry' in nodes
            and '/ekf_filter_node' in nodes
            and '/motion_controller' in nodes
            and bool(self.motion_controller_status)
            and not self.motion_controller_conflict
        )

        if self.is_sim:
            return (
                common_ready
                and '/controller_manager' in nodes
                and self.controller_active
            )

        return (
            common_ready
            and '/ros_robot_controller' in nodes
            and self._real_chassis_ready()
        )

    def _age_detail(self, topic: str) -> str:
        seen = self.last_seen.get(topic)
        if seen is None:
            return 'never'
        return f'{time.monotonic() - seen:.2f}s'

    def _base_readiness_detail(self) -> str:
        """Explain every WAIT_BASE prerequisite, including child log locations."""
        self._request_controller_state()
        nodes = self._node_names()
        scan_limit = float(self.get_parameter('scan_stale_s').value)
        imu_limit = float(self.get_parameter('imu_stale_s').value)
        odom_limit = float(self.get_parameter('odom_stale_s').value)

        checks = {
            f'scan_fresh(<={scan_limit:.1f}s)': self._fresh('scan', scan_limit),
            f'imu_fresh(<={imu_limit:.1f}s)': self._fresh('imu', imu_limit),
            f'odom_fresh(<={odom_limit:.1f}s)': self._fresh('odom', odom_limit),
            'tf_odom_to_base_footprint': self._tf_ready('odom', 'base_footprint'),
            'tf_base_footprint_to_laser_link': self._tf_ready(
                'base_footprint', 'laser_link'),
            'node_rf2o_laser_odometry': '/rf2o_laser_odometry' in nodes,
            'node_ekf_filter_node': '/ekf_filter_node' in nodes,
            'node_motion_controller': '/motion_controller' in nodes,
            'motion_status_received': bool(self.motion_controller_status),
            'motion_no_conflict': not self.motion_controller_conflict,
        }

        if self.is_sim:
            controller_service = self.controller_client.service_is_ready()
            checks['controller_service_ready'] = controller_service
            checks[f'controller_{self.controller_name}_active'] = self.controller_active
            controller_detail = (
                f'mode=sim,service_ready={controller_service},'
                f'pending={self.controller_pending},'
                f'name={self.controller_name},'
                f'states={self.controller_states or "none"}'
            )
            if self.controller_query_error:
                controller_detail += f',error={self.controller_query_error}'
        else:
            serial_port = Path(str(self.get_parameter('chassis_serial_port').value))
            motor_speed_topic = str(self.get_parameter('motor_speed_topic').value)
            try:
                motor_speed_publishers = len(
                    self.get_publishers_info_by_topic(motor_speed_topic))
            except Exception:
                motor_speed_publishers = 0
            checks[f'chassis_serial_{serial_port}'] = serial_port.exists()
            checks[f'motor_speed_publisher_{motor_speed_topic}'] = (
                motor_speed_publishers > 0)
            controller_detail = (
                f'mode=real,ros2_control=not_required,'
                f'chassis_serial={serial_port},'
                f'serial_exists={serial_port.exists()},'
                f'motor_speed_topic={motor_speed_topic},'
                f'motor_speed_publishers={motor_speed_publishers}'
            )

        missing = [name for name, ready in checks.items() if not ready]
        stable_for = 0.0 if self.stable_since is None else (
            time.monotonic() - self.stable_since)
        process_details = []
        watched = ['odometry', 'motion_control']
        if self.get_parameter('start_base').value:
            watched.append('base')
        for name in watched:
            managed = self.processes[name]
            if managed.is_running():
                state = f'running(pid={managed.process.pid})'
            elif managed.process is None:
                state = 'not_started'
            else:
                state = f'exited(rc={managed.returncode()})'
            if managed.log_path is not None:
                state += f',log={managed.log_path}'
            process_details.append(f'{name}={state}')

        return (
            f'missing={missing or "none"}; scan_age={self._age_detail("scan")}; '
            f'imu_age={self._age_detail("imu")}; '
            f'odom_age={self._age_detail("odom")}; '
            f'motion_status={self.motion_controller_status or "never"}; '
            f'motion_conflict={self.motion_controller_conflict}; '
            f'base_interface=[{controller_detail}]; stable_for={stable_for:.1f}s; '
            f'processes=[{"; ".join(process_details)}]'
        )

    def _log_base_readiness_if_due(self, force: bool = False) -> str:
        detail = self._base_readiness_detail()
        now = time.monotonic()
        period = max(
            0.5, float(self.get_parameter('base_diagnostic_period_s').value))
        if force or now - self.last_base_diagnostic >= period:
            self.last_base_diagnostic = now
            self._log_event(f'Base readiness pending: {detail}')
        return detail

    def _motion_navigation_ready(self) -> bool:
        return (
            '/motion_controller' in self._node_names()
            and self.motion_controller_status in ('idle', 'active_navigation')
            and not self.motion_controller_conflict
        )

    def _mapping_ready(self) -> bool:
        self._request_lifecycle_states()
        return (
            self._mapping_slam_ready()
            and self._nav2_lifecycle_ready()
            and self.nav_action.server_is_ready()
            and self._motion_navigation_ready()
        )

    def _mapping_slam_ready(self) -> bool:
        nodes = self._node_names()
        services = {name for name, _ in self.get_service_names_and_types()}
        return (
            '/cartographer_node' in nodes
            and '/cartographer_occupancy_grid_node' in nodes
            and all(service in services for service in
                    ('/finish_trajectory', '/write_state', '/read_metrics'))
            and self._fresh('map', float(self.get_parameter('map_stale_s').value))
            and self._tf_ready('map', 'odom')
        )

    def _localization_ready(self) -> bool:
        nodes = self._node_names()
        return (
            '/cartographer_node' in nodes
            and '/cartographer_occupancy_grid_node' in nodes
            and self._fresh('map', float(self.get_parameter('map_stale_s').value))
            and self._tf_ready('map', 'odom')
        )

    def _navigation_ready(self) -> bool:
        self._request_lifecycle_states()
        return (
            self._localization_ready()
            and self._nav2_lifecycle_ready()
            and self.nav_action.server_is_ready()
            and self._motion_navigation_ready()
        )

    def _nav2_lifecycle_ready(self) -> bool:
        """Tolerate a dropped bt_navigator get_state reply on slow DDS hosts.

        An available NavigateToPose action server is stronger evidence that
        bt_navigator activated successfully than a repeated lifecycle query.
        Controller and planner must still explicitly report ACTIVE.
        """
        return (
            self.lifecycle_states.get('/controller_server') == 3
            and self.lifecycle_states.get('/planner_server') == 3
            and (
                self.lifecycle_states.get('/bt_navigator') == 3
                or self.nav_action.server_is_ready()
            )
        )

    def _navigation_readiness_detail(self) -> str:
        nodes = self._node_names()
        lifecycle = {
            name: self.lifecycle_states.get(name, 'unknown') for name in NAV2_NODES
        }
        return (
            f'cartographer={"/cartographer_node" in nodes}, '
            f'occupancy_grid={"/cartographer_occupancy_grid_node" in nodes}, '
            f'map_fresh={self._fresh("map", float(self.get_parameter("map_stale_s").value))}, '
            f'map_to_odom={self._tf_ready("map", "odom")}, '
            f'lifecycle={lifecycle}, action={self.nav_action.server_is_ready()}, '
            f'motion_node={"/motion_controller" in nodes}, '
            f'motion_status={self.motion_controller_status or "unknown"}, '
            f'motion_status_age_s='
            f'{time.monotonic() - self.motion_controller_status_seen:.2f}, '
            f'motion_conflict={self.motion_controller_conflict}'
        )

    def _stable(self, condition: bool) -> bool:
        if not condition:
            self.stable_since = None
            return False
        if self.stable_since is None:
            self.stable_since = time.monotonic()
        return time.monotonic() - self.stable_since >= float(
            self.get_parameter('stable_duration_s').value)

    def _start_mapping_attempt(self) -> None:
        self.attempt += 1
        self.mapping_nav2_restarts = 0
        self.attempt_started_wall = time.time()
        self.pending_result = None
        self.result_locked = False
        self._reset_lifecycle_observation()
        self._publish_attempt()
        self.processes['mapping'].start()
        self._set_state('WAIT_MAPPING_READY', f'Mapping attempt {self.attempt} started')

    def _on_mapping_result(self, msg: String) -> None:
        if self.result_locked or self.state != 'MAPPING':
            return
        value = msg.data.strip()
        if not value.startswith(('MAPPING_SUCCESS', 'MAPPING_REBUILD_REQUIRED',
                                 'MAPPING_FAILED')):
            return
        self.result_locked = True
        self.pending_result = value
        self._log_event(f'Mapping result locked: {value}')

    def _stop_mapping_group(self) -> None:
        for name in ('explorer', 'saver', 'navigation_map', 'mapping'):
            self.processes[name].stop()
        self._reset_lifecycle_observation()

    def _safe_stop(self, reason: str) -> None:
        self._log_event(f'SAFE_STOP: {reason}')
        self._publish_zero_velocity()
        # SAFE_STOP is a full mission shutdown. Leaving the base or odometry
        # alive makes the next manager run collide with stale RF2O/EKF nodes.
        self._stop_owned_processes()
        self._publish_zero_velocity()
        self._set_state('SAFE_STOP')

    def _publish_zero_velocity(self) -> None:
        if not rclpy.ok(context=self.context):
            return
        try:
            self.cmd_vel_pub.publish(Twist())
        except Exception:
            pass

    def _stop_owned_processes(self) -> None:
        for name in ('explorer', 'saver', 'navigation_map', 'mapping', 'navigation', 'localization',
                     'motion_control', 'odometry', 'base'):
            try:
                self.processes[name].stop()
            except Exception as exc:
                # Continue with every remaining group even if one child is in a
                # broken process state.
                if not self._event_log.closed:
                    self._event_log.write(
                        f'{time.strftime("%Y-%m-%d %H:%M:%S")} cleanup: '
                        f'failed stopping {name}: {exc}\n')

    def _child_failed(self, name: str) -> bool:
        process = self.processes[name]
        return process.process is not None and not process.is_running()

    def _failed_children(self, names: Iterable[str]) -> Dict[str, Optional[int]]:
        return {
            name: self.processes[name].returncode()
            for name in names if self._child_failed(name)
        }

    def _tick(self) -> None:
        if self.shutdown_requested or self.shutting_down or self.state == 'SAFE_STOP':
            return
        try:
            self._tick_state()
        except Exception as exc:
            self.get_logger().error(f'Manager state machine exception: {exc}')
            self._safe_stop(f'internal manager error: {exc}')

    def _tick_state(self) -> None:
        if self._handle_lidar_restart():
            return

        if self.state == 'BOOT':
            if time.monotonic() - self.state_since < float(
                    self.get_parameter('startup_graph_settle_s').value):
                return
            counts = self._node_name_counts()
            # Refuse to stack a newly managed RF2O/EKF instance on top of an
            # odometry node that was already present before manager startup.
            # Once startup succeeds, duplicate graph names are not a SAFE_STOP
            # condition because DDS discovery can briefly retain stale entries.
            conflicts = set(ODOMETRY_NODES) | set(MAPPING_NODES)
            conflicts |= set(MOTION_CONTROL_NODES)

            if self.get_parameter('start_base').value:
                conflicts |= set(self.BASE_NODES)
            present = sorted(name for name in conflicts if counts.get(name, 0) > 0)
            if counts.get('/mapping_navigation_manager', 0) > 1:
                present.append('/mapping_navigation_manager (duplicate manager)')
            if present:
                self._safe_stop(
                    'startup refused because unmanaged/conflicting nodes already exist: '
                    f'{present}. Stop the old launch first; for an externally managed base, '
                    'ensure it does not start RF2O/EKF and run this manager with '
                    'start_base:=false.')
                return

            force_init = bool(self.get_parameter('init').value)
            if force_init:
                self.run_full_pipeline = True
                self._log_event(
                    'init=true: forcing the complete mapping-to-navigation pipeline')
            else:
                map_valid, map_detail = self.validator.validate_existing_for_navigation()
                if map_valid:
                    self.run_full_pipeline = False
                    self._log_event(
                        f'init=false: {map_detail}; skipping mapping and starting '
                        'localization/navigation directly')
                else:
                    self.run_full_pipeline = True
                    warning = (
                        f'init=false but no usable installed map is available: {map_detail}; '
                        'falling back to the complete mapping-to-navigation pipeline')
                    self.get_logger().warning(warning)
                    self._log_event(warning)
            if self.get_parameter('start_base').value:
                self.processes['base'].start()
            self.processes['odometry'].start()
            self.processes['motion_control'].start()
            self._set_state(
                'WAIT_BASE',
                'Waiting for scan, odom, TF, base interface, RF2O, EKF and '
                'motion_controller feedback')
            return

        if self.state in ('WAIT_BASE', 'WAIT_ODOMETRY_RESTART'):
            watched = ['odometry', 'motion_control']
            if self.get_parameter('start_base').value:
                watched.append('base')
            failed = self._failed_children(watched)
            if failed:
                self._safe_stop(
                    f'process exited during startup: {failed}; '
                    f'check the newest files in {self.log_dir}')
            elif self._stable(self._base_ready()):
                if self.state == 'WAIT_ODOMETRY_RESTART':
                    self.last_seen.pop('map', None)
                    self.processes['localization'].start()
                    self._reset_lifecycle_observation()
                    self._set_state('WAIT_LOCALIZATION_READY',
                                    'RF2O/EKF restarted; Cartographer localization launched')
                else:
                    if self.run_full_pipeline:
                        self._start_mapping_attempt()
                    else:
                        self.last_seen.pop('map', None)
                        self.processes['localization'].start()
                        self._reset_lifecycle_observation()
                        self._set_state(
                            'WAIT_LOCALIZATION_READY',
                            'Base ready; existing map selected; Cartographer localization '
                            'launched without mapping; '
                            f'motion_controller={self.motion_controller_status}, '
                            f'conflict={self.motion_controller_conflict}')
            else:
                detail = self._log_base_readiness_if_due()
                if self._timed_out('base_timeout_s'):
                    self._safe_stop(f'base readiness timeout: {detail}')
            return

        if self.state == 'WAIT_MAPPING_READY':
            if self._child_failed('mapping'):
                self._safe_stop('mapping launch exited before ready')
            elif self._stable(self._mapping_slam_ready()):
                self._reset_lifecycle_observation()
                self.processes['navigation_map'].start()
                self._set_state(
                    'WAIT_MAPPING_NAVIGATION_READY',
                    'Cartographer mapping ready; mapping-mode Nav2 launched')
            elif self._timed_out('mapping_timeout_s'):
                self._safe_stop('Cartographer mapping readiness timeout')
            return

        if self.state == 'WAIT_MAPPING_NAVIGATION_READY':
            if self._child_failed('mapping'):
                self._safe_stop('Cartographer mapping launch exited before Nav2 was ready')
            elif self._child_failed('navigation_map'):
                self._retry_or_stop_mapping_nav2(
                    'Mapping-mode Nav2 launch exited before ready')
            elif self._stable(self._mapping_ready()):
                self.processes['saver'].start()
                self._set_state(
                    'WAIT_SAVER',
                    'Mapping-mode Nav2 ready; save-result subscriber active; saver launched')
            elif self._timed_out('mapping_timeout_s'):
                self._retry_or_stop_mapping_nav2(
                    'Mapping-mode Nav2 readiness timeout: '
                    f'{self._navigation_readiness_detail()}')
            return

        if self.state == 'WAIT_MAPPING_NAVIGATION_STOPPED':
            remaining = set(NAV2_ALL_NODES) & self._node_names()
            action_present = self.nav_action.server_is_ready()
            cmd_vel_publishers = bool(
                self.get_publishers_info_by_topic('/cmd_vel_nav'))
            if not remaining and not action_present and not cmd_vel_publishers:
                if time.monotonic() - self.state_since >= float(
                        self.get_parameter('nav2_retry_settle_s').value):
                    self._reset_lifecycle_observation()
                    self.processes['navigation_map'].start()
                    self._set_state(
                        'WAIT_MAPPING_NAVIGATION_READY',
                        'Restarting mapping-mode Nav2 '
                        f'(retry {self.mapping_nav2_restarts})')
            elif self._timed_out('cleanup_timeout_s'):
                self._safe_stop(
                    'Mapping-mode Nav2 retry cleanup timeout: '
                    f'nodes={sorted(remaining)}, action={action_present}, '
                    f'cmd_vel_publishers={cmd_vel_publishers}')
            return

        if self.state == 'WAIT_SAVER':
            nodes = self._node_names()
            services = {name for name, _ in self.get_service_names_and_types()}
            if ('/save_map_on_exploration_complete' in nodes
                    and '/map_saver' in nodes
                    and '/map_saver/save_map' in services):
                self.processes['explorer'].start()
                self._set_state(
                    'WAIT_EXPLORER',
                    'Persistent Cartographer/map save clients ready; frontier explorer launched')
            elif self._child_failed('saver'):
                self._safe_stop('save process exited before ready')
            elif self._timed_out('auxiliary_timeout_s'):
                self._safe_stop('save node readiness timeout')
            return

        if self.state == 'WAIT_EXPLORER':
            services = {name for name, _ in self.get_service_names_and_types()}
            if ('/frontier_explorer' in self._node_names()
                    and '/control_exploration' in services
                    and self.nav_action.server_is_ready()):
                self._set_state('MAPPING', f'Mapping attempt {self.attempt} is active')
            elif self._child_failed('explorer'):
                self._safe_stop('frontier explorer exited before ready')
            elif self._timed_out('auxiliary_timeout_s'):
                self._safe_stop('frontier explorer readiness timeout')
            return

        if self.state == 'MAPPING':
            if self.pending_result:
                if self.pending_result.startswith('MAPPING_SUCCESS'):
                    self._set_state('VALIDATING_MAP', 'Validating saved map artifacts')
                elif self.pending_result.startswith('MAPPING_REBUILD_REQUIRED'):
                    if self.rebuilds < int(self.get_parameter('max_rebuilds').value):
                        self.rebuilds += 1
                        self._stop_mapping_group()
                        self.transition_kind = 'rebuild'
                        self._set_state(
                            'WAIT_MAPPING_STOPPED',
                            f'Preparing clean rebuild {self.rebuilds}; waiting for old graph cleanup',
                        )
                    else:
                        self._safe_stop('map quality insufficient; rebuild limit reached')
                else:
                    self._safe_stop(self.pending_result)
            elif (self._child_failed('mapping')
                  or self._child_failed('navigation_map')
                  or self._child_failed('explorer')
                  or self._child_failed('motion_control')):
                self._safe_stop(
                    'mapping, mapping-mode Nav2, explorer, or motion_controller '
                    'process exited unexpectedly')
            elif self._child_failed('saver'):
                self._safe_stop('save node exited without publishing a final result')
            elif time.time() - self.attempt_started_wall > float(
                    self.get_parameter('mission_timeout_s').value):
                self._safe_stop('mapping mission timeout')
            elif not self._fresh('scan', float(self.get_parameter('scan_stale_s').value)):
                self._safe_stop('/scan watchdog timeout')
            elif not self._fresh('odom', float(self.get_parameter('odom_stale_s').value)):
                self._safe_stop('/odom watchdog timeout')
            return

        if self.state == 'VALIDATING_MAP':
            valid, detail = self.validator.validate(self.attempt_started_wall)
            if not valid:
                self._safe_stop(f'map validation failed: {detail}')
                return
            self._log_event(detail)
            self._stop_mapping_group()
            self.transition_kind = 'localize'
            self._set_state('WAIT_MAPPING_STOPPED', 'Mapping group stopped; checking graph cleanup')
            return

        if self.state == 'WAIT_MAPPING_STOPPED':
            remaining = set(MAPPING_NODES) & self._node_names()
            old_action_present = self.nav_action.server_is_ready()
            old_cmd_vel_publishers = bool(self.get_publishers_info_by_topic('/cmd_vel_nav'))
            if not remaining and not old_action_present and not old_cmd_vel_publishers:
                if self.transition_kind == 'rebuild':
                    self._start_mapping_attempt()
                    return
                if self.get_parameter('restart_odometry_on_transition').value:
                    self.processes['odometry'].stop()
                    self.last_seen.pop('odom', None)
                    self._set_state('WAIT_ODOMETRY_STOPPED',
                                    'RF2O/EKF stopped; waiting for graph cleanup')
                else:
                    self.last_seen.pop('map', None)
                    self.processes['localization'].start()
                    self._reset_lifecycle_observation()
                    self._set_state('WAIT_LOCALIZATION_READY',
                                    'Cartographer localization launched')
            elif self._timed_out('cleanup_timeout_s'):
                self._safe_stop(
                    'old mapping interfaces still present: '
                    f'nodes={sorted(remaining)}, action={old_action_present}, '
                    f'cmd_vel_publishers={old_cmd_vel_publishers}')
            return

        if self.state == 'WAIT_ODOMETRY_STOPPED':
            remaining = set(ODOMETRY_NODES) & self._node_names()
            if not remaining:
                self.processes['odometry'].start()
                self._set_state('WAIT_ODOMETRY_RESTART',
                                'Restarting RF2O and EKF before localization')
            elif self._timed_out('cleanup_timeout_s'):
                self._safe_stop(
                    f'old RF2O/EKF nodes still present after cleanup: {sorted(remaining)}')
            return

        if self.state == 'WAIT_LOCALIZATION_READY':
            self._publish_zero_velocity()
            if self._child_failed('localization'):
                self._safe_stop('Cartographer localization launch exited before ready')
            elif self._localization_gate_ready():
                self.nav2_restarts = 0
                self._reset_lifecycle_observation()
                self.processes['navigation'].start()
                self._set_state('WAIT_NAVIGATION_READY',
                                'Localization readiness gate passed; Nav2 launched; '
                                f'{self.localization_last_detail}')
            else:
                self._log_localization_readiness_if_due()
                if self._timed_out('localization_timeout_s'):
                    self._safe_stop(
                        'Cartographer localization readiness timeout: '
                        f'{self.localization_last_detail}; '
                        f'{self._navigation_readiness_detail()}')
            return

        if self.state == 'WAIT_NAVIGATION_READY':
            self._publish_zero_velocity()
            if self._child_failed('navigation'):
                self._retry_or_stop_nav2('Nav2 launch exited before ready')
            elif self._stable(self._navigation_ready()):
                self._set_state(
                    'NAVIGATION',
                    'Localization, Nav2 and motion_controller are active; '
                    f'motion_controller={self.motion_controller_status}, '
                    f'conflict={self.motion_controller_conflict}')
            elif self._timed_out('nav2_timeout_s'):
                self._retry_or_stop_nav2(
                    'Nav2 readiness timeout: '
                    f'{self._navigation_readiness_detail()}')
            return

        if self.state == 'WAIT_NAVIGATION_STOPPED':
            nodes = self._node_names()
            remaining = set(NAV2_ALL_NODES) & nodes
            action_present = self.nav_action.server_is_ready()
            if not remaining and not action_present:
                if time.monotonic() - self.state_since >= float(
                        self.get_parameter('nav2_retry_settle_s').value):
                    self._reset_lifecycle_observation()
                    self.processes['navigation'].start()
                    self._set_state(
                        'WAIT_NAVIGATION_READY',
                        f'Restarting Nav2 (retry {self.nav2_restarts})')
            elif self._timed_out('cleanup_timeout_s'):
                self._safe_stop(
                    'Nav2 retry cleanup timeout: '
                    f'nodes={sorted(remaining)}, action={action_present}')
            return

        if self.state == 'NAVIGATION':
            localization_jump = self._localization_tf_jump()
            if localization_jump:
                warning = (
                    'WARN: localization correction during navigation: '
                    f'{localization_jump}; continuing navigation')
                self.get_logger().warning(warning)
                self._log_event(warning)
            if (self._child_failed('navigation')
                    or self._child_failed('localization')
                    or self._child_failed('motion_control')):
                self._safe_stop(
                    'localization, Nav2, or motion_controller launch exited unexpectedly')
            elif not self._fresh('scan', float(self.get_parameter('scan_stale_s').value)):
                self._safe_stop('/scan watchdog timeout during navigation')
            elif not self._fresh('odom', float(self.get_parameter('odom_stale_s').value)):
                self._safe_stop('/odom watchdog timeout during navigation')

    def _retry_or_stop_nav2(self, reason: str) -> None:
        maximum = int(self.get_parameter('max_nav2_restarts').value)
        if self.nav2_restarts >= maximum:
            self._safe_stop(f'{reason}; Nav2 retry limit reached')
            return
        self.nav2_restarts += 1
        self._log_event(f'{reason}; stopping Nav2 for retry {self.nav2_restarts}')
        self.processes['navigation'].stop()
        self._reset_lifecycle_observation()
        self._set_state('WAIT_NAVIGATION_STOPPED',
                        'Waiting for failed Nav2 graph interfaces to disappear')

    def _retry_or_stop_mapping_nav2(self, reason: str) -> None:
        maximum = int(self.get_parameter('max_nav2_restarts').value)
        if self.mapping_nav2_restarts >= maximum:
            self._safe_stop(f'{reason}; mapping-mode Nav2 retry limit reached')
            return
        self.mapping_nav2_restarts += 1
        self._log_event(
            f'{reason}; stopping mapping-mode Nav2 for retry '
            f'{self.mapping_nav2_restarts}')
        self.processes['navigation_map'].stop()
        self._reset_lifecycle_observation()
        self._set_state(
            'WAIT_MAPPING_NAVIGATION_STOPPED',
            'Waiting for failed mapping-mode Nav2 graph interfaces to disappear')

    def stop_all(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self._log_event('Manager shutdown requested; stopping owned process groups')
        self._publish_zero_velocity()
        self._stop_owned_processes()
        if not self._event_log.closed:
            self._event_log.close()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = MappingNavigationManager()
    try:
        rclpy.spin(node)
    except ExternalShutdownException:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_all()
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
