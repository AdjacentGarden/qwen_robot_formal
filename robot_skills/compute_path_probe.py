#!/usr/bin/env python3
"""Ask Nav2 for a path without sending a navigation command."""

import argparse
import json
import math

import rclpy
from nav2_msgs.action import ComputePathToPose
from rclpy.action import ActionClient
from rclpy.node import Node


def spin_until(node, future, timeout):
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    return future.done()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=float, required=True)
    parser.add_argument("--y", type=float, required=True)
    parser.add_argument("--yaw", type=float, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    rclpy.init()
    node = Node("nav2_compute_path_probe")
    client = ActionClient(node, ComputePathToPose, "/compute_path_to_pose")
    report = {"ok": False, "goal": {"x": args.x, "y": args.y, "yaw": args.yaw}}
    try:
        if not client.wait_for_server(timeout_sec=args.timeout):
            raise RuntimeError("compute_path_action_unavailable")
        goal = ComputePathToPose.Goal()
        goal.goal.header.frame_id = "map"
        goal.goal.header.stamp = node.get_clock().now().to_msg()
        goal.goal.pose.position.x = args.x
        goal.goal.pose.position.y = args.y
        goal.goal.pose.orientation.z = math.sin(args.yaw * 0.5)
        goal.goal.pose.orientation.w = math.cos(args.yaw * 0.5)
        sent = client.send_goal_async(goal)
        if not spin_until(node, sent, args.timeout):
            raise RuntimeError("goal_response_timeout")
        handle = sent.result()
        if handle is None or not handle.accepted:
            raise RuntimeError("goal_rejected")
        result_future = handle.get_result_async()
        if not spin_until(node, result_future, args.timeout):
            raise RuntimeError("result_timeout")
        wrapped = result_future.result()
        poses = list(wrapped.result.path.poses)
        length = 0.0
        for left, right in zip(poses, poses[1:]):
            dx = right.pose.position.x - left.pose.position.x
            dy = right.pose.position.y - left.pose.position.y
            length += math.hypot(dx, dy)
        report.update({
            "ok": bool(poses),
            "status": int(wrapped.status),
            "pose_count": len(poses),
            "path_length_m": round(length, 3),
            "planning_time_sec": round(
                wrapped.result.planning_time.sec + wrapped.result.planning_time.nanosec / 1e9, 4
            ),
        })
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        print(json.dumps(report, ensure_ascii=False))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
