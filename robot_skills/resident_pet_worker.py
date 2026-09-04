#!/usr/bin/env python3
"""Dedicated persistent owner for the pet RKNN3 model on device 0004."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

from resident_runtime_server import ResidentSkills, ThreadRoutingStream, recv_request, send_response
from resident_camera_ipc import open_capture
from car_real_contract import EXTERNAL_CMD_VEL_TOPIC, manager_allows_motion
from pet_tracking.centering import PetCenteringController


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "runtime" / "resident"
SOCKET = STATE / "pet.sock"
PID = STATE / "pet.pid"
ORIGINAL_STDOUT = sys.stdout
ORIGINAL_STDERR = sys.stderr
STDOUT_ROUTER = ThreadRoutingStream(sys.stdout)
STDERR_ROUTER = ThreadRoutingStream(sys.stderr)
sys.stdout = STDOUT_ROUTER
sys.stderr = STDERR_ROUTER


class NullCameraManager:
    def close_all(self):
        return None


class RosOwner:
    def __init__(self):
        import rclpy
        from geometry_msgs.msg import Twist
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from rclpy.time import Time
        from std_msgs.msg import Bool, String
        from tf2_ros import Buffer, TransformListener

        self.rclpy = rclpy
        self.Twist = Twist
        self.Time = Time
        if not rclpy.ok():
            rclpy.init(args=None)
        self.node = rclpy.create_node(f"resident_pet_ros_{os.getpid()}")
        self.publisher = self.node.create_publisher(Twist, EXTERNAL_CMD_VEL_TOPIC, 10)
        self.manager_state = "not_running"
        self.sensor_gate_state = "unknown"
        self.control_conflict = False
        retained_status_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.status_subscriptions = [
            self.node.create_subscription(
                String, "/mapping_manager/state",
                lambda message: setattr(self, "manager_state", str(message.data).strip().upper()),
                retained_status_qos,
            ),
            self.node.create_subscription(
                String, "/motion_controller/sensor_gate_state",
                lambda message: setattr(self, "sensor_gate_state", str(message.data).strip().lower()),
                retained_status_qos,
            ),
            self.node.create_subscription(
                Bool, "/motion_controller/control_conflict",
                lambda message: setattr(self, "control_conflict", bool(message.data)),
                retained_status_qos,
            ),
        ]
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node, spin_thread=False)
        self.executor = MultiThreadedExecutor(num_threads=1)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def _publish_twist(self, linear: float, angular: float):
        if abs(float(linear)) > 1e-9 or abs(float(angular)) > 1e-9:
            allowed, reason = manager_allows_motion(
                self.manager_state, self.sensor_gate_state
            )
            if not allowed:
                raise RuntimeError(reason or "car_motion_not_authorized")
            if self.control_conflict:
                raise RuntimeError("motion_controller_conflict")
        msg = self.Twist()
        msg.linear.x = float(linear)
        msg.angular.z = float(angular)
        self.publisher.publish(msg)

    def _publish_stop(self, repetitions=8):
        for _ in range(max(1, int(repetitions))):
            self._publish_twist(0.0, 0.0)
            time.sleep(0.04)

    def chassis_stop(self):
        self._publish_stop()
        return {"status": "stopped", "subscribers": int(self.publisher.get_subscription_count())}

    def current_yaw(self):
        import math
        last_error = None
        for parent in ("odom", "map"):
            try:
                transform = self.tf_buffer.lookup_transform(parent, "base_footprint", self.Time())
                q = transform.transform.rotation
                yaw = math.atan2(
                    2.0 * (float(q.w) * float(q.z) + float(q.x) * float(q.y)),
                    1.0 - 2.0 * (float(q.y) ** 2 + float(q.z) ** 2),
                )
                return {"available": True, "yaw": yaw, "frame_id": parent}
            except Exception as exc:
                last_error = exc
        return {"available": False, "error": f"{type(last_error).__name__}: {last_error}"}

    def close(self):
        with contextlib.suppress(Exception):
            self._publish_stop()
        with contextlib.suppress(Exception):
            self.executor.shutdown(timeout_sec=2.0)
        self.thread.join(timeout=2.5)
        with contextlib.suppress(Exception):
            self.node.destroy_node()
        self.status_subscriptions.clear()
        with contextlib.suppress(Exception):
            if self.rclpy.ok():
                self.rclpy.shutdown()


class Facade:
    pass


def load_pet_module():
    path = ROOT / "pet_tracking" / "run.py"
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("resident_pet_worker_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scan_pet_360(pet, detector, ros, stop_event, request):
    """Detect while performing one odometry-measured in-place revolution."""

    import math
    import cv2

    source = str(request.get("source") or "/dev/video22")
    target_pet = str(request.get("pet") or "dog")
    target_classes = [target_pet] if target_pet in {"dog", "cat"} else ["dog", "cat"]
    angular_speed = max(-0.35, min(0.35, float(request.get("angular_speed", 0.20))))
    if abs(angular_speed) < 0.08:
        angular_speed = 0.08 if angular_speed >= 0 else -0.08
    revolutions = max(0.9, min(1.15, float(request.get("revolutions", 1.0))))
    timeout_sec = max(8.0, min(60.0, float(request.get("timeout_sec", 42.0))))
    minimum_confirmations = max(1, min(5, int(request.get("minimum_confirmations", 2))))
    centering = PetCenteringController(
        center_tolerance_ratio=float(request.get("center_tolerance_ratio", 0.08)),
        confirmation_frames=int(request.get("center_confirmation_frames", 3)),
        minimum_turn_speed=0.055,
        maximum_turn_speed=min(0.15, max(0.08, abs(angular_speed) * 0.5)),
        search_speed=min(0.15, max(0.08, abs(angular_speed) * 0.5)),
    )
    output = Path(str(request.get("output") or STATE / f"pet_scan_{int(time.time())}.jpg"))
    output.parent.mkdir(parents=True, exist_ok=True)
    stop_event.clear()
    cap = open_capture(source)
    if not cap.isOpened():
        cap.release()
        return {"ok": False, "found": False, "status": "camera_unavailable", "error": f"camera_unavailable:{source}"}

    started = time.monotonic()
    pose = ros.current_yaw()
    previous_yaw = float(pose.get("yaw", 0.0))
    yaw_source = str(pose.get("frame_id") or "time_fallback") if pose.get("available") else "time_fallback"
    accumulated = 0.0
    confirmations = 0
    frames = 0
    best_score = 0.0
    found = False
    detected = False
    found_path = None
    last_alignment = None
    target_angle = 2.0 * math.pi * revolutions
    publish_stop = threading.Event()
    commanded_angular = angular_speed

    def publish_loop():
        while not publish_stop.is_set() and not stop_event.is_set():
            ros._publish_twist(0.0, commanded_angular)
            publish_stop.wait(0.08)

    publisher = threading.Thread(target=publish_loop, name="pet_scan_velocity", daemon=True)
    publisher.start()
    try:
        while time.monotonic() - started < timeout_sec and not stop_event.is_set():
            ok, frame = cap.read()
            if ok and frame is not None:
                frames += 1
                height, width = frame.shape[:2]
                detections = detector.detect(frame, width, height, target_classes=target_classes)
                if detections:
                    confirmations += 1
                    for detection in detections:
                        score = float(getattr(detection, "score", getattr(detection, "confidence", 0.0)) or 0.0)
                        best_score = max(best_score, score)
                    if confirmations >= minimum_confirmations:
                        detected = True
                        target = max(
                            detections,
                            key=lambda item: (
                                float(getattr(item, "score", getattr(item, "confidence", 0.0)) or 0.0),
                                max(1.0, float(item.rect.right - item.rect.left) * float(item.rect.bottom - item.rect.top)),
                            ),
                        )
                        target_center = (float(target.rect.left) + float(target.rect.right)) * 0.5
                        decision = centering.observe(target_center, width)
                        last_alignment = decision.public()
                        commanded_angular = decision.speed_right - decision.speed_left
                    if confirmations >= minimum_confirmations and decision.centered:
                        commanded_angular = 0.0
                        annotated = frame.copy()
                        for detection in detections:
                            rect = detection.rect
                            x1, y1 = int(rect.left), int(rect.top)
                            x2, y2 = int(rect.right), int(rect.bottom)
                            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 3)
                        cv2.imwrite(str(output), annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
                        found = True
                        found_path = str(output)
                        break
                else:
                    # Require consecutive detector hits.  Slowly decrementing
                    # allowed unrelated one-frame false positives to accumulate
                    # during a full revolution and eventually become a match.
                    confirmations = 0
                    if detected:
                        decision = centering.missing()
                        last_alignment = decision.public()
                        commanded_angular = decision.speed_right - decision.speed_left

            current = ros.current_yaw()
            if current.get("available"):
                yaw = float(current["yaw"])
                delta = (yaw - previous_yaw + math.pi) % (2.0 * math.pi) - math.pi
                # Count only motion in the commanded direction; localization
                # jitter cannot falsely complete a revolution.
                if delta * angular_speed > 0:
                    accumulated += abs(delta)
                previous_yaw = yaw
                yaw_source = str(current.get("frame_id") or yaw_source)
            else:
                accumulated = abs(angular_speed) * (time.monotonic() - started)
                yaw_source = "time_fallback"
            # Completing the nominal revolution is only a not-found terminal
            # condition.  Once a pet has been detected, keep the bounded scan
            # alive until the box is centred (or the overall timeout expires).
            if not detected and accumulated >= target_angle - math.radians(6.0):
                break

        elapsed = time.monotonic() - started
        completed = accumulated >= target_angle - math.radians(10.0)
        cancelled = stop_event.is_set()
        status = "found" if found else ("cancelled" if cancelled else ("completed" if completed else "scan_timeout"))
        return {
            "ok": bool(found or completed) and not cancelled,
            "found": found,
            "detected": detected,
            "centered": found,
            "status": status,
            "pet": target_pet,
            "source": source,
            "frames": frames,
            "confirmations": confirmations,
            "best_score": best_score,
            "swept_angle_rad": accumulated,
            "target_angle_rad": target_angle,
            "direction": "counterclockwise" if angular_speed > 0 else "clockwise",
            "yaw_source": yaw_source,
            "elapsed_sec": round(elapsed, 3),
            "image": found_path,
            "alignment": last_alignment,
        }
    finally:
        publish_stop.set()
        publisher.join(timeout=1.0)
        ros._publish_stop()
        cap.release()


def capture(func, stream=None):
    class Capture(io.StringIO):
        def __init__(self, kind):
            super().__init__()
            self.kind = kind

        def write(self, value):
            written = super().write(value)
            if stream is not None and value:
                stream(self.kind, str(value))
            return written

    stdout, stderr = Capture("stdout"), Capture("stderr")
    code = 0
    started = time.perf_counter()
    try:
        with STDOUT_ROUTER.route(stdout), STDERR_ROUTER.route(stderr):
            value = func()
            if isinstance(value, int):
                code = value
    except SystemExit as exc:
        code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
    except BaseException:
        code = 1
        traceback.print_exc(file=stderr)
    return {"ok": code == 0, "exit_code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(), "worker_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    PID.write_text(str(os.getpid()), encoding="ascii")
    os.environ.setdefault("PET_TRACKING_RKNN_DEVICE", "0004:41:00.0")
    os.environ.setdefault("PET_CAMERA_GUI", "0")
    pet = load_pet_module()
    started = time.perf_counter()
    detector, _ = pet.runtime_models.acquire_or_create(
        "pet_detector",
        lambda: pet.PetTrackingSystem.RKNNDetector(
            pet.PetTrackingSystem.DETECTOR_MODEL,
            conf=pet.PetTrackingSystem.DET_CONF,
            core_mask=4,
        ),
        description="resident pet detector device 0004",
    )
    ros = RosOwner()
    facade = Facade()
    facade.pet_module = pet
    facade.pet_lock = threading.Lock()
    facade.pet_stop = threading.Event()
    facade.camera = NullCameraManager()
    facade.runtime = ros
    pet._RESIDENT_STOP_EVENT = facade.pet_stop
    pet._RESIDENT_CAMERA_FACTORY = open_capture

    class ResidentPetBoard:
        def __init__(self):
            self.max_linear = float(os.getenv("PET_ROS_MAX_LINEAR", "0.55"))
            self.max_angular = float(os.getenv("PET_ROS_MAX_ANGULAR", "1.30"))
            self.legacy_max_speed = float(os.getenv("PET_LEGACY_MAX_SPEED", "300.0"))

        def set_motor_speed(self, speeds):
            motor = {int(item[0]): float(item[1]) for item in speeds}
            right = max(-1.0, min(1.0, motor.get(1, 0.0) / max(self.legacy_max_speed, 1.0)))
            left = max(-1.0, min(1.0, -motor.get(2, 0.0) / max(self.legacy_max_speed, 1.0)))
            ros._publish_twist((left + right) * 0.5 * self.max_linear, (right - left) * self.max_angular)

        def stop(self):
            ros._publish_stop()

        close = stop
        release = stop
        shutdown = stop

    def create_board():
        backend = os.getenv("PET_MOTOR_BACKEND", "ros2").strip().lower()
        return pet.PetTrackingSystem.DummyBoard() if backend in {"dummy", "none", "off", "0", "false"} else ResidentPetBoard()

    pet.PetTrackingSystem._create_board = staticmethod(create_board)
    SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    os.chmod(SOCKET, 0o600)
    server.listen(8)
    server.settimeout(0.1)
    stopping = threading.Event()
    workers = set()
    workers_lock = threading.Lock()

    def stop(_sig, _frame):
        facade.pet_stop.set()
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(json.dumps({"event": "resident_pet_ready", "pid": os.getpid(), "device": os.getenv("PET_TRACKING_RKNN_DEVICE"), "startup_ms": round((time.perf_counter() - started) * 1000.0, 3)}, ensure_ascii=False), flush=True)

    def serve(conn):
        try:
            with conn:
                request, _ = recv_request(conn)
                if request.get("op") == "pet_run":
                    streaming = bool(request.get("stream", False))

                    def stream(kind, data):
                        if streaming:
                            send_response(conn, {"type": kind, "data": data})

                    response = capture(
                        lambda: ResidentSkills._pet(facade, [str(v) for v in request.get("argv", [])]),
                        stream if streaming else None,
                    )
                    if streaming:
                        response = {"type": "final", **response}
                elif request.get("op") == "pet_scan360":
                    response = scan_pet_360(pet, detector, ros, facade.pet_stop, request)
                elif request.get("op") == "pet_stop_scan":
                    facade.pet_stop.set()
                    ros._publish_stop()
                    response = {"ok": True, "found": False, "status": "stop_requested"}
                else:
                    response = {"ok": False, "exit_code": 64, "stdout": "", "stderr": "", "error": "unsupported_operation"}
                send_response(conn, response)
        finally:
            with workers_lock:
                workers.discard(threading.current_thread())

    try:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            worker = threading.Thread(target=serve, args=(conn,), daemon=False)
            with workers_lock:
                workers.add(worker)
            worker.start()
        return 0
    finally:
        server.close()
        facade.pet_stop.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with workers_lock:
                pending = list(workers)
            if not pending:
                break
            for worker in pending:
                worker.join(timeout=0.1)
        ros.close()
        with contextlib.suppress(Exception):
            detector.release()
        if sys.stdout is STDOUT_ROUTER:
            sys.stdout = ORIGINAL_STDOUT
        if sys.stderr is STDERR_ROUTER:
            sys.stderr = ORIGINAL_STDERR
        SOCKET.unlink(missing_ok=True)
        PID.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
