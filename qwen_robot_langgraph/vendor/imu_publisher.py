#!/usr/bin/env python3
import argparse
import ctypes
import fcntl
import math
import os
import struct
import sys
import time

try:
    from geometry_msgs.msg import Vector3Stamped
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.utilities import remove_ros_args
    from sensor_msgs.msg import Imu
    from std_msgs.msg import Int32MultiArray
except ImportError:
    Vector3Stamped = None
    rclpy = None
    Node = object
    qos_profile_sensor_data = None
    Imu = None
    Int32MultiArray = None
    remove_ros_args = None


I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

WHO_AM_I = 0x0F
CTRL1_XL = 0x10
CTRL2_G = 0x11
CTRL3_C = 0x12
STATUS_REG = 0x1E
OUTX_L_G = 0x22

EXPECTED_WHO_AM_I = {0x6A, 0x6B}

ODR_TO_REG = {
    12.5: 0x10,
    26.0: 0x20,
    52.0: 0x30,
    104.0: 0x40,
    208.0: 0x50,
    416.0: 0x60,
    833.0: 0x70,
}

DEFAULT_GYRO_SCALE_RAD = 0.000152716
DEFAULT_ACCEL_SCALE_G = 0.061 / 1000.0
DEFAULT_ODR_HZ = 208.0
DEFAULT_SAMPLE_PERIOD = 0.02
DEFAULT_CALIBRATION_SAMPLE_PERIOD = 0.005
GRAVITY = 9.80665


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return parsed


class I2CMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class I2CRdwrData(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(I2CMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


class MahonyAHRS:
    def __init__(self, kp, ki):
        self.kp = kp
        self.ki = ki
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.e_int = [0.0, 0.0, 0.0]

    def reset_from_accel(self, ax, ay, az):
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if norm < 1e-9:
            return

        ax /= norm
        ay /= norm
        az /= norm

        # 静止时加速度计测到的是重力反作用方向，roll/pitch 由重力方向确定，yaw 无磁力计约束只能置 0。
        # 公式为 ZYX 欧拉角的重力投影反解：roll=atan2(ay,az)，pitch=atan2(-ax,sqrt(ay^2+az^2))。
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.sqrt(ay * ay + az * az))
        self.set_euler(roll, pitch, 0.0)
        self.e_int = [0.0, 0.0, 0.0]

    def set_euler(self, roll, pitch, yaw):
        half_roll = 0.5 * roll
        half_pitch = 0.5 * pitch
        half_yaw = 0.5 * yaw
        cr = math.cos(half_roll)
        sr = math.sin(half_roll)
        cp = math.cos(half_pitch)
        sp = math.sin(half_pitch)
        cy = math.cos(half_yaw)
        sy = math.sin(half_yaw)

        # ZYX 顺序欧拉角转四元数，q=[w,x,y,z]。
        self.q = [
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ]

    def update(self, gx, gy, gz, ax, ay, az, dt):
        if dt <= 0.0:
            return

        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if norm < 1e-9:
            return

        ax /= norm
        ay /= norm
        az /= norm

        q0, q1, q2, q3 = self.q

        vx = 2.0 * (q1 * q3 - q0 * q2)
        vy = 2.0 * (q0 * q1 + q2 * q3)
        vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3

        ex = ay * vz - az * vy
        ey = az * vx - ax * vz
        ez = ax * vy - ay * vx

        if self.ki > 0.0:
            self.e_int[0] += ex * self.ki * dt
            self.e_int[1] += ey * self.ki * dt
            self.e_int[2] += ez * self.ki * dt
        else:
            self.e_int = [0.0, 0.0, 0.0]

        gx += self.kp * ex + self.e_int[0]
        gy += self.kp * ey + self.e_int[1]
        gz += self.kp * ez + self.e_int[2]

        half_dt = 0.5 * dt
        q0_old, q1_old, q2_old, q3_old = q0, q1, q2, q3

        q0 += (-q1_old * gx - q2_old * gy - q3_old * gz) * half_dt
        q1 += (q0_old * gx + q2_old * gz - q3_old * gy) * half_dt
        q2 += (q0_old * gy - q1_old * gz + q3_old * gx) * half_dt
        q3 += (q0_old * gz + q1_old * gy - q2_old * gx) * half_dt

        norm = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3)
        if norm > 1e-9:
            self.q = [q0 / norm, q1 / norm, q2 / norm, q3 / norm]

    def quaternion_xyzw(self):
        q0, q1, q2, q3 = self.q
        return q1, q2, q3, q0

    @staticmethod
    def accel_euler_deg(ax, ay, az):
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if norm < 1e-9:
            return 0.0, 0.0, 0.0
        ax /= norm
        ay /= norm
        az /= norm
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.sqrt(ay * ay + az * az))
        return math.degrees(roll), math.degrees(pitch), 0.0

    def euler_deg(self):
        q0, q1, q2, q3 = self.q
        # 四元数转欧拉角采用 ROS 常用 ZYX 顺序：roll 绕 X，pitch 绕 Y，yaw 绕 Z。
        # 公式来源于单位四元数到 Tait-Bryan angles 的标准转换。
        roll = math.atan2(
            2.0 * (q2 * q3 + q0 * q1),
            1.0 - 2.0 * (q1 * q1 + q2 * q2),
        )
        pitch_val = -2.0 * q1 * q3 + 2.0 * q0 * q2
        pitch_val = max(-1.0, min(1.0, pitch_val))
        pitch = math.asin(pitch_val)
        yaw = math.atan2(
            2.0 * (q1 * q2 + q0 * q3),
            1.0 - 2.0 * (q2 * q2 + q3 * q3),
        )
        return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


