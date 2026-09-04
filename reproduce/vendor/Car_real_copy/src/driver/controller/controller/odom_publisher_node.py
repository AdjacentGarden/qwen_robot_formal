#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import math
import time
import rclpy
import signal
import threading
from rclpy.node import Node
from std_srvs.srv import Trigger
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Pose2D, Pose, Twist, PoseWithCovarianceStamped, TransformStamped
from ros_robot_controller_msgs.msg import MotorsState

# ==================== 协方差常量定义 ====================
POSE_COV_X = 0.02
POSE_COV_Y = 0.05
POSE_COV_Z = 1e6

POSE_COV_ROLL = 1e6
POSE_COV_PITCH = 1e6
POSE_COV_YAW = 0.05

TWIST_COV_VX = 0.02
TWIST_COV_VY = 1e6
TWIST_COV_VZ = 1e6

TWIST_COV_VROLL = 1e6
TWIST_COV_VPITCH = 1e6
TWIST_COV_VYAW = 0.03

# 构建完整的 6x6 协方差矩阵函数
def make_pose_covariance(x, y, z, roll, pitch, yaw):
    """构建 6x6 位置协方差矩阵 (行主序)"""
    return [
        float(x), 0.0, 0.0, 0.0, 0.0, 0.0,
        0.0, float(y), 0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, float(z), 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, float(roll), 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0, float(pitch), 0.0,
        0.0, 0.0, 0.0, 0.0, 0.0, float(yaw)
    ]

def make_twist_covariance(vx, vy, vz, vroll, vpitch, vyaw):
    """构建 6x6 速度协方差矩阵 (行主序)"""
    return [
        float(vx), 0.0, 0.0, 0.0, 0.0, 0.0,
        0.0, float(vy), 0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, float(vz), 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, float(vroll), 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0, float(vpitch), 0.0,
        0.0, 0.0, 0.0, 0.0, 0.0, float(vyaw)
    ]

# 定义各个协方差矩阵
ODOM_POSE_COVARIANCE = make_pose_covariance(
    POSE_COV_X, POSE_COV_Y, POSE_COV_Z,
    POSE_COV_ROLL, POSE_COV_PITCH, POSE_COV_YAW
)

ODOM_POSE_COVARIANCE_STOP = ODOM_POSE_COVARIANCE

ODOM_TWIST_COVARIANCE = make_twist_covariance(
    TWIST_COV_VX, TWIST_COV_VY, TWIST_COV_VZ,
    TWIST_COV_VROLL, TWIST_COV_VPITCH, TWIST_COV_VYAW
)

ODOM_TWIST_COVARIANCE_STOP = ODOM_TWIST_COVARIANCE

# 验证协方差矩阵长度
assert len(ODOM_POSE_COVARIANCE) == 36, f"Pose covariance length is {len(ODOM_POSE_COVARIANCE)}, should be 36"
assert len(ODOM_POSE_COVARIANCE_STOP) == 36, f"Pose stop covariance length is {len(ODOM_POSE_COVARIANCE_STOP)}, should be 36"
assert len(ODOM_TWIST_COVARIANCE) == 36, f"Twist covariance length is {len(ODOM_TWIST_COVARIANCE)}, should be 36"
assert len(ODOM_TWIST_COVARIANCE_STOP) == 36, f"Twist stop covariance length is {len(ODOM_TWIST_COVARIANCE_STOP)}, should be 36"



def rpy2qua(roll, pitch, yaw):
    cy = math.cos(yaw*0.5)
    sy = math.sin(yaw*0.5)
    cp = math.cos(pitch*0.5)
    sp = math.sin(pitch*0.5)
    cr = math.cos(roll * 0.5)
    sr = math.sin(roll * 0.5)
    
    q = Pose()
    q.orientation.w = cy * cp * cr + sy * sp * sr
    q.orientation.x = cy * cp * sr - sy * sp * cr
    q.orientation.y = sy * cp * sr + cy * sp * cr
    q.orientation.z = sy * cp * cr - cy * sp * sr
    return q.orientation


def qua2rpy(x, y, z, w):
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(2 * (w * y - x * z))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (z * z + y * y))
    return roll, pitch, yaw


