import argparse
import json
import os
import sys
import cv2
import numpy as np
import time
import math
import signal
import threading
import multiprocessing
import subprocess
from enum import Enum, auto
from typing import List, Optional
import warnings
import queue
from contextlib import contextmanager

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CURRENT_DIR)
for _path in (_CURRENT_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pathlib import Path
import types

SKILL_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SKILL_DIR / "assets"
RUNTIME_DIR = Path(os.getenv("SINGLE_FUNCTION_RUNTIME_DIR", str(SKILL_DIR / "runtime")))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


class _RuntimeConfig:
    FACE_CAMERA_ID = os.getenv("FACE_CAMERA_ID", "/dev/video22")
    PET_DETECTOR_MODEL = os.getenv(
        "PET_DETECTOR_MODEL",
        str(ASSETS_DIR / "model" / "yolov8s_rknn3.rknn"),
    )
    PET_DETECTOR_WEIGHT = os.getenv(
        "PET_DETECTOR_WEIGHT",
        str(ASSETS_DIR / "model" / "yolov8s_rknn3.weight"),
    )
    PET_TRACKING_RESULT_PATH = os.getenv(
        "PET_TRACKING_RESULT_PATH",
        str(RUNTIME_DIR / "pet_tracking_result.txt"),
    )
    PET_TRACKING_OUTPUT_VIDEO = os.getenv(
        "PET_TRACKING_OUTPUT_VIDEO",
        str(RUNTIME_DIR / "pet_tracking_record.mp4"),
    )

    @staticmethod
    def get_mp_context():
        default_method = "fork" if os.name == "posix" else "spawn"
        method = os.getenv("PET_MP_START_METHOD", default_method).strip() or default_method
        try:
            return multiprocessing.get_context(method)
        except ValueError:
            return multiprocessing.get_context()


runtime_config = _RuntimeConfig

os.environ.setdefault("FACE_CAMERA_ID", str(runtime_config.FACE_CAMERA_ID))
os.environ.setdefault("PET_DETECTOR_MODEL", str(runtime_config.PET_DETECTOR_MODEL))
os.environ.setdefault("PET_DETECTOR_WEIGHT", str(runtime_config.PET_DETECTOR_WEIGHT))
os.environ.setdefault("PET_TRACKING_RESULT_PATH", str(runtime_config.PET_TRACKING_RESULT_PATH))
os.environ.setdefault("PET_TRACKING_OUTPUT_VIDEO", str(runtime_config.PET_TRACKING_OUTPUT_VIDEO))
os.environ.setdefault("PET_MOTOR_BACKEND", "ros2")
os.environ.setdefault("PET_ROS_CMD_VEL_TOPIC", os.getenv("ROBOT_CMD_VEL_TOPIC", "/cmd_vel"))


class _RuntimeModels:
    def __init__(self):
        self._models = {}

    def acquire_or_create(self, name, factory, *, description=""):
        model = self._models.get(name)
        if model is not None:
            return model, False
        model = factory()
        self._models[name] = model
        return model, True

    def release_all(self):
        for model in list(self._models.values()):
            release = getattr(model, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
        self._models.clear()


runtime_models = _RuntimeModels()
_RESIDENT_STOP_EVENT = None
_RESIDENT_CAMERA_FACTORY = None

_speaker_module = types.ModuleType("speaker")
_speaker_module._mp_q = None


def _speaker_init_mp_queue(q=None):
    _speaker_module._mp_q = q
    return True


def speak(text):
    if text:
        print(text)
    q = getattr(_speaker_module, "_mp_q", None)
    if q is not None:
        try:
            q.put(str(text), block=False)
        except Exception:
            pass
    return True


_speaker_module.init_mp_queue = _speaker_init_mp_queue
_speaker_module.speak = speak
sys.modules.setdefault("speaker", _speaker_module)

from rknn3lite.api import RKNN3Lite

warnings.filterwarnings("ignore")


def _single_function_emit_ready(skill_name, text):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    print(_json.dumps({
        "event": "skill_ready",
        "skill_name": skill_name,
        "kind": "ready",
        "text": text,
    }, ensure_ascii=False), flush=True)


def _single_function_wait_start_gate(start_gate_path, should_stop=None):
    if not start_gate_path:
        return True
    gate = Path(start_gate_path)
    while True:
        if gate.exists():
            return True
        if callable(should_stop) and should_stop():
            return False
        time.sleep(0.05)


def _single_function_emit_progress(skill_name, **payload):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    data = {"event": "skill_progress", "skill_name": skill_name, "kind": "progress"}
    data.update(payload)
    print(_json.dumps(data, ensure_ascii=False), flush=True)


class PetTrackingSystem:
    DETECTOR_MODEL = runtime_config.PET_DETECTOR_MODEL
    WEIGHT_MODEL = runtime_config.PET_DETECTOR_WEIGHT

    DET_CONF = 0.12

    TRACK_SEARCH_TIMEOUT_SEC = 18.0
    TRACK_RECORD_DURATION_SEC = 15.0
    FIND_SEARCH_TIMEOUT_SEC = float(os.getenv("PET_FIND_TIMEOUT_SEC", "15.0"))

    TRACK_SEARCH_SPIN_SPEED = float(os.getenv("PET_TRACK_SEARCH_SPIN_SPEED", "0.18"))

    TRACK_OUTPUT_VIDEO_PATH = runtime_config.PET_TRACKING_OUTPUT_VIDEO
    TRACK_RESULT_PATH = runtime_config.PET_TRACKING_RESULT_PATH
    GUI_AVAILABLE: Optional[bool] = None

    COCO80_NAMES = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
        "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
        "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
        "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
        "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush"
    ]

    COCO_91 = {
        1: "person", 2: "bicycle", 3: "car", 4: "motorcycle", 5: "airplane",
        6: "bus", 7: "train", 8: "truck", 9: "boat", 10: "traffic light",
        11: "fire hydrant", 13: "stop sign", 14: "parking meter", 15: "bench",
        16: "bird", 17: "cat", 18: "dog", 19: "horse", 20: "sheep",
        21: "cow", 22: "elephant", 23: "bear", 24: "zebra", 25: "giraffe",
        27: "backpack", 28: "umbrella", 31: "handbag", 32: "tie", 33: "suitcase",
        34: "frisbee", 35: "skis", 36: "snowboard", 37: "sports ball", 38: "kite",
        39: "baseball bat", 40: "baseball glove", 41: "skateboard", 42: "surfboard",
        43: "tennis racket", 44: "bottle", 46: "wine glass", 47: "cup",
        48: "fork", 49: "knife", 50: "spoon", 51: "bowl", 52: "banana",
        53: "apple", 54: "sandwich", 55: "orange", 56: "broccoli", 57: "carrot",
        58: "hot dog", 59: "pizza", 60: "donut", 61: "cake", 62: "chair",
        63: "couch", 64: "potted plant", 65: "bed", 67: "dining table", 70: "toilet",
        72: "tv", 73: "laptop", 74: "mouse", 75: "remote", 76: "keyboard",
        77: "cell phone", 78: "microwave", 79: "oven", 80: "toaster",
        81: "sink", 82: "refrigerator", 84: "book", 85: "clock", 86: "vase",
        87: "scissors", 88: "teddy bear", 89: "hair drier", 90: "toothbrush"
    }

    class RectF:
        def __init__(self, left, top, right, bottom):
            self.left = float(left)
            self.top = float(top)
            self.right = float(right)
            self.bottom = float(bottom)

        def width(self):
            return max(0.0, self.right - self.left)

        def height(self):
            return max(0.0, self.bottom - self.top)

        def centerX(self):
            return (self.left + self.right) * 0.5

        def centerY(self):
            return (self.top + self.bottom) * 0.5

    class Detection:
        def __init__(self, title, confidence, rect):
            self.title = title
            self.confidence = float(confidence)
            self.rect = rect

    class TrackerState(Enum):
        IDLE = auto()
        TRACKING = auto()
        BUFFER_WAIT = auto()
        SEARCHING = auto()

    class DrawBox:
        def __init__(self, rect, title: str, score: float, is_target: bool):
            self.rect = rect
            self.title = title
            self.score = score
            self.is_target = is_target

    class FPSCounter:
        def __init__(self, smooth: float = 0.90):
            self.smooth = float(smooth)
            self.last_time = None
            self.fps = 0.0

        def update(self) -> float:
            now = time.perf_counter()

            if self.last_time is None:
                self.last_time = now
                return self.fps

            dt = now - self.last_time
            self.last_time = now

            if dt <= 1e-6:
                return self.fps

            instant_fps = 1.0 / dt

            if self.fps <= 0.0:
                self.fps = instant_fps
            else:
                self.fps = self.smooth * self.fps + (1.0 - self.smooth) * instant_fps

            return self.fps

    class DummyBoard:
        def set_motor_speed(self, speeds):
            PetTrackingSystem._debug_motor_log(f"DummyBoard ignored motor command: {speeds}")
            pass

    class MotorQueueProxy:
        def __init__(self, motor_mp_q):
            self.motor_mp_q = motor_mp_q

        def set_motor_speed(self, speeds):
            if self.motor_mp_q is None:
                PetTrackingSystem._debug_motor_log(f"MotorQueueProxy missing queue, drop command: {speeds}")
                return
            try:
                enqueue_mono_ns = time.monotonic_ns()
                payload = {
                    "speeds": speeds,
                    "trace_id": f"{os.getpid()}-{enqueue_mono_ns}",
                    "enqueue_mono_ns": enqueue_mono_ns,
                }
                self.motor_mp_q.put(("set_motor_speed", payload), block=False)
                PetTrackingSystem._debug_motor_log(f"MotorQueueProxy enqueue: {payload}")
            except Exception:
                PetTrackingSystem._debug_motor_log(f"MotorQueueProxy enqueue failed: {speeds}")
                pass

    class Ros2CmdVelBoard:
        def __init__(self):
            self.topic = os.getenv(
                "PET_ROS_CMD_VEL_TOPIC",
                os.getenv("ROBOT_CMD_VEL_TOPIC", "/cmd_vel"),
            )
            self.max_linear = float(os.getenv("PET_ROS_MAX_LINEAR", "0.55"))
            self.max_angular = float(os.getenv("PET_ROS_MAX_ANGULAR", "1.30"))
            self.linear_sign = float(os.getenv(
                "PET_ROS_LINEAR_SIGN",
                os.getenv("ROBOT_CMD_LINEAR_SIGN", "1.0"),
            ))
            self.angular_sign = float(os.getenv(
                "PET_ROS_ANGULAR_SIGN",
                os.getenv("ROBOT_CMD_ANGULAR_SIGN", "1.0"),
            ))
            self.legacy_max_speed = float(os.getenv(
                "PET_LEGACY_MAX_SPEED",
                os.getenv("ROBOT_OLD_MOTOR_MAX_SPEED", "300.0"),
            ))
            self._closed = False

            import rclpy
            from rclpy.node import Node
            from geometry_msgs.msg import Twist

            self.rclpy = rclpy
            self.Twist = Twist

            if not rclpy.ok():
                try:
                    from rclpy.signals import SignalHandlerOptions
                    rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
                except Exception:
                    rclpy.init(args=None)

            self.node = Node(f"pet_camera_cmd_vel_publisher_{os.getpid()}")
            self.pub = self.node.create_publisher(Twist, self.topic, 10)

            PetTrackingSystem._debug_motor_log(
                f"Ros2CmdVelBoard init ok topic={self.topic}, "
                f"max_linear={self.max_linear}, max_angular={self.max_angular}, "
                f"linear_sign={self.linear_sign}, angular_sign={self.angular_sign}"
            )

        def set_motor_speed(self, speeds):
            """
            兼容旧 Board.set_motor_speed([[1, right], [2, left]])。
            PetTrackingSystem.set_motor() 发送：
                [1, max_speed * speed_right]
                [2, max_speed * speed_left * -1]
            这里反向解析为左右轮归一化速度，再转换为 /cmd_vel。
            """
            if self._closed:
                return

            try:
                motor = {int(item[0]): float(item[1]) for item in speeds}

                right_norm = motor.get(1, 0.0) / max(self.legacy_max_speed, 1.0)
                left_norm = -motor.get(2, 0.0) / max(self.legacy_max_speed, 1.0)

                right_norm = max(-1.0, min(1.0, right_norm))
                left_norm = max(-1.0, min(1.0, left_norm))

                linear_x = (left_norm + right_norm) * 0.5 * self.max_linear
                angular_z = (right_norm - left_norm) * self.max_angular

                linear_x *= self.linear_sign
                angular_z *= self.angular_sign

                msg = self.Twist()
                msg.linear.x = float(linear_x)
                msg.linear.y = 0.0
                msg.linear.z = 0.0
                msg.angular.x = 0.0
                msg.angular.y = 0.0
                msg.angular.z = float(angular_z)

                self.pub.publish(msg)
                try:
                    self.rclpy.spin_once(self.node, timeout_sec=0.0)
                except Exception:
                    pass

                PetTrackingSystem._debug_motor_log(
                    f"Ros2CmdVelBoard publish speeds={speeds}, "
                    f"left={left_norm:.3f}, right={right_norm:.3f}, "
                    f"linear_x={linear_x:.4f}, angular_z={angular_z:.4f}"
                )

            except Exception as e:
                PetTrackingSystem._debug_motor_log(
                    f"Ros2CmdVelBoard set_motor_speed failed: {e}, speeds={speeds}"
                )

        def stop(self):
            try:
                repeat = int(os.getenv("PET_ROS_STOP_REPEAT", "8"))
                interval = float(os.getenv("PET_ROS_STOP_INTERVAL_SEC", "0.04"))
                for _ in range(repeat):
                    msg = self.Twist()
                    msg.linear.x = 0.0
                    msg.angular.z = 0.0
                    self.pub.publish(msg)
                    try:
                        self.rclpy.spin_once(self.node, timeout_sec=0.0)
                    except Exception:
                        pass
                    time.sleep(interval)
                PetTrackingSystem._debug_motor_log("Ros2CmdVelBoard stop published zero cmd_vel")
            except Exception as e:
                PetTrackingSystem._debug_motor_log(f"Ros2CmdVelBoard stop failed: {e}")

        def close(self):
            if self._closed:
                return
            try:
                self.stop()
            except Exception:
                pass
            self._closed = True
            try:
                self.node.destroy_node()
            except Exception:
                pass

        def release(self):
            self.close()

        def shutdown(self):
            self.close()

    @staticmethod
    @contextmanager
    def _silence_native_output():
        if os.getenv("PET_TRACKING_SILENCE_CHILD_LOGS", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            yield
            return

        devnull_fd = None
        saved_stdout_fd = None
        saved_stderr_fd = None

        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            saved_stdout_fd = os.dup(1)
            saved_stderr_fd = os.dup(2)

            os.dup2(devnull_fd, 1)
            os.dup2(devnull_fd, 2)

            yield

        finally:
            if saved_stdout_fd is not None:
                os.dup2(saved_stdout_fd, 1)
                os.close(saved_stdout_fd)

            if saved_stderr_fd is not None:
                os.dup2(saved_stderr_fd, 2)
                os.close(saved_stderr_fd)

            if devnull_fd is not None:
                os.close(devnull_fd)

    @classmethod
    def _run_search_task_quietly(cls, *args):
        with cls._silence_native_output():
            return cls.background_pet_search_task(*args)

    @classmethod
    def _run_tracking_task_quietly(cls, *args):
        if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return cls.background_tracking_task(*args)
        with cls._silence_native_output():
            return cls.background_tracking_task(*args)

    class CameraReader:
        def __init__(self, src=r'/dev/video22', width=640, height=640):
            self.cap = _RESIDENT_CAMERA_FACTORY(src) if callable(_RESIDENT_CAMERA_FACTORY) else cv2.VideoCapture(src)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            # ! buffer size
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not self.cap.isOpened():
                raise RuntimeError(f"无法打开摄像头流: {src}")

            self._lock = threading.Lock()
            self._stop = threading.Event()
            self.ret, self.frame = self.cap.read()

            self.thread = threading.Thread(target=self._update, daemon=True)
            self.thread.start()

        def _update(self):
            while not self._stop.is_set():
                ret, frame = self.cap.read()

                if ret and frame is not None:
                    with self._lock:
                        self.ret = ret
                        self.frame = frame
                else:
                    time.sleep(0.005)

            if self.cap.isOpened():
                self.cap.release()

        def read(self):
            with self._lock:
                if self.frame is not None:
                    return self.ret, self.frame.copy()
            return False, None

        def isOpened(self):
            return not self._stop.is_set() and self.cap.isOpened()

        def release(self):
            self._stop.set()

            if self.thread.is_alive():
                self.thread.join(timeout=2.0)

            if self.cap.isOpened():
                self.cap.release()

    class RKNNDetector:
        INPUT_W = 640
        INPUT_H = 640

        @staticmethod
        def _check_device_access():
            if os.name != "posix" or os.geteuid() == 0:
                return
            required_paths = [
                "/dev/dma_heap/system",
                "/dev/dri/renderD128",
            ]
            inaccessible = [
                item for item in required_paths
                if os.path.exists(item) and not os.access(item, os.R_OK | os.W_OK)
            ]
            if inaccessible:
                message = (
                    "RKNN/NPU device permission denied: "
                    + ", ".join(inaccessible)
                    + ". Current boot fix: sudo chmod a+rw /dev/dma_heap/system /dev/dri/renderD128"
                )
                try:
                    PetTrackingSystem._debug_motor_log(message)
                except Exception:
                    pass
                raise PermissionError(message)

        def __init__(self, path, conf=0.25, core_mask=4):
            self.conf = conf
            self._check_device_access()

            mask_map = {
                1: 0x01,
                2: 0x02,
                3: 0x04,
                4: 0x07,
            }
            self._rknn_core = mask_map.get(core_mask, 0x01)

            self.rknn = RKNN3Lite()

            if not os.path.exists(path):
                raise FileNotFoundError(path)

            if not os.path.exists(PetTrackingSystem.WEIGHT_MODEL):
                raise FileNotFoundError(PetTrackingSystem.WEIGHT_MODEL)

            ret = self.rknn.load_rknn(path, PetTrackingSystem.WEIGHT_MODEL)
            if ret != 0:
                raise RuntimeError(f"load_rknn failed: {ret}")

            ids = self.rknn.get_devices_id()
            if not ids:
                raise RuntimeError("没有找到 RKNN 设备")

            preferred = os.getenv("PET_TRACKING_RKNN_DEVICE", "0004:41:00.0")
            selected = ids[-1]
            for item in ids:
                text = item.decode("ascii", errors="ignore") if isinstance(item, bytes) else str(item)
                if text == preferred:
                    selected = item
                    break
            ret = self.rknn.init_runtime(target="rk3588", core_mask=0x01, device_id=selected)
            if ret != 0:
                raise RuntimeError(f"init_runtime failed: {ret}")

        def _preprocess(self, bgr: np.ndarray):
            oh, ow = bgr.shape[:2]

            scale = min(self.INPUT_W / ow, self.INPUT_H / oh)
            nw = int(ow * scale)
            nh = int(oh * scale)

            resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)

            canvas = np.full((self.INPUT_H, self.INPUT_W, 3), 114, dtype=np.uint8)

            pad_left = (self.INPUT_W - nw) // 2
            pad_top = (self.INPUT_H - nh) // 2

            canvas[pad_top:pad_top + nh, pad_left:pad_left + nw] = resized

            inp = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
            inp = np.expand_dims(inp, 0)

            return inp, scale, pad_left, pad_top

        def _postprocess(self, outputs, scale, pad_left, pad_top, orig_w, orig_h, target_cls_ids: set):
            raw = np.array(outputs[0], dtype=np.float32)

            if raw.ndim == 3 and raw.shape[0] == 1:
                raw = raw[0]

            valid = np.any(raw != 0, axis=1)
            raw = raw[valid]

            if len(raw) == 0:
                return []

            scores = raw[:, 0]
            cls_ids = raw[:, 1].astype(np.int32)

            x1_lb = raw[:, 2]
            y1_lb = raw[:, 3]
            x2_lb = raw[:, 4]
            y2_lb = raw[:, 5]

            mask = (scores >= self.conf) & np.isin(cls_ids, list(target_cls_ids))

            if not mask.any():
                return []

            scores = scores[mask]
            cls_ids = cls_ids[mask]

            x1_lb = x1_lb[mask]
            y1_lb = y1_lb[mask]
            x2_lb = x2_lb[mask]
            y2_lb = y2_lb[mask]

            x1 = np.clip((x1_lb - pad_left) / scale, 0, orig_w)
            y1 = np.clip((y1_lb - pad_top) / scale, 0, orig_h)
            x2 = np.clip((x2_lb - pad_left) / scale, 0, orig_w)
            y2 = np.clip((y2_lb - pad_top) / scale, 0, orig_h)

            valid2 = (x2 > x1) & (y2 > y1)

            results = []

            for i in np.where(valid2)[0]:
                if cls_ids[i] < len(PetTrackingSystem.COCO80_NAMES):
                    cls_name = PetTrackingSystem.COCO80_NAMES[cls_ids[i]]
                else:
                    cls_name = f"class_{cls_ids[i]}"

                results.append(
                    PetTrackingSystem.Detection(
                        title=cls_name,
                        confidence=float(scores[i]),
                        rect=PetTrackingSystem.RectF(
                            float(x1[i]),
                            float(y1[i]),
                            float(x2[i]),
                            float(y2[i]),
                        )
                    )
                )

            return results

        def detect(self, bgr: np.ndarray, W: int, H: int, target_classes: List[str]):
            target_cls_ids = {
                i for i, name in enumerate(PetTrackingSystem.COCO80_NAMES)
                if name in target_classes
            }

            if not target_cls_ids:
                return []

            inp, scale, pad_left, pad_top = self._preprocess(bgr)

            outputs = self.rknn.inference(inputs=[inp])

            if outputs is None:
                return []

            return self._postprocess(
                outputs,
                scale,
                pad_left,
                pad_top,
                W,
                H,
                target_cls_ids,
            )

        def release(self):
            try:
                self.rknn.release()
            except Exception:
                pass

    class MultiBoxTracker:
        MIN_SIZE = 24.0
        BASE_SPEED = float(os.getenv("PET_TRACK_BASE_SPEED", "1.0"))
        STEERING_GAIN = float(os.getenv("PET_TRACK_STEERING_GAIN", "0.52"))
        MOVING_STEERING_GAIN = float(os.getenv("PET_TRACK_MOVING_STEERING_GAIN", "0.46"))
        DEAD_ZONE = float(os.getenv("PET_TRACK_DEAD_ZONE", "0.09"))
        MIN_STEER = float(os.getenv("PET_TRACK_MIN_STEER", "0.08"))
        MOVING_MIN_STEER = float(os.getenv("PET_TRACK_MOVING_MIN_STEER", os.getenv("PET_TRACK_MIN_STEER", "0.08")))
        SPEED_EMA_ALPHA = float(os.getenv("PET_TRACK_SPEED_EMA_ALPHA", "0.32"))
        MAX_SPEED_STEP = float(os.getenv("PET_TRACK_MAX_SPEED_STEP", "0.080"))
        STOP_SPEED_STEP = float(os.getenv("PET_TRACK_STOP_SPEED_STEP", "0.120"))
        OUTPUT_DEADBAND = float(os.getenv("PET_TRACK_OUTPUT_DEADBAND", "0.03"))
        WIDE_ERROR_TURN = float(os.getenv("PET_TRACK_WIDE_ERROR_TURN", "0.65"))
        CURVE_START_ERROR = float(os.getenv("PET_TRACK_CURVE_START_ERROR", "0.08"))
        MIN_CURVE_LINEAR_SCALE = float(os.getenv("PET_TRACK_MIN_CURVE_LINEAR_SCALE", "0.18"))
        BOX_EMA_ALPHA = max(0.0, min(1.0, float(os.getenv("PET_TRACK_BOX_EMA_ALPHA", "0.35"))))
        LOST_COAST_SEC = max(0.0, float(os.getenv("PET_TRACK_LOST_COAST_SEC", "0.45")))
        LOST_DECAY = max(0.0, min(1.0, float(os.getenv("PET_TRACK_LOST_DECAY", "0.82"))))
        BUFFER_WAIT_SEC = max(0.1, float(os.getenv("PET_TRACK_BUFFER_WAIT_SEC", "2.0")))

        def __init__(self):
            self.currentState = PetTrackingSystem.TrackerState.IDLE
            self.lastKnownLocation = None
            self.lastMoveDirection = 0.0

            self.currentLeftSpeed = 0.0
            self.currentRightSpeed = 0.0

            self.frameWidth = 640
            self.frameHeight = 480

            self.drawBoxes = []

            self.lastSeenTime = time.time()
            self.searchStartTime = 0.0

            self.stationary_start_time = None
            self.is_backing_up = False

        @staticmethod
        def _clamp(value, low=-1.0, high=1.0):
            return max(low, min(high, value))

        @staticmethod
        def _deadband(value, threshold):
            if abs(value) < threshold:
                return 0.0
            return value

        @staticmethod
        def _limit_delta(prev, target, max_step):
            delta = target - prev

            if delta > max_step:
                delta = max_step
            elif delta < -max_step:
                delta = -max_step

            return prev + delta

        def _smooth_speed_pair(self, target_left, target_right):

            target_left = self._clamp(target_left)
            target_right = self._clamp(target_right)

            target_left = self._deadband(target_left, self.OUTPUT_DEADBAND)
            target_right = self._deadband(target_right, self.OUTPUT_DEADBAND)

            # ! speed EMA 
            ema_left = self.currentLeftSpeed + self.SPEED_EMA_ALPHA * (target_left - self.currentLeftSpeed)
            ema_right = self.currentRightSpeed + self.SPEED_EMA_ALPHA * (target_right - self.currentRightSpeed)

            if abs(target_left) < self.OUTPUT_DEADBAND:
                left_step = self.STOP_SPEED_STEP
            else:
                left_step = self.MAX_SPEED_STEP

            if abs(target_right) < self.OUTPUT_DEADBAND:
                right_step = self.STOP_SPEED_STEP
            else:
                right_step = self.MAX_SPEED_STEP

            smooth_left = self._limit_delta(self.currentLeftSpeed, ema_left, left_step)
            smooth_right = self._limit_delta(self.currentRightSpeed, ema_right, right_step)

            smooth_left = self._clamp(smooth_left)
            smooth_right = self._clamp(smooth_right)

            smooth_left = self._deadband(smooth_left, self.OUTPUT_DEADBAND)
            smooth_right = self._deadband(smooth_right, self.OUTPUT_DEADBAND)

            return smooth_left, smooth_right

        def _select_single_target(self, valid):
            if not valid:
                return None
            return max(valid, key=lambda det: det.confidence)

        @staticmethod
        def _blend_rect(prev, current, alpha):
            if prev is None or alpha >= 1.0:
                return current
            if alpha <= 0.0:
                return prev
            return PetTrackingSystem.RectF(
                prev.left + alpha * (current.left - prev.left),
                prev.top + alpha * (current.top - prev.top),
                prev.right + alpha * (current.right - prev.right),
                prev.bottom + alpha * (current.bottom - prev.bottom),
            )

        def trackResults(self, results, currFrame=None):
            if currFrame is not None:
                self.frameWidth = currFrame.shape[1]
                self.frameHeight = currFrame.shape[0]

            self.drawBoxes.clear()

            # ! min rect size restriction
            valid = [
                r for r in results
                if r.rect.width() >= self.MIN_SIZE and r.rect.height() >= self.MIN_SIZE
            ]

            current_time = time.time()
            best = self._select_single_target(valid)

            # ! refine part: traction
            if best is not None:
                rect = best.rect
                if self.lastKnownLocation is not None:
                    rect = self._blend_rect(self.lastKnownLocation, best.rect, self.BOX_EMA_ALPHA)
                    dx = rect.centerX() - self.lastKnownLocation.centerX()
                    self.lastMoveDirection = self.lastMoveDirection * 0.8 + dx * 0.2
                self.lastKnownLocation = rect

                self.currentState = PetTrackingSystem.TrackerState.TRACKING
                self.lastSeenTime = current_time

                for det in valid:
                    draw_rect = rect if det is best else det.rect
                    self.drawBoxes.append(
                        PetTrackingSystem.DrawBox(
                            draw_rect,
                            det.title,
                            det.confidence,
                            True,
                        )
                    )

                return

            if self.currentState == PetTrackingSystem.TrackerState.TRACKING:
                self.currentState = PetTrackingSystem.TrackerState.BUFFER_WAIT

            elif self.currentState == PetTrackingSystem.TrackerState.BUFFER_WAIT:
                if current_time - self.lastSeenTime > self.BUFFER_WAIT_SEC:
                    self.currentState = PetTrackingSystem.TrackerState.SEARCHING
                    self.searchStartTime = current_time

            elif self.currentState == PetTrackingSystem.TrackerState.SEARCHING:
                if current_time - self.searchStartTime > 6.0:
                    self.currentState = PetTrackingSystem.TrackerState.IDLE
                    self.lastKnownLocation = None

        def updateTarget(self):
            target_left = 0.0
            target_right = 0.0
            state = PetTrackingSystem.TrackerState

            if self.currentState == state.TRACKING and self.lastKnownLocation is not None:
                error = 1.0 - 2.0 * self.lastKnownLocation.centerX() / float(self.frameWidth)
                abs_error = abs(error)

                area = (
                    self.lastKnownLocation.width() * self.lastKnownLocation.height()
                ) / float(self.frameWidth * self.frameHeight)

                height_ratio = self.lastKnownLocation.height() / float(self.frameHeight)

                forward = 0.0

                is_stationary = (
                    abs(self.currentLeftSpeed) < 0.05
                    and abs(self.currentRightSpeed) < 0.05
                )

                if is_stationary:
                    if self.stationary_start_time is None:
                        self.stationary_start_time = time.time()
                else:
                    self.stationary_start_time = None

                if self.is_backing_up:
                    if height_ratio < 0.80:
                        self.is_backing_up = False
                    else:
                        forward = -self.BASE_SPEED * 0.70
                else:
                    if height_ratio < 0.80 and area < 0.40:
                        if height_ratio <= 0.50:
                            raw_forward = self.BASE_SPEED
                        else:
                            raw_forward = self.BASE_SPEED * ((0.80 - height_ratio) / 0.30)

                        forward = max(0.0, raw_forward)

                    if self.stationary_start_time is not None:
                        if time.time() - self.stationary_start_time > 1.0:
                            if height_ratio > 0.80 or area > 0.55:
                                self.is_backing_up = True

                dynamic_dead_zone = self.DEAD_ZONE
                steer = 0.0

                if abs_error > dynamic_dead_zone:
                    if forward > 0.0:
                        steering_gain = self.MOVING_STEERING_GAIN
                        min_steer = self.MOVING_MIN_STEER
                    elif forward < 0.0:
                        steering_gain = self.MOVING_STEERING_GAIN
                        min_steer = self.MOVING_MIN_STEER
                    else:
                        steering_gain = self.STEERING_GAIN
                        min_steer = self.MIN_STEER * 1.5

                    steer = error * steering_gain

                    if forward < 0.0:
                        steer = -steer

                    if 0.0 < steer < min_steer:
                        steer = min_steer
                    elif -min_steer < steer < 0.0:
                        steer = -min_steer

                # Blend translation and steering continuously. Moderate horizontal
                # error follows an arc; only a target near the image edge requires
                # an in-place heading correction.
                if abs_error <= self.CURVE_START_ERROR:
                    curve_scale = 1.0
                elif abs_error >= self.WIDE_ERROR_TURN:
                    curve_scale = 0.0
                else:
                    span = max(self.WIDE_ERROR_TURN - self.CURVE_START_ERROR, 1e-6)
                    curve_scale = (self.WIDE_ERROR_TURN - abs_error) / span
                    curve_scale = max(self.MIN_CURVE_LINEAR_SCALE, curve_scale)
                forward *= curve_scale

                target_left = forward - steer
                target_right = forward + steer

            elif self.currentState == state.BUFFER_WAIT:
                lost_age = time.time() - self.lastSeenTime
                if lost_age <= self.LOST_COAST_SEC:
                    target_left = self.currentLeftSpeed * self.LOST_DECAY
                    target_right = self.currentRightSpeed * self.LOST_DECAY
                else:
                    target_left = 0.0
                    target_right = 0.0
                self.is_backing_up = False
                self.stationary_start_time = None

            elif self.currentState == state.SEARCHING:
                search_speed = PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED

                if self.lastMoveDirection > 0:
                    target_left = search_speed
                    target_right = -search_speed
                else:
                    target_left = -search_speed
                    target_right = search_speed

                self.is_backing_up = False
                self.stationary_start_time = None

            elif self.currentState == state.IDLE:
                target_left = 0.0
                target_right = 0.0
                self.is_backing_up = False
                self.stationary_start_time = None

            self.currentLeftSpeed, self.currentRightSpeed = self._smooth_speed_pair(
                target_left,
                target_right,
            )

            return self.currentLeftSpeed, self.currentRightSpeed

    def __init__(self, model_path=None):
        self.model_path = model_path or self.DETECTOR_MODEL

        self.pid_file = "/tmp/pet_tracking_pid.txt"

        self._process = None

        self.motor_command_handler = None
        self.start_hook = None

        self._motor_mp_q = None
        self._motor_bridge_thread = None
        self._motor_bridge_stop = threading.Event()

    @staticmethod
    def _detect_gui_available() -> bool:
        gui_env = os.getenv("PET_CAMERA_GUI", "").strip().lower()

        if gui_env in {"0", "false", "off", "no"}:
            return False

        if gui_env in {"1", "true", "on", "yes"}:
            return True

        if not os.getenv("DISPLAY"):
            return False

        try:
            probe = subprocess.run(
                ["xdpyinfo"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
            return probe.returncode == 0

        except FileNotFoundError:
            return True

        except Exception:
            return False

    @classmethod
    def _gui_available(cls) -> bool:
        if cls.GUI_AVAILABLE is None:
            cls.GUI_AVAILABLE = cls._detect_gui_available()

            if not cls.GUI_AVAILABLE:
                print("[PetCamera] 未检测到可用图形显示环境，已启用无窗口模式")

        return cls.GUI_AVAILABLE

    @classmethod
    def _show_frame(cls, window_name: str, frame, delay: int = 1) -> int:
        if not cls._gui_available():
            return -1

        cv2.imshow(window_name, frame)

        if delay <= 0:
            return -1

        return cv2.waitKey(delay) & 0xFF

    @classmethod
    def _close_windows(cls) -> None:
        if not cls._gui_available():
            return

        try:
            cv2.destroyAllWindows()

            for _ in range(10):
                cv2.waitKey(1)

        except Exception:
            pass

    @staticmethod
    def _safe_remove_pid(pid_file: str) -> None:
        if not pid_file or not os.path.exists(pid_file):
            return

        try:
            os.remove(pid_file)

        except OSError:
            try:
                with open(pid_file, "w") as f:
                    f.write("")
            except OSError:
                pass

    @staticmethod
    def _debug_motor_log(message: str) -> None:
        path = os.getenv("PET_MOTOR_DEBUG_LOG", "/tmp/pet_motor_bridge.log")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fp:
                fp.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"mono_ns={time.monotonic_ns()} pid={os.getpid()} {message}\n"
                )
        except Exception:
            pass

    @staticmethod
    def _create_board():
        """
        创建底盘控制对象。

        主程序模式：
            通常不会走这里，因为主程序会注入 motor_command_handler，
            pet_camera 子进程会使用 MotorQueueProxy 把速度放进队列。

        单独启动 pet_camera.py：
            没有 motor_mp_q，所以会走这里。
            默认使用 Ros2CmdVelBoard 发布 /cmd_vel。

        环境变量：
            PET_MOTOR_BACKEND=ros2   使用 ROS2 /cmd_vel 控制底盘
            PET_MOTOR_BACKEND=dummy  只跑视觉，不控制底盘
        """
        backend = os.getenv("PET_MOTOR_BACKEND", "ros2").strip().lower()

        if backend in {"dummy", "none", "off", "0", "false"}:
            PetTrackingSystem._debug_motor_log("PET_MOTOR_BACKEND=dummy, using DummyBoard")
            return PetTrackingSystem.DummyBoard()

        if backend in {"ros2", "cmd_vel", "cmdvel", "1", "true"}:
            try:
                return PetTrackingSystem.Ros2CmdVelBoard()
            except Exception as e:
                PetTrackingSystem._debug_motor_log(
                    f"create Ros2CmdVelBoard failed: {e}, fallback DummyBoard"
                )
                print(f"[PetTrackingSystem] ROS2 /cmd_vel 初始化失败，退回 DummyBoard: {e}")
                return PetTrackingSystem.DummyBoard()

        PetTrackingSystem._debug_motor_log(
            f"unknown PET_MOTOR_BACKEND={backend}, fallback DummyBoard"
        )
        return PetTrackingSystem.DummyBoard()

    @staticmethod
    def _ros2_backend_enabled():
        backend = os.getenv("PET_MOTOR_BACKEND", "ros2").strip().lower()
        return backend in {"ros2", "cmd_vel", "cmdvel", "1", "true"}

    @classmethod
    def _publish_ros2_stop_once(cls):
        if not cls._ros2_backend_enabled():
            return
        try:
            board = cls.Ros2CmdVelBoard()
            board.stop()
            try:
                board._closed = True
                board.node.destroy_node()
            except Exception:
                pass
            cls._debug_motor_log("parent published ROS2 zero cmd_vel stop")
        except Exception as e:
            cls._debug_motor_log(f"parent ROS2 zero cmd_vel stop failed: {e}")

    @staticmethod
    def _release_board(board):
        if board is None:
            return

        if isinstance(board, PetTrackingSystem.MotorQueueProxy):
            try:
                PetTrackingSystem.set_motor(board, 0.0, 0.0)
            except Exception:
                pass
            return

        if isinstance(board, PetTrackingSystem.Ros2CmdVelBoard):
            try:
                board.close()
            except Exception:
                pass
            return

        try:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)
            time.sleep(0.05)
        except Exception:
            pass

        try:
            if hasattr(board, "enable_recv"):
                board.enable_recv = False
        except Exception:
            pass

        for method_name in ("release", "close", "shutdown", "stop"):
            try:
                method = getattr(board, method_name, None)
                if callable(method):
                    method()
                    print(f"[PetTrackingSystem] Board.{method_name}() 已调用")
                    return
            except Exception as e:
                print(f"[PetTrackingSystem] 调用 Board.{method_name}() 失败: {e}")

        try:
            port = getattr(board, "port", None)
            if port is not None and hasattr(port, "close"):
                port.close()
                print("[PetTrackingSystem] Board 串口 port 已关闭")
        except Exception as e:
            print(f"[PetTrackingSystem] 关闭 Board 串口失败: {e}")

    @staticmethod
    def set_motor(board, speed_right, speed_left, max_speed=None):
        if board is None:
            return

        try:
            speed_right = max(-1.0, min(1.0, float(speed_right)))
            speed_left = max(-1.0, min(1.0, float(speed_left)))

            if max_speed is None:
                max_speed = float(os.getenv("PET_LEGACY_MAX_SPEED", "300"))
            max_speed = max(1.0, float(max_speed))

            board.set_motor_speed([
                [1, int(max_speed * speed_right)],
                [2, int(max_speed * speed_left * -1)],
            ])

        except Exception as e:
            print(f"[PetTrackingSystem] 设置底轮速度失败: {e}")

    @staticmethod
    def _build_video_writer(frame_shape, output_path: str):
        h, w = frame_shape[:2]

        output_dir = os.path.dirname(output_path)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        for codec in ("mp4v", "avc1", "H264", "XVID", "MJPG"):
            writer = cv2.VideoWriter(
                output_path,
                cv2.VideoWriter_fourcc(*codec),
                20.0,
                (w, h),
            )

            if writer.isOpened():
                return writer

            try:
                writer.release()
            except Exception:
                pass

        return None

    @staticmethod
    def _record_found_video_for_app(cap, first_frame, duration_sec=5.0):
        """Record a stationary post-detection clip and publish it to the app outbox.

        The caller owns ``cap`` and has already stopped the chassis.  Reusing that
        capture avoids reopening /dev/video22 and keeps the resident pet model and
        camera broker hot.  The bridge watches ``skills/runtime/exploration`` for
        the atomically-created ``video_ready.json`` manifest.
        """
        duration_sec = max(0.2, float(duration_sec))
        fps = 15.0
        session = SKILL_DIR.parent / "runtime" / "exploration" / f"pet_search_{int(time.time() * 1000)}"
        session.mkdir(parents=True, exist_ok=False)
        raw_path = session / "doudou_found_raw.mp4"
        final_path = session / "doudou_found_5s.mp4"

        frame = first_frame
        if frame is None or not getattr(frame, "size", 0):
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("post_detection_camera_frame_unavailable")
        height, width = frame.shape[:2]
        writer = cv2.VideoWriter(
            str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
        )
        if not writer.isOpened():
            raise RuntimeError("post_detection_video_writer_open_failed")

        started = time.monotonic()
        next_frame = started
        written = 0
        try:
            while time.monotonic() - started < duration_sec:
                if written == 0:
                    current = frame
                    ok = True
                else:
                    ok, current = cap.read()
                if ok and current is not None:
                    if current.shape[1] != width or current.shape[0] != height:
                        current = cv2.resize(current, (width, height), interpolation=cv2.INTER_LINEAR)
                    writer.write(current)
                    written += 1
                next_frame += 1.0 / fps
                time.sleep(max(0.0, next_frame - time.monotonic()))
        finally:
            writer.release()

        minimum_frames = int(duration_sec * fps * 0.70)
        if written < minimum_frames:
            raise RuntimeError(f"post_detection_video_too_few_frames:{written}")
        conversion = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_path),
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(final_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45.0,
            check=False,
        )
        if conversion.returncode != 0 or not final_path.is_file():
            raise RuntimeError(f"post_detection_transcode_failed:{conversion.stderr[-300:]}")
        try:
            raw_path.unlink()
        except OSError:
            pass

        manifest = {
            "schema": 1,
            "status": "pending_upload",
            "title": "豆豆找到了",
            "video_path": str(final_path),
            "duration_sec": duration_sec,
            "created_at": time.time(),
            "frame_count": written,
            "width": width,
            "height": height,
            "source": "fixed_point_pet_search",
        }
        ready = session / "video_ready.json"
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(ready)
        return manifest

    @staticmethod
    def _spd_bar(canvas, cx, cy, speed, label):
        bh, bw = 100, 26

        x0 = cx - bw // 2
        y0 = cy - bh // 2

        cv2.rectangle(
            canvas,
            (x0, y0),
            (x0 + bw, y0 + bh),
            (50, 50, 50),
            -1,
        )

        fill = int(abs(speed) * bh / 2)

        if speed >= 0:
            col = (50, 220, 50)
            cv2.rectangle(
                canvas,
                (x0 + 2, y0 + bh // 2 - fill),
                (x0 + bw - 2, y0 + bh // 2),
                col,
                -1,
            )
        else:
            col = (50, 50, 220)
            cv2.rectangle(
                canvas,
                (x0 + 2, y0 + bh // 2),
                (x0 + bw - 2, y0 + bh // 2 + fill),
                col,
                -1,
            )

        cv2.rectangle(
            canvas,
            (x0, y0),
            (x0 + bw, y0 + bh),
            (180, 180, 180),
            1,
        )

        cv2.line(
            canvas,
            (x0, cy),
            (x0 + bw, cy),
            (255, 255, 255),
            1,
        )

        cv2.putText(
            canvas,
            label,
            (cx - 8, y0 + bh + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (220, 220, 220),
            1,
        )

        cv2.putText(
            canvas,
            f"{speed:+.2f}",
            (cx - 22, y0 + bh + 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (220, 220, 220),
            1,
        )

    @classmethod
    def draw_tracking_ui(cls, frame, tracker, target_pet: str, fps: Optional[float] = None):
        vis = frame.copy()

        H, W = vis.shape[:2]

        for db in tracker.drawBoxes:
            color = (0, 255, 0)
            status = "Target"

            x1 = int(db.rect.left)
            y1 = int(db.rect.top)
            x2 = int(db.rect.right)
            y2 = int(db.rect.bottom)

            cv2.rectangle(
                vis,
                (x1, y1),
                (x2, y2),
                color,
                3,
            )

            cv2.putText(
                vis,
                f"{db.title}|{status}({db.score:.2f})",
                (x1, max(20, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
                cv2.LINE_AA,
            )

        cls._spd_bar(vis, 36, H - 75, tracker.currentLeftSpeed, "L")
        cls._spd_bar(vis, 76, H - 75, tracker.currentRightSpeed, "R")

        info_lines = [
            f"Mode: Follow [{target_pet}]",
            f"State: {tracker.currentState.name}",
            f"Smooth: alpha={tracker.SPEED_EMA_ALPHA:.2f}, step={tracker.MAX_SPEED_STEP:.3f}",
        ]

        if fps is not None and fps > 0:
            info_lines.append(f"FPS: {fps:.1f}")

        for i, text in enumerate(info_lines):
            cv2.putText(
                vis,
                text,
                (W - 360, 30 + i * 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (220, 220, 220),
                2,
                cv2.LINE_AA,
            )

        return vis

    @staticmethod
    def background_pet_search_task(video_source, model_path, target_pet, tts_mp_q=None, motor_mp_q=None, timeout_sec=None):
        import speaker

        speaker.init_mp_queue(tts_mp_q)

        board = None
        board_owned = False

        if motor_mp_q is not None:
            board = PetTrackingSystem.MotorQueueProxy(motor_mp_q)
            PetTrackingSystem._debug_motor_log("search child using MotorQueueProxy")
        else:
            PetTrackingSystem._debug_motor_log("search child has no motor queue; falling back to _create_board")
            board = PetTrackingSystem._create_board()
            board_owned = not isinstance(board, PetTrackingSystem.DummyBoard)

        detector = None
        detector_owned = False
        cap = None
        found = False
        error = None
        video_manifest = None
        try:
            search_timeout = max(0.1, float(timeout_sec if timeout_sec is not None else PetTrackingSystem.FIND_SEARCH_TIMEOUT_SEC))
        except (TypeError, ValueError):
            search_timeout = PetTrackingSystem.FIND_SEARCH_TIMEOUT_SEC
        start_time = time.time()

        pet_dict = {
            "cat": "小猫",
            "dog": "小狗",
        }

        pet_name = pet_dict.get(target_pet, "宠物")

        if target_pet in {"cat", "dog"}:
            target_classes = [target_pet]
        else:
            target_classes = ["cat", "dog"]

        try:
            detector, detector_owned = runtime_models.acquire_or_create(
                "pet_detector",
                lambda: PetTrackingSystem.RKNNDetector(
                    model_path,
                    conf=PetTrackingSystem.DET_CONF,
                    core_mask=4,
                ),
                description="宠物检测 YOLO RKNN",
            )

            cap = PetTrackingSystem.CameraReader(video_source)

            while cap.isOpened() and (time.time() - start_time) < search_timeout:
                ret, frame = cap.read()

                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                h, w = frame.shape[:2]

                dets = detector.detect(
                    frame,
                    w,
                    h,
                    target_classes=target_classes,
                )

                vis = frame.copy()

                cv2.putText(
                    vis,
                    f"Searching for [{pet_name}]...",
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 165, 255),
                    2,
                )

                if dets:
                    found = True

                    PetTrackingSystem.set_motor(board, 0.0, 0.0)

                    for det in dets:
                        x1 = int(det.rect.left)
                        y1 = int(det.rect.top)
                        x2 = int(det.rect.right)
                        y2 = int(det.rect.bottom)

                        cv2.rectangle(
                            vis,
                            (x1, y1),
                            (x2, y2),
                            (0, 255, 0),
                            3,
                        )

                        cv2.putText(
                            vis,
                            "FOUND",
                            (x1, max(20, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            1,
                            (0, 255, 0),
                            2,
                        )

                    PetTrackingSystem._show_frame("Pet Search", vis, 800)
                    video_manifest = PetTrackingSystem._record_found_video_for_app(
                        cap,
                        vis,
                        duration_sec=5.0,
                    )
                    break

                PetTrackingSystem._show_frame("Pet Search", vis, 1)

                PetTrackingSystem.set_motor(
                    board,
                    speed_right=0.10,
                    speed_left=-0.10,
                )

        except Exception as exc:
            error = str(exc)

        finally:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)

            if board_owned:
                PetTrackingSystem._release_board(board)
                board = None

            if detector_owned:
                detector.release()

            if error is None and found:
                speak(f"这里有一只{pet_name}")
            elif error is None:
                speak(f"抱歉，我没有发现{pet_name}")

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            PetTrackingSystem._close_windows()

            elapsed_sec = round(time.time() - start_time, 3)
            result_payload = {
                "ok": error is None,
                "skill": "pet_tracking",
                "mode": "find",
                "pet": target_pet,
                "source": str(video_source),
                "found": bool(found and error is None),
                "state": "error" if error else ("found" if found else "not_found"),
                "elapsed_sec": elapsed_sec,
                "timeout_sec": search_timeout,
                "video": video_manifest,
                "video_status": (
                    str(video_manifest.get("status"))
                    if isinstance(video_manifest, dict)
                    else ("not_required" if not found else "failed")
                ),
            }
            if error:
                result_payload["error"] = error
            PetTrackingSystem._write_result_payload(result_payload)
            time.sleep(0.2)
            return result_payload

    @staticmethod
    def background_tracking_task(
        video_source,
        model_path,
        target_pet,
        pid_file_path,
        tts_mp_q=None,
        motor_mp_q=None,
        success_tracking_sec=None,
        start_gate_path=None,
        search_timeout_sec=None,
        record_duration_sec=None,
    ):
        import speaker

        with open(PetTrackingSystem.TRACK_RESULT_PATH, "w") as f:
            f.write("failure")

        speaker.init_mp_queue(tts_mp_q)

        is_running = True

        def handle_sigterm(signum, frame_obj):
            nonlocal is_running
            is_running = False

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, handle_sigterm)
            signal.signal(signal.SIGINT, handle_sigterm)

        pet_dict = {
            "cat": "小猫",
            "dog": "小狗",
        }

        pet_name = pet_dict.get(target_pet, "宠物")

        if target_pet in {"cat", "dog"}:
            target_classes = [target_pet]
        else:
            target_classes = ["cat", "dog"]

        cap = None
        board = None
        board_owned = False
        detector = None
        detector_owned = False
        video_writer = None
        video_writer_failed = False
        recording_start_time = None
        search_timeout = max(
            0.5,
            float(search_timeout_sec)
            if search_timeout_sec is not None
            else PetTrackingSystem.TRACK_SEARCH_TIMEOUT_SEC,
        )
        record_duration = max(
            0.2,
            float(record_duration_sec)
            if record_duration_sec is not None
            else PetTrackingSystem.TRACK_RECORD_DURATION_SEC,
        )
        tracking_success_sec = (
            float(success_tracking_sec)
            if success_tracking_sec is not None
            else record_duration
        )
        tracking_success_sec = max(0.2, tracking_success_sec)
        tracked_accum_sec = 0.0
        last_loop_time = None
        wrote_success = False
        error = None
        has_tracked = False
        start_time = time.time()

        try:
            if motor_mp_q is not None:
                board = PetTrackingSystem.MotorQueueProxy(motor_mp_q)
                PetTrackingSystem._debug_motor_log("tracking child using MotorQueueProxy")
            else:
                PetTrackingSystem._debug_motor_log("tracking child has no motor queue; falling back to _create_board")
                board = PetTrackingSystem._create_board()
                board_owned = not isinstance(board, PetTrackingSystem.DummyBoard)

            detector, detector_owned = runtime_models.acquire_or_create(
                "pet_detector",
                lambda: PetTrackingSystem.RKNNDetector(
                    model_path,
                    conf=PetTrackingSystem.DET_CONF,
                    core_mask=4,
                ),
                description="宠物检测 YOLO RKNN",
            )

            tracker = PetTrackingSystem.MultiBoxTracker()
            cap = PetTrackingSystem.CameraReader(video_source)

            fps_counter = PetTrackingSystem.FPSCounter()

            if start_gate_path:
                _single_function_emit_ready("pet_tracking", "我开始寻找宠物。")
                if not _single_function_wait_start_gate(start_gate_path, lambda: not is_running):
                    return

            _single_function_emit_progress("pet_tracking", state="searching", target=target_pet, started_at=start_time)

            if os.path.exists(PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH):
                try:
                    os.remove(PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH)
                except OSError:
                    pass

            # ! 新增：光流法兜底 初始化变量
            last_frame_gray = None
            tracked_features = None 
            last_bbox = None
            # 光流法参数：窗口 15x15，金字塔层数 2
            lk_params = dict(winSize=(15, 15), maxLevel=2,
                             criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03))
            # !       

            while cap.isOpened() and is_running and not (
                _RESIDENT_STOP_EVENT is not None and _RESIDENT_STOP_EVENT.is_set()
            ):
                now = time.time()
                if last_loop_time is None:
                    frame_dt = 0.0
                else:
                    frame_dt = max(0.0, now - last_loop_time)
                last_loop_time = now

                ret, frame = cap.read()

                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                h, w = frame.shape[:2]

                # ! 获取当前帧的灰度图，供光流法使用
                current_frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # !

                dets = detector.detect(
                    frame,
                    w,
                    h,
                    target_classes=target_classes,
                )

                # ! 新增：光流法兜底 核心逻辑
                if dets is not None and len(dets) > 0:
                    # 状况 A：YOLO 成功检测到目标，提取特征点备用
                    # 取出坐标信息，兼容当前 Detection.rect 和旧 list/tuple 格式。
                    try:
                        det_box = dets[0]
                        if hasattr(det_box, "rect"):
                            rect = det_box.rect
                            x1, y1, x2, y2 = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
                        elif hasattr(det_box, "x1"):
                            x1, y1, x2, y2 = int(det_box.x1), int(det_box.y1), int(det_box.x2), int(det_box.y2)
                        else:
                            x1, y1, x2, y2 = map(int, det_box[:4])

                        # 确保坐标在图像范围内
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(w, x2), min(h, y2)

                        if x2 > x1 and y2 > y1:
                            last_bbox = [x1, y1, x2, y2]
                            roi_gray = current_frame_gray[y1:y2, x1:x2]

                            # 在 YOLO 框内提取最多 15 个强角点
                            corners = cv2.goodFeaturesToTrack(roi_gray, maxCorners=15, qualityLevel=0.1, minDistance=5)

                            if corners is not None:
                                # 将局部坐标还原回整图坐标
                                corners[:, 0, 0] += x1
                                corners[:, 0, 1] += y1
                                tracked_features = corners
                    except Exception:
                        pass

                else:
                    # 状况 B：YOLO 漏检，启动光流盲推
                    if last_frame_gray is not None and tracked_features is not None and len(tracked_features) > 0:
                        new_features, status, error = cv2.calcOpticalFlowPyrLK(
                            last_frame_gray, current_frame_gray, tracked_features, None, **lk_params
                        )

                        good_new = new_features[status == 1]
                        good_old = tracked_features[status == 1]

                        if len(good_new) > 2:
                            movements = good_new - good_old
                            dx, dy = np.mean(movements, axis=0)

                            ox1, oy1, ox2, oy2 = last_bbox
                            nx1 = max(0, int(ox1 + dx))
                            ny1 = max(0, int(oy1 + dy))
                            nx2 = min(w, int(ox2 + dx))
                            ny2 = min(h, int(oy2 + dy))

                            last_bbox = [nx1, ny1, nx2, ny2]
                            tracked_features = good_new.reshape(-1, 1, 2)

                            # 伪造一份 Detection，保持 trackResults 所需的 .rect 接口。
                            mock_det = PetTrackingSystem.Detection(
                                target_classes[0] if target_classes else target_pet,
                                0.5,
                                PetTrackingSystem.RectF(nx1, ny1, nx2, ny2),
                            )
                            dets = [mock_det]
                        else:
                            tracked_features = None
                            last_bbox = None

                # 记录当前灰度图给下一帧使用
                last_frame_gray = current_frame_gray.copy()
                # !

                tracker.trackResults(dets, frame)

                current_fps = fps_counter.update()

                vis = PetTrackingSystem.draw_tracking_ui(
                    frame,
                    tracker,
                    target_pet,
                    fps=current_fps,
                )

                key = PetTrackingSystem._show_frame("Pet Follower", vis, 1)

                if video_writer is None and not video_writer_failed:
                    video_writer = PetTrackingSystem._build_video_writer(
                        vis.shape,
                        PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH,
                    )

                    if video_writer is None:
                        video_writer_failed = True
                        print(
                            f"警告: 无法创建带框宠物追踪视频，继续执行追踪: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )
                    else:
                        print(
                            f"开始录制带框宠物追踪视频: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                if video_writer is not None:
                    video_writer.write(vis)

                if not has_tracked:
                    if tracker.currentState == PetTrackingSystem.TrackerState.TRACKING:
                        has_tracked = True
                        recording_start_time = now
                        _single_function_emit_progress("pet_tracking", state="tracking", target=target_pet, last_seen_at=now)

                        print(
                            f"已锁定{pet_name}，继续录制带框视频: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                    else:
                        PetTrackingSystem.set_motor(
                            board,
                            speed_right=PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                            speed_left=-PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                        )

                        if now - start_time > search_timeout:
                            _single_function_emit_progress("pet_tracking", state="not_found", target=target_pet, elapsed_seconds=round(now - start_time, 2))
                            speak("对不起，我没找到宠物")
                            print("寻找超时，未找到目标宠物，跟踪进程结束")
                            break

                        if key in [ord("x"), ord("e"), ord("q"), 27]:
                            print("\n收到按键退出指令")
                            break

                        continue

                left_speed, right_speed = tracker.updateTarget()

                if tracker.currentState == PetTrackingSystem.TrackerState.IDLE:
                    PetTrackingSystem.set_motor(
                        board,
                        speed_right=PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                        speed_left=-PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                    )
                else:
                    tracked_accum_sec += frame_dt
                    if not wrote_success and tracked_accum_sec >= tracking_success_sec:
                        with open(PetTrackingSystem.TRACK_RESULT_PATH, "w") as f:
                            f.write("success")
                        wrote_success = True
                        print(f"累计稳定追踪 {tracked_accum_sec:.2f}s，判定追踪成功")
                    PetTrackingSystem.set_motor(
                        board,
                        speed_right=right_speed,
                        speed_left=left_speed,
                    )

                if recording_start_time is not None:
                    if now - recording_start_time >= record_duration:
                        print(
                            f"视频录制完成（{record_duration:.0f}s）: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                        with open(PetTrackingSystem.TRACK_RESULT_PATH, "w") as f:
                            f.write("success")

                        break

                if key in [ord("x"), ord("e"), ord("q"), 27]:
                    print("\n收到按键退出指令")
                    break

        except Exception as e:
            print(f"异常: {e}")
            error = str(e)

        finally:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)

            if board_owned:
                PetTrackingSystem._release_board(board)
                board = None

            if detector is not None and detector_owned:
                detector.release()

            if video_writer is not None:
                try:
                    video_writer.release()
                except Exception:
                    pass

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            PetTrackingSystem._close_windows()

            PetTrackingSystem._safe_remove_pid(pid_file_path)

            time.sleep(0.2)

        return {
            "ok": error is None,
            "skill": "pet_tracking",
            "mode": "track",
            "pet": target_pet,
            "source": str(video_source),
            "found": bool(has_tracked and error is None),
            "state": "error" if error else ("tracked" if has_tracked else "not_found"),
            "tracking_success": bool(wrote_success and error is None),
            "elapsed_sec": round(time.time() - start_time, 3),
            "error": error,
        }

    def _ensure_motor_bridge(self, ctx):
        if not callable(self.motor_command_handler):
            self._debug_motor_log("motor_command_handler 未注入，宠物追踪将不直接控制底盘")
            return None

        if self._motor_mp_q is None:
            self._motor_mp_q = ctx.Queue()
            self._debug_motor_log("created multiprocessing motor queue")

        if self._motor_bridge_thread is None or not self._motor_bridge_thread.is_alive():
            self._motor_bridge_stop.clear()

            self._motor_bridge_thread = threading.Thread(
                target=self._motor_bridge_loop,
                daemon=True,
                name="pet-tracking-motor-bridge",
            )

            self._motor_bridge_thread.start()
            self._debug_motor_log("started pet motor bridge thread")

        return self._motor_mp_q

    def _motor_bridge_loop(self):
        while not self._motor_bridge_stop.is_set():
            try:
                cmd, payload = self._motor_mp_q.get(timeout=0.2)

            except queue.Empty:
                continue

            except Exception:
                continue

            if cmd == "set_motor_speed" and callable(self.motor_command_handler):
                try:
                    receive_mono_ns = time.monotonic_ns()
                    if isinstance(payload, dict) and "speeds" in payload:
                        speeds = payload.get("speeds")
                        payload["bridge_receive_mono_ns"] = receive_mono_ns
                    else:
                        speeds = payload
                        payload = {
                            "speeds": speeds,
                            "trace_id": "",
                            "enqueue_mono_ns": 0,
                            "bridge_receive_mono_ns": receive_mono_ns,
                        }
                    self._debug_motor_log(f"bridge received motor command: {payload}")
                    try:
                        self.motor_command_handler(speeds, trace_meta=payload)
                    except TypeError:
                        self.motor_command_handler(speeds)
                except Exception as e:
                    self._debug_motor_log(f"bridge failed to publish motor command {payload}: {e}")
                    print(f"宠物追踪电机桥接失败: {e}")

    def _drain_motor_queue(self):
        q = self._motor_mp_q
        if q is None:
            return
        for _ in range(200):
            try:
                q.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return

    def _stop_motor_bridge(self):
        self._motor_bridge_stop.set()

        if self._motor_bridge_thread is not None and self._motor_bridge_thread.is_alive():
            self._motor_bridge_thread.join(timeout=1.0)

        self._drain_motor_queue()
        self._motor_bridge_thread = None
        self._motor_mp_q = None

    @staticmethod
    def _write_result_payload(payload):
        result_path = str(PetTrackingSystem.TRACK_RESULT_PATH)
        result_dir = os.path.dirname(result_path)
        if result_dir:
            os.makedirs(result_dir, exist_ok=True)
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except OSError as exc:
            print(f"write_pet_tracking_result_failed:{exc}", flush=True)

    @staticmethod
    def _read_result_payload():
        try:
            with open(PetTrackingSystem.TRACK_RESULT_PATH, "r", encoding="utf-8", errors="replace") as f:
                payload = json.loads(f.read())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def find_pet(self, video_source, target_pet, timeout_sec=None):
        print(f"启动寻宠进程: {target_pet}")

        import speaker

        try:
            search_timeout = max(1.0, float(timeout_sec if timeout_sec is not None else self.FIND_SEARCH_TIMEOUT_SEC))
        except (TypeError, ValueError):
            search_timeout = self.FIND_SEARCH_TIMEOUT_SEC
        self._write_result_payload(
            {
                "ok": False,
                "skill": "pet_tracking",
                "mode": "find",
                "pet": target_pet,
                "source": str(video_source),
                "found": False,
                "state": "pending",
                "timeout_sec": search_timeout,
            }
        )

        ctx = runtime_config.get_mp_context()

        if speaker._mp_q is None:
            speaker.init_mp_queue(ctx.Queue())

        begin_session = getattr(self.motor_command_handler, "begin_session", None)
        if callable(begin_session):
            try:
                begin_session()
            except Exception as e:
                self._debug_motor_log(f"begin_session failed: {e}")

        motor_mp_q = self._ensure_motor_bridge(ctx)
        self._debug_motor_log(f"find_pet motor_queue_ready={motor_mp_q is not None}")

        p = ctx.Process(
            target=self.__class__._run_search_task_quietly,
            args=(
                video_source,
                self.model_path,
                target_pet,
                speaker._mp_q,
                motor_mp_q,
                search_timeout,
            ),
            daemon=True,
        )

        p.start()
        p.join(timeout=search_timeout + 45.0)
        timed_out = p.is_alive()
        if timed_out:
            p.terminate()
            p.join(timeout=3.0)
            if p.is_alive():
                p.kill()
                p.join(timeout=2.0)

        self._stop_motor_bridge()
        stop_motion = getattr(self.motor_command_handler, "stop_motion", None)
        if callable(stop_motion):
            try:
                stop_motion(accept_commands=False)
            except Exception:
                pass

        print("寻宠进程已退出")
        result = self._read_result_payload()
        if not isinstance(result, dict) or result.get("state") == "pending":
            result = {
                "ok": False,
                "skill": "pet_tracking",
                "mode": "find",
                "pet": target_pet,
                "source": str(video_source),
                "found": False,
                "state": "error",
                "timeout_sec": search_timeout,
                "error": "find_process_timeout" if timed_out else "missing_find_result",
            }
            self._write_result_payload(result)
        elif timed_out:
            result["ok"] = False
            result["found"] = False
            result["state"] = "error"
            result["error"] = "find_process_timeout"
            self._write_result_payload(result)
        return result

    def start_pet_tracking(
        self,
        video_source,
        target_pet,
        success_tracking_sec=None,
        start_gate_path=None,
        search_timeout_sec=None,
        record_duration_sec=None,
    ):
        print(f"启动进程跟踪宠物: {target_pet}")

        if self._process is not None and self._process.is_alive():
            return

        self.__class__._safe_remove_pid(self.pid_file)
        begin_session = getattr(self.motor_command_handler, "begin_session", None)
        if callable(begin_session):
            try:
                begin_session()
            except Exception as e:
                self._debug_motor_log(f"begin_session failed: {e}")

        import speaker

        ctx = runtime_config.get_mp_context()

        if speaker._mp_q is None:
            speaker.init_mp_queue(ctx.Queue())

        motor_mp_q = self._ensure_motor_bridge(ctx)
        self._debug_motor_log(f"start_pet_tracking motor_queue_ready={motor_mp_q is not None}")

        p = ctx.Process(
            target=self.__class__._run_tracking_task_quietly,
            args=(
                video_source,
                self.model_path,
                target_pet,
                self.pid_file,
                speaker._mp_q,
                motor_mp_q,
                success_tracking_sec,
                start_gate_path,
                search_timeout_sec,
                record_duration_sec,
            ),
            daemon=True,
        )

        p.start()
        self._process = p

        if callable(self.start_hook):
            try:
                self.start_hook(target_pet)
            except Exception as e:
                print(f"宠物追踪启动钩子失败: {e}")

        try:
            with open(self.pid_file, "w") as f:
                f.write(str(p.pid))
        except OSError as e:
            print(f"写入 PID 文件失败: {e}")

    def stop_pet_tracking(self):
        try:
            self._stop_motor_bridge()
            self.__class__._publish_ros2_stop_once()
            stop_motion = getattr(self.motor_command_handler, "stop_motion", None)
            if callable(stop_motion):
                try:
                    stop_motion(accept_commands=False)
                except Exception:
                    pass
            elif callable(self.motor_command_handler):
                try:
                    self.motor_command_handler([[1, 0], [2, 0]])
                except Exception:
                    pass
            self._terminate_process()

        finally:
            self.__class__._safe_remove_pid(self.pid_file)

            stop_motion = getattr(self.motor_command_handler, "stop_motion", None)
            if callable(stop_motion):
                try:
                    stop_motion(accept_commands=False)
                except Exception:
                    pass
            elif callable(self.motor_command_handler):
                try:
                    self.motor_command_handler([[1, 0], [2, 0]])
                except Exception:
                    pass

            self._stop_motor_bridge()

        print("已经关闭宠物跟踪进程")

    def _terminate_process(self):
        if self._process is not None:
            try:
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=3.0)

                    if self._process.is_alive():
                        self._process.kill()
                        self._process.join(timeout=2.0)

            except Exception:
                pass

            finally:
                self._process = None

            return

        if not os.path.exists(self.pid_file):
            return

        try:
            with open(self.pid_file) as f:
                pid_str = f.read().strip()

            if not pid_str.isdigit():
                return

            pid = int(pid_str)

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(3.0)

                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)

                except ProcessLookupError:
                    pass

            except ProcessLookupError:
                pass

            except PermissionError:
                pass

        except Exception:
            pass

    @classmethod
    def smoke_test(cls):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        tracker = cls.MultiBoxTracker()

        detections = [
            cls.Detection(
                "dog",
                0.91,
                cls.RectF(230, 130, 410, 390),
            )
        ]

        tracker.trackResults(detections, frame)

        for i in range(20):
            left, right = tracker.updateTarget()
            print(f"step={i:02d}, left={left:.3f}, right={right:.3f}")

        vis = cls.draw_tracking_ui(frame, tracker, "dog")

        print("PetTrackingSystem smoke test passed")
        print(
            f"state={tracker.currentState.name}, "
            f"left={tracker.currentLeftSpeed:.3f}, "
            f"right={tracker.currentRightSpeed:.3f}, "
            f"frame_shape={vis.shape}"
        )

    @staticmethod
    def _normalize_video_source(value):
        if isinstance(value, int):
            return value

        text = str(value)

        return int(text) if text.isdigit() else text

    @classmethod
    def main(cls):
        parser = argparse.ArgumentParser(description="PetTrackingSystem")
        parser.add_argument(
            "action",
            nargs="?",
            choices=["smoke", "find", "find_route", "find_and_track", "find_route_and_track", "track", "stop"],
            default=None,
            help="single_function action; default: track",
        )
        parser.add_argument(
            "--mode",
            choices=["smoke", "find", "find_route", "find_and_track", "find_route_and_track", "track", "stop"],
            default=None,
            help="Explicit mode; overrides positional action.",
        )
        parser.add_argument(
            "--source",
            default=os.getenv("PET_CAMERA_ID", str(runtime_config.FACE_CAMERA_ID)),
            help="Camera id, device path, video path, or RTSP URL. Default: /dev/video22.",
        )
        parser.add_argument(
            "--camera",
            default=None,
            help="Alias for --source.",
        )
        parser.add_argument(
            "--pet",
            choices=["cat", "dog", "all"],
            default="dog",
        )
        parser.add_argument(
            "--duration",
            type=float,
            default=20.0,
            help="Track duration in seconds. Use 0 to run until Ctrl+C or internal completion.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=None,
            help="Alias for --duration, used by single_function runners.",
        )
        parser.add_argument(
            "--search-timeout",
            type=float,
            default=None,
            help="Maximum seconds to rotate and search before giving up.",
        )
        parser.add_argument(
            "--track-duration",
            type=float,
            default=None,
            help="Seconds to follow and record after the target is acquired.",
        )
        parser.add_argument("--base-speed", type=float, default=None, help="Normalized forward tracking speed.")
        parser.add_argument("--max-linear", type=float, default=None, help="Maximum ROS linear velocity in m/s.")
        parser.add_argument("--max-angular", type=float, default=None, help="Maximum ROS angular velocity in rad/s.")
        parser.add_argument("--steering-gain", type=float, default=None, help="Tracking steering gain.")
        parser.add_argument("--speed-ema-alpha", type=float, default=None, help="Speed response smoothing factor.")
        parser.add_argument("--max-speed-step", type=float, default=None, help="Maximum normalized speed change per frame.")
        parser.add_argument("--search-spin-speed", type=float, default=None, help="Normalized in-place search rotation speed.")
        parser.add_argument(
            "--search-mode",
            default=None,
            help=argparse.SUPPRESS,
        )
        parser.add_argument(
            "--model",
            default=cls.DETECTOR_MODEL,
            help="RKNN detector model path.",
        )
        parser.add_argument(
            "--backend",
            choices=["ros2", "dummy"],
            default=os.getenv("PET_MOTOR_BACKEND", "ros2"),
            help="ros2 publishes Twist to /cmd_vel; dummy runs vision without moving.",
        )
        parser.add_argument(
            "--cmd-vel-topic",
            default=os.getenv("PET_ROS_CMD_VEL_TOPIC", os.getenv("ROBOT_CMD_VEL_TOPIC", "/cmd_vel")),
            help="ROS2 Twist topic. Default: /cmd_vel.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print a JSON status line when the action finishes.",
        )
        parser.add_argument(
            "--start-gate",
            default=None,
            help="Path to a start gate file; tracking starts after the file appears.",
        )
        parser.add_argument(
            "--resume-from-interrupt",
            action="store_true",
            help="Accepted by the task runtime; tracking resumes by restoring scene and searching again.",
        )
        parser.add_argument(
            "--target",
            default=None,
            help="Optional target hint restored from interruption context.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate arguments without opening camera/NPU or publishing cmd_vel.",
        )

        argv = []
        for item in sys.argv[1:]:
            if item == "--no-gui":
                os.environ["PET_CAMERA_GUI"] = "0"
                continue
            argv.append(item)
        args = parser.parse_args(argv)
        args.mode = args.mode or args.action or "track"
        find_modes = {"find", "find_route", "find_and_track", "find_route_and_track"}
        track_after_find_modes = {"find_and_track", "find_route_and_track"}
        if args.camera:
            args.source = args.camera
        if args.timeout is not None:
            args.duration = args.timeout

        os.environ["PET_MOTOR_BACKEND"] = args.backend
        os.environ["PET_ROS_CMD_VEL_TOPIC"] = args.cmd_vel_topic
        os.environ.setdefault("PET_CAMERA_GUI", "0")
        if args.base_speed is not None:
            cls.MultiBoxTracker.BASE_SPEED = max(0.0, min(1.0, float(args.base_speed)))
            os.environ["PET_TRACK_BASE_SPEED"] = str(cls.MultiBoxTracker.BASE_SPEED)
        if args.max_linear is not None:
            os.environ["PET_ROS_MAX_LINEAR"] = str(max(0.0, float(args.max_linear)))
        if args.max_angular is not None:
            os.environ["PET_ROS_MAX_ANGULAR"] = str(max(0.0, float(args.max_angular)))
        if args.steering_gain is not None:
            cls.MultiBoxTracker.STEERING_GAIN = max(0.0, float(args.steering_gain))
            cls.MultiBoxTracker.MOVING_STEERING_GAIN = cls.MultiBoxTracker.STEERING_GAIN
            os.environ["PET_TRACK_STEERING_GAIN"] = str(cls.MultiBoxTracker.STEERING_GAIN)
        if args.speed_ema_alpha is not None:
            cls.MultiBoxTracker.SPEED_EMA_ALPHA = max(0.0, min(1.0, float(args.speed_ema_alpha)))
            os.environ["PET_TRACK_SPEED_EMA_ALPHA"] = str(cls.MultiBoxTracker.SPEED_EMA_ALPHA)
        if args.max_speed_step is not None:
            cls.MultiBoxTracker.MAX_SPEED_STEP = max(0.001, float(args.max_speed_step))
            os.environ["PET_TRACK_MAX_SPEED_STEP"] = str(cls.MultiBoxTracker.MAX_SPEED_STEP)
        if args.search_spin_speed is not None:
            cls.TRACK_SEARCH_SPIN_SPEED = max(0.0, min(1.0, float(args.search_spin_speed)))
            os.environ["PET_TRACK_SEARCH_SPIN_SPEED"] = str(cls.TRACK_SEARCH_SPIN_SPEED)

        if args.dry_run:
            print(json.dumps({
                "ok": True,
                "skill": "pet_tracking",
                "dry_run": True,
                "mode": args.mode,
                "source": str(args.source),
                "pet": args.pet,
                "timeout_sec": args.duration if args.mode in find_modes else None,
                "backend": args.backend,
                "cmd_vel_topic": args.cmd_vel_topic,
                "model": str(args.model),
            }, ensure_ascii=False))
            return

        print("=" * 80)
        print("[PetCamera]")
        print(f"mode    : {args.mode}")
        print(f"source  : {args.source}")
        print(f"pet     : {args.pet}")
        print(f"backend : {args.backend}")
        print(f"cmd_vel : {args.cmd_vel_topic}")
        print("=" * 80)

        find_result = None
        if args.mode == "smoke":
            cls.smoke_test()
        else:
            target_pet = args.pet if args.pet != "all" else "pet"
            source = cls._normalize_video_source(args.source)
            system = cls(model_path=args.model)

            if args.mode in find_modes and args.mode not in track_after_find_modes:
                find_result = system.find_pet(source, target_pet, timeout_sec=args.duration)
            elif args.mode == "track" or args.mode in track_after_find_modes:
                search_timeout = max(
                    0.5,
                    float(args.search_timeout)
                    if args.search_timeout is not None
                    else cls.TRACK_SEARCH_TIMEOUT_SEC,
                )
                record_duration = max(
                    0.2,
                    float(args.track_duration)
                    if args.track_duration is not None
                    else float(args.duration),
                )
                # !
                success_tracking_sec = max(0.2, record_duration * 0.2)
                started_at = time.time()
                system.start_pet_tracking(
                    source,
                    target_pet,
                    success_tracking_sec=success_tracking_sec,
                    start_gate_path=args.start_gate,
                    search_timeout_sec=search_timeout,
                    record_duration_sec=record_duration,
                )
                try:
                    deadline = time.time() + search_timeout + record_duration + 5.0
                    while system._process is not None and system._process.is_alive() and time.time() < deadline:
                        system._process.join(timeout=0.2)
                except KeyboardInterrupt:
                    pass
                finally:
                    system.stop_pet_tracking()
                if args.mode in track_after_find_modes:
                    track_text = ""
                    try:
                        with open(cls.TRACK_RESULT_PATH, "r", encoding="utf-8", errors="replace") as fp:
                            track_text = fp.read().strip().lower()
                    except OSError:
                        pass
                    found = track_text == "success"
                    find_result = {
                        "ok": True,
                        "skill": "pet_tracking",
                        "mode": "find_and_track",
                        "pet": target_pet,
                        "source": str(source),
                        "found": found,
                        "state": "tracked" if found else "not_found",
                        "elapsed_sec": round(time.time() - started_at, 3),
                        "timeout_sec": search_timeout,
                    }
            elif args.mode == "stop":
                system.stop_pet_tracking()

        if args.json:
            track_result = None
            result_path = str(PetTrackingSystem.TRACK_RESULT_PATH)
            if args.mode in {"track", "find_and_track"} and os.path.exists(result_path):
                try:
                    with open(result_path, "r", encoding="utf-8", errors="replace") as fp:
                        track_result = fp.read().strip()
                except OSError:
                    track_result = None
            if args.mode in find_modes and find_result is None:
                find_result = PetTrackingSystem._read_result_payload()
            payload = {
                "ok": True,
                "skill": "pet_tracking",
                "mode": args.mode,
                "source": str(args.source),
                "pet": args.pet,
                "timeout_sec": args.duration if args.mode in find_modes else None,
                "backend": args.backend,
                "cmd_vel_topic": args.cmd_vel_topic,
                "track_result": track_result,
                "result_path": result_path,
            }
            if args.mode in find_modes and isinstance(find_result, dict):
                payload["ok"] = bool(find_result.get("ok", True))
                payload["found"] = bool(find_result.get("found", False))
                payload["state"] = str(find_result.get("state") or "")
                payload["elapsed_sec"] = find_result.get("elapsed_sec")
                payload["find_result"] = find_result
                payload["tracked_after_found"] = bool(args.mode in track_after_find_modes and find_result.get("found"))
                payload["video_path"] = str(PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH) if payload["tracked_after_found"] else None
                if find_result.get("error"):
                    payload["error"] = find_result.get("error")
            print(json.dumps(payload, ensure_ascii=False))
        if args.mode in find_modes and isinstance(find_result, dict) and find_result.get("ok") is False:
            raise SystemExit(1)


def _cli_backend_from_argv():
    backend = os.getenv("PET_MOTOR_BACKEND", "ros2")
    for index, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--backend" and index + 1 < len(sys.argv):
            backend = sys.argv[index + 1]
        elif arg.startswith("--backend="):
            backend = arg.split("=", 1)[1]
    return str(backend or "ros2").strip().lower()


def _maybe_reexec_with_ros_env_for_cli():
    if "--help" in sys.argv or "-h" in sys.argv or "--dry-run" in sys.argv:
        return
    if os.environ.get("PET_CAMERA_ROS_ENV_REEXEC") == "1":
        return
    if _cli_backend_from_argv() in {"dummy", "none", "off", "0", "false"}:
        return
    try:
        import rclpy  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    env = os.environ.copy()
    env["PET_CAMERA_ROS_ENV_REEXEC"] = "1"
    setup_files = [
        "/opt/ros/humble/setup.bash",
        "/home/test/car_real_copy_zhenghang/install/setup.bash",
    ]
    source_lines = "\n".join(
        f'[ -f "{item}" ] && source "{item}"' for item in setup_files
    )
    script = (
        "set -e\n"
        + source_lines
        + "\nexport PET_CAMERA_ROS_ENV_REEXEC=1\n"
        + "exec \"$@\"\n"
    )
    print(
        "[PetCamera] rclpy is not available in this shell; "
        "sourcing ROS2 setup files and restarting pet_camera.py",
        flush=True,
    )
    os.execvpe(
        "bash",
        ["bash", "-lc", script, "pet-camera-ros-env", sys.executable, *sys.argv],
        env,
    )

if __name__ == "__main__":
    _maybe_reexec_with_ros_env_for_cli()
    PetTrackingSystem.main()