class I2CImu:
    def __init__(self, bus, addr, odr_hz, gyro_scale_rad, accel_scale_g):
        self.path = f"/dev/i2c-{bus}"
        self.addr = addr
        self.odr_hz = odr_hz
        self.gyro_scale_rad = gyro_scale_rad
        self.accel_scale_ms2 = accel_scale_g * GRAVITY
        self.fd = None
        self.lock_fd = None
        self.expected_odr_reg = odr_to_reg_value(self.odr_hz)

    @property
    def kernel_device_name(self):
        return f"{os.path.basename(self.path)}".replace("i2c-", "") + f"-{self.addr:04x}"

    @property
    def kernel_driver_path(self):
        return f"/sys/bus/i2c/devices/{self.kernel_device_name}/driver"

    def acquire_process_lock(self):
        lock_path = f"/run/lock/asm330lhh-i2c-{self.kernel_device_name}.lock"
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o664)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError(
                f"IMU is already owned by another user-space process: {lock_path}"
            ) from exc

    def assert_kernel_unbound(self):
        if os.path.islink(self.kernel_driver_path):
            driver = os.path.realpath(self.kernel_driver_path)
            raise RuntimeError(
                "ASM330LHH is still owned by the kernel driver "
                f"{driver}. Start robot-imu-exclusive.service before this node; "
                "I2C_SLAVE_FORCE is intentionally not used."
            )

    def open(self):
        self.acquire_process_lock()
        self.assert_kernel_unbound()
        try:
            self.fd = os.open(self.path, os.O_RDWR)
            # The normal ioctl is deliberate: fail instead of bypassing an
            # active kernel driver and corrupting the accelerometer stream.
            fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)
            self.configure()
        except Exception:
            self.close()
            raise

    def configure(self):
        # CTRL3_C: BDU=1 锁存高低字节，IF_INC=1 允许从 OUTX_L_G 开始自动递增读取 12 字节。
        self.write_reg(CTRL3_C, 0x44)
        time.sleep(0.01)

        odr_reg = self.expected_odr_reg
        # CTRL2_G/CTRL1_XL: ODR[7:4] 设置输出频率，FS[3:2]=00 对应 gyro ±250dps、accel ±2g。
        self.write_reg(CTRL2_G, odr_reg)
        time.sleep(0.01)
        self.write_reg(CTRL1_XL, odr_reg)
        # 至少等待一个输出周期，公式：T = 1 / ODR，确保后续读取到新样本。
        time.sleep(max(0.01, 1.0 / self.odr_hz))

        who_am_i = self.read_reg(WHO_AM_I)
        if who_am_i not in EXPECTED_WHO_AM_I:
            raise RuntimeError(
                f"unexpected IMU WHO_AM_I=0x{who_am_i:02X} on {self.path} addr=0x{self.addr:02X}"
            )

        self.verify_configuration()

    def verify_configuration(self):
        ctrl1 = self.read_reg(CTRL1_XL)
        ctrl2 = self.read_reg(CTRL2_G)
        ctrl3 = self.read_reg(CTRL3_C)
        expected = self.expected_odr_reg
        # Bits 3:2 are the full-scale selection. Both must remain zero for
        # accel +/-2 g and gyro +/-250 dps; checking only ODR hid conflicts.
        if (ctrl1 & 0xFC) != expected or (ctrl2 & 0xFC) != expected or (ctrl3 & 0x44) != 0x44:
            raise RuntimeError(
                "IMU register ownership violation: "
                f"CTRL1_XL=0x{ctrl1:02X}, CTRL2_G=0x{ctrl2:02X}, CTRL3_C=0x{ctrl3:02X}; "
                f"expected CTRL1/2 masked value 0x{expected:02X} and CTRL3_C BDU+IF_INC"
            )

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    @staticmethod
    def _i16(lo, hi):
        return struct.unpack("<h", bytes([lo, hi]))[0]

    def write_reg(self, reg, value):
        os.write(self.fd, bytes([reg & 0xFF, value & 0xFF]))

    def read_reg(self, reg):
        return self.read_regs(reg, 1)[0]

    def read_regs(self, start_reg, length):
        if length <= 0:
            return b""

        reg_buf = (ctypes.c_uint8 * 1)(start_reg & 0xFF)
        data_buf = (ctypes.c_uint8 * length)()
        msgs = (I2CMsg * 2)(
            I2CMsg(self.addr, 0, 1, reg_buf),
            I2CMsg(self.addr, I2C_M_RD, length, data_buf),
        )
        ioctl_data = I2CRdwrData(msgs, 2)
        fcntl.ioctl(self.fd, I2C_RDWR, ioctl_data)
        return bytes(data_buf)

    def read_sensor_sample(self):
        data = self.read_regs(OUTX_L_G, 12)
        if len(data) != 12:
            raise RuntimeError(f"IMU read length unexpected: {len(data)}")

        counts = tuple(self._i16(data[index], data[index + 1]) for index in range(0, 12, 2))
        gx, gy, gz, ax, ay, az = counts
        gyro = tuple(value * self.gyro_scale_rad for value in (gx, gy, gz))
        accel = tuple(value * self.accel_scale_ms2 for value in (ax, ay, az))
        return counts, gyro, accel

    def read_sensor_units(self):
        _, gyro, accel = self.read_sensor_sample()
        return gyro, accel

    def wait_data_ready(self, timeout_sec):
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            status = self.read_reg(STATUS_REG)
            # STATUS_REG: bit0=XLDA，bit1=GDA，两个都置位时再读取同一批 gyro+accel 输出寄存器。
            if (status & 0x03) == 0x03:
                return True
            time.sleep(0.0005)
        return False


