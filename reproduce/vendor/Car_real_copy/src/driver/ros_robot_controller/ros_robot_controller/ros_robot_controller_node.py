#!/usr/bin/env python3
# encoding: utf-8
# stm32 ros2 package - Minimal Version (RPM Direct)
import math
import time
import rclpy
import threading
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from std_srvs.srv import Trigger
from std_msgs.msg import Bool, Float32, String, UInt8, UInt16
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Imu
import os
import sys

# 获取当前脚本所在目录（ros_robot_controller 文件夹）
current_dir = os.path.dirname(os.path.abspath(__file__))
# 获取上级目录（当前目录）
parent_dir = os.path.dirname(current_dir)

# 添加上级目录到路径，以便导入 ros_robot_controller_msgs
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# 现在可以导入了
from ros_robot_controller.ros_robot_controller_sdk import Board
from ros_robot_controller.head_attitude_filter import RobustMahonyAHRS
from ros_robot_controller.head_settle_policy import (
    HeadArrivalBrakePolicy,
    HeadCommandDeadline,
    HeadSpeedProfile,
    HeadSettlePolicy,
)
from ros_robot_controller_msgs.msg import MotorState, MotorsState


# Real-robot IMU installation offset measured with the head physically level.
# Horizontal target = head_horizontal_roll_deg + this constant.
HEAD_HORIZONTAL_ROLL_OFFSET_DEG = 5.0
HEAD_HORIZONTAL_COMMAND_DEG = 180.0 + HEAD_HORIZONTAL_ROLL_OFFSET_DEG


def _clip(value, low, high):
    return max(low, min(high, value))


