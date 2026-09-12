#!/usr/bin/env python3
"""Runs copied, calibrated head controller in ROS_DOMAIN_ID=83; wheel packets cannot pass."""
import atexit
import json
import os
from pathlib import Path
import sys
import time
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'vendor'), str(ROOT/'src')]
os.environ['ROS_DOMAIN_ID'] = '83'
os.environ['ROS_LOG_DIR'] = str(ROOT/'runtime/ros_logs')
from robot_graph.wheel_guard import validate_head_packet
from ros_robot_controller import ros_robot_controller_sdk as sdk

OriginalBoard = sdk.Board

class HeadOnlyBoard(OriginalBoard):
    def __init__(self, *args, **kwargs):
        kwargs['auto_kill'] = False
        self.audit_lock = threading.Lock()
        self.head_packets = 0
        self.blocked_wheel_calls = 0
        super().__init__(*args, **kwargs)
        atexit.register(self.report)
        threading.Thread(target=self.heartbeat, daemon=True).start()

    def set_motor_speed(self, *args, **kwargs):
        # Existing startup/watchdog/shutdown zero-wheel calls are suppressed too.
        self.blocked_wheel_calls += 1

    def buf_write(self, func, data):
        validate_head_packet(func, data, sdk.PacketFunction.PACKET_FUNC_MOTOR)
        self.head_packets += 1
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
            temp.write_text(json.dumps({'pid':os.getpid(),'updated_at':time.time(),'head_packets':self.head_packets,'suppressed_wheel_calls':self.blocked_wheel_calls,'wheel_packets_sent':0,'guard':'only motor subcommand 0, motor ID 3, max speed 40'}))
            temp.replace(path)

sdk.Board = HeadOnlyBoard
from ros_robot_controller.ros_robot_controller_node import main
main()
