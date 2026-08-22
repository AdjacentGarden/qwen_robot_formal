#!/usr/bin/env python3
"""Isolated ROS graph fixture for the read-only health monitor test."""

import math
import time

import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.action import ActionServer
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Imu, LaserScan
from tf2_ros import TransformBroadcaster


class FakeHealthyGraph(Node):
    def __init__(self) -> None:
        super().__init__("qwen_health_monitor_test_fixture")
        sensor_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT)
        map_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.scan_raw = self.create_publisher(LaserScan, "/scan_raw", sensor_qos)
        self.scan = self.create_publisher(LaserScan, "/scan", sensor_qos)
        self.imu = self.create_publisher(Imu, "/imu", sensor_qos)
        self.odom = self.create_publisher(Odometry, "/odom", 10)
        self.map = self.create_publisher(OccupancyGrid, "/map", map_qos)
        self.create_subscription(Twist, "/cmd_vel", lambda _msg: None, 10)
        self.tf = TransformBroadcaster(self)
        self._services_for_test = [
            self.create_service(GetState, f"/{name}/get_state", self.get_active_state)
            for name in ("map_server", "planner_server", "bt_navigator")
        ]
        self._actions_for_test = [
            ActionServer(self, ComputePathToPose, "/compute_path_to_pose", self.compute_path),
            ActionServer(self, NavigateToPose, "/navigate_to_pose", self.navigate),
        ]
        self.create_timer(0.10, self.publish_samples)

    @staticmethod
    def get_active_state(_request, response):
        response.current_state.id = State.PRIMARY_STATE_ACTIVE
        response.current_state.label = "active"
        return response

    @staticmethod
    async def compute_path(_goal):
        return ComputePathToPose.Result()

    @staticmethod
    async def navigate(_goal):
        return NavigateToPose.Result()

    def publish_samples(self) -> None:
        stamp = self.get_clock().now().to_msg()
        scan = LaserScan()
        scan.header.stamp = stamp
        scan.header.frame_id = "laser"
        scan.angle_min = -math.pi
        scan.angle_max = math.pi
        scan.angle_increment = math.pi / 180.0
        scan.range_min = 0.05
        scan.range_max = 10.0
        scan.ranges = [2.0] * 360
        self.scan_raw.publish(scan)
        self.scan.publish(scan)
        imu = Imu()
        imu.header.stamp = stamp
        self.imu.publish(imu)
        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_footprint"
        self.odom.publish(odom)
        occupancy = OccupancyGrid()
        occupancy.header.stamp = stamp
        occupancy.header.frame_id = "map"
        occupancy.info.resolution = 0.05
        occupancy.info.width = 2
        occupancy.info.height = 2
        occupancy.data = [0, 0, 0, 0]
        self.map.publish(occupancy)
        transform = TransformStamped()
        transform.header.stamp = stamp
        transform.header.frame_id = "map"
        transform.child_frame_id = "base_footprint"
        transform.transform.rotation.w = 1.0
        self.tf.sendTransform(transform)


def main() -> None:
    rclpy.init()
    node = FakeHealthyGraph()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
