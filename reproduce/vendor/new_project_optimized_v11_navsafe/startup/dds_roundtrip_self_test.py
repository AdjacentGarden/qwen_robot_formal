#!/usr/bin/env python3
"""Hardware-free service and action response round-trip test for the selected RMW."""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing as mp
import os
import time


def server(ready: mp.Event, stop: mp.Event) -> None:
    import rclpy
    from action_tutorials_interfaces.action import Fibonacci
    from example_interfaces.srv import AddTwoInts
    from rclpy.action import ActionServer
    from rclpy.context import Context
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions

    context = Context()
    rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
    node = Node(f"v8_dds_test_server_{os.getpid()}", context=context)

    def add(request, response):
        response.sum = request.a + request.b
        return response

    def fib(goal_handle):
        sequence = [0, 1]
        for _ in range(2, max(2, goal_handle.request.order)):
            sequence.append(sequence[-1] + sequence[-2])
        goal_handle.succeed()
        result = Fibonacci.Result()
        result.sequence = sequence
        return result

    service = node.create_service(AddTwoInts, "/v8_dds_roundtrip/add", add)
    action = ActionServer(node, Fibonacci, "/v8_dds_roundtrip/fibonacci", execute_callback=fib)
    executor = MultiThreadedExecutor(context=context, num_threads=2)
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


def wait_future(executor, context, future, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while context.ok() and not future.done() and time.monotonic() < deadline:
        executor.spin_once(timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))
    return bool(future.done())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    runs = max(1, int(args.runs))
    mp.set_start_method("spawn", force=True)
    ready = mp.Event()
    stop = mp.Event()
    process = mp.Process(target=server, args=(ready, stop), daemon=False)
    process.start()
    if not ready.wait(8.0):
        process.terminate()
        process.join(2.0)
        print(json.dumps({"ok": False, "error": "test_server_start_timeout"}))
        return 2

    import rclpy
    from action_tutorials_interfaces.action import Fibonacci
    from example_interfaces.srv import AddTwoInts
    from rclpy.action import ActionClient
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions

    context = Context()
    executor = node = service_client = action_client = None
    started = time.monotonic()
    payload = {"ok": False}
    try:
        rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = Node(f"v8_dds_test_client_{os.getpid()}", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        service_client = node.create_client(AddTwoInts, "/v8_dds_roundtrip/add")
        if not service_client.wait_for_service(timeout_sec=5.0):
            payload["error"] = "service_discovery_timeout"
            return_code = 3
        else:
            action_client = ActionClient(node, Fibonacci, "/v8_dds_roundtrip/fibonacci")
            if not action_client.wait_for_server(timeout_sec=5.0):
                payload["error"] = "action_discovery_timeout"
                return_code = 6
            else:
                return_code = 0
                completed_runs = 0
                for index in range(1, runs + 1):
                    request = AddTwoInts.Request()
                    request.a = 20
                    request.b = 22
                    service_future = service_client.call_async(request)
                    if not wait_future(executor, context, service_future, 5.0):
                        payload.update({"error": "service_response_timeout", "failed_run": index})
                        return_code = 4
                        break
                    if service_future.result().sum != 42:
                        payload.update({"error": "service_wrong_result", "failed_run": index})
                        return_code = 5
                        break

                    goal = Fibonacci.Goal()
                    goal.order = 6
                    goal_future = action_client.send_goal_async(goal)
                    if not wait_future(executor, context, goal_future, 5.0):
                        payload.update({"error": "action_goal_response_timeout", "failed_run": index})
                        return_code = 7
                        break
                    goal_handle = goal_future.result()
                    if goal_handle is None or not goal_handle.accepted:
                        payload.update({"error": "action_goal_rejected", "failed_run": index})
                        return_code = 8
                        break
                    result_future = goal_handle.get_result_async()
                    if not wait_future(executor, context, result_future, 5.0):
                        payload.update({"error": "action_result_timeout", "failed_run": index})
                        return_code = 9
                        break
                    sequence = list(result_future.result().result.sequence)
                    if sequence != [0, 1, 1, 2, 3, 5]:
                        payload.update(
                            {"error": "action_wrong_result", "failed_run": index, "action_sequence": sequence}
                        )
                        return_code = 10
                        break
                    completed_runs = index
                if return_code == 0:
                    payload.update(
                        {
                            "ok": True,
                            "runs": runs,
                            "completed_runs": completed_runs,
                            "service_failures": 0,
                            "action_goal_response_failures": 0,
                            "action_result_failures": 0,
                            "service_sum": 42,
                            "action_sequence": [0, 1, 1, 2, 3, 5],
                            "rmw": os.getenv("RMW_IMPLEMENTATION", ""),
                            "fastdds_profile": os.getenv("FASTDDS_DEFAULT_PROFILES_FILE", ""),
                        }
                    )
                else:
                    payload.update({"runs": runs, "completed_runs": completed_runs})
        payload["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        print(json.dumps(payload))
        return return_code
    finally:
        with contextlib.suppress(Exception):
            if service_client is not None and node is not None:
                node.destroy_client(service_client)
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
        stop.set()
        process.join(3.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