def parse_axis_map(axis_map):
    axes = {"x": 0, "y": 1, "z": 2}
    parts = [part.strip().lower() for part in axis_map.split(",")]
    if len(parts) != 3:
        raise ValueError("axis map must have 3 comma-separated entries, e.g. y,-x,z")

    result = []
    used = set()
    for part in parts:
        sign = 1.0
        if part.startswith("-"):
            sign = -1.0
            part = part[1:]
        elif part.startswith("+"):
            part = part[1:]

        if part not in axes:
            raise ValueError(f"bad axis '{part}' in axis map")
        if part in used:
            raise ValueError("axis map cannot reuse a sensor axis")
        used.add(part)
        result.append((axes[part], sign))
    return result


def map_vec(vec, mapping):
    return tuple(sign * vec[index] for index, sign in mapping)


def clamp(value, limit):
    return max(-limit, min(limit, value))


def accel_norm(accel):
    return math.sqrt(accel[0] * accel[0] + accel[1] * accel[1] + accel[2] * accel[2])


def is_valid_sample(gyro, accel, max_gyro_rad_s, min_accel_norm, max_accel_norm):
    if any(not math.isfinite(value) for value in gyro + accel):
        return False
    if max(abs(gyro[0]), abs(gyro[1]), abs(gyro[2])) > max_gyro_rad_s:
        return False
    norm = accel_norm(accel)
    return min_accel_norm <= norm <= max_accel_norm


