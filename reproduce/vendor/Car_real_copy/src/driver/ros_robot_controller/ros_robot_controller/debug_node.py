#!/usr/bin/env python3

import os
import sys
import time
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

class DebugNode(Node):
    def __init__(self):
        super().__init__('debug_node')
        self.get_logger().info('='*60)
        self.get_logger().info(f'PID: {os.getpid()}')
        self.get_logger().info(f'RMW: {os.environ.get("RMW_IMPLEMENTATION", "NOT SET")}')
        self.get_logger().info(f'DOMAIN_ID: {os.environ.get("ROS_DOMAIN_ID", "NOT SET")}')
        self.get_logger().info(f'Node: {self.get_name()}')
        self.get_logger().info('='*60)
        
        # 创建一个发布者，发布测试消息
        self.pub = self.create_publisher(String, 'debug_topic', 10)
        self.timer = self.create_timer(2.0, self.timer_callback)
        self.counter = 0
    
    def timer_callback(self):
        msg = String()
        msg.data = f"Debug message {self.counter}"
        self.pub.publish(msg)
        self.get_logger().info(f"Published: {msg.data}")
        self.counter += 1

def main():
    print("Initializing rclpy...")
    rclpy.init()
    
    print("Creating node...")
    node = DebugNode()
    
    print("Node created, spinning...")
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\nKeyboard interrupt")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()