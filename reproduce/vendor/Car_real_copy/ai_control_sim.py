#!/usr/bin/env python3
"""交互式 AI 外部运动控制模拟器。

本程序只向 /cmd_vel_external 发布速度。非零速度就是运动请求，归零就是释放请求，
不会直接向底盘最终 /cmd_vel 发布命令。
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
import rclpy
from geometry_msgs.msg import Twist
from motion_controller.msg import NavGoal
from rclpy.clock import Clock, ClockType
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String, UInt16, UInt32
from std_srvs.srv import SetBool, Trigger


HELP = """
启动方式：python3 ai_control_sim.py [mode=hardware|mode=sim]（默认 hardware）

可用命令：
  move <线速度> <角速度> [秒]  发布一段定时速度，例如 move 0.1 0.0 2
  goal <x> <y> <yaw度> [对墙]   发送导航目标；yaw范围-180~180，例如 goal 1 -0.5 180 true
  cancel_goal                  通过 motion_controller 取消导航目标
  align_wall                  直接请求墙面对齐
  cancel_align                取消墙面对齐
  sensor_gate_off             同步暂停 Cartographer 的 scan/IMU/odom；墙面对齐仍可读取原始 /scan
  sensor_gate_on              恢复三路数据并等待 RF2O、融合里程计和地图稳定
  Head <角度>                 真机头部目标角度，整数 0~360 度；仿真模式禁用
  manager_start [init]        启动 Manager；init 默认 false
  manager_restart [init]      完整关闭并重新启动 Manager
  manager_stop                关闭当前 Manager 及其全部任务进程
  w [秒]                       前进，默认持续 shortcut_duration
  s [秒]                       后退
  a [秒]                       原地左转（angular.z 为正）
  d [秒]                       原地右转（angular.z 为负）
  x                            立即将 AI 速度归零并自动释放 external 控制权
  status                       显示本模拟器状态
  help                         显示帮助
  quit                         将 AI 速度归零并退出

