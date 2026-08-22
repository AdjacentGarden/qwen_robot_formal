#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
import rclpy
from rclpy.node import Node

SKILL_NAME = "head_control"


def _single_function_cli_preflight(skill_name: str):
    raw = list(sys.argv[1:])
    dry_run = False
    json_mode = False
    timeout = None
    kept = [sys.argv[0]]
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        if arg == "--json":
            json_mode = True
            i += 1
            continue
        if arg == "--timeout":
            if i + 1 < len(raw):
                timeout = raw[i + 1]
                i += 2
            else:
                i += 1
            continue
        if arg.startswith("--timeout="):
            timeout = arg.split("=", 1)[1]
            i += 1
            continue
        kept.append(arg)
        i += 1
    sys.argv[:] = kept
    if json_mode:
        os.environ["SINGLE_FUNCTION_JSON"] = "1"
    if timeout is not None:
        os.environ["SINGLE_FUNCTION_TIMEOUT"] = str(timeout)
    if dry_run:
        action = "level"
        for token in kept[1:]:
            if token in {"up", "raise", "look_up", "down", "lower", "look_down", "level", "center", "flat", "angle"}:
                action = token
                break
        print(json.dumps({
            "ok": True,
            "status": "dry_run",
            "skill": skill_name,
            "action": action,
            "result": {"argv": kept[1:], "timeout": timeout},
            "error": None,
            "metrics": {"ts": round(time.time(), 3)},
        }, ensure_ascii=False))
        raise SystemExit(0)


_single_function_cli_preflight(SKILL_NAME)

try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import LaserScan
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import Float32, UInt16
    from std_srvs.srv import SetBool
except Exception as exc:
    raise SystemExit(f"ROS2 Python modules are unavailable. Use run.sh so ROS2 is sourced. Detail: {exc}")


