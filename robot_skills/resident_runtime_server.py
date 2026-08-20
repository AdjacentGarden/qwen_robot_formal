#!/usr/bin/env python3
"""Resident owner for all standalone skills.

The process owns ROS/DDS, model contexts and lazily cached Python modules.  Every
skill wrapper is a thin Unix-socket client, so command execution no longer starts
a new heavy Python interpreter or a new ROS participant.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shlex
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from shared_runtime_server import SharedRuntime, recv_request, send_response
from resident_camera_ipc import SharedCameraError, open_capture


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "resident"
SOCKET_PATH = STATE_DIR / "skills.sock"
PID_FILE = STATE_DIR / "server.pid"
STATUS_FILE = STATE_DIR / "status.json"

CAMERA_SKILLS = {
    "back_camera_capture": ("back", "capture"),
    "back_camera_record": ("back", "record"),
    "camera_capture": (None, "capture"),
    "camera_record": (None, "record"),
    "front_camera_capture": ("front", "capture"),
    "front_camera_record": ("front", "record"),
}
MOVE_SKILLS = {
    "move_forward": (1.0, 0.0),
    "move_backward": (-1.0, 0.0),
    "move_left": (0.0, 1.0),
    "move_right": (0.0, -1.0),
}
GENERIC_SKILLS = {
    "environment_perception",
    "fan_control",
    "feeder_control",
    "light_control",
    "media_player",
    "projector_control",
    "welcome_projection",
    "push_up",
    "pull_up",
    "squat",
    "pet_tracking",
    "person_tracking",
    "realtime_information",
    "reminder_cancel",
    "reminder_query",
    "reminder_schedule",
}
ALL_SKILLS = set(CAMERA_SKILLS) | set(MOVE_SKILLS) | GENERIC_SKILLS | {
    "face_recognition", "face_registration", "head_control",
    "navigation_goto", "navigation_list", "autonomous_projection",
    "pet_map_search",
}
PROFILE_DISABLED_SKILLS = {
    "autonomous_projection": "fixed_point_profile_uses_meeting_projection",
    "pet_map_search": "fixed_point_profile_uses_find_pet",
    "fan_control": "implementation_removed_do_not_execute",
}


class ThreadRoutingStream(io.TextIOBase):
    """Route Python writes to a request-local sink without global redirects."""

    def __init__(self, base):
        self.base = base
        self.local = threading.local()

    @contextlib.contextmanager
    def route(self, sink):
        previous = getattr(self.local, "sink", None)
        self.local.sink = sink
        try:
            yield
        finally:
            self.local.sink = previous

    def write(self, value):
        target = getattr(self.local, "sink", None) or self.base
        return target.write(value)

    def flush(self):
        target = getattr(self.local, "sink", None) or self.base
        return target.flush()

    def isatty(self):
        return bool(getattr(self.base, "isatty", lambda: False)())

    def fileno(self):
        return self.base.fileno()

    @property
    def encoding(self):
        return getattr(self.base, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self.base, name)


class ResourceBusy(RuntimeError):
    def __init__(self, resource: str, owners: dict[str, Any]):
        super().__init__(f"resource_busy:{resource}")
        self.resource = resource
        self.owners = owners


class ResourceCoordinator:
    """Serialize only genuinely conflicting hardware/model resources."""

    def __init__(self):
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.RLock()
        self.owners: dict[str, dict[str, Any]] = {}

    def _lock(self, resource: str) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(resource, threading.Lock())

    @contextlib.contextmanager
    def acquire(self, resources: list[str], label: str, timeout: float):
        names = sorted(set(resources))
        acquired: list[tuple[str, threading.Lock]] = []
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            for name in names:
                lock = self._lock(name)
                remaining = max(0.0, deadline - time.monotonic())
                if not lock.acquire(timeout=remaining):
                    with self.guard:
                        owners = {key: dict(value) for key, value in self.owners.items() if key == name}
                    raise ResourceBusy(name, owners)
                acquired.append((name, lock))
            with self.guard:
                started = time.time()
                for name, _lock in acquired:
                    self.owners[name] = {"skill": label, "started_at": started, "thread": threading.get_ident()}
            yield
        finally:
            with self.guard:
                for name, _lock in acquired:
                    owner = self.owners.get(name)
                    if owner and owner.get("thread") == threading.get_ident():
                        self.owners.pop(name, None)
            for _name, lock in reversed(acquired):
                lock.release()

    def status(self):
        with self.guard:
            return {key: dict(value) for key, value in self.owners.items()}


class CameraLease:
    def __init__(self, manager: "CameraManager", key: str):
        self.manager = manager
        self.key = key

    def isOpened(self):
        return self.manager.is_opened(self.key)

    def read(self):
        return self.manager.read(self.key)

    def set(self, prop, value):
        return self.manager.set(self.key, prop, value)

    def get(self, prop):
        return self.manager.get(self.key, prop)

    def release(self):
        # The resident owner keeps the descriptor warm.  Resource arbitration
        # explicitly closes it before a legacy long-running camera skill.
        return None


class CameraManager:
    DEFAULTS = {
        "front": {"device": "/dev/video22", "width": 640, "height": 480, "fps": 15.0},
        "back": {"device": "/dev/video31", "width": 640, "height": 480, "fps": 15.0},
    }

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self._caps: dict[str, Any] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._global = threading.RLock()

    def _lock(self, key: str) -> threading.RLock:
        with self._global:
            return self._locks.setdefault(key, threading.RLock())

    def _open(self, device: str, width: int = 640, height: int = 480, fps: float = 15.0):
        key = str(device)
        with self._lock(key):
            cap = self._caps.get(key)
            if cap is not None and cap.isOpened():
                return cap, False
            if cap is not None:
                with contextlib.suppress(Exception):
                    cap.release()
            cap = open_capture(key)
            if not cap.isOpened():
                cap.release()
                raise SharedCameraError(f"camera_frame_unavailable:{key}")
            cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, int(width))
            cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            cap.set(self.cv2.CAP_PROP_FPS, float(fps))
            self._caps[key] = cap
            # The broker continuously warms the physical device. Mapping its
            # shared memory is not a cold camera open and needs no warmup loop.
            return cap, False

    def lease(self, device: str, width=640, height=480, fps=15.0) -> CameraLease:
        self._open(device, width, height, fps)
        return CameraLease(self, str(device))

    def is_opened(self, key: str) -> bool:
        cap = self._caps.get(key)
        return bool(cap is not None and cap.isOpened())

    def read(self, key: str):
        with self._lock(key):
            cap = self._caps.get(key)
            if cap is None or not cap.isOpened():
                cap, _ = self._open(key)
            ok, frame = cap.read()
            if not ok or frame is None:
                with contextlib.suppress(Exception):
                    cap.release()
                self._caps.pop(key, None)
                try:
                    cap, _ = self._open(key)
                    ok, frame = cap.read()
                except Exception:
                    return False, None
            return ok, frame

    def set(self, key: str, prop, value):
        cap, _ = self._open(key)
        return cap.set(prop, value)

    def get(self, key: str, prop):
        cap, _ = self._open(key)
        return cap.get(prop)

    def close_all(self):
        with self._global:
            caps = list(self._caps.values())
            self._caps.clear()
        for cap in caps:
            with contextlib.suppress(Exception):
                cap.release()

    def execute(self, skill: str, argv: list[str], fixed_camera: str | None, fixed_action: str) -> int:
        import argparse
        parser = argparse.ArgumentParser(description=f"Resident camera skill: {skill}")
        parser.add_argument("action", nargs="?", choices=["capture", "record", "check"], default=fixed_action)
        if fixed_camera is None:
            parser.add_argument("--camera", "--camera-name", dest="camera", choices=["front", "back"], default="front")
        parser.add_argument("--device")
        parser.add_argument("--output", "--output-path", dest="output")
        parser.add_argument("--duration", "--seconds", dest="duration", type=float, default=3.0)
        parser.add_argument("--width", type=int)
        parser.add_argument("--height", type=int)
        parser.add_argument("--fps", type=float)
        parser.add_argument("--warmup-frames", type=int)
        parser.add_argument("--json-params")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        extra = json.loads(args.json_params) if args.json_params else {}
        camera = fixed_camera or str(extra.get("camera_name") or extra.get("camera") or args.camera)
        cfg = dict(self.DEFAULTS[camera])
        device = str(extra.get("device") or args.device or cfg["device"])
        width = int(extra.get("width") or args.width or cfg["width"])
        height = int(extra.get("height") or args.height or cfg["height"])
        fps = float(extra.get("fps") or args.fps or cfg["fps"])
        warmup = int(extra.get("warmup_frames") if extra.get("warmup_frames") is not None else (args.warmup_frames if args.warmup_frames is not None else 5))
        action = fixed_action if fixed_action in {"capture", "record"} else str(extra.get("action") or args.action)
        extension = ".jpg" if action == "capture" else ".mp4"
        output = Path(extra.get("output_path") or args.output or ROOT / "runtime" / "media" / f"{camera}_camera{extension}")
        duration = float(extra.get("seconds") or extra.get("duration") or args.duration)
        result = {"camera": camera, "device": device, "output": str(output), "width": width, "height": height, "fps": fps}
        if args.dry_run:
            print(json.dumps({"ok": True, "skill": skill, "action": action, "status": "dry_run", "result": result, "error": None}, ensure_ascii=False))
            return 0
        cap, opened_now = self._open(device, width, height, fps)
        writer = None
        try:
            if opened_now:
                for _ in range(max(0, warmup)):
                    cap.read()
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"camera_read_failed:{device}")
            output.parent.mkdir(parents=True, exist_ok=True)
            if action == "check":
                result["frame_shape"] = list(frame.shape)
            elif action == "capture":
                if not self.cv2.imwrite(str(output), frame, [self.cv2.IMWRITE_JPEG_QUALITY, 90]):
                    raise RuntimeError(f"image_write_failed:{output}")
            else:
                h, w = frame.shape[:2]
                writer = self.cv2.VideoWriter(str(output), self.cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
                if not writer.isOpened():
                    raise RuntimeError(f"video_writer_open_failed:{output}")
                deadline = time.monotonic() + max(0.1, duration)
                while time.monotonic() < deadline:
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        writer.write(frame)
            print(json.dumps({"ok": True, "skill": skill, "action": action, "status": "completed", "result": result, "error": None}, ensure_ascii=False))
            return 0
        finally:
            if writer is not None:
                writer.release()


class ResidentSkills:
    def __init__(self):
        from std_msgs.msg import UInt16
        from std_srvs.srv import SetBool

        self.started = time.time()
        self.runtime = SharedRuntime()
        self.camera = CameraManager()
        self.UInt16 = UInt16
        self.SetBool = SetBool
        self.head_pub = self.runtime.ros_node.create_publisher(UInt16, "/step_motor_angle", 10)
        self.lidar_guard_client = self.runtime.ros_node.create_client(
            SetBool, "/head_lidar_guard/set_live")
        # Serialize the complete guard -> motion -> feedback -> restore cycle.
        self.head_control_lock = threading.RLock()
        self.modules: dict[str, Any] = {}
        self.argv_lock = threading.RLock()
        self.resources = ResourceCoordinator()
        self.runtime.resource_status_provider = self.resources.status
        self.resource_wait_sec = float(os.getenv("RESIDENT_RESOURCE_WAIT_SEC", "0.25"))
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self.stdout_router = ThreadRoutingStream(sys.stdout)
        self.stderr_router = ThreadRoutingStream(sys.stderr)
        sys.stdout = self.stdout_router
        sys.stderr = self.stderr_router
        self.fitness_lock = threading.Lock()
        self.pet_lock = threading.Lock()
        self.pet_stop = threading.Event()
        self.exploration_stop = threading.Event()
        self.fitness_engine = None
        self.person_engine = None
        self.pet_module = None
        self.long_camera_skills = {"environment_perception", "face_recognition", "face_registration", "pet_tracking", "person_tracking", "push_up", "pull_up", "squat"}
        self.preload_results: list[dict[str, Any]] = []
        self._face_module = None
        from autonomy_exploration import AutonomyEngine
        self.exploration = AutonomyEngine(self, ROOT / "config" / "exploration.json")

    def _set_lidar_live(self, enable: bool, timeout: float = 2.0) -> dict[str, Any]:
        """Synchronously acknowledge the scan gate without stopping the lidar.

        The ROS executor runs on its own thread, so waiting here does not block
        the service response.  A missing guard is a hard failure for tilt
        commands: moving first and hoping that a subscriber catches the target
        would reintroduce the map-corruption race this interlock removes.
        """
        client = self.lidar_guard_client
        deadline = time.monotonic() + max(0.1, float(timeout))
        if not client.wait_for_service(timeout_sec=max(0.05, deadline - time.monotonic())):
            return {"called": False, "ok": False, "message": "lidar_guard_service_unavailable"}
        attempts = 0
        retryable = {
            True: {"head_not_stably_level"},
            # Nav2 can report success just before the guard's short
            # motion-hold window expires.  Waiting here prevents a normal
            # navigation -> head-up transition from becoming a false fatal
            # lidar_guard_not_ready result.
            False: {"base_is_moving"},
        }
        while time.monotonic() < deadline:
            attempts += 1
            request = self.SetBool.Request()
            request.data = bool(enable)
            future = client.call_async(request)
            done = threading.Event()
            future.add_done_callback(lambda _future: done.set())
            remaining = max(0.01, deadline - time.monotonic())
            if not done.wait(min(0.75, remaining)):
                if time.monotonic() >= deadline:
                    break
                continue
            try:
                response = future.result()
            except Exception as exc:
                return {"called": True, "ok": False, "message": f"lidar_guard_error:{exc}", "attempts": attempts}
            message = str(response.message)
            if response.success:
                return {
                    "called": True,
                    "ok": True,
                    "message": message,
                    "live": bool(enable),
                    "attempts": attempts,
                }
            if message not in retryable[bool(enable)]:
                return {
                    "called": True,
                    "ok": False,
                    "message": message,
                    "live": None,
                    "attempts": attempts,
                }
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return {"called": True, "ok": False, "message": "lidar_guard_timeout", "attempts": attempts}

    def _load_module(self, key: str, path: Path):
        if key in self.modules:
            return self.modules[key]
        before = time.perf_counter()
        old_path = list(sys.path)
        sys.path.insert(0, str(path.parent))
        sys.path.insert(0, str(ROOT))
        try:
            spec = importlib.util.spec_from_file_location(f"resident_{key}", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot_load_module:{path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self.modules[key] = module
            self.preload_results.append({"skill": key, "elapsed_ms": round((time.perf_counter() - before) * 1000, 3), "ok": True})
            return module
        finally:
            sys.path[:] = old_path

    def preload(self):
        # Import dependency-heavy modules once. Hardware actions are not run.
        for skill in [
            "environment_perception", "light_control", "feeder_control",
            "projector_control", "realtime_information", "push_up",
            "pull_up", "squat", "pet_tracking",
        ]:
            try:
                self._load_module(skill, ROOT / skill / "run.py")
            except BaseException as exc:
                self.preload_results.append({"skill": skill, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        environment = self.modules.get("environment_perception")
        if environment is not None:
            environment._RESIDENT_CAMERA_FACTORY = open_capture
        try:
            module = self._load_module("face_common", ROOT / "face_recognition" / "face_common.py")
            # The copied face module is patched to honor these optional resident
            # overrides. Without them it remains fully backward compatible.
            module._RESIDENT_MODEL_OVERRIDE = self.runtime.facenet
            module._RESIDENT_DETECTOR_OVERRIDE = self.runtime.retina
            module._RESIDENT_CAMERA_PROVIDER = self._face_camera_provider
            self._face_module = module
        except BaseException as exc:
            self.preload_results.append({"skill": "face_common_bind", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        try:
            engine = self._load_module("fitness_engine", ROOT / "push_up" / "engine.py")

            class ResidentFaceAdapter:
                def __init__(self, model):
                    self.model = model

                def feature(self, face):
                    return self.model.extract(face)

                def release(self):
                    return None

            engine._RESIDENT_DETECTOR_OVERRIDE = self.runtime.yolo
            engine._RESIDENT_REID_OVERRIDE = self.runtime.reid
            engine._RESIDENT_FACE_OVERRIDE = ResidentFaceAdapter(self.runtime.facenet)
            engine._RESIDENT_CAPTURE_FACTORY = open_capture
            self.fitness_engine = engine
        except BaseException as exc:
            self.preload_results.append({"skill": "fitness_engine_bind", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        try:
            person = self._load_module(
                "person_tracking_resident_engine",
                ROOT / "person_tracking" / "face_reid_person_tracking.py",
            )
            owner = self

            class ResidentPersonRos:
                def __init__(self, config, execute):
                    self.config = config["tracking"]
                    self.execute = bool(execute)
                    self.closed = False

                def publish(self, linear, angular):
                    if not self.execute or self.closed:
                        return
                    max_linear = float(self.config["maximum_linear_speed"])
                    max_angular = float(self.config["maximum_angular_speed"])
                    linear = max(-max_linear, min(max_linear, float(linear))) * float(self.config["linear_sign"])
                    angular = max(-max_angular, min(max_angular, float(angular))) * float(self.config["angular_sign"])
                    owner.runtime._publish_twist(linear, angular)

                def stop(self):
                    if self.execute and not self.closed:
                        owner.runtime._publish_stop(int(self.config["stop_publish_repetitions"]))

                def close(self):
                    if not self.closed:
                        self.stop()
                        self.closed = True

            class ResidentPersonHead:
                """Head adapter sharing the resident publisher and feedback."""

                def __init__(self, config, enabled):
                    self.config = config["tracking"]
                    self.enabled = bool(enabled)
                    self.raised = False

                def _move(self, action):
                    result = owner._move_head_resident(
                        action,
                        discovery_timeout=float(self.config.get("head_discovery_timeout_seconds", 3.0)),
                        feedback_timeout=float(self.config.get("head_feedback_timeout_seconds", 18.0)),
                        feedback_tolerance=float(self.config.get("head_feedback_tolerance_degrees", 1.0)),
                    )
                    if not result["ok"]:
                        raise person.ReIDTestError(
                            "resident head command failed: "
                            + json.dumps(result, ensure_ascii=False)
                        )
                    return result

                def raise_for_tracking(self):
                    if not self.enabled:
                        return
                    # Ensure every partial-motion failure still restores level.
                    self.raised = True
                    self._move("up")
                    time.sleep(max(0.0, float(self.config.get("head_settle_seconds", 0.15))))

                def restore(self):
                    if not self.raised:
                        return
                    try:
                        self._move("level")
                    except Exception as exc:
                        person.emit("head_restore_failed", ok=False, error=str(exc))
                    finally:
                        self.raised = False

            person._RESIDENT_DETECTOR_OVERRIDE = self.runtime.yolo
            person._RESIDENT_REID_OVERRIDE = self.runtime.reid
            # Person tracking expects ``feature`` while the shared RKNN
            # FaceNet runtime exposes ``extract``.  Reuse the fitness adapter.
            person._RESIDENT_FACE_OVERRIDE = ResidentFaceAdapter(self.runtime.facenet)
            person._RESIDENT_ROS_FACTORY = lambda config, execute: ResidentPersonRos(config, execute)
            person._RESIDENT_CAPTURE_FACTORY = open_capture
            person._RESIDENT_HEAD_FACTORY = lambda config, enabled: ResidentPersonHead(config, enabled)
            self.person_engine = person
        except BaseException as exc:
            self.preload_results.append({"skill": "person_tracking_bind", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if os.getenv("RESIDENT_EXTERNAL_PET_WORKER", "1").strip().lower() in {"1", "true", "yes", "on"}:
            self.preload_results.append({"skill": "pet_detector_bind", "ok": True, "mode": "external_resident_worker"})
            return
        try:
            pet = self.modules.get("pet_tracking") or self._load_module("pet_tracking", ROOT / "pet_tracking" / "run.py")
            started = time.perf_counter()
            pet.runtime_models.acquire_or_create(
                "pet_detector",
                lambda: pet.PetTrackingSystem.RKNNDetector(
                    pet.PetTrackingSystem.DETECTOR_MODEL,
                    conf=pet.PetTrackingSystem.DET_CONF,
                    core_mask=4,
                ),
                description="resident pet detector",
            )
            owner = self

            class ResidentPetBoard:
                def __init__(self):
                    self.max_linear = float(os.getenv("PET_ROS_MAX_LINEAR", "0.55"))
                    self.max_angular = float(os.getenv("PET_ROS_MAX_ANGULAR", "1.30"))
                    self.legacy_max_speed = float(os.getenv("PET_LEGACY_MAX_SPEED", "300.0"))

                def set_motor_speed(self, speeds):
                    motor = {int(item[0]): float(item[1]) for item in speeds}
                    right = max(-1.0, min(1.0, motor.get(1, 0.0) / max(self.legacy_max_speed, 1.0)))
                    left = max(-1.0, min(1.0, -motor.get(2, 0.0) / max(self.legacy_max_speed, 1.0)))
                    owner.runtime._publish_twist(
                        (left + right) * 0.5 * self.max_linear,
                        (right - left) * self.max_angular,
                    )

                def stop(self):
                    owner.runtime._publish_stop()

                def close(self):
                    self.stop()

                release = close
                shutdown = close

            def create_board():
                backend = os.getenv("PET_MOTOR_BACKEND", "ros2").strip().lower()
                if backend in {"dummy", "none", "off", "0", "false"}:
                    return pet.PetTrackingSystem.DummyBoard()
                return ResidentPetBoard()

            pet.PetTrackingSystem._create_board = staticmethod(create_board)
            pet._RESIDENT_STOP_EVENT = self.pet_stop
            self.pet_module = pet
            self.preload_results.append({"skill": "pet_detector_bind", "ok": True, "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)})
        except BaseException as exc:
            self.preload_results.append({"skill": "pet_detector_bind", "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _face_camera_provider(self, device, cfg):
        return self.camera.lease(str(device), int(cfg.width), int(cfg.height), 15.0), {"requested": str(device), "resolved": str(device), "resident": True}

    def _capture_call(self, func: Callable[[], Any], stream: Callable[[str, str], None] | None = None) -> dict[str, Any]:
        class Capture(io.StringIO):
            def __init__(self, kind: str):
                super().__init__()
                self.kind = kind

            def write(self, value):
                written = super().write(value)
                if stream is not None and value:
                    stream(self.kind, str(value))
                return written

        out, err = Capture("stdout"), Capture("stderr")
        code = 0
        started = time.perf_counter()
        try:
            with self.stdout_router.route(out), self.stderr_router.route(err):
                value = func()
                if isinstance(value, int):
                    code = value
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
        except BaseException:
            code = 1
            traceback.print_exc(file=err)
        return {
            "ok": code == 0,
            "exit_code": code,
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
            "server_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    @contextlib.contextmanager
    def _argv(self, program: str, argv: list[str]):
        old_argv = list(sys.argv)
        old_cwd = Path.cwd()
        sys.argv[:] = [program, *argv]
        try:
            yield
        finally:
            sys.argv[:] = old_argv
            os.chdir(old_cwd)

    def _generic(self, skill: str, argv: list[str]) -> int:
        if skill in {"pull_up", "squat"} and "--dry-run" in argv:
            clean = [value for value in argv if value not in {"--dry-run", "--json"}]
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("action", nargs="?", default="run")
            parser.add_argument("--camera")
            parser.add_argument("--duration", type=int, default=30)
            parser.add_argument("--start-gate")
            parser.add_argument("--initial-count", type=int, default=0)
            parser.add_argument("--resume-from-interrupt", action="store_true")
            parser.add_argument("--initial-elapsed-seconds", type=float, default=0.0)
            args, _ = parser.parse_known_args(clean)
            print(json.dumps({
                "ok": True, "status": "dry_run", "skill": skill,
                "action": args.action,
                "result": {
                    "camera": args.camera or "/dev/video31",
                    "duration": args.duration,
                    "initial_count": args.initial_count,
                },
                "error": None,
            }, ensure_ascii=False))
            return 0
        if skill == "person_tracking":
            module = self._load_module("person_tracking_engine", ROOT / "person_tracking" / "run.py")
            module._person_tracking_cli_main()
            return 0
        if skill.startswith("reminder_"):
            # The external reminder CLI still parses process-global sys.argv.
            # Reminder operations share one persistent store, so a narrow lock
            # here is both concurrency-safe and semantically correct.
            with self.argv_lock, self._argv(str(ROOT / skill / "run.py"), argv):
                sys.path.insert(0, "/home/test/new_project")
                from new_project.reminder_cli import main
                return int(main(skill) or 0)
        module = self._load_module(skill, ROOT / skill / "run.py")
        if skill == "pet_tracking":
            module.PetTrackingSystem.main()
            return 0
        main = getattr(module, "main", None)
        if not callable(main):
            raise RuntimeError(f"skill_has_no_callable_main:{skill}")
        return int(main(argv) or 0)

    def _move(self, skill: str, argv: list[str]) -> int:
        parser = argparse.ArgumentParser(description=f"Resident {skill}")
        parser.add_argument("--speed", type=float, default=0.12)
        parser.add_argument("--angular-speed", type=float, default=0.35)
        parser.add_argument("--duration", type=float, default=1.0)
        parser.add_argument("--topic", default="/cmd_vel")
        parser.add_argument("--discovery-timeout", type=float, default=0.8)
        parser.add_argument("--allow-no-subscriber", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--timeout")
        args = parser.parse_args(argv)
        linear_sign, angular_sign = MOVE_SKILLS[skill]
        linear = linear_sign * abs(args.speed) if linear_sign else 0.0
        angular = angular_sign * abs(args.angular_speed) if angular_sign else 0.0
        if args.dry_run:
            result = {"status": "dry_run", "linear_x": linear, "angular_z": angular, "duration": args.duration}
        else:
            result = self.runtime.chassis_move({
                "linear_x": linear, "angular_z": angular, "duration": args.duration,
                "discovery_timeout": args.discovery_timeout,
                "allow_no_subscriber": args.allow_no_subscriber,
            })
        print(json.dumps({"ok": True, "skill": skill, "action": skill.removeprefix("move_"), "result": result}, ensure_ascii=False))
        return 0

    def _move_head_resident(
        self,
        action: str,
        *,
        angle: int | None = None,
        repeat: int = 5,
        interval: float = 0.05,
        discovery_timeout: float = 0.8,
        feedback_timeout: float = 8.0,
        feedback_tolerance: float = 5.0,
        feedback_stable_sec: float = 0.15,
        feedback_max_roll_span: float = 1.5,
        feedback_max_rate: float = 4.0,
        feedback_max_age: float = 0.5,
    ) -> dict[str, Any]:
        """Move the head through the resident ROS participant.

        Person tracking already runs inside this process.  Reusing this
        long-lived publisher avoids spawning a second rclpy participant and
        eliminates cold DDS discovery races between projection and follow.
        """
        aliases = {"raise": "up", "look_up": "up", "lower": "down", "look_down": "down", "center": "level", "flat": "level", "neutral": "level"}
        action = aliases.get(str(action).strip().lower(), str(action).strip().lower())
        defaults = {
            "up": int(os.getenv("HEAD_UP_ANGLE", "211")),
            "down": int(os.getenv("HEAD_DOWN_ANGLE", "163")),
            "level": int(os.getenv("HEAD_LEVEL_ANGLE", "185")),
        }
        if action == "angle" and angle is None:
            raise ValueError("action=angle requires --angle")
        if angle is None:
            if action not in defaults:
                raise ValueError(f"unknown head action: {action}")
            angle = defaults[action]
        angle = int(angle)

        with self.head_control_lock:
            level_angle = defaults["level"]
            tilting = abs(angle - level_angle) > 4
            guard_before = self._set_lidar_live(False) if tilting else {
                "called": False, "ok": True, "message": "already_guarded_or_leveling"
            }
            if tilting and not guard_before.get("ok"):
                return {
                    "ok": False,
                    "skill": "head_control",
                    "action": action,
                    "angle": angle,
                    "topic": "/step_motor_angle",
                    "subscribers": int(self.head_pub.get_subscription_count()),
                    "feedback": {"ok": False, "reason": "motion_not_started"},
                    "service": guard_before,
                    "error": "lidar_guard_not_ready",
                    "transport": "resident_ros_participant",
                }
            deadline = time.monotonic() + max(0.0, float(discovery_timeout))
            subscribers = int(self.head_pub.get_subscription_count())
            while subscribers <= 0 and time.monotonic() < deadline:
                time.sleep(0.03)
                subscribers = int(self.head_pub.get_subscription_count())

            msg = self.UInt16()
            msg.data = max(0, min(65535, angle))
            for index in range(max(1, int(repeat))):
                self.head_pub.publish(msg)
                if index + 1 < max(1, int(repeat)):
                    time.sleep(max(0.0, float(interval)))

            feedback = self.runtime.wait_for_head_target(
                angle,
                tolerance_deg=max(0.5, float(feedback_tolerance)),
                stable_sec=max(0.05, float(feedback_stable_sec)),
                maximum_roll_span_deg=max(0.25, float(feedback_max_roll_span)),
                maximum_rate_dps=max(0.5, float(feedback_max_rate)),
                maximum_feedback_age_sec=max(0.1, float(feedback_max_age)),
                timeout_sec=max(0.2, float(feedback_timeout)),
            )
            subscribers = max(subscribers, int(self.head_pub.get_subscription_count()))
            delivered = subscribers > 0
            at_target = bool(feedback.get("ok"))
            scan_sequence_before = self.runtime.current_scan_sequence()
            guard_after = self._set_lidar_live(True, timeout=2.5) if not tilting and at_target else guard_before
            fresh_scan = (
                self.runtime.wait_for_fresh_scan(
                    after_sequence=scan_sequence_before,
                    timeout_sec=2.5,
                    maximum_age_sec=0.5,
                )
                if not tilting and at_target and guard_after.get("ok")
                else {"ok": bool(tilting), "skipped": True}
            )
            localization_recovery = {"ok": True, "skipped": True, "reason": "head_remains_tilted"}
            if not tilting and at_target and guard_after.get("ok") and fresh_scan.get("ok"):
                # A single fresh /scan only proves that the guard reopened.  It
                # does not prove that laser odometry, map TF and Nav2 have
                # flushed old timestamps.  When Nav2 is present (the complete
                # robot project), keep the head action open until that whole
                # chain is continuously healthy.  Head-only deployments remain
                # usable because they have no navigation server to protect.
                if self.runtime.nav_client.server_is_ready():
                    localization_recovery = self.runtime.wait_for_navigation_health(
                        timeout_sec=float(os.getenv("HEAD_LOCALIZATION_RECOVERY_TIMEOUT_SEC", "12.0")),
                        stable_sec=float(os.getenv("HEAD_LOCALIZATION_RECOVERY_STABLE_SEC", "1.0")),
                        maximum_age_sec=float(os.getenv("HEAD_LOCALIZATION_MAXIMUM_AGE_SEC", "0.60")),
                        minimum_updates=int(os.getenv("HEAD_LOCALIZATION_MINIMUM_UPDATES", "5")),
                        require_navigation_server=True,
                    )
                else:
                    localization_recovery = {
                        "ok": True,
                        "skipped": True,
                        "reason": "navigation_server_not_running",
                    }
            guard_after = {
                **guard_after,
                "fresh_scan": fresh_scan,
                "localization_recovery": localization_recovery,
            }
            guard_ok = (
                bool(guard_after.get("ok"))
                and bool(fresh_scan.get("ok"))
                and bool(localization_recovery.get("ok"))
            )
            return {
                "ok": delivered and at_target and guard_ok,
                "skill": "head_control",
                "action": action,
                "angle": angle,
                "topic": "/step_motor_angle",
                "subscribers": subscribers,
                "feedback": feedback,
                "service": guard_after,
                "error": (
                    None if delivered and at_target and guard_ok
                    else "no_subscribers" if not delivered
                    else "head_target_unconfirmed" if not at_target
                    else "lidar_fresh_scan_resume_failed" if not fresh_scan.get("ok")
                    else "localization_recovery_failed"
                ),
                "transport": "resident_ros_participant",
            }

    def _head(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser(description="Resident head control")
        parser.add_argument("action", nargs="?", default="level")
        parser.add_argument("--angle", type=int)
        parser.add_argument("--topic", default="/step_motor_angle")
        parser.add_argument("--wait", type=float, default=0.35)
        parser.add_argument("--repeat", type=int, default=5)
        parser.add_argument("--interval", type=float, default=0.05)
        parser.add_argument("--discovery-timeout", type=float, default=0.8)
        parser.add_argument("--service-timeout", type=float, default=0.45)
        # car_real_copy_zhenghang stops inside a 5 degree head deadband, so the
        # physical arrival gate must use the same tolerance.
        parser.add_argument("--feedback-timeout", type=float, default=12.0)
        parser.add_argument("--feedback-tolerance", type=float, default=5.0)
        parser.add_argument("--feedback-stable-sec", type=float, default=0.15)
        parser.add_argument("--feedback-max-roll-span", type=float, default=1.5)
        parser.add_argument("--feedback-max-rate", type=float, default=4.0)
        parser.add_argument("--feedback-max-age", type=float, default=0.5)
        parser.add_argument("--call-services", action="store_true", help="Deprecated no-op; the lidar driver stays on while /scan is guarded.")
        parser.add_argument("--skip-services", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--timeout")
        args = parser.parse_args(argv)
        aliases = {"raise": "up", "look_up": "up", "lower": "down", "look_down": "down", "center": "level", "flat": "level", "neutral": "level"}
        action = aliases.get(args.action.strip().lower(), args.action.strip().lower())
        defaults = {"up": int(os.getenv("HEAD_UP_ANGLE", "211")), "down": int(os.getenv("HEAD_DOWN_ANGLE", "163")), "level": int(os.getenv("HEAD_LEVEL_ANGLE", "185"))}
        if action == "angle" and args.angle is None:
            raise ValueError("action=angle requires --angle")
        angle = int(args.angle if args.angle is not None else defaults[action])
        level_angle = int(os.getenv("HEAD_LEVEL_ANGLE", "185"))
        is_level = action == "level" or abs(angle - level_angle) <= 2
        if args.dry_run:
            print(json.dumps({
                "ok": True,
                "status": "dry_run",
                "skill": "head_control",
                "action": action,
                "result": {
                    "angle": angle,
                    "lidar_driver": "always_running",
                    "scan_policy": "guard_before_tilt_restore_after_stable_level",
                },
            }, ensure_ascii=False))
            return 0

        result = self._move_head_resident(
            action,
            angle=angle,
            repeat=args.repeat,
            interval=args.interval,
            discovery_timeout=args.discovery_timeout,
            feedback_timeout=args.feedback_timeout,
            feedback_tolerance=args.feedback_tolerance,
            feedback_stable_sec=args.feedback_stable_sec,
            feedback_max_roll_span=args.feedback_max_roll_span,
            feedback_max_rate=args.feedback_max_rate,
            feedback_max_age=args.feedback_max_age,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2

    def _navigation(self, skill: str, argv: list[str]) -> int:
        if skill == "navigation_list":
            points = json.loads((ROOT / "points" / "named_points.json").read_text(encoding="utf-8"))
            print(json.dumps({"ok": True, "skill": skill, "action": "list", "points": points}, ensure_ascii=False))
            return 0
        parser = argparse.ArgumentParser(description="Resident navigation")
        parser.add_argument("tokens", nargs="*")
        parser.add_argument("--action", choices=["goto", "list"])
        parser.add_argument("--json-params")
        parser.add_argument("--point")
        parser.add_argument("--x", type=float)
        parser.add_argument("--y", type=float)
        parser.add_argument("--yaw", type=float, default=0.0)
        parser.add_argument("--frame-id", default="map")
        parser.add_argument("--timeout", type=float, default=120.0)
        parser.add_argument("--server-wait-timeout", type=float, default=5.0)
        parser.add_argument("--goal-response-timeout", type=float, default=8.0)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        args, _unknown = parser.parse_known_args(argv)
        if args.tokens and args.tokens[0] == "stop":
            result = self.runtime.navigation_cancel_request()
            print(json.dumps({"ok": bool(result.get("ok", True)), "skill": "navigation_goto", "action": "stop", "result": result}, ensure_ascii=False))
            return 0 if result.get("ok", True) else 2
        if args.action == "list" or (args.tokens and args.tokens[0] == "list"):
            return self._navigation("navigation_list", [])
        extra = json.loads(args.json_params) if args.json_params else {}
        point = args.point or extra.get("point") or extra.get("destination") or extra.get("name")
        tokens = list(args.tokens)
        if tokens and tokens[0] == "goto":
            tokens.pop(0)
        if not point and tokens:
            if len(tokens) >= 2:
                try:
                    extra["x"], extra["y"] = float(tokens[0]), float(tokens[1])
                    if len(tokens) >= 3:
                        extra["yaw"] = float(tokens[2])
                except ValueError:
                    point = " ".join(tokens)
            else:
                point = tokens[0]
        request = {
            "point": point or "",
            "x": args.x if args.x is not None else extra.get("x"),
            "y": args.y if args.y is not None else extra.get("y"),
            "yaw": args.yaw if args.yaw is not None else extra.get("yaw", 0.0),
            "frame_id": extra.get("frame_id", args.frame_id),
            "timeout": args.timeout,
            "server_wait_timeout": args.server_wait_timeout,
            "goal_response_timeout": args.goal_response_timeout,
        }
        if args.dry_run:
            result = {"status": "dry_run", "goal": self.runtime._resolve_navigation_goal(request)}
        else:
            result = self.runtime.navigation_goal(request)
        ok = result.get("status") in {"dry_run", "succeeded"}
        print(json.dumps({"ok": ok, "skill": skill, "action": "goto", "result": result, "error": None if ok else result.get("status")}, ensure_ascii=False))
        return 0 if ok else 1

    def _face(self, skill: str, argv: list[str]) -> int:
        if self._face_module is None:
            raise RuntimeError("face_runtime_not_ready")
        if skill == "face_recognition":
            return int(self._face_module.run_recognition_cli(ROOT / "face_recognition", argv) or 0)
        return int(self._face_module.run_registration_cli(ROOT / "face_registration", argv) or 0)

    def _fitness_state_path(self, skill: str) -> Path:
        return ROOT / "runtime" / "fitness" / skill / f"{skill}_state.json"

    def _fitness(self, skill: str, argv: list[str]) -> int:
        if self.fitness_engine is None:
            raise RuntimeError("fitness_runtime_not_ready")
        parser = argparse.ArgumentParser(description=f"Resident {skill} counter")
        parser.add_argument("action", nargs="?", default="run", choices=["run", "start", "query", "stop", "check"])
        parser.add_argument("--camera", "--source", dest="camera")
        parser.add_argument("--identity-camera")
        parser.add_argument("--duration", type=float, default=30.0)
        parser.add_argument("--name", default=os.getenv("FITNESS_IDENTITY", "zhangsan"))
        parser.add_argument(
            "--identity-policy",
            choices=["face_and_reid", "anonymous"],
            default="face_and_reid",
        )
        parser.add_argument("--output")
        parser.add_argument("--start-gate")
        parser.add_argument("--projector-after-identity", action="store_true")
        parser.add_argument("--preparation-delay", type=float, default=4.0)
        parser.add_argument("--initial-count", type=int, default=0)
        parser.add_argument("--resume-from-interrupt", action="store_true")
        parser.add_argument("--initial-elapsed-seconds", type=float, default=0.0)
        parser.add_argument("--frame-step", type=int, default=1)
        parser.add_argument("--max-frames", type=int, default=0)
        parser.add_argument("--start", type=float, default=0.0)
        parser.add_argument("--end", type=float)
        parser.add_argument("--config", default=str(ROOT / "push_up" / "config.json"))
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--timeout", type=float)
        args = parser.parse_args(argv)
        state_file = self._fitness_state_path(skill)
        if args.dry_run:
            print(json.dumps({"ok": True, "status": "dry_run", "skill": skill, "action": args.action, "result": {"camera": args.camera or "/dev/video31", "identity_camera": args.identity_camera or "/dev/video31", "identity_policy": args.identity_policy, "duration": args.duration, "name": args.name, "projector_after_identity": bool(args.projector_after_identity)}, "error": None}, ensure_ascii=False))
            return 0
        if args.action == "query":
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                state = {"state": "idle", "running": False, "count": 0, "current_count": 0, "session_count": 0}
            print(json.dumps({"ok": True, "skill": skill, "action": "query", "status": state.get("state", "idle"), "result": state, "count": int(state.get("count") or state.get("current_count") or 0)}, ensure_ascii=False))
            return 0
        if args.action == "stop":
            self.fitness_engine._request_stop()
            print(json.dumps({"ok": True, "skill": skill, "action": "stop", "status": "stop_requested"}, ensure_ascii=False))
            return 0
        if args.action == "check":
            print(json.dumps({"ok": True, "skill": skill, "action": "check", "status": "ready", "result": {"resident_models": ["person_detector", "person_reid", "face_embedding"]}}, ensure_ascii=False))
            return 0
        if not self.fitness_lock.acquire(blocking=False):
            print(json.dumps({"ok": False, "skill": skill, "action": args.action, "status": "busy", "error": "fitness_runtime_busy"}, ensure_ascii=False))
            return 2
        try:
            config = self.fitness_engine.load_config(Path(args.config))
            hardware = json.loads((ROOT / "config" / "hardware.json").read_text(encoding="utf-8"))
            back = (hardware.get("cameras") or {}).get("back") or {}
            config["camera"].update({
                "source": str(back.get("device") or config["camera"]["source"]),
                "width": int(back.get("width") or config["camera"]["width"]),
                "height": int(back.get("height") or config["camera"]["height"]),
                "fps": int(back.get("fps") or config["camera"]["fps"]),
            })
            config["face"]["database"] = str(ROOT / "face_data" / "faces.db")
            config["paths"]["runtime_dir"] = str(ROOT / "runtime" / "fitness" / skill)
            args.source = str(args.camera or config["camera"]["source"])
            args.identity_source = str(args.identity_camera or back.get("device") or "/dev/video31")
            args.exercise = skill
            args.state_file = str(state_file)
            generated_gate: Path | None = None
            previous_event_callback = self.fitness_engine._RESIDENT_EVENT_CALLBACK
            projection_started = False
            fitness_finished_normally = False
            if args.projector_after_identity:
                gate_dir = ROOT / "runtime" / "fitness" / "start_gates"
                gate_dir.mkdir(parents=True, exist_ok=True)
                generated_gate = gate_dir / f"{skill}_{os.getpid()}_{threading.get_ident()}.ready"
                generated_gate.unlink(missing_ok=True)
                args.start_gate = str(generated_gate)

                def on_fitness_event(event: dict[str, Any]) -> None:
                    nonlocal projection_started
                    if event.get("kind") != "ready" or projection_started:
                        return
                    projection_started = True
                    try:
                        module = self.modules.get("projector_control") or self._load_module(
                            "projector_control", ROOT / "projector_control" / "run.py"
                        )
                        module.main(["fitness_video_on", "--json"])
                    except SystemExit as exc:
                        if int(exc.code or 0) != 0:
                            raise RuntimeError("fitness_projector_start_failed") from exc
                    time.sleep(max(0.0, min(8.0, float(args.preparation_delay))))
                    generated_gate.touch()

                self.fitness_engine._RESIDENT_EVENT_CALLBACK = on_fitness_event
            self.camera.close_all()
            try:
                with self.runtime.model_lock:
                    result_code = int(self.fitness_engine.command_count(args, config) or 0)
                fitness_finished_normally = (
                    result_code == 0 and not self.fitness_engine._STOP_REQUESTED
                )
                return result_code
            except Exception as exc:
                print(json.dumps({
                    "ok": False,
                    "skill": skill,
                    "action": args.action,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }, ensure_ascii=False))
                return 1
            finally:
                self.fitness_engine._RESIDENT_EVENT_CALLBACK = previous_event_callback
                if generated_gate is not None:
                    generated_gate.unlink(missing_ok=True)
                # The declarative procedure owns normal projector shutdown so
                # it can verify the off result and run its head-level fallback.
                # An interrupted or failed fitness session may never reach that
                # next procedure step, so clean up locally only on abnormal exit.
                if projection_started and not fitness_finished_normally:
                    try:
                        module = self.modules.get("projector_control") or self._load_module(
                            "projector_control", ROOT / "projector_control" / "run.py"
                        )
                        module.main(["off", "--json"])
                    except BaseException as exc:
                        print(json.dumps({
                            "event": "skill_event",
                            "skill_name": skill,
                            "kind": "cleanup_warning",
                            "text": "运动已经结束，但投影关闭或头部复位没有完全确认。",
                            "error": f"{type(exc).__name__}: {exc}",
                        }, ensure_ascii=False), flush=True)
        finally:
            self.fitness_lock.release()

    def _pet(self, argv: list[str]) -> int:
        if self.pet_module is None:
            worker_socket = STATE_DIR / "pet.sock"
            header = json.dumps({"op": "pet_run", "argv": argv, "stream": True, "payload_len": 0}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(3600.0)
                conn.connect(str(worker_socket))
                conn.sendall(struct.pack("!I", len(header)) + header)
                while True:
                    size = struct.unpack("!I", self._recv_exact(conn, 4))[0]
                    response = json.loads(self._recv_exact(conn, size).decode("utf-8"))
                    kind = response.get("type")
                    if kind == "stdout":
                        print(str(response.get("data") or ""), end="", flush=True)
                        continue
                    if kind == "stderr":
                        print(str(response.get("data") or ""), end="", file=sys.stderr, flush=True)
                        continue
                    if kind == "final":
                        break
                    break
            return int(response.get("exit_code", 0 if response.get("ok") else 1))
        cls = self.pet_module.PetTrackingSystem
        parser = argparse.ArgumentParser(description="Resident pet tracking")
        parser.add_argument("action", nargs="?", choices=["smoke", "find", "find_route", "find_and_track", "find_route_and_track", "track", "stop"], default=None)
        parser.add_argument("--mode", choices=["smoke", "find", "find_route", "find_and_track", "find_route_and_track", "track", "stop"])
        parser.add_argument("--source", default=os.getenv("PET_CAMERA_ID", "/dev/video22"))
        parser.add_argument("--camera")
        parser.add_argument("--pet", choices=["cat", "dog", "all"], default="dog")
        parser.add_argument("--duration", type=float, default=20.0)
        parser.add_argument("--timeout", type=float)
        parser.add_argument("--search-timeout", type=float)
        parser.add_argument("--track-duration", type=float)
        parser.add_argument("--base-speed", type=float)
        parser.add_argument("--max-linear", type=float)
        parser.add_argument("--max-angular", type=float)
        parser.add_argument("--steering-gain", type=float)
        parser.add_argument("--speed-ema-alpha", type=float)
        parser.add_argument("--max-speed-step", type=float)
        parser.add_argument("--search-spin-speed", type=float)
        parser.add_argument("--search-mode")
        parser.add_argument("--model", default=cls.DETECTOR_MODEL)
        parser.add_argument("--backend", choices=["ros2", "dummy"], default=os.getenv("PET_MOTOR_BACKEND", "ros2"))
        parser.add_argument("--cmd-vel-topic", default="/cmd_vel")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--start-gate")
        parser.add_argument("--resume-from-interrupt", action="store_true")
        parser.add_argument("--target")
        parser.add_argument("--dry-run", action="store_true")
        clean = [item for item in argv if item != "--no-gui"]
        args = parser.parse_args(clean)
        mode = args.mode or args.action or "track"
        if args.camera:
            args.source = args.camera
        if args.timeout is not None:
            args.duration = args.timeout
        os.environ["PET_MOTOR_BACKEND"] = args.backend
        os.environ["PET_ROS_CMD_VEL_TOPIC"] = args.cmd_vel_topic
        os.environ["PET_CAMERA_GUI"] = "0"
        if args.base_speed is not None:
            cls.MultiBoxTracker.BASE_SPEED = max(0.0, min(1.0, args.base_speed))
        if args.max_linear is not None:
            os.environ["PET_ROS_MAX_LINEAR"] = str(max(0.0, args.max_linear))
        if args.max_angular is not None:
            os.environ["PET_ROS_MAX_ANGULAR"] = str(max(0.0, args.max_angular))
        if args.steering_gain is not None:
            cls.MultiBoxTracker.STEERING_GAIN = max(0.0, args.steering_gain)
            cls.MultiBoxTracker.MOVING_STEERING_GAIN = cls.MultiBoxTracker.STEERING_GAIN
        if args.speed_ema_alpha is not None:
            cls.MultiBoxTracker.SPEED_EMA_ALPHA = max(0.0, min(1.0, args.speed_ema_alpha))
        if args.max_speed_step is not None:
            cls.MultiBoxTracker.MAX_SPEED_STEP = max(0.001, args.max_speed_step)
        if args.search_spin_speed is not None:
            cls.TRACK_SEARCH_SPIN_SPEED = max(0.0, min(1.0, args.search_spin_speed))
        find_modes = {"find", "find_route", "find_and_track", "find_route_and_track"}
        track_modes = {"track", "find_and_track", "find_route_and_track"}
        if args.dry_run:
            print(json.dumps({"ok": True, "skill": "pet_tracking", "dry_run": True, "mode": mode, "source": str(args.source), "pet": args.pet, "timeout_sec": args.duration if mode in find_modes else None, "backend": args.backend, "cmd_vel_topic": args.cmd_vel_topic, "resident_model": True}, ensure_ascii=False))
            return 0
        if mode == "stop":
            self.pet_stop.set()
            self.runtime.chassis_stop()
            print(json.dumps({"ok": True, "skill": "pet_tracking", "mode": "stop", "status": "stop_requested"}, ensure_ascii=False))
            return 0
        if not self.pet_lock.acquire(blocking=False):
            print(json.dumps({"ok": False, "skill": "pet_tracking", "mode": mode, "error": "pet_runtime_busy"}, ensure_ascii=False))
            return 2
        source = cls._normalize_video_source(args.source)
        target = args.pet if args.pet != "all" else "pet"
        self.pet_stop.clear()
        started = time.perf_counter()
        try:
            if mode == "smoke":
                cls.smoke_test()
                result = {"ok": True, "state": "completed"}
            elif mode in track_modes:
                search_timeout = max(0.5, float(args.search_timeout if args.search_timeout is not None else cls.TRACK_SEARCH_TIMEOUT_SEC))
                record_duration = max(0.2, float(args.track_duration if args.track_duration is not None else args.duration))
                result = cls.background_tracking_task(
                    source, args.model, target, "/tmp/resident_pet_tracking.pid",
                    None, None, max(0.2, record_duration * 0.2), args.start_gate,
                    search_timeout, record_duration,
                )
                if result is None:
                    result = cls._read_result_payload()
            else:
                result = cls.background_pet_search_task(source, args.model, target, None, None, args.duration)
            result_ok = bool(result is not None and isinstance(result, dict) and result.get("ok", False))
            if args.json or True:
                result_error = None if result_ok else ((result or {}).get("error") if isinstance(result, dict) else None) or "pet_tracking_no_success_result"
                print(json.dumps({"ok": result_ok, "skill": "pet_tracking", "mode": mode, "source": str(args.source), "pet": args.pet, "elapsed_sec": round((time.perf_counter() - started), 3), "result": result, "resident_model": True, "error": result_error}, ensure_ascii=False))
            return 0 if result_ok else 1
        finally:
            self.runtime.chassis_stop()
            self.pet_lock.release()

    def _autonomous_projection(self, argv: list[str]) -> int:
        return int(self.exploration.projection_cli(argv) or 0)

    def _pet_map_search(self, argv: list[str]) -> int:
        return int(self.exploration.pet_search_cli(argv) or 0)

    def _person(self, argv: list[str]) -> int:
        if self.person_engine is None:
            raise RuntimeError("person_runtime_not_ready")
        parser = argparse.ArgumentParser(description="Resident person tracking")
        parser.add_argument("action", nargs="?", default="track", choices=["find", "track", "run", "stop", "check"])
        parser.add_argument("--name", "--target", dest="name", default="zhangsan")
        parser.add_argument("--camera", "--source", dest="source")
        parser.add_argument("--duration", "--seconds", dest="seconds", type=float, default=30.0)
        parser.add_argument("--output")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--base-speed", type=float)
        parser.add_argument("--max-linear", type=float)
        parser.add_argument("--max-angular", type=float)
        parser.add_argument("--steering-gain", type=float)
        parser.add_argument("--speed-ema-alpha", type=float)
        parser.add_argument("--max-speed-step", type=float)
        parser.add_argument("--search-spin-speed", type=float)
        parser.add_argument("--raise-head", action="store_true")
        parser.add_argument("--skip-head-control", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        if str(args.name).strip().lower() in {"张三", "zhangsan"}:
            args.name = "zhangsan"
        if args.dry_run:
            print(json.dumps({"ok": True, "skill": "person_tracking", "action": args.action, "status": "dry_run", "result": {"identity": args.name, "source": args.source or "/dev/video22", "duration": args.seconds, "movement_enabled": args.execute, "resident_models": True}}, ensure_ascii=False))
            return 0
        if args.action == "stop":
            self.person_engine._request_stop()
            self.runtime.chassis_stop()
            print(json.dumps({"ok": True, "skill": "person_tracking", "action": "stop", "status": "stop_requested"}, ensure_ascii=False))
            return 0
        if args.action == "check":
            print(json.dumps({"ok": True, "skill": "person_tracking", "action": "check", "status": "ready", "result": {"cmd_vel_subscribers": int(self.runtime.cmd_vel_pub.get_subscription_count()), "resident_models": ["person_detector", "person_reid", "face_embedding"]}}, ensure_ascii=False))
            return 0
        if not self.fitness_lock.acquire(blocking=False):
            print(json.dumps({"ok": False, "skill": "person_tracking", "action": args.action, "error": "vision_runtime_busy"}, ensure_ascii=False))
            return 2
        try:
            config_path = ROOT / "person_tracking" / "config.json"
            config = self.person_engine.load_config(config_path)
            config["face"]["database"] = str(ROOT / "face_data" / "faces.db")
            config["paths"]["runtime_dir"] = str(ROOT / "runtime" / "person_tracking")
            tracking = config["tracking"]
            for key, value in {
                "maximum_linear_speed": args.max_linear,
                "maximum_angular_speed": args.max_angular,
                "angular_gain": args.steering_gain,
                "forward_speed": args.base_speed,
            }.items():
                if value is not None:
                    tracking[key] = float(value)
            effective = ROOT / "runtime" / "person_tracking" / "effective_config.json"
            effective.parent.mkdir(parents=True, exist_ok=True)
            effective.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            engine_argv = ["--config", str(effective), "--name", args.name, "--seconds", str(max(0.1, args.seconds))]
            if args.source:
                engine_argv += ["--source", args.source]
            if args.output:
                engine_argv += ["--output", args.output]
            if args.execute:
                engine_argv.append("--execute")
            if args.raise_head:
                engine_argv.append("--raise-head")
            if args.skip_head_control:
                engine_argv.append("--skip-head-control")
            with self.runtime.model_lock, self._argv(str(ROOT / "person_tracking" / "face_reid_person_tracking.py"), engine_argv):
                return int(self.person_engine.main() or 0)
        finally:
            self.runtime.chassis_stop()
            self.fitness_lock.release()

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        chunks = []
        while size:
            chunk = conn.recv(size)
            if not chunk:
                raise ConnectionError("pet worker closed connection")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _option(argv: list[str], name: str, default: str | None = None) -> str | None:
        for index, item in enumerate(argv):
            if item == name and index + 1 < len(argv):
                return argv[index + 1]
            if item.startswith(name + "="):
                return item.split("=", 1)[1]
        return default

    def _resources_for(self, skill: str, argv: list[str]) -> list[str]:
        if "--dry-run" in argv:
            return []
        action = argv[0] if argv and not argv[0].startswith("-") else ""
        resources: list[str] = []
        if skill in MOVE_SKILLS:
            resources.append("base")
        elif skill == "navigation_goto" and action != "list":
            resources.append("base")
        elif skill == "autonomous_projection" and action != "stop":
            resources.extend(["base", "head", "front_camera", "projector"])
        elif skill == "pet_map_search" and action != "stop":
            resources.extend(["base", "front_camera", "pet_npu"])
        elif skill == "head_control":
            resources.append("head")
        elif skill in {"face_recognition", "face_registration"}:
            resources.append("main_npu")
        elif skill in {"push_up", "pull_up", "squat"} and action not in {"query", "stop", "check"}:
            resources.extend(["fitness_session", "main_npu"])
            if "--projector-after-identity" in argv:
                resources.append("projector")
        elif skill == "person_tracking" and action not in {"stop", "check"}:
            resources.append("main_npu")
            if "--execute" in argv:
                resources.append("base")
            if "--execute" in argv or "--raise-head" in argv:
                resources.append("head")
        elif skill == "pet_tracking" and action not in {"stop"}:
            resources.append("pet_npu")
            backend = str(self._option(argv, "--backend", "ros2") or "ros2").lower()
            if backend == "ros2" and action != "smoke":
                resources.append("base")
        elif skill == "projector_control" and action not in {"off", "status", "meeting_pause", "meeting_resume"}:
            resources.append("projector")
        elif skill == "welcome_projection" and action not in {"status", "stop", "prepare"}:
            resources.append("projector")
        elif skill == "light_control":
            resources.append("light_device")
        elif skill == "feeder_control":
            resources.append("feeder_device")
        elif skill.startswith("reminder_"):
            resources.append("reminder_store")
        return resources

    @staticmethod
    def _is_priority_control(skill: str, argv: list[str]) -> bool:
        action = argv[0] if argv and not argv[0].startswith("-") else ""
        if skill in {"push_up", "pull_up", "squat"} and action in {"query", "stop"}:
            return True
        if skill in {"pet_tracking", "person_tracking"} and action == "stop":
            return True
        if skill in {"autonomous_projection", "pet_map_search"} and action == "stop":
            return True
        if skill == "projector_control" and action in {"off", "status", "meeting_pause", "meeting_resume"}:
            return True
        return False

    def run(self, skill: str, argv: list[str], stream: Callable[[str, str], None] | None = None) -> dict[str, Any]:
        if skill not in ALL_SKILLS:
            return {"ok": False, "exit_code": 64, "stdout": "", "stderr": "", "error": f"unknown_skill:{skill}"}
        if skill in PROFILE_DISABLED_SKILLS:
            return {
                "ok": False,
                "exit_code": 69,
                "stdout": "",
                "stderr": "",
                "error": f"disabled_skill:{skill}:{PROFILE_DISABLED_SKILLS[skill]}",
            }

        def invoke():
            if skill in CAMERA_SKILLS:
                fixed_camera, fixed_action = CAMERA_SKILLS[skill]
                return self.camera.execute(skill, argv, fixed_camera, fixed_action)
            if skill in MOVE_SKILLS:
                return self._move(skill, argv)
            if skill == "head_control":
                return self._head(argv)
            if skill in {"navigation_goto", "navigation_list"}:
                return self._navigation(skill, argv)
            if skill == "autonomous_projection":
                return self._autonomous_projection(argv)
            if skill == "pet_map_search":
                return self._pet_map_search(argv)
            if skill in {"face_recognition", "face_registration"}:
                return self._face(skill, argv)
            if skill in {"push_up", "pull_up", "squat"}:
                return self._fitness(skill, argv)
            if skill == "pet_tracking":
                return self._pet(argv)
            if skill == "person_tracking":
                return self._person(argv)
            return self._generic(skill, argv)

        def scheduled():
            if self._is_priority_control(skill, argv):
                return invoke()
            requested = self._resources_for(skill, argv)
            try:
                with self.resources.acquire(requested, skill, self.resource_wait_sec):
                    return invoke()
            except ResourceBusy as exc:
                print(json.dumps({
                    "ok": False, "skill": skill, "status": "resource_busy",
                    "resource": exc.resource, "owners": exc.owners,
                    "error": str(exc),
                }, ensure_ascii=False))
                return 75

        return self._capture_call(scheduled, stream)

    def status(self):
        pet_pid = None
        pet_alive = False
        try:
            pet_pid = int((STATE_DIR / "pet.pid").read_text(encoding="ascii").strip())
            os.kill(pet_pid, 0)
            pet_alive = True
        except (OSError, ValueError):
            pet_alive = False
        try:
            camera_broker = json.loads((STATE_DIR / "camera_status.json").read_text(encoding="utf-8"))
        except Exception as exc:
            camera_broker = {"state": "unavailable", "error": f"{type(exc).__name__}:{exc}"}
        return {
            "pid": os.getpid(),
            "uptime_sec": round(time.time() - self.started, 3),
            "skills": sorted(ALL_SKILLS - set(PROFILE_DISABLED_SKILLS)),
            "disabled_skills": dict(PROFILE_DISABLED_SKILLS),
            "count": len(ALL_SKILLS - set(PROFILE_DISABLED_SKILLS)),
            "preload": self.preload_results,
            "models": ["retinaface", "facenet", "yolo", "reid", "mediapipe_pose"],
            "ros_node": self.runtime.ros_node.get_name(),
            "pet_worker": {
                "pid": pet_pid,
                "alive": pet_alive,
                "socket_ready": (STATE_DIR / "pet.sock").is_socket(),
            },
            "camera_broker": camera_broker,
            "active_resources": self.resources.status(),
            "resource_wait_sec": self.resource_wait_sec,
        }

    def close(self):
        self.exploration_stop.set()
        self.camera.close_all()
        self.runtime.close()
        if sys.stdout is self.stdout_router:
            sys.stdout = self._original_stdout
        if sys.stderr is self.stderr_router:
            sys.stderr = self._original_stderr


def _write_status(state: str, owner: ResidentSkills | None, error: str | None = None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "pid": os.getpid(), "updated_at": time.time(), "socket": str(SOCKET_PATH), "error": error}
    if owner is not None:
        payload.update(owner.status())
    temp = STATUS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATUS_FILE)


def main() -> int:
    argparse.ArgumentParser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    _write_status("loading", None)
    owner = None
    server = None
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        owner = ResidentSkills()
        owner.preload()
        SOCKET_PATH.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(32)
        server.settimeout(0.1)
        _write_status("ready", owner)
        print(json.dumps({"event": "resident_runtime_ready", **owner.status(), "socket": str(SOCKET_PATH)}, ensure_ascii=False), flush=True)

        def serve(conn):
            with conn:
                try:
                    request, payload = recv_request(conn)
                    op = str(request.get("op", ""))
                    if op == "ping":
                        response = {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "result": owner.status()}
                    elif op == "skill_run":
                        streaming = bool(request.get("stream", False))

                        def stream(kind: str, data: str):
                            if streaming:
                                send_response(conn, {"type": kind, "data": data})

                        response = owner.run(
                            str(request.get("skill", "")),
                            [str(v) for v in request.get("argv", [])],
                            stream if streaming else None,
                        )
                        if streaming:
                            response = {"type": "final", **response}
                    else:
                        response = owner.runtime.handle(request, payload)
                        response.setdefault("exit_code", 0 if response.get("ok") else 1)
                        response.setdefault("stdout", "")
                        response.setdefault("stderr", "")
                    send_response(conn, response)
                except BaseException as exc:
                    with contextlib.suppress(Exception):
                        send_response(conn, {"ok": False, "exit_code": 1, "stdout": "", "stderr": "", "error": f"{type(exc).__name__}: {exc}"})

        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
        _write_status("stopping", owner)
        return 0
    except BaseException as exc:
        _write_status("failed", owner, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1
    finally:
        if server is not None:
            server.close()
        if owner is not None:
            owner.close()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        _write_status("stopped", None)


if __name__ == "__main__":
    raise SystemExit(main())
