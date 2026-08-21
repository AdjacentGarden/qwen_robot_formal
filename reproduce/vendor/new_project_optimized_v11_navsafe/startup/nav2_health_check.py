#!/usr/bin/env python3
"""Non-motion Nav2 readiness check: lifecycle service round-trip + action discovery."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--action", default="/navigate_to_pose")
    parser.add_argument("--service", default="/bt_navigator/get_state")
    args = parser.parse_args()
    timeout = max(0.2, float(args.timeout))
    started = time.monotonic()

    try:
        import rclpy
        from lifecycle_msgs.srv import GetState
        from nav2_msgs.action import NavigateToPose
        from rclpy.action import ActionClient
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"ros_import_failed: {exc}"}))
        return 2

    context = Context()
    executor = node = action_client = state_client = None
    try:
        rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = Node(f"v8_nav2_health_{os.getpid()}_{time.monotonic_ns() % 1000000}", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        action_client = ActionClient(node, NavigateToPose, args.action)
        action_ready = bool(action_client.wait_for_server(timeout_sec=timeout))
        if not action_ready:
            print(json.dumps({"ok": False, "error": "action_server_unavailable", "action": args.action}))
            return 3

        state_client = node.create_client(GetState, args.service)
        if not state_client.wait_for_service(timeout_sec=timeout):
            print(json.dumps({"ok": False, "error": "health_service_unavailable", "service": args.service}))
            return 4

        future = state_client.call_async(GetState.Request())
        deadline = time.monotonic() + timeout
        while context.ok() and not future.done() and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        if not future.done():
            print(json.dumps({"ok": False, "error": "health_response_timeout", "service": args.service}))
            return 5
        try:
            response = future.result()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"health_response_error: {exc}", "service": args.service}))
            return 6
        label = str(getattr(getattr(response, "current_state", None), "label", ""))
        ok = label == "active"
        print(
            json.dumps(
                {
                    "ok": ok,
                    "action_server_ready": action_ready,
                    "bt_navigator_state": label,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "rmw": os.getenv("RMW_IMPLEMENTATION", ""),
                    "fastdds_profile": os.getenv("FASTDDS_DEFAULT_PROFILES_FILE", ""),
                    "cyclonedds_uri": os.getenv("CYCLONEDDS_URI", ""),
                    "error": "" if ok else f"bt_navigator_not_active: {label or 'unknown'}",
                }
            )
        )
        return 0 if ok else 7
    finally:
        with contextlib.suppress(Exception):
            if state_client is not None and node is not None:
                node.destroy_client(state_client)
        with contextlib.suppress(Exception):
            if action_client is not None:
                action_client.destroy()
        with contextlib.suppress(Exception):
            if node is not None:
                if executor is not None:
                    executor.remove_node(node)
                node.destroy_node()
        with contextlib.suppress(Exception):
            if executor is not None:
                executor.shutdown(timeout_sec=1.0)
        with contextlib.suppress(Exception):
            context.try_shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
