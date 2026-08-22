#!/usr/bin/env python3
"""Level the head during Manager startup without publishing base velocity."""

from __future__ import annotations

import argparse
import json
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--level-angle", type=int, default=185)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    if args.plan:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "publishes_base_velocity": False,
            "sequence": [
                "sensor_gate=false",
                f"head_target={args.level_angle}",
                "wait_head_aligned",
                "sensor_gate=true",
                "wait_sensor_gate_ready",
            ],
        }, ensure_ascii=False))
        return 0

    import rclpy
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, String, UInt16
    from std_srvs.srv import SetBool

    rclpy.init(args=None)
    node = rclpy.create_node("qwen_car_real_startup_head_guard")
    publisher = node.create_publisher(UInt16, "/step_motor_angle", 10)
    gate_client = node.create_client(
        SetBool, "/motion_controller/set_sensor_gate_enabled"
    )
    state = {
        "head_status": "unknown",
        "head_aligned": False,
        "sensor_gate_state": "unknown",
        "manager_state": "unknown",
    }
    retained_status_qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )
    subscriptions = [
        node.create_subscription(String, "/head/status", lambda m: state.__setitem__("head_status", str(m.data)), retained_status_qos),
        node.create_subscription(Bool, "/head/aligned", lambda m: state.__setitem__("head_aligned", bool(m.data)), retained_status_qos),
        node.create_subscription(String, "/motion_controller/sensor_gate_state", lambda m: state.__setitem__("sensor_gate_state", str(m.data).lower()), retained_status_qos),
        node.create_subscription(String, "/mapping_manager/state", lambda m: state.__setitem__("manager_state", str(m.data).upper()), retained_status_qos),
    ]
    deadline = time.monotonic() + max(5.0, float(args.timeout))

    def spin_until(predicate) -> bool:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
            if state["manager_state"] == "SAFE_STOP":
                return False
            if predicate():
                return True
        return False

    def set_gate(enabled: bool) -> bool:
        if not spin_until(lambda: gate_client.service_is_ready()):
            return False
        request = SetBool.Request()
        request.data = enabled
        future = gate_client.call_async(request)
        if not spin_until(future.done):
            return False
        response = future.result()
        return bool(response and response.success)

    try:
        if not set_gate(False):
            raise RuntimeError("startup_sensor_gate_disable_failed")
        if not spin_until(lambda: state["sensor_gate_state"] == "disabled"):
            raise RuntimeError("startup_sensor_gate_disable_unconfirmed")
        if not spin_until(lambda: publisher.get_subscription_count() > 0):
            raise RuntimeError("startup_head_subscriber_unavailable")
        target = UInt16()
        target.data = max(0, min(65535, int(args.level_angle)))
        for _ in range(5):
            publisher.publish(target)
            rclpy.spin_once(node, timeout_sec=0.05)
        if not spin_until(lambda: (
            state["head_aligned"]
            and str(state["head_status"]).lower().startswith("succeeded;")
            and f"target={target.data}" in str(state["head_status"]).lower()
        )):
            raise RuntimeError(f"startup_head_level_failed:{state['head_status']}")
        if not set_gate(True):
            raise RuntimeError("startup_sensor_gate_enable_failed")
        # Full gate recovery also requires the localization map, which Manager
        # intentionally starts after WAIT_BASE.  Confirm that recovery began;
        # robot_stack then waits for Manager's authoritative ``ready`` state.
        if not spin_until(lambda: state["sensor_gate_state"] in {"recovering", "ready"}):
            raise RuntimeError("startup_sensor_gate_recovery_not_started")
        print(json.dumps({"ok": True, "head_status": state["head_status"], **state}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), **state}, ensure_ascii=False))
        return 1
    finally:
        subscriptions.clear()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