class Controller(Node):
    
    def __init__(self, name):
        rclpy.init()
        super().__init__(name)

        self.x = 0.0
        self.y = 0.0
        self.linear_x = 0.0
        self.linear_y = 0.0
        self.angular_z = 0.0
        self.cmd_linear_x = 0.0
        self.cmd_angular_z = 0.0
        self.pose_yaw = 0
        self.last_time = None
        self.current_time = None
        self.last_motor_speed_time = 0.0
        signal.signal(signal.SIGINT, self.shutdown)

        # 声明参数
        self.declare_parameter('pub_odom_topic', True)
        self.declare_parameter('base_frame_id', 'base_footprint')
        self.declare_parameter('odom_frame_id', 'odom')
        self.declare_parameter('linear_correction_factor', 1.00)
        self.declare_parameter('angular_correction_factor', 1.00)
        self.declare_parameter('machine_type', os.environ.get('MACHINE_TYPE', 'default'))
        self.declare_parameter('use_wheel_speed_feedback', True)
        self.declare_parameter('motor_speed_topic', '/motor_speed')
        self.declare_parameter('wheel_diameter', 0.07)
        self.declare_parameter('wheel_track', 0.2948)
        self.declare_parameter('motor_speed_unit', 'rpm')
        self.declare_parameter('left_motor_id', 2)
        self.declare_parameter('right_motor_id', 1)
        self.declare_parameter('motor_speed_timeout', 0.2)
        self.declare_parameter('wheel_linear_direction', -1.0)
        # 【新增】航向方向修正参数
        self.declare_parameter('angular_direction', 1.0)
        
        self.pub_odom_topic = self.get_parameter('pub_odom_topic').value
        self.base_frame_id = self.get_parameter('base_frame_id').value
        self.odom_frame_id = self.get_parameter('odom_frame_id').value
        
        self.linear_factor = self.get_parameter('linear_correction_factor').value
        self.angular_factor = self.get_parameter('angular_correction_factor').value
        self.use_wheel_speed_feedback = bool(self.get_parameter('use_wheel_speed_feedback').value)
        self.motor_speed_topic = str(self.get_parameter('motor_speed_topic').value)
        self.wheel_diameter = float(self.get_parameter('wheel_diameter').value)
        self.wheel_track = float(self.get_parameter('wheel_track').value)
        self.motor_speed_unit = str(self.get_parameter('motor_speed_unit').value).lower()
        self.left_motor_id = int(self.get_parameter('left_motor_id').value)
        self.right_motor_id = int(self.get_parameter('right_motor_id').value)
        self.motor_speed_timeout = float(self.get_parameter('motor_speed_timeout').value)
        self.wheel_linear_direction = float(self.get_parameter('wheel_linear_direction').value)
        self.angular_direction = float(self.get_parameter('angular_direction').value)  # 【新增】

        self.clock = self.get_clock() 
        if self.pub_odom_topic:
            self.odom = Odometry()
            self.odom.header.frame_id = self.odom_frame_id
            self.odom.child_frame_id = self.base_frame_id
            
            self.odom.pose.covariance = ODOM_POSE_COVARIANCE
            self.odom.twist.covariance = ODOM_TWIST_COVARIANCE
            
            self.odom_pub = self.create_publisher(Odometry, 'odom_raw', 1)
            self.dt = 1.0/50.0

            threading.Thread(target=self.cal_odom_fun, daemon=True).start()
        
        self.get_logger().info('\033[1;32mLinear factor: %f, Angular factor: %f\033[0m' % 
                               (self.linear_factor, self.angular_factor))
        self.get_logger().info('\033[1;32mAngular direction: %f\033[0m' % self.angular_direction)
        
        self.pose_pub = self.create_publisher(PoseWithCovarianceStamped, 'set_pose', 1)
        self.create_subscription(Pose2D, 'set_odom', self.set_odom, 1)
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 1)
        self.create_subscription(MotorsState, self.motor_speed_topic, self.motor_speed_callback, 10)
        self.create_service(Trigger, 'controller/load_calibrate_param', self.load_calibrate_param)

        self.create_service(Trigger, '~/init_finish', self.get_node_state)
        self.get_logger().info('\033[1;32mNode started successfully\033[0m')

    def speed_to_rps(self, speed):
        if self.motor_speed_unit == 'rpm':
            return speed / 60.0
        if self.motor_speed_unit == 'rps':
            return speed

        if not self.warned_invalid_motor_speed_unit:
            self.get_logger().warn(
                'unsupported motor_speed_unit "%s", treating feedback as rpm' % self.motor_speed_unit
            )
            self.warned_invalid_motor_speed_unit = True
        return speed / 60.0

    def get_node_state(self, request, response):
        response.success = True
        return response

    def shutdown(self, signum, frame):
        self.get_logger().info('\033[1;32mShutting down...\033[0m')
        try:
            self.destroy_node()
        except:
            pass

    def load_calibrate_param(self, request, response):
        self.linear_factor = self.get_parameter('~linear_correction_factor').value or 1.00
        self.angular_factor = self.get_parameter('~angular_correction_factor').value or 1.00
        self.get_logger().info('\033[1;32mLoaded calibrate param\033[0m')

        response.success = True
        return response

    def set_odom(self, msg):
        self.odom = Odometry()
        self.odom.header.frame_id = self.odom_frame_id
        self.odom.child_frame_id = self.base_frame_id
        
        self.odom.pose.covariance = ODOM_POSE_COVARIANCE
        self.odom.twist.covariance = ODOM_TWIST_COVARIANCE
        self.odom.pose.pose.position.x = msg.x
        self.odom.pose.pose.position.y = msg.y
        self.pose_yaw = msg.theta
        self.odom.pose.pose.orientation = rpy2qua(0, 0, self.pose_yaw)
        
        self.linear_x = 0
        self.linear_y = 0
        self.angular_z = 0
        
        pose = PoseWithCovarianceStamped()
        pose.header.frame_id = self.odom_frame_id
        pose.header.stamp = self.clock.now().to_msg()
        pose.pose.pose = self.odom.pose.pose
        pose.pose.covariance = ODOM_POSE_COVARIANCE
        self.pose_pub.publish(pose)

    def cmd_vel_callback(self, msg):
        self.cmd_linear_x = msg.linear.x
        self.cmd_angular_z = msg.angular.z
        if not self.use_wheel_speed_feedback:
            self.linear_x = self.cmd_linear_x
            self.linear_y = 0.0
            self.angular_z = self.cmd_angular_z

    def motor_speed_callback(self, msg):
        left_speed = None
        right_speed = None
        for motor in msg.data:
            speed_value = getattr(motor, 'rpm', None)
            if speed_value is None:
                speed_value = getattr(motor, 'rps', None)
            if motor.id == self.left_motor_id:
                if speed_value is not None:
                    left_speed = float(speed_value)
            elif motor.id == self.right_motor_id:
                if speed_value is not None:
                    right_speed = float(speed_value)

        if left_speed is None or right_speed is None:
            return
        if self.wheel_diameter <= 0.0 or self.wheel_track <= 0.0:
            return

        # 轮速反解算
        left_rps = self.speed_to_rps(left_speed)
        right_rps = self.speed_to_rps(right_speed)
        left_linear = math.pi * self.wheel_diameter * left_rps
        right_linear = math.pi * self.wheel_diameter * right_rps
        
        # 线速度
        self.linear_x = self.wheel_linear_direction * (left_linear + right_linear) / 2.0
        self.linear_y = 0.0
        
        # 【修改】角速度计算，添加方向修正
        # 原公式: (left_linear - right_linear) / wheel_track
        # 修改后: 乘以 angular_direction 来修正方向
        raw_angular_z = (right_linear - left_linear) / self.wheel_track  # 使用 right - left
        self.angular_z = self.angular_direction * raw_angular_z
        
        self.last_motor_speed_time = time.time()

    def cal_odom_fun(self):
        while True:
            self.current_time = time.time()
            if self.last_time is None:
                self.dt = 0.0
            else:
                self.dt = self.current_time - self.last_time
            self.odom.header.stamp = self.clock.now().to_msg()

            if self.use_wheel_speed_feedback and self.last_motor_speed_time > 0.0:
                if self.current_time - self.last_motor_speed_time > self.motor_speed_timeout:
                    self.linear_x = 0.0
                    self.linear_y = 0.0
                    self.angular_z = 0.0

            # 里程计积分
            self.x += math.cos(self.pose_yaw) * self.linear_x * self.dt - math.sin(self.pose_yaw) * self.linear_y * self.dt
            self.y += math.sin(self.pose_yaw) * self.linear_x * self.dt + math.cos(self.pose_yaw) * self.linear_y * self.dt

            self.odom.pose.pose.position.x = self.linear_factor * self.x
            self.odom.pose.pose.position.y = self.linear_factor * self.y
            self.odom.pose.pose.position.z = 0.0

            self.pose_yaw += self.angular_factor * self.angular_z * self.dt
            
            # 规范化角度到 [-pi, pi]
            if self.pose_yaw > math.pi:
                self.pose_yaw -= 2.0 * math.pi
            elif self.pose_yaw < -math.pi:
                self.pose_yaw += 2.0 * math.pi

            self.odom.pose.pose.orientation = rpy2qua(0.0, 0.0, self.pose_yaw)
            self.odom.twist.twist.linear.x = self.linear_x
            self.odom.twist.twist.linear.y = self.linear_y
            self.odom.twist.twist.angular.z = self.angular_z

            # 根据运动状态选择协方差
            if abs(self.linear_x) < 0.001 and abs(self.linear_y) < 0.001 and abs(self.angular_z) < 0.001:
                self.odom.pose.covariance = ODOM_POSE_COVARIANCE_STOP
                self.odom.twist.covariance = ODOM_TWIST_COVARIANCE_STOP
            else:
                self.odom.pose.covariance = ODOM_POSE_COVARIANCE
                self.odom.twist.covariance = ODOM_TWIST_COVARIANCE

            self.odom_pub.publish(self.odom)
            self.last_time = self.current_time
            time.sleep(0.02)


def main():
    node = Controller('odom_publisher')
    rclpy.spin(node)


if __name__ == "__main__":
    main()