def odr_to_reg_value(odr_hz):
    for supported_hz, reg_value in ODR_TO_REG.items():
        if math.isclose(odr_hz, supported_hz, rel_tol=0.0, abs_tol=1e-6):
            return reg_value
    supported = ", ".join(str(value) for value in ODR_TO_REG)
    raise ValueError(f"unsupported ASM330LHH ODR {odr_hz}; supported values: {supported}")


def calibrate_gyro(imu, samples, sample_period, wait_sec, max_abs_rad_s):
    print("开始陀螺仪静态校正，请保持机器人完全静止")
    time.sleep(wait_sec)

    sums = [0.0, 0.0, 0.0]
    valid = 0
    for _ in range(samples):
        try:
            gyro, _ = imu.read_sensor_units()
            if max(abs(gyro[0]), abs(gyro[1]), abs(gyro[2])) <= max_abs_rad_s:
                sums[0] += gyro[0]
                sums[1] += gyro[1]
                sums[2] += gyro[2]
                valid += 1
        except Exception as exc:
            print(f"校正采样失败: {exc}", file=sys.stderr)
        time.sleep(sample_period)

    if valid == 0:
        print("警告：没有有效校正样本，gyro bias 使用 0", file=sys.stderr)
        return (0.0, 0.0, 0.0), valid

    bias = (sums[0] / valid, sums[1] / valid, sums[2] / valid)
    print(
        "gyro_bias(rad/s): "
        f"gx={bias[0]:.6f}, gy={bias[1]:.6f}, gz={bias[2]:.6f}, "
        f"valid={valid}/{samples}"
    )
    return bias, valid


def average_accel(imu, samples, sample_period):
    sums = [0.0, 0.0, 0.0]
    valid = 0
    for _ in range(samples):
        try:
            _, accel = imu.read_sensor_units()
            sums[0] += accel[0]
            sums[1] += accel[1]
            sums[2] += accel[2]
            valid += 1
        except Exception as exc:
            print(f"加速度初始化采样失败: {exc}", file=sys.stderr)
        time.sleep(sample_period)

    if valid == 0:
        return None

    return (sums[0] / valid, sums[1] / valid, sums[2] / valid)


