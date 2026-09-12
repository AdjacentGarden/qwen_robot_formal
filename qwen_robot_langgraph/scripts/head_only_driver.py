#!/usr/bin/env python3
"""Runs copied, calibrated head controller in ROS_DOMAIN_ID=83; wheel packets cannot pass."""
import atexit
import json
import os
from pathlib import Path
import sys
import time
import threading
import struct

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'vendor'), str(ROOT/'src')]
os.environ['ROS_DOMAIN_ID'] = '83'
os.environ['ROS_LOG_DIR'] = str(ROOT/'runtime/ros_logs')
from robot_graph.wheel_guard import validate_head_packet, validate_zero_wheel_stop
from ros_robot_controller import ros_robot_controller_sdk as sdk

OriginalBoard = sdk.Board

class HeadOnlyBoard(OriginalBoard):
    def __init__(self, *args, **kwargs):
        kwargs['auto_kill'] = False
        self.audit_lock = threading.Lock()
        self.head_packets = 0
        self.head_nonzero_packets = 0
        self.head_zero_packets = 0
        self.last_head_speed = 0.0
        self.max_abs_head_speed = 0.0
        self.blocked_wheel_calls = 0
        super().__init__(*args, **kwargs)
        atexit.register(self.report)
        threading.Thread(target=self.heartbeat, daemon=True).start()

    def set_motor_speed(self, left, right, *args, **kwargs):
        """Suppress exact stop calls and reject any request capable of motion."""
        validate_zero_wheel_stop(left, right)
        with self.audit_lock:
            self.blocked_wheel_calls += 1

    def buf_write(self, func, data):
        payload = validate_head_packet(func, data, sdk.PacketFunction.PACKET_FUNC_MOTOR)
        speed = struct.unpack('<f', payload[2:])[0]
        with self.audit_lock:
            self.head_packets += 1
            self.last_head_speed = speed
            self.max_abs_head_speed = max(self.max_abs_head_speed, abs(speed))
            if speed:
                self.head_nonzero_packets += 1
            else:
                self.head_zero_packets += 1
        result = super().buf_write(func, data)
        return result

    def heartbeat(self):
        while True:
            self.report()
            time.sleep(1)

    def report(self):
        with self.audit_lock:
            path = ROOT/'runtime/head_wire_audit.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps({
                'pid':os.getpid(), 'updated_at':time.time(),
                'head_packets':self.head_packets,
                'head_nonzero_packets':self.head_nonzero_packets,
                'head_zero_packets':self.head_zero_packets,
                'last_head_speed':self.last_head_speed,
                'max_abs_head_speed':self.max_abs_head_speed,
                'suppressed_wheel_calls':self.blocked_wheel_calls,
                'wheel_packets_sent':0,
                'guard':'only motor subcommand 0, motor ID 3, max speed 40; all wheel calls suppressed',
            }))
            temp.replace(path)

sdk.Board = HeadOnlyBoard
from ros_robot_controller.ros_robot_controller_node import main
main()