ACTION_ALIASES = {
    "up": "up",
    "raise": "up",
    "look_up": "up",
    "??": "up",
    "down": "down",
    "lower": "down",
    "look_down": "down",
    "??": "down",
    "level": "level",
    "center": "level",
    "flat": "level",
    "neutral": "level",
    "??": "level",
    "????": "level",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _target_angle(action: str, explicit_angle: Optional[int]) -> Tuple[str, int]:
    normalized = ACTION_ALIASES.get(str(action or "level").strip().lower(), str(action or "level").strip().lower())
    if normalized == "angle":
        if explicit_angle is None:
            raise ValueError("action=angle requires --angle")
        return normalized, int(explicit_angle)
    if explicit_angle is not None:
        return normalized, int(explicit_angle)
    if normalized == "up":
        return normalized, _env_int("HEAD_UP_ANGLE", 211)
    if normalized == "down":
        return normalized, _env_int("HEAD_DOWN_ANGLE", 163)
    if normalized == "level":
        return normalized, _env_int("HEAD_LEVEL_ANGLE", 185)
    raise ValueError(f"unsupported head action: {action}")


class HeadControlNode(Node):
    def __init__(self, topic: str):
        super().__init__("skill_head_control")
        self.pub = self.create_publisher(UInt16, topic, 10)
        self.latest_roll: tuple[float, float] | None = None
        self.scan_sequence = 0
        self.latest_scan_at: float | None = None
        self.roll_sub = self.create_subscription(
            Float32, "/head/roll_deg", self._on_roll, 10
        )
        self.guard_client = self.create_client(SetBool, "/head_lidar_guard/set_live")
        self.scan_sub = self.create_subscription(
            LaserScan, "/scan", self._on_scan, qos_profile_sensor_data
        )

    def _on_roll(self, message) -> None:
        self.latest_roll = (float(message.data), time.monotonic())

    def _on_scan(self, _message) -> None:
        self.scan_sequence += 1
        self.latest_scan_at = time.monotonic()

    def wait_for_target(
        self,
        target: float,
        timeout_sec: float,
        tolerance_deg: float,
        stable_sec: float = 0.15,
    ) -> dict:
        deadline = time.monotonic() + max(0.2, float(timeout_sec))
        stable_since = None
        last = None
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.03)
            sample = self.latest_roll
            if sample is None:
                continue
            roll, timestamp = sample
            age = max(0.0, time.monotonic() - timestamp)
            error = (roll - float(target) + 180.0) % 360.0 - 180.0
            at_target = age <= 0.3 and abs(error) <= max(0.5, float(tolerance_deg))
            last = {
                "available": age <= 0.3,
                "roll_deg": roll,
                "target_deg": float(target),
                "error_deg": error,
                "feedback_age_sec": age,
                "at_target": at_target,
            }
            if at_target:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= max(0.0, stable_sec):
                    return {**last, "ok": True}
            else:
                stable_since = None
        return {**(last or {"available": False}), "ok": False, "error": "head_target_timeout"}

    def wait_for_subscribers(self, timeout_sec: float, minimum: int = 1) -> int:
        end = time.time() + max(0.0, float(timeout_sec))
        required = max(1, int(minimum))
        count = int(self.pub.get_subscription_count())
        while rclpy.ok() and count < required and time.time() < end:
            rclpy.spin_once(self, timeout_sec=0.05)
            count = int(self.pub.get_subscription_count())
        return count

    def publish_angle(self, angle: int, repeat: int, interval: float, discovery_timeout: float, minimum_subscribers: int = 1) -> int:
        subscribers = self.wait_for_subscribers(discovery_timeout, minimum_subscribers)
        if subscribers < max(1, int(minimum_subscribers)):
            return subscribers
        msg = UInt16()
        msg.data = max(0, min(65535, int(angle)))
        count = max(1, int(repeat))
        for index in range(count):
            self.pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.02)
            if index + 1 < count:
                time.sleep(max(0.0, float(interval)))
        return max(subscribers, int(self.pub.get_subscription_count()))

    def set_lidar_guard(self, enabled: bool, timeout_sec: float) -> dict:
        timeout_sec = max(0.2, float(timeout_sec))
        deadline = time.monotonic() + timeout_sec
        if not self.guard_client.wait_for_service(timeout_sec=timeout_sec):
            return {"called": False, "ok": False, "message": "head_lidar_guard_unavailable"}
        attempts = 0
        while rclpy.ok() and time.monotonic() < deadline:
            attempts += 1
            request = SetBool.Request()
            request.data = bool(enabled)
            future = self.guard_client.call_async(request)
            while rclpy.ok() and not future.done() and time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=0.03)
            if not future.done() or future.result() is None:
                break
            response = future.result()
            message = str(response.message)
            if response.success:
                return {"called": True, "ok": True, "message": message, "attempts": attempts}
            if not enabled or message != "head_not_stably_level":
                return {"called": True, "ok": False, "message": message, "attempts": attempts}
            rclpy.spin_once(self, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))
        return {"called": True, "ok": False, "message": "head_lidar_guard_timeout", "attempts": attempts}

    def wait_for_fresh_scan(self, after_sequence: int, timeout_sec: float = 2.5) -> dict:
        deadline = time.monotonic() + max(0.2, float(timeout_sec))
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.03)
            if self.scan_sequence > int(after_sequence) and self.latest_scan_at is not None:
                age = max(0.0, time.monotonic() - self.latest_scan_at)
                if age <= 0.5:
                    return {"ok": True, "scan_sequence": self.scan_sequence, "scan_age_sec": age}
        return {
            "ok": False,
            "error": "fresh_scan_resume_timeout",
            "scan_sequence": self.scan_sequence,
            "after_sequence": int(after_sequence),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the robot head stepper motor by publishing target angle.")
    parser.add_argument("action", nargs="?", default="level", help="up, down, level, or angle")
    parser.add_argument("--angle", type=int, default=None, help="Direct target angle. Overrides action default.")
    parser.add_argument("--topic", default=os.getenv("ROBOT_STEP_MOTOR_ANGLE_TOPIC", "/step_motor_angle"))
    parser.add_argument("--wait", type=float, default=_env_float("HEAD_WAIT_SEC", 0.35), help="Seconds to wait after publishing.")
    parser.add_argument("--repeat", type=int, default=_env_int("HEAD_PUBLISH_REPEAT", 5))
    parser.add_argument("--interval", type=float, default=_env_float("HEAD_PUBLISH_INTERVAL_SEC", 0.05))
    parser.add_argument("--discovery-timeout", type=float, default=_env_float("HEAD_DISCOVERY_TIMEOUT_SEC", 5.0))
    parser.add_argument("--min-subscribers", type=int, default=_env_int("HEAD_MIN_SUBSCRIBERS", 2), help="Require both the lidar guard and motor controller before publishing.")
    # These are discovery upper bounds. Normal calls proceed immediately once
    # the already-running ROS endpoints have been found.
    parser.add_argument("--service-timeout", type=float, default=_env_float("HEAD_SERVICE_TIMEOUT_SEC", 5.0))
    parser.add_argument("--feedback-timeout", type=float, default=_env_float("HEAD_FEEDBACK_TIMEOUT_SEC", 12.0))
    parser.add_argument("--feedback-tolerance", type=float, default=_env_float("HEAD_FEEDBACK_TOLERANCE_DEG", 5.0))
    parser.add_argument("--call-services", action="store_true", help="Explicitly use the head lidar guard (now enabled by default).")
    parser.add_argument("--skip-services", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    args = parser.parse_args()

    json_mode = args.json or os.getenv("SINGLE_FUNCTION_JSON", "0") == "1"
    action, angle = _target_angle(args.action, args.angle)

    rclpy.init(args=None)
    node = HeadControlNode(args.topic)
    service_result = {"called": False, "ok": None, "message": "skipped"}
    try:
        level_angle = _env_int("HEAD_LEVEL_ANGLE", 185)
        is_level_target = abs(int(angle) - int(level_angle)) <= 4
        subscribers = node.wait_for_subscribers(args.discovery_timeout, args.min_subscribers)
        if subscribers < max(1, int(args.min_subscribers)):
            print(json.dumps({"ok": False, "skill": SKILL_NAME, "action": action, "angle": angle, "topic": args.topic, "subscribers": subscribers, "required_subscribers": max(1, int(args.min_subscribers)), "service": service_result, "error": "head_transport_unavailable", "message": "The lidar guard and motor controller were not both discovered; no head command was published."}, ensure_ascii=False))
            return 2
        if not args.skip_services and not is_level_target:
            service_result = node.set_lidar_guard(False, args.service_timeout)
            if not service_result.get("ok"):
                print(json.dumps({"ok": False, "skill": SKILL_NAME, "action": action, "angle": angle, "service": service_result, "error": "lidar_guard_disable_failed"}, ensure_ascii=False))
                return 2
        elif args.skip_services:
            service_result = {"called": False, "ok": True, "message": "explicitly_skipped"}

        subscribers = node.publish_angle(angle, args.repeat, args.interval, args.discovery_timeout, args.min_subscribers)
        # if action == 'level':
        #     node.resume_lidar()

        if subscribers < max(1, int(args.min_subscribers)):
            result = {
                "ok": False,
                "skill": SKILL_NAME,
                "action": action,
                "angle": angle,
                "topic": args.topic,
                "subscribers": subscribers,
                "required_subscribers": max(1, int(args.min_subscribers)),
                "service": service_result,
                "error": "head_transport_unavailable",
                "message": "The lidar guard and motor controller were not both discovered; no head command was published.",
            }
            print(json.dumps(result, ensure_ascii=False))
            return 2
        feedback = node.wait_for_target(
            angle,
            timeout_sec=args.feedback_timeout,
            tolerance_deg=args.feedback_tolerance,
        )
        if not feedback.get("ok"):
            print(json.dumps({"ok": False, "skill": SKILL_NAME, "action": action, "angle": angle, "topic": args.topic, "subscribers": subscribers, "feedback": feedback, "service": service_result, "error": "head_target_unconfirmed"}, ensure_ascii=False))
            return 2

        if not args.skip_services and is_level_target:
            # A short-lived publisher can initially discover the motor
            # controller before the guard subscriber.  Re-publish after the
            # physical level feedback is stable, while this node has had time
            # to discover both subscribers, then explicitly reopen /scan.
            subscribers = max(
                subscribers,
                node.publish_angle(
                    angle,
                    args.repeat,
                    args.interval,
                    max(1.5, args.discovery_timeout),
                    args.min_subscribers,
                ),
            )
            settle_until = time.monotonic() + 0.30
            while rclpy.ok() and time.monotonic() < settle_until:
                rclpy.spin_once(node, timeout_sec=0.03)
            scan_sequence_before = node.scan_sequence
            service_result = node.set_lidar_guard(True, max(2.5, args.service_timeout))
            if not service_result.get("ok"):
                print(json.dumps({"ok": False, "skill": SKILL_NAME, "action": action, "angle": angle, "topic": args.topic, "subscribers": subscribers, "feedback": feedback, "service": service_result, "error": "lidar_guard_enable_failed"}, ensure_ascii=False))
                return 2
            fresh_scan = node.wait_for_fresh_scan(scan_sequence_before, 2.5)
            service_result = {**service_result, "fresh_scan": fresh_scan}
            if not fresh_scan.get("ok"):
                print(json.dumps({"ok": False, "skill": SKILL_NAME, "action": action, "angle": angle, "topic": args.topic, "subscribers": subscribers, "feedback": feedback, "service": service_result, "error": "lidar_fresh_scan_resume_failed"}, ensure_ascii=False))
                return 2

        result = {
            "ok": True,
            "skill": SKILL_NAME,
            "action": action,
            "angle": angle,
            "topic": args.topic,
            "subscribers": subscribers,
            "feedback": feedback,
            "service": service_result,
        }
        if json_mode:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    if "--dry-run" in sys.argv[1:]:
        raise SystemExit(main())
    client = Path(__file__).resolve().parents[1] / "resident_skill_client.py"
    os.execv(sys.executable, [sys.executable, str(client), "head_control", *sys.argv[1:]])
