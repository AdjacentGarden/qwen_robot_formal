#!/usr/bin/env python3
"""Hardware-free regression test for V8 navigation goal-response handling."""

from __future__ import annotations

import contextlib
import json
import multiprocessing as mp
import os
import time

from run import send_goal_with_rclpy


def fake_nav2_server(ready: mp.Event, stop: mp.Event, goal_delay: float) -> None:
    import rclpy
    from lifecycle_msgs.msg import State
    from lifecycle_msgs.srv import GetState
    from nav2_msgs.action import NavigateToPose
    from rclpy.action import ActionServer, GoalResponse
    from rclpy.context import Context
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions

    context = Context()
    rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
    node = Node(f"v8_fake_nav2_{os.getpid()}", context=context)

    def get_state(_request, response):
        response.current_state = State(id=3, label="active")
        return response

    def goal_callback(_goal):
        time.sleep(max(0.0, goal_delay))
        return GoalResponse.ACCEPT

    def execute(goal_handle):
        goal_handle.succeed()
        return NavigateToPose.Result()

    service = node.create_service(GetState, "/bt_navigator/get_state", get_state)
    action = ActionServer(
        node,
        NavigateToPose,
        "/navigate_to_pose",
        goal_callback=goal_callback,
        execute_callback=execute,
    )
    executor = MultiThreadedExecutor(context=context, num_threads=4)
    executor.add_node(node)
    ready.set()
    try:
        while context.ok() and not stop.is_set():
            executor.spin_once(timeout_sec=0.05)
    finally:
        with contextlib.suppress(Exception):
            action.destroy()
        with contextlib.suppress(Exception):
            node.destroy_service(service)
        with contextlib.suppress(Exception):
            executor.remove_node(node)
            executor.shutdown(timeout_sec=1.0)
        with contextlib.suppress(Exception):
            node.destroy_node()
        with contextlib.suppress(Exception):
            context.try_shutdown()


def run_case(goal_delay: float, response_timeout: float, response_attempts: int) -> dict:
    ready = mp.Event()
    stop = mp.Event()
    process = mp.Process(target=fake_nav2_server, args=(ready, stop, goal_delay), daemon=False)
    process.start()
    if not ready.wait(8.0):
        process.terminate()
        process.join(2.0)
        return {"ok": False, "error": "fake_nav2_start_timeout"}
    try:
        return send_goal_with_rclpy(
            {"x": 0.0, "y": 0.0, "yaw": 0.0, "frame_id": "map", "mode": "test", "name": "test"},
            "/navigate_to_pose",
            timeout=5.0,
            server_wait_timeout=1.0,
            server_retry_attempts=2,
            server_retry_delay=0.1,
            goal_response_timeout=response_timeout,
            goal_response_attempts=response_attempts,
            health_response_timeout=1.0,
            cancel_response_timeout=0.5,
        )
    finally:
        stop.set()
        process.join(3.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)


def main() -> int:
    mp.set_start_method("spawn", force=True)
    success = run_case(goal_delay=0.0, response_timeout=1.0, response_attempts=2)
    timeout = run_case(goal_delay=1.2, response_timeout=0.25, response_attempts=2)
    timeout_attempts = timeout.get("attempts") if isinstance(timeout.get("attempts"), list) else []
    recreated = len(timeout_attempts) == 2 and all(item.get("node_recreated") for item in timeout_attempts)
    ok = (
        success.get("ok") is True
        and timeout.get("ok") is False
        and timeout.get("error") == "goal_response_timeout"
        and recreated
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "success_case": success,
                "timeout_case": timeout,
                "fresh_context_attempts_verified": recreated,
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