提示：运动命令到期后会自动归零。真机第一次测试请架空驱动轮并准备硬件急停。
""".strip()


class AiControlSimulator(Node):
    def __init__(self) -> None:
        super().__init__('ai_control_sim')

        self.declare_parameter('cmd_vel_external_topic', '/cmd_vel_external')
        self.declare_parameter(
            'nav_goal_with_options_topic', '/motion_controller/nav_goal_with_options')
        self.declare_parameter(
            'cancel_nav_goal_service', '/motion_controller/cancel_nav_goal')
        self.declare_parameter('align_wall_service', '/motion_controller/align_wall')
        self.declare_parameter(
            'cancel_wall_alignment_service',
            '/motion_controller/cancel_wall_alignment')
        self.declare_parameter(
            'sensor_gate_enable_service',
            '/motion_controller/set_sensor_gate_enabled')
        self.declare_parameter('head_angle_topic', '/step_motor_angle')
        self.declare_parameter('head_min_angle_deg', 0)
        self.declare_parameter('head_max_angle_deg', 360)
        self.declare_parameter('publish_frequency', 10.0)
        self.declare_parameter('default_linear_velocity', 0.10)
        self.declare_parameter('default_angular_velocity', 0.20)
        self.declare_parameter('shortcut_duration', 1.0)
        self.declare_parameter('max_command_duration', 10.0)
        self.declare_parameter('max_linear_velocity', 0.30)
        self.declare_parameter('max_angular_velocity', 0.40)
        self.declare_parameter('manager_workspace_root', str(Path.cwd()))
        # Hardware is the safe/default deployment. main() translates the short
        # CLI form mode=sim into this ROS parameter when simulation is wanted.
        self.declare_parameter('manager_use_sim_time', False)
        self.declare_parameter('manager_stop_timeout_s', 20.0)
        self.declare_parameter('stop_manager_on_ai_exit', True)

        self.cmd_topic = str(self.get_parameter('cmd_vel_external_topic').value)
        nav_goal_with_options_topic = str(
            self.get_parameter('nav_goal_with_options_topic').value)
        cancel_nav_goal_service = str(
            self.get_parameter('cancel_nav_goal_service').value)
        align_wall_service = str(self.get_parameter('align_wall_service').value)
        cancel_wall_alignment_service = str(
            self.get_parameter('cancel_wall_alignment_service').value)
        sensor_gate_enable_service = str(
            self.get_parameter('sensor_gate_enable_service').value)
        self.head_angle_topic = str(self.get_parameter('head_angle_topic').value)
        self.head_min_angle = int(self.get_parameter('head_min_angle_deg').value)
        self.head_max_angle = int(self.get_parameter('head_max_angle_deg').value)
        self.publish_frequency = float(self.get_parameter('publish_frequency').value)
        self.default_linear = float(
            self.get_parameter('default_linear_velocity').value)
        self.default_angular = float(
            self.get_parameter('default_angular_velocity').value)
        self.shortcut_duration = float(self.get_parameter('shortcut_duration').value)
        self.max_command_duration = float(
            self.get_parameter('max_command_duration').value)
        self.max_linear = float(self.get_parameter('max_linear_velocity').value)
        self.max_angular = float(self.get_parameter('max_angular_velocity').value)
        self.manager_workspace = Path(str(
            self.get_parameter('manager_workspace_root').value)).expanduser().resolve()
        self.manager_use_sim_time = bool(
            self.get_parameter('manager_use_sim_time').value)
        self.manager_stop_timeout = float(
            self.get_parameter('manager_stop_timeout_s').value)
        self.stop_manager_on_ai_exit = bool(
            self.get_parameter('stop_manager_on_ai_exit').value)
        self._validate_parameters()

        self.publisher = self.create_publisher(Twist, self.cmd_topic, 10)
        self.head_angle_publisher = self.create_publisher(
            UInt16, self.head_angle_topic, 10)
        self.nav_goal_with_options_publisher = self.create_publisher(
            NavGoal, nav_goal_with_options_topic, 10)
        self.cancel_nav_goal_client = self.create_client(
            Trigger, cancel_nav_goal_service)
        self.align_wall_client = self.create_client(Trigger, align_wall_service)
        self.cancel_wall_client = self.create_client(
            Trigger, cancel_wall_alignment_service)
        self.sensor_gate_enable_client = self.create_client(
            SetBool, sensor_gate_enable_service)
        self.manager_shutdown_client = self.create_client(
            Trigger, '/mapping_manager/shutdown')
        self.create_service(
            SetBool, '~/manager_start', self._manager_start_service)
        self.create_service(
            SetBool, '~/manager_restart', self._manager_restart_service)
        self.create_service(
            Trigger, '~/manager_stop', self._manager_stop_service)

        conflict_qos = QoSProfile(depth=1)
        conflict_qos.reliability = ReliabilityPolicy.RELIABLE
        conflict_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        wall_status_qos = QoSProfile(depth=10)
        wall_status_qos.reliability = ReliabilityPolicy.RELIABLE
        wall_status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(
            Bool, '/motion_controller/control_conflict',
            self._on_conflict, conflict_qos)
        self.create_subscription(
            Bool, '/motion_controller/lidar_enabled',
            self._on_lidar_enabled, conflict_qos)
        self.create_subscription(
            String, '/motion_controller/sensor_gate_state',
            self._on_sensor_gate_state, conflict_qos)
        self.create_subscription(
            String, '/motion_controller/status',
            self._on_controller_status, conflict_qos)
        self.create_subscription(
            String, '/motion_controller/warning',
            self._on_controller_warning, conflict_qos)
        self.create_subscription(
            String, '/motion_controller/nav_goal_status',
            self._on_nav_goal_status, conflict_qos)
        self.create_subscription(
            String, '/motion_controller/wall_alignment_status',
            self._on_wall_alignment_status, wall_status_qos)
        self.create_subscription(
            String, '/head/status', self._on_head_status, conflict_qos)
        self.create_subscription(
            Bool, '/head/aligned', self._on_head_aligned, conflict_qos)
        self.create_subscription(
            String, '/mapping_manager/state', self._on_manager_state, conflict_qos)
        self.create_subscription(
            String, '/mapping_manager/event', self._on_manager_event, conflict_qos)
        self.create_subscription(
            UInt32, '/mapping_manager/attempt', self._on_manager_attempt, conflict_qos)

        self._lock = threading.Lock()
        self._command = Twist()
        self._command_deadline = 0.0
        self._controller_status = 'unknown'
        self._controller_warning = ''
        self._nav_goal_status = 'unknown'
        self._nav_goal_feedback_time = 0.0
        self._wall_alignment_status = 'unknown'
        self._head_status = 'unknown'
        self._head_aligned = False
        self._control_conflict = False
        self._lidar_enabled = True
        self._sensor_gate_state = 'unknown'
        self._manager_state = 'not_running'
        self._manager_event = ''
        self._manager_safe_stop_reason = ''
        self._manager_attempt = 0
        self._manager_process: subprocess.Popen | None = None
        self._manager_log = None

        # 使用 steady clock 驱动发布，避免 use_sim_time 或 /clock 暂停影响安全归零。
        steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = self.create_timer(
            1.0 / self.publish_frequency, self._publish_tick, clock=steady_clock)
        self.manager_watchdog = self.create_timer(
            0.5, self._manager_watchdog_tick, clock=steady_clock)

    def _validate_parameters(self) -> None:
        values = (
            self.publish_frequency,
            self.shortcut_duration,
            self.max_command_duration,
            self.max_linear,
            self.max_angular,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError('频率、持续时间和速度限制必须是有限正数')
        if not math.isfinite(self.default_linear) or not math.isfinite(
                self.default_angular):
            raise ValueError('默认速度必须是有限数值')
        if not self.head_angle_topic:
            raise ValueError('head_angle_topic 不能为空')
        if not 0 <= self.head_min_angle <= self.head_max_angle <= 65535:
            raise ValueError('Head 角度限制必须位于 UInt16 的 0~65535 范围内')

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return max(-limit, min(value, limit))

    def set_motion(self, linear: float, angular: float, duration: float) -> None:
        if not all(math.isfinite(value) for value in (linear, angular, duration)):
            raise ValueError('速度和持续时间必须是有限数值')
        if duration <= 0.0:
            raise ValueError('持续时间必须大于 0')
        duration = min(duration, self.max_command_duration)
        command = Twist()
        command.linear.x = self._clamp(linear, self.max_linear)
        command.angular.z = self._clamp(angular, self.max_angular)
        with self._lock:
            self._command = command
            self._command_deadline = time.monotonic() + duration
        print(
            f'AI 命令：linear.x={command.linear.x:.3f} m/s，'
            f'angular.z={command.angular.z:.3f} rad/s，持续 {duration:.2f} s',
            flush=True,
        )

    def stop_ai_command(self) -> None:
        with self._lock:
            self._command = Twist()
            self._command_deadline = 0.0
        self._publish_zero(repeat=2)

    def send_nav_goal(
            self, x: float, y: float, yaw_degrees: float,
            align_to_wall: bool = False) -> None:
        if not all(math.isfinite(value) for value in (x, y, yaw_degrees)):
            raise ValueError('目标 x、y、yaw 必须是有限数值')
        if not -180.0 <= yaw_degrees <= 180.0:
            raise ValueError('目标 yaw 必须在 -180~180 度范围内')
        goal = NavGoal()
        goal.x = x
        goal.y = y
        goal.yaw = yaw_degrees
        goal.align_to_wall = align_to_wall
        self.nav_goal_with_options_publisher.publish(goal)
        print(
            f'已向 motion_controller 请求导航：x={x:.3f}, y={y:.3f}, '
            f'yaw={yaw_degrees:.2f}°, align_to_wall={align_to_wall}', flush=True)

    def cancel_nav_goal(self) -> None:
        if not self.cancel_nav_goal_client.service_is_ready():
            print('取消失败：motion_controller cancel_nav_goal 服务尚未就绪', flush=True)
            return
        future = self.cancel_nav_goal_client.call_async(Trigger.Request())
        future.add_done_callback(
            lambda result_future: print(
                f'取消导航：{result_future.result().success}，'
                f'{result_future.result().message}', flush=True))

    @staticmethod
    def _parse_bool(value: str) -> bool:
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'off'):
            return False
        raise ValueError('布尔值必须是 true 或 false')

    def request_wall_alignment(self, cancel: bool = False) -> None:
        client = self.cancel_wall_client if cancel else self.align_wall_client
        operation = '取消墙面对齐' if cancel else '直接墙面对齐'
        if not client.service_is_ready():
            print(f'{operation}失败：motion_controller 服务尚未就绪', flush=True)
            return
        future = client.call_async(Trigger.Request())
        future.add_done_callback(
            lambda result_future: print(
                f'{operation}：{result_future.result().success}，'
                f'{result_future.result().message}', flush=True))

    def set_sensor_gate_enabled(self, enabled: bool) -> None:
        if not self.sensor_gate_enable_client.service_is_ready():
            print('传感器门控失败：motion_controller 服务尚未就绪', flush=True)
            return
        request = SetBool.Request()
        request.data = enabled
        future = self.sensor_gate_enable_client.call_async(request)
        operation = '开启传感器门控' if enabled else '关闭传感器门控'
        future.add_done_callback(
            lambda result_future: print(
                f'{operation}：{result_future.result().success}，'
                f'{result_future.result().message}', flush=True))

    def set_head_angle(self, angle: float) -> None:
        if self.manager_use_sim_time:
            print('Head 命令仅支持真机；当前 mode=sim，未发布命令', flush=True)
            return
        if not math.isfinite(angle):
            raise ValueError('Head 角度必须是有限数值')
        rounded = round(angle)
        if not math.isclose(angle, rounded, abs_tol=1e-9):
            raise ValueError('Head 角度必须是整数（底层消息类型为 UInt16）')
        angle_deg = int(rounded)
        if not self.head_min_angle <= angle_deg <= self.head_max_angle:
            raise ValueError(
                f'Head 角度范围为 {self.head_min_angle}~{self.head_max_angle} 度')
        message = UInt16()
        message.data = angle_deg
        self.head_angle_publisher.publish(message)
        self._head_status = f'command_sent;target={angle_deg}'
        self._head_aligned = False
        print(
            f'已发布真机头部目标角度：{angle_deg}° -> {self.head_angle_topic}；'
            '最长调整时间 5 秒',
            flush=True)

    def _publish_tick(self) -> None:
        publish_command = False
        expired = False
        with self._lock:
            if self._command_deadline and time.monotonic() >= self._command_deadline:
                self._command = Twist()
                self._command_deadline = 0.0
                expired = True
            elif self._command_deadline:
                publish_command = True
            command = self._command
        if publish_command:
            self.publisher.publish(command)
        elif expired:
            self._publish_zero(repeat=2)
            print('AI 运动命令已到期，自动归零并停止空闲发布', flush=True)

    def _publish_zero(self, repeat: int = 1) -> None:
        for _ in range(repeat):
            self.publisher.publish(Twist())

    def _manager_node_present(self) -> bool:
        return any(
            name == 'mapping_navigation_manager' and namespace == '/'
            for name, namespace in self.get_node_names_and_namespaces())

    def _manager_feedback_active(self) -> bool:
        owned = (
            self._manager_process is not None
            and self._manager_process.poll() is None)
        return owned or self._manager_node_present()

    def start_manager(self, init: bool = False) -> None:
        if self._manager_process is not None and self._manager_process.poll() is None:
            print('Manager 已由 AI 启动并正在运行', flush=True)
            return
        if self._manager_node_present():
            print('启动拒绝：检测到外部 Manager；AI 不会创建重复节点', flush=True)
            return
        log_dir = self.manager_workspace / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f'ai-manager-{time.strftime("%Y%m%d-%H%M%S")}.log'
        self._manager_log = log_path.open('a', encoding='utf-8')
        command = [
            'ros2', 'run', 'robot_bringup', 'mapping_navigation_manager.py',
            '--ros-args', '-p', f'init:={str(init).lower()}',
            '-p', f'workspace_root:={self.manager_workspace}',
            '-p', f'use_sim_time:={str(self.manager_use_sim_time).lower()}',
        ]
        try:
            self._manager_process = subprocess.Popen(
                command, cwd=self.manager_workspace, stdin=subprocess.DEVNULL,
                stdout=self._manager_log, stderr=subprocess.STDOUT,
                start_new_session=True, text=True)
        except Exception:
            self._manager_log.close()
            self._manager_log = None
            raise
        self._manager_state = 'starting'
        self._manager_safe_stop_reason = ''
        print(
            f'Manager 启动中：pid={self._manager_process.pid}, init={init}, '
            f'mode={"sim" if self.manager_use_sim_time else "hardware"}, '
            f'log={log_path}', flush=True)

    def stop_manager(self) -> None:
        process = self._manager_process
        self.stop_ai_command()
        manager_present = self._manager_node_present()
        owned_running = process is not None and process.poll() is None
        if not manager_present and not owned_running:
            self._manager_process = None
            self._close_manager_log()
            print('Manager 未运行', flush=True)
            return
        print('正在关闭 Manager 及其拥有的任务进程……', flush=True)
        requested = self._request_manager_shutdown_service()
        if not requested:
            print('Manager shutdown 服务不可用，执行任务清理工具', flush=True)
            cleanup = subprocess.run(
                ['ros2', 'run', 'robot_bringup', 'cleanup_robot_mission.py'],
                cwd=self.manager_workspace, check=False)
            if cleanup.returncode != 0:
                print(f'Manager 清理失败，returncode={cleanup.returncode}', flush=True)
                return
        if owned_running:
            try:
                process.wait(timeout=self.manager_stop_timeout)
            except subprocess.TimeoutExpired:
                # Manager children may already be gone while the `ros2 run`
                # wrapper remains alive. The process group belongs exclusively
                # to this supervisor, so finish that wrapper deterministically.
                print('Manager 优雅关闭超时，正在结束其专属进程组', flush=True)
                try:
                    os.killpg(process.pid, signal.SIGINT)
                    process.wait(timeout=5.0)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5.0)
                    except ProcessLookupError:
                        pass
                    except subprocess.TimeoutExpired:
                        print('Manager 进程组仍未退出；拒绝启动重复 Manager', flush=True)
                        return
        else:
            deadline = time.monotonic() + self.manager_stop_timeout
            while self._manager_node_present() and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._manager_node_present():
                print('外部 Manager 关闭超时；拒绝启动重复 Manager', flush=True)
                return
        self._manager_process = None
        self._close_manager_log()
        self._manager_state = 'not_running'
        print('Manager 已关闭', flush=True)

    def _close_manager_log(self) -> None:
        if self._manager_log is not None:
            self._manager_log.close()
            self._manager_log = None

    def _request_manager_shutdown_service(self) -> bool:
        if not self.manager_shutdown_client.service_is_ready():
            return False
        completed = threading.Event()
        result = {'success': False}
        future = self.manager_shutdown_client.call_async(Trigger.Request())

        def done(response_future) -> None:
            try:
                result['success'] = bool(response_future.result().success)
            except Exception:
                result['success'] = False
            completed.set()

        future.add_done_callback(done)
        return completed.wait(timeout=3.0) and result['success']

    def restart_manager(self, init: bool = False) -> None:
        self.stop_manager()
        deadline = time.monotonic() + 5.0
        while self._manager_node_present() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._manager_node_present():
            print('重启中止：旧 Manager 节点仍在 ROS graph 中', flush=True)
            return
        self.start_manager(init)

    def _manager_start_service(self, request: SetBool.Request,
                               response: SetBool.Response) -> SetBool.Response:
        try:
            self.start_manager(bool(request.data))
            response.success = (
                self._manager_process is not None
                and self._manager_process.poll() is None)
            response.message = (
                f'Manager started with init={bool(request.data)}'
                if response.success else 'Manager was not started; inspect AI output')
        except Exception as exc:
            response.success = False
            response.message = f'Manager start failed: {exc}'
        return response

    def _manager_restart_service(self, request: SetBool.Request,
                                 response: SetBool.Response) -> SetBool.Response:
        try:
            self.restart_manager(bool(request.data))
            response.success = (
                self._manager_process is not None
                and self._manager_process.poll() is None)
            response.message = (
                f'Manager restarted with init={bool(request.data)}'
                if response.success else 'Manager was not restarted; inspect AI output')
        except Exception as exc:
            response.success = False
            response.message = f'Manager restart failed: {exc}'
        return response

    def _manager_stop_service(self, _request: Trigger.Request,
                              response: Trigger.Response) -> Trigger.Response:
        try:
            self.stop_manager()
            response.success = not self._manager_node_present()
            response.message = (
                'Manager stopped' if response.success
                else 'Manager is external or still present in ROS graph')
        except Exception as exc:
            response.success = False
            response.message = f'Manager stop failed: {exc}'
        return response

    def _manager_watchdog_tick(self) -> None:
        process = self._manager_process
        if process is None or process.poll() is None:
            return
        return_code = process.returncode
        self._manager_process = None
        if self._manager_log is not None:
            self._manager_log.close()
            self._manager_log = None
        if self._manager_state != 'SAFE_STOP':
            self._manager_state = 'exited'
            self.stop_ai_command()
            print(f'警告：Manager 进程意外退出，returncode={return_code}', flush=True)

    def _on_conflict(self, message: Bool) -> None:
        if message.data and not self._control_conflict:
            print('警告：motion_controller 检测到多个控制源同时发送非零命令！', flush=True)
        elif not message.data and self._control_conflict:
            print('控制源冲突已解除', flush=True)
        self._control_conflict = message.data

    def _on_lidar_enabled(self, message: Bool) -> None:
        if message.data != self._lidar_enabled:
            print(f'雷达数据转发：{"已开启" if message.data else "已屏蔽"}', flush=True)
        self._lidar_enabled = message.data

    def _on_sensor_gate_state(self, message: String) -> None:
        if message.data != self._sensor_gate_state:
            print(f'传感器门控状态：{message.data}', flush=True)
        self._sensor_gate_state = message.data

    def _on_controller_status(self, message: String) -> None:
        self._controller_status = message.data

    def _on_head_status(self, message: String) -> None:
        if message.data != self._head_status:
            print(f'头部调整状态：{message.data}', flush=True)
        self._head_status = message.data

    def _on_head_aligned(self, message: Bool) -> None:
        if message.data and not self._head_aligned:
            print('头部调整稳定完成', flush=True)
        self._head_aligned = message.data

    def _on_controller_warning(self, message: String) -> None:
        warning = message.data.strip()
        if warning and warning != self._controller_warning:
            print(f'AI 请求被拒绝/冲突：{warning}', flush=True)
        self._controller_warning = warning

    def _on_nav_goal_status(self, message: String) -> None:
        previous = self._nav_goal_status
        self._nav_goal_status = message.data
        self._nav_goal_feedback_time = time.monotonic()
        # 心跳包含持续变化的 elapsed/distance，不逐条刷屏；阶段变化和最终结果可见。
        if message.data != previous and not message.data.startswith('navigating;'):
            print(f'导航反馈：{message.data}', flush=True)

    def _on_wall_alignment_status(self, message: String) -> None:
        previous = self._wall_alignment_status
        self._wall_alignment_status = message.data
        if message.data != previous:
            print(f'墙面对齐反馈：{message.data}', flush=True)

    def _on_manager_state(self, message: String) -> None:
        if not self._manager_feedback_active():
            # The Manager topics are transient-local. A newly started AI node
            # receives the previous run's terminal state even when no Manager
            # currently exists; that history must not trigger a new alarm.
            return
        previous = self._manager_state
        self._manager_state = message.data.strip() or 'unknown'
        if self._manager_state == 'SAFE_STOP':
            self.stop_ai_command()
            # The event topic carries the reason and is the single alarm source.
        elif self._manager_state != previous:
            print(f'Manager 状态：{previous} -> {self._manager_state}', flush=True)

    def _on_manager_event(self, message: String) -> None:
        if not self._manager_feedback_active():
            return
        event = message.data.strip()
        if not event or event == self._manager_event:
            return
        self._manager_event = event
        if event.startswith('SAFE_STOP:'):
            self._manager_safe_stop_reason = event.partition(':')[2].strip()
            self.stop_ai_command()
            print(f'严重：Manager {event}；AI 运动已释放', flush=True)
        elif any(token in event for token in (
                'timeout', 'failed', 'rebuild', 'LIDAR_RESTART',
                'Localization readiness gate passed')):
            print(f'Manager 事件：{event}', flush=True)

    def _on_manager_attempt(self, message: UInt32) -> None:
        self._manager_attempt = int(message.data)

    def show_status(self) -> None:
        with self._lock:
            remaining = max(0.0, self._command_deadline - time.monotonic())
            linear = self._command.linear.x
            angular = self._command.angular.z
        feedback_age = (
            max(0.0, time.monotonic() - self._nav_goal_feedback_time)
            if self._nav_goal_feedback_time else float('inf'))
        print(
            f'external_request_active={remaining > 0.0 and (abs(linear) > 0.0 or abs(angular) > 0.0)}, '
            f'controller_status={self._controller_status}, '
            f'conflict={self._control_conflict}, '
            f'warning={self._controller_warning or "none"}, '
            f'nav_goal_status={self._nav_goal_status}, '
            f'nav_feedback_age={feedback_age:.2f}s, '
            f'wall_alignment_status={self._wall_alignment_status}, '
            f'head_status={self._head_status}, '
            f'head_aligned={self._head_aligned}, '
            f'lidar_enabled={self._lidar_enabled}, '
            f'sensor_gate_state={self._sensor_gate_state}, '
            f'manager_state={self._manager_state}, '
            f'manager_attempt={self._manager_attempt}, '
            f'manager_event={self._manager_event or "none"}, '
            f'manager_safe_stop={self._manager_safe_stop_reason or "none"}, '
            f'manager_owned={self._manager_process is not None}, '
            f'command=({linear:.3f}, {angular:.3f}), '
            f'remaining={remaining:.2f}s',
            flush=True,
        )

    def request_shutdown(self) -> None:
        self.stop_ai_command()
        if self.stop_manager_on_ai_exit:
            self.stop_manager()
        if rclpy.ok(context=self.context):
            rclpy.shutdown(context=self.context)


def input_loop(node: AiControlSimulator) -> None:
    print(HELP, flush=True)
    while rclpy.ok(context=node.context):
        try:
            line = input('ai-control> ').strip()
        except (EOFError, KeyboardInterrupt):
            node.request_shutdown()
            return
        if not line:
            continue
        try:
            parts = shlex.split(line)
            command = parts[0].lower()
            if command == 'move':
                if len(parts) not in (3, 4):
                    raise ValueError('格式：move <线速度> <角速度> [秒]')
                duration = float(parts[3]) if len(parts) == 4 else node.shortcut_duration
                node.set_motion(float(parts[1]), float(parts[2]), duration)
            elif command == 'goal':
                if len(parts) not in (4, 5):
                    raise ValueError('格式：goal <x> <y> <yaw度(-180~180)> [align_to_wall]')
                align = node._parse_bool(parts[4]) if len(parts) == 5 else False
                node.send_nav_goal(
                    float(parts[1]), float(parts[2]), float(parts[3]), align)
            elif command == 'cancel_goal':
                node.cancel_nav_goal()
            elif command == 'align_wall':
                node.request_wall_alignment()
            elif command == 'cancel_align':
                node.request_wall_alignment(cancel=True)
            elif command == 'sensor_gate_off':
                node.set_sensor_gate_enabled(False)
            elif command == 'sensor_gate_on':
                node.set_sensor_gate_enabled(True)
            elif command == 'head':
                if len(parts) != 2:
                    raise ValueError('格式：Head <角度>')
                node.set_head_angle(float(parts[1]))
            elif command in ('manager_start', 'manager_restart'):
                if len(parts) > 2:
                    raise ValueError(f'格式：{command} [init]')
                init = (
                    True if len(parts) == 2 and parts[1].lower() == 'init'
                    else node._parse_bool(parts[1]) if len(parts) == 2
                    else False)
                if command == 'manager_start':
                    node.start_manager(init)
                else:
                    node.restart_manager(init)
            elif command == 'manager_stop':
                if len(parts) != 1:
                    raise ValueError('格式：manager_stop')
                node.stop_manager()
            elif command in ('w', 's', 'a', 'd'):
                if len(parts) > 2:
                    raise ValueError(f'格式：{command} [秒]')
                duration = float(parts[1]) if len(parts) == 2 else node.shortcut_duration
                values = {
                    'w': (node.default_linear, 0.0),
                    's': (-node.default_linear, 0.0),
                    'a': (0.0, node.default_angular),
                    'd': (0.0, -node.default_angular),
                }
                node.set_motion(*values[command], duration)
            elif command == 'x':
                node.stop_ai_command()
                print('AI 速度已归零', flush=True)
            elif command == 'status':
                node.show_status()
            elif command == 'help':
                print(HELP, flush=True)
            elif command in ('quit', 'exit', 'q'):
                node.request_shutdown()
                return
            else:
                print('未知命令。输入 help 查看帮助。', flush=True)
        except ValueError as exc:
            print(f'命令错误：{exc}', flush=True)


def _parse_mode_argument(args: list[str]) -> list[str]:
    """Translate mode=sim/hardware without exposing ROS parameter syntax."""
    remaining = []
    selected_mode = None
    for argument in args:
        if argument.startswith('mode='):
            if selected_mode is not None:
                raise ValueError('mode 只能指定一次')
            selected_mode = argument.partition('=')[2].strip().lower()
            if selected_mode not in ('sim', 'hardware'):
                raise ValueError('mode 必须是 sim 或 hardware')
        else:
            remaining.append(argument)
    if selected_mode is None:
        return remaining
    use_sim_time = 'true' if selected_mode == 'sim' else 'false'
    parameter = ['-p', f'manager_use_sim_time:={use_sim_time}']
    if '--ros-args' not in remaining:
        return [*remaining, '--ros-args', *parameter]
    ros_start = remaining.index('--ros-args')
    try:
        ros_end = remaining.index('--', ros_start + 1)
    except ValueError:
        ros_end = len(remaining)
    return [*remaining[:ros_end], *parameter, *remaining[ros_end:]]


def main(args=None) -> None:
    raw_args = list(sys.argv[1:] if args is None else args)
    try:
        ros_args = _parse_mode_argument(raw_args)
    except ValueError as exc:
        print(f'启动参数错误：{exc}', flush=True)
        return
    rclpy.init(args=ros_args)
    node = AiControlSimulator()
    executor = SingleThreadedExecutor(context=node.context)
    executor.add_node(node)
    thread = threading.Thread(target=input_loop, args=(node,), daemon=True)
    thread.start()
    try:
        executor.spin()
    except ExternalShutdownException:
        pass
    except KeyboardInterrupt:
        if rclpy.ok(context=node.context):
            node.stop_ai_command()
    finally:
        if rclpy.ok(context=node.context):
            node.stop_ai_command()
        executor.remove_node(node)
        executor.shutdown()
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