class ImuCartographerNode(Node):
    def __init__(self, args):
        super().__init__("imu_cartographer_publisher")
        self.args = args
        self.mapping = parse_axis_map(args.axis_map)
        self.imu = I2CImu(args.i2c_bus, args.device_addr, args.odr_hz, args.gyro_scale_rad, args.accel_scale_g)
        self.imu.open()

        self.bias, _ = calibrate_gyro(
            self.imu,
            args.calibration_samples,
            args.calibration_sample_period,
            args.calibration_wait_sec,
            args.calibration_max_gyro_rad_s,
        )

        self.ahrs = MahonyAHRS(args.mahony_kp, args.mahony_ki)
        accel_init = average_accel(
            self.imu,
            args.initial_accel_samples,
            args.calibration_sample_period,
        )
        if accel_init is not None:
            accel_robot_init = map_vec(accel_init, self.mapping)
            self.ahrs.reset_from_accel(*accel_robot_init)
            roll, pitch, yaw = self.ahrs.euler_deg()
            self.get_logger().info(
                f"initialized IMU roll/pitch from accel: roll={roll:.2f}deg, pitch={pitch:.2f}deg, yaw={yaw:.2f}deg"
            )
        # SensorDataQoS is KEEP_LAST/VOLATILE/BEST_EFFORT. IMU samples are
        # time-sensitive, so dropping an old sample is preferable to blocking
        # the publisher while a subscriber catches up.
        sensor_qos = qos_profile_sensor_data
        self.pub = self.create_publisher(Imu, args.topic, sensor_qos)
        self.raw_pub = self.create_publisher(Imu, args.raw_topic, sensor_qos)
        self.raw_counts_pub = self.create_publisher(
            Int32MultiArray, args.raw_counts_topic, sensor_qos)
        self.euler_pub = (
            self.create_publisher(Vector3Stamped, args.euler_topic, sensor_qos)
            if args.publish_euler
            else None
        )
        self.last_time = time.monotonic()
        self.filtered_gyro = [0.0, 0.0, 0.0]
        self.filtered_accel = [0.0, 0.0, 0.0]
        self.rejected_samples = 0
        self.last_configuration_check = time.monotonic()
        self.ownership_fault = False
        self.timer = self.create_timer(args.sample_period, self.publish_once)

        self.get_logger().info(
            f"publishing Cartographer IMU: topic={args.topic}, frame={args.frame_id}, "
            f"rate={1.0 / args.sample_period:.1f}Hz, qos=BEST_EFFORT, "
            f"axis_map={args.axis_map}, i2c={self.imu.path}, addr=0x{args.device_addr:02X}"
        )
        self.get_logger().info(
            f"publishing exclusive raw IMU: topic={args.raw_topic}, "
            f"counts_topic={args.raw_counts_topic}, frame={args.raw_frame_id}"
        )
        if self.euler_pub is not None:
            self.get_logger().info(f"publishing IMU Euler angles in degrees: topic={args.euler_topic}")

    def publish_once(self):
        try:
            if self.ownership_fault:
                return
            monotonic_now = time.monotonic()
            if monotonic_now - self.last_configuration_check >= self.args.configuration_check_period:
                try:
                    self.imu.assert_kernel_unbound()
                    self.imu.verify_configuration()
                except Exception as exc:
                    self.ownership_fault = True
                    self.get_logger().error(
                        f"stopping IMU publication after ownership/configuration failure: {exc}"
                    )
                    return
                self.last_configuration_check = monotonic_now

            if self.args.wait_data_ready:
                self.imu.wait_data_ready(self.args.data_ready_timeout_sec)

            counts, raw_gyro_sensor, accel_sensor = self.imu.read_sensor_sample()
            stamp = self.get_clock().now().to_msg()

            raw_msg = Imu()
            raw_msg.header.stamp = stamp
            raw_msg.header.frame_id = self.args.raw_frame_id
            raw_msg.orientation_covariance[0] = -1.0
            raw_msg.angular_velocity.x, raw_msg.angular_velocity.y, raw_msg.angular_velocity.z = raw_gyro_sensor
            raw_msg.linear_acceleration.x, raw_msg.linear_acceleration.y, raw_msg.linear_acceleration.z = accel_sensor
            self.raw_pub.publish(raw_msg)

            counts_msg = Int32MultiArray()
            counts_msg.data = list(counts)
            self.raw_counts_pub.publish(counts_msg)

            gyro_sensor = tuple(raw_gyro_sensor[i] - self.bias[i] for i in range(3))

            saturated = any(abs(value) >= self.args.reject_saturated_count for value in counts)
            if saturated or not is_valid_sample(
                gyro_sensor,
                accel_sensor,
                self.args.reject_max_gyro_rad_s,
                self.args.reject_min_accel_norm,
                self.args.reject_max_accel_norm,
            ):
                self.rejected_samples += 1
                self.get_logger().warn(
                    "rejected abnormal IMU sample: "
                    f"gyro=({gyro_sensor[0]:.4f}, {gyro_sensor[1]:.4f}, {gyro_sensor[2]:.4f}) rad/s, "
                    f"accel=({accel_sensor[0]:.3f}, {accel_sensor[1]:.3f}, {accel_sensor[2]:.3f}) m/s^2, "
                    f"|a|={accel_norm(accel_sensor):.3f}, saturated={saturated}, "
                    f"counts={counts}, rejected={self.rejected_samples}",
                    throttle_duration_sec=2.0,
                )
                return

            gyro_robot = map_vec(gyro_sensor, self.mapping)
            accel_robot = map_vec(accel_sensor, self.mapping)

            gyro_robot = tuple(clamp(v, self.args.max_gyro_rad_s) for v in gyro_robot)
            accel_robot = tuple(clamp(v, self.args.max_accel_m_s2) for v in accel_robot)

            alpha = self.args.low_pass_alpha
            for i in range(3):
                self.filtered_gyro[i] = alpha * gyro_robot[i] + (1.0 - alpha) * self.filtered_gyro[i]
                self.filtered_accel[i] = alpha * accel_robot[i] + (1.0 - alpha) * self.filtered_accel[i]

            now = time.monotonic()
            dt = now - self.last_time
            self.last_time = now
            self.ahrs.update(
                self.filtered_gyro[0],
                self.filtered_gyro[1],
                self.filtered_gyro[2],
                self.filtered_accel[0],
                self.filtered_accel[1],
                self.filtered_accel[2],
                dt,
            )

            msg = Imu()
            msg.header.stamp = stamp
            msg.header.frame_id = self.args.frame_id

            qx, qy, qz, qw = self.ahrs.quaternion_xyzw()
            msg.orientation.x = qx if self.args.publish_orientation else 0.0
            msg.orientation.y = qy if self.args.publish_orientation else 0.0
            msg.orientation.z = qz if self.args.publish_orientation else 0.0
            msg.orientation.w = qw if self.args.publish_orientation else 1.0
            msg.orientation_covariance = (
                [self.args.orientation_covariance, 0.0, 0.0,
                 0.0, self.args.orientation_covariance, 0.0,
                 0.0, 0.0, self.args.orientation_covariance]
                if self.args.publish_orientation
                else [-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            )

            msg.angular_velocity.x = self.filtered_gyro[0]
            msg.angular_velocity.y = self.filtered_gyro[1]
            msg.angular_velocity.z = self.filtered_gyro[2]
            msg.angular_velocity_covariance = [
                self.args.angular_velocity_covariance, 0.0, 0.0,
                0.0, self.args.angular_velocity_covariance, 0.0,
                0.0, 0.0, self.args.angular_velocity_covariance,
            ]

            msg.linear_acceleration.x = self.filtered_accel[0]
            msg.linear_acceleration.y = self.filtered_accel[1]
            msg.linear_acceleration.z = self.filtered_accel[2]
            msg.linear_acceleration_covariance = [
                self.args.linear_acceleration_covariance, 0.0, 0.0,
                0.0, self.args.linear_acceleration_covariance, 0.0,
                0.0, 0.0, self.args.linear_acceleration_covariance,
            ]

            self.pub.publish(msg)
            roll, pitch, yaw = self.ahrs.euler_deg()
            if self.args.euler_from_accel:
                roll, pitch, yaw = MahonyAHRS.accel_euler_deg(
                    self.filtered_accel[0],
                    self.filtered_accel[1],
                    self.filtered_accel[2],
                )
            elif self.args.euler_yaw_zero:
                yaw = 0.0

            if self.euler_pub is not None:
                euler_msg = Vector3Stamped()
                euler_msg.header = msg.header
                euler_msg.vector.x = roll
                euler_msg.vector.y = pitch
                euler_msg.vector.z = yaw
                self.euler_pub.publish(euler_msg)

            if self.args.print_debug:
                filtered_accel_norm = math.sqrt(sum(v * v for v in self.filtered_accel))
                print(
                    f"roll={roll:8.2f} pitch={pitch:8.2f} yaw={yaw:8.2f} | "
                    f"gyro=({self.filtered_gyro[0]: .4f}, {self.filtered_gyro[1]: .4f}, {self.filtered_gyro[2]: .4f}) rad/s | "
                    f"accel=({self.filtered_accel[0]: .3f}, {self.filtered_accel[1]: .3f}, {self.filtered_accel[2]: .3f}) "
                    f"| |a|={filtered_accel_norm:.3f}",
                    end="\r",
                    flush=True,
                )
        except Exception as exc:
            self.get_logger().warn(f"IMU publish failed: {exc}")

    def destroy_node(self):
        try:
            self.imu.close()
        finally:
            super().destroy_node()


def print_only_loop(args):
    mapping = parse_axis_map(args.axis_map)
    imu = I2CImu(args.i2c_bus, args.device_addr, args.odr_hz, args.gyro_scale_rad, args.accel_scale_g)
    imu.open()
    try:
        bias, _ = calibrate_gyro(
            imu,
            args.calibration_samples,
            args.calibration_sample_period,
            args.calibration_wait_sec,
            args.calibration_max_gyro_rad_s,
        )

        ahrs = MahonyAHRS(args.mahony_kp, args.mahony_ki)
        filtered_gyro = [0.0, 0.0, 0.0]
        filtered_accel = [0.0, 0.0, 0.0]
        last_time = time.monotonic()

        print("开始输出机器人坐标系下的 IMU 数据，Ctrl-C 停止")
        print("默认映射: robot_x=-sensor_y, robot_y=-sensor_x, robot_z=-sensor_z")
        while True:
            if args.wait_data_ready:
                imu.wait_data_ready(args.data_ready_timeout_sec)
            gyro_sensor, accel_sensor = imu.read_sensor_units()
            gyro_sensor = tuple(gyro_sensor[i] - bias[i] for i in range(3))
            gyro_robot = map_vec(gyro_sensor, mapping)
            accel_robot = map_vec(accel_sensor, mapping)

            alpha = args.low_pass_alpha
            for i in range(3):
                filtered_gyro[i] = alpha * gyro_robot[i] + (1.0 - alpha) * filtered_gyro[i]
                filtered_accel[i] = alpha * accel_robot[i] + (1.0 - alpha) * filtered_accel[i]

            now = time.monotonic()
            dt = now - last_time
            last_time = now
            ahrs.update(
                filtered_gyro[0],
                filtered_gyro[1],
                filtered_gyro[2],
                filtered_accel[0],
                filtered_accel[1],
                filtered_accel[2],
                dt,
            )
            roll, pitch, yaw = ahrs.euler_deg()
            filtered_accel_norm = math.sqrt(sum(v * v for v in filtered_accel))
            print(
                f"roll={roll:8.2f} pitch={pitch:8.2f} yaw={yaw:8.2f} | "
                f"gyro=({filtered_gyro[0]: .4f}, {filtered_gyro[1]: .4f}, {filtered_gyro[2]: .4f}) rad/s | "
                f"accel=({filtered_accel[0]: .3f}, {filtered_accel[1]: .3f}, {filtered_accel[2]: .3f}) m/s^2 | "
                f"|a|={filtered_accel_norm:.3f}",
                end="\r",
                flush=True,
            )
            time.sleep(args.sample_period)
    finally:
        imu.close()


def diagnose_loop(args):
    imu = I2CImu(args.i2c_bus, args.device_addr, args.odr_hz, args.gyro_scale_rad, args.accel_scale_g)
    imu.open()
    try:
        print("开始输出 IMU 原始传感器坐标系数据，Ctrl-C 停止")
        while True:
            if args.wait_data_ready:
                imu.wait_data_ready(args.data_ready_timeout_sec)
            gyro_sensor, accel_sensor = imu.read_sensor_units()
            raw_accel_norm = math.sqrt(sum(v * v for v in accel_sensor))
            valid = is_valid_sample(
                gyro_sensor,
                accel_sensor,
                args.reject_max_gyro_rad_s,
                args.reject_min_accel_norm,
                args.reject_max_accel_norm,
            )
            print(
                f"gyro_sensor=({gyro_sensor[0]: .5f}, {gyro_sensor[1]: .5f}, {gyro_sensor[2]: .5f}) rad/s | "
                f"accel_sensor=({accel_sensor[0]: .5f}, {accel_sensor[1]: .5f}, {accel_sensor[2]: .5f}) m/s^2 | "
                f"|a|={raw_accel_norm:.5f} | valid={valid}",
                flush=True,
            )
            time.sleep(args.sample_period)
    finally:
        imu.close()


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Read ASM330LHH over I2C and publish Cartographer-compatible sensor_msgs/Imu."
    )
    parser.add_argument("--topic", default="/imu", help="ROS topic for sensor_msgs/Imu")
    parser.add_argument("--raw-topic", default="/imu/raw", help="Unmapped, unfiltered sensor-frame Imu topic")
    parser.add_argument("--raw-counts-topic", default="/imu/raw_counts", help="Six signed register counts: gx,gy,gz,ax,ay,az")
    parser.add_argument("--euler-topic", default="/imu/euler_deg", help="ROS topic for Euler angles in degrees")
    parser.add_argument("--frame-id", default="imu_link", help="IMU frame_id; match Cartographer tracking_frame")
    parser.add_argument("--raw-frame-id", default="imu_sensor_link", help="Physical ASM330LHH sensor coordinate frame")
    parser.add_argument("--i2c-bus", type=int, default=4)
    parser.add_argument("--device-addr", type=lambda value: int(value, 0), default=0x6A)
    parser.add_argument(
        "--sample-period",
        type=positive_float,
        default=DEFAULT_SAMPLE_PERIOD,
        help="ROS publication period in seconds. Default 0.02 = 50 Hz.",
    )
    parser.add_argument(
        "--calibration-sample-period",
        type=positive_float,
        default=DEFAULT_CALIBRATION_SAMPLE_PERIOD,
        help="Calibration read period in seconds; independent of ROS publication rate.",
    )
    parser.add_argument("--odr-hz", type=float, default=DEFAULT_ODR_HZ, help="ASM330LHH output data rate. Use 208 for ~200Hz reads.")
    parser.add_argument("--calibration-samples", type=int, default=1000)
    parser.add_argument("--calibration-wait-sec", type=float, default=2.0)
    parser.add_argument("--calibration-max-gyro-rad-s", type=float, default=0.35)
    parser.add_argument("--gyro-scale-rad", type=float, default=DEFAULT_GYRO_SCALE_RAD)
    parser.add_argument("--accel-scale-g", type=float, default=DEFAULT_ACCEL_SCALE_G)
    parser.add_argument("--low-pass-alpha", type=float, default=0.35)
    parser.add_argument("--max-gyro-rad-s", type=float, default=4.0)
    parser.add_argument("--max-accel-m-s2", type=float, default=30.0)
    parser.add_argument("--wait-data-ready", action="store_true")
    parser.add_argument("--data-ready-timeout-sec", type=float, default=0.01)
    parser.add_argument("--reject-min-accel-norm", type=float, default=6.0)
    parser.add_argument("--reject-max-accel-norm", type=float, default=13.0)
    parser.add_argument("--reject-max-gyro-rad-s", type=float, default=8.0)
    parser.add_argument("--reject-saturated-count", type=int, default=32760)
    parser.add_argument("--configuration-check-period", type=float, default=1.0)
    parser.add_argument(
        "--axis-map",
        default="-y,-x,-z",
        help=(
            "robot axes expressed as sensor axes. Default -y,-x,-z means "
            "robot_x=-sensor_y, robot_y=-sensor_x, robot_z=-sensor_z."
        ),
    )
    parser.add_argument("--mahony-kp", type=float, default=4.2)
    parser.add_argument("--mahony-ki", type=float, default=0.0)
    parser.add_argument("--angular-velocity-covariance", type=float, default=0.02)
    parser.add_argument("--linear-acceleration-covariance", type=float, default=0.08)
    parser.add_argument("--orientation-covariance", type=float, default=0.05)
    parser.add_argument(
        "--publish-orientation",
        action="store_true",
        help="Publish Mahony orientation. Leave disabled for Cartographer to avoid feeding drifting yaw.",
    )
    parser.add_argument("--initial-accel-samples", type=int, default=50)
    parser.add_argument(
        "--euler-from-accel",
        action="store_true",
        help="Publish Euler roll/pitch directly from accelerometer and force yaw to 0.",
    )
    parser.add_argument(
        "--euler-yaw-zero",
        action="store_true",
        help="Force Euler yaw output to 0 because ASM330LHH has no magnetometer yaw reference.",
    )
    parser.add_argument("--print-debug", action="store_true")
    parser.add_argument("--publish-euler", action="store_true", help="Publish Euler angles as geometry_msgs/Vector3Stamped in degrees.")
    parser.add_argument("--print-only", action="store_true", help="Do not use ROS; only print mapped values.")
    parser.add_argument("--diagnose-raw", action="store_true", help="Print raw sensor-frame values without mapping/filtering.")
    return parser


def main():
    argv = sys.argv[1:]
    if remove_ros_args is not None:
        argv = remove_ros_args(args=argv)
    args = build_arg_parser().parse_args(argv)

    if args.diagnose_raw:
        diagnose_loop(args)
        return

    if args.print_only:
        print_only_loop(args)
        return

    if rclpy is None:
        print("rclpy/sensor_msgs not available. Source ROS 2 first, or run with --print-only.", file=sys.stderr)
        sys.exit(2)

    rclpy.init()
    node = ImuCartographerNode(args)

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