class RosRobotController(Node):
    def __init__(self):
        super().__init__('ros_robot_controller')
        
        # 声明参数
        self.declare_parameter('device', '/dev/ttyS0')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('cmd_vel_topic', '/cmd_vel')
        self.declare_parameter('wheel_track', 0.2948)      # 轮距 (米)
        self.declare_parameter('wheel_diameter', 0.07)    # 轮子直径 (米)
        self.declare_parameter('max_motor_speed', 200.0)  # 最大电机转速 (RPM)
        self.declare_parameter('cmd_vel_timeout', 0.5)    # 速度指令超时 (秒)
        self.declare_parameter('motor_speed_topic', '/motor_speed')
        self.declare_parameter('motor_speed_raw_topic', '/motor_speed/raw')
        self.declare_parameter('control_rate', 50.0)      # 控制频率 (Hz)
        self.declare_parameter('head_imu_topic', '/imu/raw')
        self.declare_parameter('head_imu_timeout', 0.25)
        # 100 samples at the default 50 Hz keeps the original ~2 second
        # stationary calibration window (previously 400 samples at ~200 Hz).
        self.declare_parameter('head_calibration_samples', 100)
        self.declare_parameter('head_calibration_max_gyro_rad_s', 0.10)
        self.declare_parameter('head_calibration_max_gyro_std_rad_s', 0.02)
        self.declare_parameter('head_calibration_max_accel_std_ms2', 0.15)
        self.declare_parameter(
            'head_command_horizontal_deg', HEAD_HORIZONTAL_COMMAND_DEG)
        self.declare_parameter('head_horizontal_roll_deg', 180.0)
        self.declare_parameter('head_kp_angle', 1.0)
        self.declare_parameter('head_kp_rate', 2.0)
        self.declare_parameter('head_k_ff', 0.0)
        self.declare_parameter('head_angle_deadband_deg', 2.0)
        self.declare_parameter('head_angle_restart_deg', 4.0)
        self.declare_parameter('head_settle_rate_dps', 0.5)
        self.declare_parameter('head_settle_enter_hold_sec', 0.50)
        self.declare_parameter('head_settle_exit_hold_sec', 0.20)
        self.declare_parameter('head_motion_restart_rate_dps', 1.0)
        self.declare_parameter('head_motion_restart_hold_sec', 0.05)
        self.declare_parameter('head_arrival_brake_engage_rate_dps', 2.0)
        self.declare_parameter('head_arrival_brake_release_rate_dps', 0.50)
        self.declare_parameter('head_motor_deadband', 8.0)
        self.declare_parameter('head_max_motor_speed', 60.0)
        self.declare_parameter('head_max_desired_rate_dps', 15.0)
        self.declare_parameter('head_medium_error_deg', 4.0)
        self.declare_parameter('head_far_error_deg', 10.0)
        self.declare_parameter('head_near_rate_dps', 12.0)
        self.declare_parameter('head_medium_rate_dps', 30.0)
        self.declare_parameter('head_far_rate_dps', 50.0)
        self.declare_parameter('head_command_timeout_sec', 5.0)
        self.declare_parameter('head_filter_alpha', 0.4)
        self.declare_parameter('head_mahony_kp', 10.0)
        self.declare_parameter('head_mahony_ki', 0.008)
        self.declare_parameter('head_mahony_min_accel_ms2', 7.3549875)
        self.declare_parameter('head_mahony_max_accel_ms2', 12.2583125)
        self.declare_parameter('head_roll_smoothing', 0.15)
        self.declare_parameter('head_max_gyro_rad_s', 4.2)
        
        # 读取参数
        self.device = self.get_parameter('device').value
        self.baudrate = self.get_parameter('baudrate').value
        self.cmd_vel_topic = self.get_parameter('cmd_vel_topic').value
        self.wheel_track = float(self.get_parameter('wheel_track').value)
        self.wheel_diameter = float(self.get_parameter('wheel_diameter').value)
        self.max_motor_speed = abs(float(self.get_parameter('max_motor_speed').value))
        self.cmd_vel_timeout = float(self.get_parameter('cmd_vel_timeout').value)
        self.motor_speed_topic = self.get_parameter('motor_speed_topic').value
        self.motor_speed_raw_topic = self.get_parameter('motor_speed_raw_topic').value
        self.control_rate = max(1.0, float(self.get_parameter('control_rate').value))
        self.head_imu_topic = self.get_parameter('head_imu_topic').value
        self.head_imu_timeout = max(0.05, float(self.get_parameter('head_imu_timeout').value))
        self.head_calibration_samples = max(50, int(self.get_parameter('head_calibration_samples').value))
        self.head_command_horizontal_deg = float(self.get_parameter('head_command_horizontal_deg').value)
        self.head_horizontal_roll_deg = (
            float(self.get_parameter('head_horizontal_roll_deg').value)
            + HEAD_HORIZONTAL_ROLL_OFFSET_DEG) % 360.0
        
        # 初始化 Board
        self.board = Board(device=self.device, baudrate=self.baudrate)
        self.reception_enabled = True
        self.board.enable_reception()
        
        # 状态变量
        self.running = True
        self.is_shutting_down = False
        self.last_cmd_vel_time = time.monotonic()
        self.last_control_time = time.monotonic()
        self.has_cmd_vel = False
        
        # 目标转速 (RPM)
        self.target_left_rpm = 0.0
        self.target_right_rpm = 0.0
        
        # 测量转速 (RPM)
        self.measured_left_rpm = 0.0
        self.measured_right_rpm = 0.0
        
        # 创建发布者
        self.motor_speed_pub = self.create_publisher(MotorsState, self.motor_speed_topic, 10)
        self.motor_speed_raw_pub = self.create_publisher(MotorsState, self.motor_speed_raw_topic, 10)
        self.wakeup_pub = self.create_publisher(UInt8, '~/wakeup', 10)
        
        # 创建订阅者
        self.cmd_vel_sub = self.create_subscription(Twist, self.cmd_vel_topic, self.cmd_vel_callback, 10)
        self.enable_recv_sub = self.create_subscription(UInt8, '~/enable_reception', self.set_reception_enabled, 1)
        
        # 停止电机
        self.board.set_motor_speed(0, 0)
        
        # 创建定时器
        self.control_timer = self.create_timer(1.0 / self.control_rate, self.control_loop)
        self.watchdog_timer = self.create_timer(0.1, self.cmd_vel_watchdog)
        
        # 启动发布线程
        self.pub_thread = threading.Thread(target=self.pub_callback, daemon=True)
        self.pub_thread.start()
        
        # 创建服务
        self.init_service = self.create_service(Trigger, '~/init_finish', self.get_node_state)
        
        # 计算速度转换系数
        if self.wheel_diameter > 0:
            self.rpm_to_ms = (math.pi * self.wheel_diameter) / 60.0  # RPM -> m/s
            self.ms_to_rpm = 60.0 / (math.pi * self.wheel_diameter)   # m/s -> RPM
        else:
            self.rpm_to_ms = 0.0
            self.ms_to_rpm = 0.0
        
        print('\033[1;32m' + '='*60)
        print('ROS Robot Controller Started (RPM Direct Mode)')
        print(f'  device: {self.device}')
        print(f'  baudrate: {self.baudrate}')
        print(f'  cmd_vel_topic: {self.cmd_vel_topic}')
        print(f'  wheel_track: {self.wheel_track} m')
        print(f'  wheel_diameter: {self.wheel_diameter} m')
        print(f'  max_motor_speed: {self.max_motor_speed} RPM')
        print(f'  control_rate: {self.control_rate} Hz')
        print(f'  Speed conversion: 1 m/s = {self.ms_to_rpm:.1f} RPM')
        print(f'  Speed conversion: 1 RPM = {self.rpm_to_ms:.3f} m/s')   
        print('='*60 + '\033[0m')


        # -------- 头部控制：只订阅唯一 IMU publisher，不再访问 I2C --------
        self.lock_step_motor = False  # 锁定步进电机，避免与底盘控制冲突
        self.latest_imu_sample = None
        self.latest_imu_sequence = 0
        self.imu_condition = threading.Condition()
        self.horizontal_roll = None
        self.requested_head_angle = self.head_command_horizontal_deg
        self.target_roll = None
        self.target_roll_lock = threading.Lock()
        self.head_settle_policy = HeadSettlePolicy(
            self.requested_head_angle,
            enter_deg=float(self.get_parameter('head_angle_deadband_deg').value),
            exit_deg=float(self.get_parameter('head_angle_restart_deg').value),
            rate_dps=float(self.get_parameter('head_settle_rate_dps').value),
            exit_hold_sec=float(
                self.get_parameter('head_settle_exit_hold_sec').value),
            enter_hold_sec=float(
                self.get_parameter('head_settle_enter_hold_sec').value),
            motion_restart_rate_dps=float(
                self.get_parameter('head_motion_restart_rate_dps').value),
            motion_restart_hold_sec=float(
                self.get_parameter('head_motion_restart_hold_sec').value),
        )
        self.head_arrival_brake_policy = HeadArrivalBrakePolicy(
            engage_rate_dps=float(
                self.get_parameter('head_arrival_brake_engage_rate_dps').value),
            release_rate_dps=float(
                self.get_parameter('head_arrival_brake_release_rate_dps').value),
        )
        self.head_speed_profile = HeadSpeedProfile(
            deadband_deg=float(self.get_parameter('head_angle_deadband_deg').value),
            medium_error_deg=float(self.get_parameter('head_medium_error_deg').value),
            far_error_deg=float(self.get_parameter('head_far_error_deg').value),
            near_rate_dps=float(self.get_parameter('head_near_rate_dps').value),
            medium_rate_dps=float(self.get_parameter('head_medium_rate_dps').value),
            far_rate_dps=float(self.get_parameter('head_far_rate_dps').value),
        )
        self.head_command_deadline = HeadCommandDeadline(
            float(self.get_parameter('head_command_timeout_sec').value))
        self.head_command_timed_out = False

        self.head_imu_sub = self.create_subscription(
            Imu,
            self.head_imu_topic,
            self._head_imu_callback,
            qos_profile_sensor_data,
        )

        # 相对于启动水平基准的实时控制诊断，便于在不访问 I2C 的情况下调参。
        self.head_current_angle_pub = self.create_publisher(
            Float32, '/head/current_angle_deg', 10)
        self.head_target_angle_pub = self.create_publisher(
            Float32, '/head/target_angle_deg', 10)
        self.head_angle_error_pub = self.create_publisher(
            Float32, '/head/angle_error_deg', 10)
        self.head_motor_command_pub = self.create_publisher(
            Float32, '/head/motor_command', 10)
        self.head_angular_rate_pub = self.create_publisher(
            Float32, '/head/angular_rate_dps', 10)
        self.head_in_position_pub = self.create_publisher(
            Bool, '/head/in_position', 10)
        self.head_arrival_brake_pub = self.create_publisher(
            Bool, '/head/arrival_brake_active', 10)
        head_status_qos = QoSProfile(depth=1)
        head_status_qos.reliability = ReliabilityPolicy.RELIABLE
        head_status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.head_status_pub = self.create_publisher(
            String, '/head/status', head_status_qos)
        self.head_aligned_pub = self.create_publisher(
            Bool, '/head/aligned', head_status_qos)
        self.last_head_status = ''
        self.last_head_diagnostics_time = 0.0

        # 第一轮抗过冲整定：降低角度环刚度，同时提高角速度反馈阻尼。
        # 参数在控制循环中读取，因此可通过 ros2 param set 在线小步调整。
        self.kp_angle = float(self.get_parameter('head_kp_angle').value)
        self.kp_rate = float(self.get_parameter('head_kp_rate').value)
        self.k_ff = float(self.get_parameter('head_k_ff').value)

        # 启动头部控制线程
        self.head_thread = None
        self.head_running = threading.Event()
        self.board.set_step_motor_speed(0, 100)  # 初始停转
        self.step_motor_speed_last = 0
        self._start_head_control()
        self._publish_head_status('initializing', False)

        # 订阅目标角度话题 /step_motor_angle
        self.step_angle_sub = self.create_subscription(
            UInt16, '/step_motor_angle', self.step_angle_callback, 10)

        self.get_logger().info('\033[1;32m' + '='*60)
        self.get_logger().info('ROS Robot Controller with Head Stabilization Started')
        self.get_logger().info(f'  chassis device: {self.device}')
        self.get_logger().info(f'  head IMU subscription: {self.head_imu_topic}')
        self.get_logger().info(
            f'  current physical head pose will be calibrated as horizontal command '
            f'{self.head_command_horizontal_deg:.1f} deg')
        self.get_logger().info('='*60 + '\033[0m')


    # ========== 头部 IMU 订阅与控制 ==========
    @staticmethod
    def _wrap_degrees(angle):
        return (angle + 180.0) % 360.0 - 180.0

    def _head_imu_callback(self, msg):
        gyro = (
            msg.angular_velocity.x,
            msg.angular_velocity.y,
            msg.angular_velocity.z,
        )
        accel = (
            msg.linear_acceleration.x,
            msg.linear_acceleration.y,
            msg.linear_acceleration.z,
        )
        values = gyro + accel
        accel_magnitude = math.sqrt(sum(value * value for value in accel))
        max_gyro = max(
            0.0, float(self.get_parameter('head_max_gyro_rad_s').value))
        if not all(math.isfinite(value) for value in values) or any(
                abs(value) > max_gyro for value in gyro):
            self.get_logger().warning(
                'rejected non-finite or excessive head IMU gyro message',
                throttle_duration_sec=2.0,
            )
            return
        min_accel = float(self.get_parameter('head_mahony_min_accel_ms2').value)
        max_accel = float(self.get_parameter('head_mahony_max_accel_ms2').value)
        if not min_accel <= accel_magnitude <= max_accel:
            self.get_logger().warning(
                f'head IMU acceleration outside gravity window: '
                f'|a|={accel_magnitude:.3f} m/s^2; gyro integration retained',
                throttle_duration_sec=2.0,
            )
        with self.imu_condition:
            self.latest_imu_sequence += 1
            self.latest_imu_sample = (time.monotonic(), gyro, accel)
            self.imu_condition.notify_all()

    def _wait_for_new_imu(self, after_sequence):
        deadline = time.monotonic() + self.head_imu_timeout
        with self.imu_condition:
            while self.latest_imu_sequence <= after_sequence and self.head_running.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return after_sequence, None
                self.imu_condition.wait(timeout=remaining)
            return self.latest_imu_sequence, self.latest_imu_sample

    def _head_control_loop(self):
        """Consume /imu/raw; this controller never opens the IMU I2C device."""
        ahrs = RobustMahonyAHRS(
            Kp=float(self.get_parameter('head_mahony_kp').value),
            Ki=float(self.get_parameter('head_mahony_ki').value),
            min_accel_ms2=float(
                self.get_parameter('head_mahony_min_accel_ms2').value),
            max_accel_ms2=float(
                self.get_parameter('head_mahony_max_accel_ms2').value),
            roll_smoothing=float(
                self.get_parameter('head_roll_smoothing').value),
        )
        sequence = 0
        calibration_max_gyro = max(
            0.0,
            float(self.get_parameter('head_calibration_max_gyro_rad_s').value))
        calibration_max_gyro_std = max(
            0.0,
            float(
                self.get_parameter('head_calibration_max_gyro_std_rad_s').value))
        calibration_max_accel_std = max(
            0.0,
            float(
                self.get_parameter('head_calibration_max_accel_std_ms2').value))
        gyro_bias = None
        initial_accel = None
        while self.head_running.is_set() and gyro_bias is None:
            gyro_sums = [0.0, 0.0, 0.0]
            gyro_square_sums = [0.0, 0.0, 0.0]
            accel_sums = [0.0, 0.0, 0.0]
            accel_square_sums = [0.0, 0.0, 0.0]
            calibration_count = 0
            self.get_logger().info(
                f'waiting for {self.head_calibration_samples} stationary samples on '
                f'{self.head_imu_topic}; keep the current horizontal head still')
            while (
                    self.head_running.is_set()
                    and calibration_count < self.head_calibration_samples):
                sequence, sample = self._wait_for_new_imu(sequence)
                if sample is None:
                    with self.target_roll_lock:
                        self.head_settle_policy.reset_observation_timers()
                        self.head_arrival_brake_policy.reset()
                    if self.step_motor_speed_last != 0:
                        self.board.set_step_motor_speed(0, 100)
                        self.step_motor_speed_last = 0
                    self.get_logger().warning(
                        f'waiting for head IMU topic {self.head_imu_topic}',
                        throttle_duration_sec=2.0,
                    )
                    continue
                _, gyro, accel = sample
                if (
                        not ahrs.accel_is_valid(*accel)
                        or max(abs(value) for value in gyro) > calibration_max_gyro):
                    self.get_logger().warning(
                        'head calibration sample rejected; keep the head level and still',
                        throttle_duration_sec=2.0,
                    )
                    continue
                for index in range(3):
                    gyro_sums[index] += gyro[index]
                    gyro_square_sums[index] += gyro[index] * gyro[index]
                    accel_sums[index] += accel[index]
                    accel_square_sums[index] += accel[index] * accel[index]
                calibration_count += 1

            if not self.head_running.is_set():
                return
            candidate_gyro_bias = tuple(
                value / calibration_count for value in gyro_sums)
            candidate_accel = tuple(
                value / calibration_count for value in accel_sums)
            gyro_std = tuple(math.sqrt(max(
                0.0,
                gyro_square_sums[index] / calibration_count
                - candidate_gyro_bias[index] ** 2,
            )) for index in range(3))
            accel_std = tuple(math.sqrt(max(
                0.0,
                accel_square_sums[index] / calibration_count
                - candidate_accel[index] ** 2,
            )) for index in range(3))
            if (
                    max(gyro_std) <= calibration_max_gyro_std
                    and max(accel_std) <= calibration_max_accel_std):
                gyro_bias = candidate_gyro_bias
                initial_accel = candidate_accel
            else:
                self.get_logger().warning(
                    'head moved during calibration; restarting sample collection: '
                    f'max gyro std={max(gyro_std):.5f} rad/s, '
                    f'max accel std={max(accel_std):.5f} m/s^2')

        if not ahrs.reset_from_accel(*initial_accel):
            self.get_logger().error(
                'head calibration average is outside the gravity window; motor stopped')
            self.board.set_step_motor_speed(0, 100)
            self.step_motor_speed_last = 0
            return
        initial_roll, _, _ = ahrs.get_euler()
        measured_start_roll = initial_roll % 360.0
        # The startup pose is only an AHRS initial observation. It must not
        # become the horizontal reference: the head may be raised or lowered
        # when power is applied. Gravity defines the absolute roll convention,
        # whose horizontal target is configurable (180 deg for this hardware).
        self.horizontal_roll = self.head_horizontal_roll_deg
        with self.target_roll_lock:
            command_offset = self._wrap_degrees(
                self.requested_head_angle - self.head_command_horizontal_deg)
            self.target_roll = (self.horizontal_roll + command_offset) % 360.0
            self.head_settle_policy.set_target(self.requested_head_angle)
            self.head_command_timed_out = False
            self.head_command_deadline.begin()
        self.get_logger().info(
            'head startup leveling initialized: '
            f'measured_roll={measured_start_roll:.3f} deg, '
            f'horizontal_target_roll={self.horizontal_roll:.3f} deg, '
            f'installation_offset={HEAD_HORIZONTAL_ROLL_OFFSET_DEG:+.3f} deg, '
            f'gyro_bias=({gyro_bias[0]:+.6f}, {gyro_bias[1]:+.6f}, {gyro_bias[2]:+.6f}) rad/s')
        self._publish_head_status(
            f'moving;target={self.requested_head_angle:.0f};reason=startup_leveling',
            False)

        # 低通滤波系数
        alpha = float(self.get_parameter('head_filter_alpha').value)
        gx_f, gy_f, gz_f = 0.0, 0.0, 0.0
        ax_f, ay_f, az_f = initial_accel
        last_sample_time = None
        self.get_logger().info('Head stabilization loop started from subscribed IMU data')

        while self.head_running.is_set() and not self.is_shutting_down:
            sequence, sample = self._wait_for_new_imu(sequence)
            if sample is None:
                with self.target_roll_lock:
                    self.head_settle_policy.reset_observation_timers()
                    self.head_arrival_brake_policy.reset()
                    imu_timeout_terminal = self.head_command_deadline.expired()
                    if imu_timeout_terminal:
                        command_elapsed = self.head_command_deadline.elapsed()
                        self.head_command_timed_out = True
                        self.head_command_deadline.finish()
                if self.step_motor_speed_last != 0:
                    self.board.set_step_motor_speed(0, 100)
                    self.step_motor_speed_last = 0
                if imu_timeout_terminal:
                    self._publish_head_status(
                        f'timeout;target={self.requested_head_angle:.0f};'
                        f'elapsed={command_elapsed:.2f};reason=imu_unavailable', False)
                self.get_logger().error(
                    f'head IMU timeout ({self.head_imu_timeout:.3f}s); motor stopped',
                    throttle_duration_sec=2.0,
                )
                continue
            sample_time, gyro, accel = sample
            dt = 1.0 / 208.0 if last_sample_time is None else sample_time - last_sample_time
            last_sample_time = sample_time
            if dt <= 0.0 or dt > 0.1:
                with self.target_roll_lock:
                    self.head_settle_policy.reset_observation_timers()
                    self.head_arrival_brake_policy.reset()
                dt = 1.0 / 208.0

            # 解析并滤波
            raw_gx = gyro[0] - gyro_bias[0]
            raw_gy = gyro[1] - gyro_bias[1]
            raw_gz = gyro[2] - gyro_bias[2]
            gx_f = alpha * raw_gx + (1 - alpha) * gx_f
            gy_f = alpha * raw_gy + (1 - alpha) * gy_f
            gz_f = alpha * raw_gz + (1 - alpha) * gz_f

            ax_f = alpha * accel[0] + (1 - alpha) * ax_f
            ay_f = alpha * accel[1] + (1 - alpha) * ay_f
            az_f = alpha * accel[2] + (1 - alpha) * az_f

            # 2. 姿态更新
            ahrs.update(gx_f, gy_f, gz_f, ax_f, ay_f, az_f, dt)
            roll, pitch, yaw = ahrs.get_euler()
            roll %= 360.0

            # 3. 控制计算。每个周期重新读取，允许用 ros2 param set 在线调节。
            self.kp_angle = max(0.0, float(self.get_parameter('head_kp_angle').value))
            self.kp_rate = max(0.0, float(self.get_parameter('head_kp_rate').value))
            self.k_ff = float(self.get_parameter('head_k_ff').value)
            angle_deadband = max(
                0.0, float(self.get_parameter('head_angle_deadband_deg').value))
            angle_restart = max(
                angle_deadband + 0.01,
                float(self.get_parameter('head_angle_restart_deg').value))
            settle_rate = max(
                0.0, float(self.get_parameter('head_settle_rate_dps').value))
            settle_enter_hold = max(
                0.0,
                float(self.get_parameter('head_settle_enter_hold_sec').value))
            settle_exit_hold = max(
                0.0,
                float(self.get_parameter('head_settle_exit_hold_sec').value))
            motion_restart_rate = max(
                0.0,
                float(self.get_parameter('head_motion_restart_rate_dps').value))
            motion_restart_hold = max(
                0.0,
                float(self.get_parameter('head_motion_restart_hold_sec').value))
            arrival_brake_engage_rate = max(
                0.0,
                float(self.get_parameter(
                    'head_arrival_brake_engage_rate_dps').value))
            arrival_brake_release_rate = max(
                0.0,
                float(self.get_parameter(
                    'head_arrival_brake_release_rate_dps').value))
            motor_deadband = max(
                0.0, float(self.get_parameter('head_motor_deadband').value))
            max_head_speed = max(
                0.0, float(self.get_parameter('head_max_motor_speed').value))
            max_desired_rate = max(
                0.0,
                float(self.get_parameter('head_max_desired_rate_dps').value))
            medium_error = max(
                angle_deadband + 0.01,
                float(self.get_parameter('head_medium_error_deg').value))
            far_error = max(
                medium_error + 0.01,
                float(self.get_parameter('head_far_error_deg').value))
            near_rate = max(
                0.0, float(self.get_parameter('head_near_rate_dps').value))
            medium_rate = max(
                near_rate, float(self.get_parameter('head_medium_rate_dps').value))
            far_rate = max(
                medium_rate, float(self.get_parameter('head_far_rate_dps').value))
            if max_desired_rate > 0.0:
                near_rate = min(near_rate, max_desired_rate)
                medium_rate = max(near_rate, min(medium_rate, max_desired_rate))
                far_rate = max(medium_rate, min(far_rate, max_desired_rate))
            alpha = _clip(
                float(self.get_parameter('head_filter_alpha').value), 0.0, 1.0)

            current_omega = math.degrees(gx_f)             # 绕X轴角速度 (deg/s)
            with self.target_roll_lock:
                # Target snapshot and settle-state update are one atomic
                # operation relative to step_angle_callback. A newly received
                # target cannot be re-latched using the previous target error.
                tgt = self.target_roll
                angle_error = self._wrap_degrees(tgt - roll)
                self.head_settle_policy.configure(
                    angle_deadband,
                    angle_restart,
                    settle_rate,
                    settle_exit_hold,
                    settle_enter_hold,
                    motion_restart_rate if motion_restart_rate > 0.0 else None,
                    motion_restart_hold,
                )
                self.head_arrival_brake_policy.configure(
                    arrival_brake_engage_rate,
                    min(arrival_brake_release_rate,
                        max(0.0, arrival_brake_engage_rate - 0.01)),
                )
                self.head_speed_profile.configure(
                    angle_deadband,
                    medium_error,
                    far_error,
                    near_rate,
                    medium_rate,
                    far_rate,
                )
                was_settled = self.head_settle_policy.settled
                command_was_active = self.head_command_deadline.active
                if was_settled:
                    settled = self.head_settle_policy.update(
                        angle_error, current_omega, now=sample_time)
                    arrival_brake_active = self.head_arrival_brake_policy.update(
                        angle_error,
                        current_omega,
                        angle_deadband,
                        settled,
                    )
                else:
                    arrival_brake_active = self.head_arrival_brake_policy.update(
                        angle_error,
                        current_omega,
                        angle_deadband,
                        False,
                    )
                    settled = self.head_settle_policy.update(
                        angle_error,
                        current_omega,
                        now=sample_time,
                        entry_allowed=not arrival_brake_active,
                    )
                    if settled:
                        self.head_arrival_brake_policy.reset()
                        arrival_brake_active = False
                new_timeout = (
                    not settled and self.head_command_deadline.expired())
                if new_timeout:
                    self.head_command_timed_out = True
                timed_out = self.head_command_timed_out
                command_elapsed = self.head_command_deadline.elapsed()
                if settled or timed_out:
                    self.head_command_deadline.finish()
            if settled != was_settled:
                state = 'stopped in position' if settled else 'correcting'
                self.get_logger().info(
                    f'head control state: {state}, error={angle_error:+.3f} deg, '
                    f'rate={current_omega:+.3f} deg/s')
                if settled and command_was_active:
                    self._publish_head_status(
                        f'succeeded;target={self.requested_head_angle:.0f};'
                        f'error={angle_error:+.2f};rate={current_omega:+.2f}', True)
                elif command_was_active:
                    self._publish_head_status(
                        f'correcting;target={self.requested_head_angle:.0f}', False)

            if new_timeout:
                self.head_arrival_brake_policy.reset()
                # Enforce the deadline independently of the normal motor lock
                # path: a timed-out task must never leave the motor running.
                self.board.set_step_motor_speed(0, 100)
                self.step_motor_speed_last = 0
                self._publish_head_status(
                    f'timeout;target={self.requested_head_angle:.0f};'
                    f'error={angle_error:+.2f};elapsed={command_elapsed:.2f}', False)
                self.get_logger().error(
                    f'head command timed out after {command_elapsed:.2f}s; '
                    f'target={self.requested_head_angle:.0f}, error={angle_error:+.2f} deg')

            # Inside the arrival window command zero desired rate, so the rate
            # loop brakes residual motion before the settled latch is entered.
            des_omega = self.head_speed_profile.desired_rate(angle_error)
            rate_error = des_omega - current_omega

            feedforward = self.k_ff * ax_f                 # 加速度前馈（当前默认为 0）
            motor_cmd = -(self.kp_rate * rate_error + feedforward)

            # A command deadline limits how long clients wait for an arrival
            # result; it must not discard the latest stabilization target.
            # Continue correcting after a timeout so the head holds the most
            # recently received angle until another command replaces it.
            if settled:
                motor_cmd = 0.0

            if arrival_brake_active:
                motor_cmd = self.head_arrival_brake_policy.apply_minimum_command(
                    motor_cmd,
                    current_omega,
                    motor_deadband,
                )

            # 普通小命令仍然归零；只有目标窗内确认仍在运动时，迟滞
            # 制动策略才把命令提升到电机的最小有效速度。
            motor_cmd = _clip(motor_cmd, -max_head_speed, max_head_speed)
            if abs(motor_cmd) < motor_deadband:
                motor_cmd = 0

            diagnostics_time = time.monotonic()
            if diagnostics_time - self.last_head_diagnostics_time >= 0.05:
                # rclpy may invalidate its context before destroy_node() gets a
                # chance to clear head_running (for example, ros2 launch SIGINT).
                # Exit cleanly instead of publishing from this worker thread
                # into an already-invalid ROS context.
                if not rclpy.ok() or self.is_shutting_down:
                    break
                current_offset = self._wrap_degrees(roll - self.horizontal_roll)
                target_offset = self._wrap_degrees(tgt - self.horizontal_roll)
                try:
                    for publisher, value in (
                            (self.head_current_angle_pub, current_offset),
                            (self.head_target_angle_pub, target_offset),
                            (self.head_angle_error_pub, angle_error),
                            (self.head_motor_command_pub, motor_cmd),
                            (self.head_angular_rate_pub, current_omega)):
                        message = Float32()
                        message.data = float(value)
                        publisher.publish(message)
                    in_position_message = Bool()
                    in_position_message.data = settled
                    self.head_in_position_pub.publish(in_position_message)
                    brake_message = Bool()
                    brake_message.data = arrival_brake_active
                    self.head_arrival_brake_pub.publish(brake_message)
                except Exception as exc:
                    if not rclpy.ok() or self.is_shutting_down:
                        break
                    self.get_logger().warn(
                        f'head diagnostics publish error: {exc}')
                self.last_head_diagnostics_time = diagnostics_time

            # 发送步进电机转速指令（负值抬头，正值低头）
            # 104Hz
            
            try:
                if int(motor_cmd) != self.step_motor_speed_last and not self.lock_step_motor:
                    self.board.set_step_motor_speed(int(motor_cmd),100)
                    # self.get_logger().info(f'Head control: roll={roll:.1f}°, error={angle_error:.1f}°, cmd={int(motor_cmd)}')
                    self.step_motor_speed_last = int(motor_cmd)
            except Exception as e:
                self.get_logger().warn(f'set_step_motor_speed error: {e}')

        # 退出时停转
        self.board.set_step_motor_speed(0,100)
        self.step_motor_speed_last = 0
        self.get_logger().info('Head control loop stopped')

    def _start_head_control(self):
        """启动头部控制线程"""
        self.head_running.set()
        self.head_thread = threading.Thread(
            target=self._head_control_loop, daemon=True)
        self.head_thread.start()

    def step_angle_callback(self, msg):
        """接收目标角度指令（UInt16，单位：度）"""
        angle = float(msg.data)
        with self.target_roll_lock:
            previous_angle = self.requested_head_angle
            previous_active = self.head_command_deadline.active
            self.requested_head_angle = angle
            self.head_command_timed_out = False
            changed = self.head_settle_policy.set_target(angle)
            already_settled = self.head_settle_policy.settled
            if already_settled and not changed:
                self.head_command_deadline.finish()
            else:
                self.head_command_deadline.begin()
            if self.horizontal_roll is not None:
                offset = self._wrap_degrees(angle - self.head_command_horizontal_deg)
                self.target_roll = (self.horizontal_roll + offset) % 360.0
            if changed:
                self.head_arrival_brake_policy.reset()
        if changed:
            if previous_active:
                self._publish_head_status(
                    f'preempted;target={previous_angle:.0f};by={angle:.0f}', False)
            self._publish_head_status(f'moving;target={angle:.0f}', False)
            self.get_logger().info(
                f'Set target head command to {angle}°, '
                f'horizontal command={self.head_command_horizontal_deg:.1f}°')
        else:
            # A repeated command is still a new AI request. Force a fresh latched
            # reply instead of leaving the client waiting for an unchanged state.
            self.last_head_status = ''
            state = 'succeeded' if already_settled else 'correcting'
            self._publish_head_status(
                f'{state};target={angle:.0f};unchanged=true', already_settled)

    def _publish_head_status(self, status, aligned):
        """Publish a latched task result for AI clients, like wall alignment."""
        if status == self.last_head_status:
            return
        self.last_head_status = status
        status_message = String()
        status_message.data = status
        self.head_status_pub.publish(status_message)
        aligned_message = Bool()
        aligned_message.data = bool(aligned)
        self.head_aligned_pub.publish(aligned_message)
    

    # ========== 底盘原有方法 ==========
    def get_node_state(self, request, response):
        response.success = True
        response.message = "Node initialized"
        return response
    
    def pub_callback(self):
        """50Hz 发布线程"""
        while self.running and not self.is_shutting_down:
            try:
                if self.reception_enabled and rclpy.ok():
                    self.lock_step_motor = True  # 锁定步进电机，避免干扰头部控制
                    self.pub_motor_speed_data()
                    self.pub_wakeup()
                    self.lock_step_motor = False  # 解锁步进电机
            except Exception:
                pass
            time.sleep(0.02)
    
    def pub_wakeup(self):
        """发布唤醒信号"""
        try:
            wkup_data = self.board.get_wkup()
            if wkup_data == 1 and not self.is_shutting_down and rclpy.ok():
                msg = UInt8()
                msg.data = 1
                self.wakeup_pub.publish(msg)
                self.get_logger().info('🎤 Wakeup triggered!')
        except Exception:
            pass
    
    def set_reception_enabled(self, msg):
        enabled = bool(msg.data)
        self.get_logger().info(f'enable_reception: {enabled}')
        self.reception_enabled = enabled
    
    def _rpm_to_linear_speed(self, left_rpm, right_rpm):
        """
        将左右轮 RPM 转换为线速度和角速度
        :return: (linear_speed, angular_speed)
        """
        # RPM -> m/s
        left_linear = left_rpm * self.rpm_to_ms
        right_linear = right_rpm * self.rpm_to_ms
        
        # 差速驱动正运动学
        linear_speed = (left_linear + right_linear) / 2.0
        angular_speed = (right_linear - left_linear) / self.wheel_track
        
        return linear_speed, angular_speed
    
    def _linear_speed_to_rpm(self, linear_speed, angular_speed):
        """
        将线速度和角速度转换为左右轮 RPM
        :return: (left_rpm, right_rpm)
        """
        # 差速驱动逆运动学
        left_linear = linear_speed - angular_speed * self.wheel_track / 2.0
        right_linear = linear_speed + angular_speed * self.wheel_track / 2.0
        
        # m/s -> RPM
        left_rpm = left_linear * self.ms_to_rpm
        right_rpm = right_linear * self.ms_to_rpm
        
        return left_rpm, right_rpm
    
    def _stop_motors(self):
        """停止电机"""
        try:
            self.board.set_motor_speed(0, 0)
        except:
            pass
        self.target_left_rpm = 0.0
        self.target_right_rpm = 0.0
    
    def cmd_vel_callback(self, msg):
        """
        速度指令回调
        Twist.linear.x: 前进速度 (m/s)，正值前进
        Twist.angular.z: 旋转角速度 (rad/s)，正值左转
        """
        if self.is_shutting_down:
            return
            
        self.last_cmd_vel_time = time.monotonic()
        self.has_cmd_vel = True
        
        linear_speed = float(msg.linear.x)
        angular_speed = float(msg.angular.z)
        
        # 转换为 RPM
        left_rpm, right_rpm = self._linear_speed_to_rpm(linear_speed, angular_speed)
        
        # 限幅到最大转速
        self.target_left_rpm = _clip(left_rpm, -self.max_motor_speed, self.max_motor_speed)
        self.target_right_rpm = _clip(right_rpm, -self.max_motor_speed, self.max_motor_speed)
        
    
    def control_loop(self):
        """控制循环 - 直接发送 RPM 到电机"""
        if self.is_shutting_down:
            return
        
        # 如果没有速度指令或需要停止，直接返回
        if not self.has_cmd_vel:
            return
        
        # 直接发送目标 RPM (set_motor_speed 接收的是 RPM)
        self.lock_step_motor = True  # 锁定步进电机，避免干扰头部控制
        self.board.set_motor_speed(self.target_left_rpm, self.target_right_rpm)
        self.lock_step_motor = False  # 解锁步进电机
    
    def cmd_vel_watchdog(self):
        """速度指令看门狗"""
        if self.is_shutting_down:
            return
            
        if not self.has_cmd_vel:
            return
        
        elapsed = time.monotonic() - self.last_cmd_vel_time
        if elapsed > self.cmd_vel_timeout:
            self.get_logger().warn(f'cmd_vel timeout ({elapsed:.2f}s), stopping motors')
            self._stop_motors()
            self.has_cmd_vel = False
    
    def pub_motor_speed_data(self):
        """发布电机速度数据 (RPM)"""
        try:
            data = self.board.get_motor_speed()
            
            if data is None or self.is_shutting_down:
                return
            
            # get_motor_speed 返回 (left_rpm, right_rpm)
            left_rpm, right_rpm = data
            
            # 发布原始速度 (RPM)
            raw_msg = MotorsState()
            raw_left_msg = MotorState()
            raw_left_msg.id = 1   # 左电机 ID=1
            raw_left_msg.rpm = float(left_rpm)
            raw_right_msg = MotorState()
            raw_right_msg.id = 2   # 右电机 ID=2
            raw_right_msg.rpm = float(right_rpm)
            raw_msg.data = [raw_left_msg, raw_right_msg]
            
            if rclpy.ok():
                self.motor_speed_raw_pub.publish(raw_msg)
            
            # 保存测量值
            self.measured_left_rpm = float(left_rpm)
            self.measured_right_rpm = float(right_rpm)
            
            # 发布转换后的速度 (同样 RPM，不改变)
            speed_msg = MotorsState()
            left_msg = MotorState()
            left_msg.id = 1
            left_msg.rpm = self.measured_left_rpm
            right_msg = MotorState()
            right_msg.id = 2
            right_msg.rpm = self.measured_right_rpm
            speed_msg.data = [left_msg, right_msg]
            
            if rclpy.ok():
                self.motor_speed_pub.publish(speed_msg)
            
        except Exception:
            pass
    
    def destroy_node(self):
        """节点销毁时清理资源"""
        print('\nShutting down node, cleaning up...')
        
        # 设置关闭标志
        self.is_shutting_down = True
        self.running = False

        # 停止头部控制线程
        if self.head_thread and self.head_thread.is_alive():
            self.head_running.clear()
            with self.imu_condition:
                self.imu_condition.notify_all()
            self.head_thread.join(timeout=2.0)
        # 关闭步进电机
        try:
            self.step_motor_speed_last = 0
            self.board.set_step_motor_speed(0, 100)  
        except:
            pass    
        
        # 停止电机
        try:
            self.board.set_motor_speed(0, 0)
            time.sleep(0.1)
        except:
            pass
        
        # 停止接收线程
        try:
            self.board.enable_recv = False
        except:
            pass
        
        # 等待发布线程结束
        if hasattr(self, 'pub_thread') and self.pub_thread.is_alive():
            self.pub_thread.join(timeout=1.0)
        
        # 取消定时器
        try:
            if hasattr(self, 'control_timer'):
                self.control_timer.cancel()
            if hasattr(self, 'watchdog_timer'):
                self.watchdog_timer.cancel()
        except:
            pass
        
        # 关闭串口
        try:
            if hasattr(self.board, 'port') and self.board.port.is_open:
                self.board.port.close()
        except:
            pass
        
        # 调用父类销毁
        super().destroy_node()
        
        print('Cleanup complete')


def main(args=None):
    # 初始化
    rclpy.init(args=args)
    node = None
    
    try:
        node = RosRobotController()
        rclpy.spin(node)
    except KeyboardInterrupt:
        print('\n' + '='*60)
        print('User interrupted (Ctrl+C)')
        print('='*60)
    except Exception as e:
        print(f'Error: {e}')
        import traceback
        traceback.print_exc()
    finally:
        # 清理节点
        if node is not None:
            try:
                node.destroy_node()
            except:
                pass
        
        try:
            if rclpy.ok():
                rclpy.shutdown()
        except:
            pass
        
        print('Node shutdown complete')


if __name__ == '__main__':
    main()
