#!/usr/bin/env python3
"""Periodically save Cartographer state and save once more before shutdown."""

from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path

import rclpy
from cartographer_ros_msgs.srv import WriteState
from rclpy.signals import SignalHandlerOptions


def _save(node, client, output: str, timeout: float) -> bool:
    Path(output).expanduser().parent.mkdir(parents=True, exist_ok=True)
    if not client.wait_for_service(timeout_sec=timeout):
        node.get_logger().warning("Cartographer /write_state service is unavailable")
        return False

    request = WriteState.Request()
    request.filename = str(Path(output).expanduser())
    request.include_unfinished_submaps = True
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done() or future.result() is None:
        node.get_logger().error(f"Timed out saving Cartographer state: {output}")
        return False

    status = future.result().status
    if int(status.code) != 0:
        node.get_logger().error(
            f"Cartographer state save failed ({status.code}): {status.message}"
        )
        return False
    node.get_logger().info(f"Saved Cartographer state: {output}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--service", default="/write_state")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--save-period", type=float, default=30.0)
    parser.add_argument("--initial-save-delay", type=float, default=8.0)
    parser.add_argument("--retry-period", type=float, default=3.0)
    args = parser.parse_args()

    rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node("cartographer_shutdown_saver")
    client = node.create_client(WriteState, args.service)
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    next_save = time.monotonic() + max(0.0, args.initial_save_delay)
    try:
        while not stopping and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
            if time.monotonic() < next_save:
                continue
            success = _save(node, client, args.output, max(0.1, args.timeout))
            delay = args.save_period if success else args.retry_period
            next_save = time.monotonic() + max(0.1, delay)

        if rclpy.ok():
            _save(node, client, args.output, max(0.1, args.timeout))
        return 0
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
