#!/usr/bin/env python3
"""Exit successfully after Cartographer mapping is genuinely usable."""

import sys
import time

import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from tf2_ros import Buffer, TransformException, TransformListener


class MappingReadyGate(Node):
    def __init__(self):
        super().__init__('mapping_ready_gate')
        self.declare_parameter('timeout_s', 120.0)
        self.declare_parameter('stable_duration_s', 3.0)
        self.last_map = 0.0
        qos = QoSProfile(depth=1)
        qos.reliability = ReliabilityPolicy.RELIABLE
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(OccupancyGrid, '/map', self._on_map, qos)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

    def _on_map(self, msg):
        if msg.info.width > 0 and msg.info.height > 0:
            self.last_map = time.monotonic()

    def ready(self):
        nodes = set(self.get_node_names())
        services = {name for name, _ in self.get_service_names_and_types()}
        try:
            self.tf_buffer.lookup_transform(
                'map', 'odom', rclpy.time.Time())
            tf_ready = True
        except TransformException:
            tf_ready = False
        return (
            '/cartographer_node' in nodes
            and '/cartographer_occupancy_grid_node' in nodes
            and {'/finish_trajectory', '/write_state', '/read_metrics'} <= services
            and time.monotonic() - self.last_map <= 5.0
            and tf_ready
        )


def main():
    rclpy.init()
    node = MappingReadyGate()
    started = time.monotonic()
    stable_since = None
    timeout = float(node.get_parameter('timeout_s').value)
    stable_duration = float(node.get_parameter('stable_duration_s').value)
    try:
        while rclpy.ok() and time.monotonic() - started < timeout:
            rclpy.spin_once(node, timeout_sec=0.2)
            if node.ready():
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_duration:
                    node.get_logger().info(
                        'Cartographer nodes, services, /map and map->odom are ready')
                    return 0
            else:
                stable_since = None
        node.get_logger().error('Timed out waiting for Cartographer mapping readiness')
        return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
