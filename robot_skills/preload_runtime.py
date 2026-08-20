#!/usr/bin/env python3
"""Safe warm-cache experiment for v11_single_function_skills.

This process never publishes robot commands and never calls appliance APIs.
RKNN contexts and cameras are released after one warm-up inference/read so the
existing standalone skills remain the only owners when they are started.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "preload"
STATUS_FILE = STATE_DIR / "status.json"
PID_FILE = STATE_DIR / "preload.pid"
READY_FILE = STATE_DIR / "ready"


class PreloadFailure(RuntimeError):
    pass


def now() -> float:
    return time.time()


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, "ts": round(now(), 3), **payload}, ensure_ascii=False, default=str), flush=True)


def rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return -1.0


def write_status(state: str, stages: list[dict[str, Any]], started: float, error: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    value = {
        "state": state,
        "pid": os.getpid(),
        "started_at": started,
        "updated_at": now(),
        "elapsed_sec": round(time.perf_counter() - STARTED_MONOTONIC, 3),
        "rss_mb": rss_mb(),
        "stages": stages,
        "error": error,
        "safety": {
            "publishes_motion": False,
            "controls_hardware": False,
            "holds_camera": False,
            "holds_rknn_context": False,
        },
    }
    temp = STATUS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(STATUS_FILE)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PreloadFailure(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_stage(stages: list[dict[str, Any]], name: str, action: Callable[[], Any], required: bool = True) -> Any:
    started = time.perf_counter()
    try:
        result = action()
        elapsed = round((time.perf_counter() - started) * 1000.0, 2)
        record = {"name": name, "ok": True, "required": required, "elapsed_ms": elapsed}
        if result is not None:
            record["result"] = result
        stages.append(record)
        emit("preload_stage", **record)
        return result
    except Exception as exc:
        elapsed = round((time.perf_counter() - started) * 1000.0, 2)
        record = {
            "name": name,
            "ok": False,
            "required": required,
            "elapsed_ms": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        }
        stages.append(record)
        emit("preload_stage", **record)
        if required:
            raise
        return None


def import_dependencies() -> dict[str, str]:
    names = [
        "numpy",
        "cv2",
        "requests",
        "websockets",
        "rclpy",
        "nav2_msgs.action",
        "rknnlite.api",
        "rknn3lite.api",
        "mediapipe",
    ]
    versions: dict[str, str] = {}
    for name in names:
        module = importlib.import_module(name)
        versions[name] = str(getattr(module, "__version__", "loaded"))
    return versions


def model_paths() -> list[str]:
    config_path = ROOT / "push_up" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configured = [Path(value) for value in config.get("models", {}).values()]
    configured.append(ROOT / "face_recognition" / "assets" / "model" / "RetinaFace_resnet50_320_fp.rknn")
    result: list[str] = []
    seen: set[str] = set()
    for path in configured:
        resolved = path.resolve()
        key = str(resolved)
        if key not in seen:
            if not resolved.is_file():
                raise FileNotFoundError(resolved)
            result.append(str(resolved))
            seen.add(key)
    return result


def read_model_files(paths: list[str]) -> tuple[list[bytes], dict[str, Any]]:
    # Retaining the blocks prevents the kernel from immediately reclaiming these
    # pages while the experiment is running. This does not retain an RKNN context.
    blocks: list[bytes] = []
    sizes: dict[str, int] = {}
    for value in paths:
        path = Path(value)
        data = path.read_bytes()
        blocks.append(data)
        sizes[str(path)] = len(data)
    return blocks, {"files": len(paths), "total_mb": round(sum(sizes.values()) / 1048576.0, 2), "sizes": sizes}


def warm_ros() -> dict[str, float]:
    import rclpy
    from nav2_msgs.action import NavigateToPose
    from rclpy.action import ActionClient

    timings: dict[str, float] = {}
    started = time.perf_counter()
    rclpy.init(args=None)
    timings["init_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    node = None
    try:
        started = time.perf_counter()
        node = rclpy.create_node(f"v11_skill_preload_{os.getpid()}")
        timings["node_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        started = time.perf_counter()
        client = ActionClient(node, NavigateToPose, "navigate_to_pose")
        timings["action_client_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        client.destroy()
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return timings


def warm_face_detector() -> dict[str, Any]:
    import numpy as np

    module = load_module("v11_preload_retinaface", ROOT / "face_recognition" / "retinaface_rknn.py")
    path = ROOT / "face_recognition" / "assets" / "model" / "RetinaFace_resnet50_320_fp.rknn"
    model = module.RetinaFaceRKNN(path)
    try:
        detections = model.detect(np.zeros((320, 320, 3), dtype=np.uint8), score_threshold=0.99)
        return {"model": str(path), "dummy_detections": len(detections)}
    finally:
        model.release()


def warm_face_embedding() -> dict[str, Any]:
    import numpy as np

    face_dir = str(ROOT / "face_recognition")
    if face_dir not in sys.path:
        sys.path.insert(0, face_dir)
    module = load_module("v11_preload_face_common", ROOT / "face_recognition" / "face_common.py")
    cfg = module.FaceConfig(ROOT / "face_recognition")
    model = module.RKNNFaceEmbeddingModel(cfg.model_path)
    try:
        feature = model.extract(np.zeros((160, 160, 3), dtype=np.uint8))
        return {"model": str(cfg.model_path), "feature_dimension": int(feature.size)}
    finally:
        model.release()


def load_fitness_config() -> tuple[Any, dict[str, Any]]:
    module = load_module("v11_preload_fitness_pipeline", ROOT / "push_up" / "pipeline.py")
    config = json.loads((ROOT / "push_up" / "config.json").read_text(encoding="utf-8"))
    return module, config


def warm_yolo(module: Any, config: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    models = config["models"]
    detector_cfg = config["detector"]
    model = module.PersonDetector(
        models["person_detector"],
        models["person_detector_weight"],
        detector_cfg["confidence"],
        detector_cfg["npu_core_mask"],
        detector_cfg.get("device_id", "0002:21:00.0"),
    )
    try:
        detections = model.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        return {"model": models["person_detector"], "dummy_detections": len(detections)}
    finally:
        model.release()


def warm_reid(module: Any, config: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    model_path = config["models"]["person_reid"]
    model = module.RknnReID(model_path, config["reid"]["npu_core"])
    try:
        feature = model.feature(np.zeros((256, 128, 3), dtype=np.uint8))
        return {"model": model_path, "feature_dimension": int(feature.size)}
    finally:
        model.release()


def warm_pose() -> dict[str, Any]:
    import cv2
    import mediapipe as mp
    import numpy as np

    pose = mp.solutions.pose.Pose(
        static_image_mode=False,
        model_complexity=1,
        enable_segmentation=False,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    try:
        image = cv2.cvtColor(np.zeros((480, 640, 3), dtype=np.uint8), cv2.COLOR_BGR2RGB)
        result = pose.process(image)
        return {"landmarks": bool(result.pose_landmarks)}
    finally:
        pose.close()


def warm_camera(device: str) -> dict[str, Any]:
    import cv2

    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"cannot open {device}")
    frames = 0
    started = time.perf_counter()
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(8):
            ok, frame = cap.read()
            if ok and frame is not None:
                frames += 1
    finally:
        cap.release()
    return {"device": device, "frames": frames, "warmup_ms": round((time.perf_counter() - started) * 1000.0, 2), "released": True}


STARTED_MONOTONIC = time.perf_counter()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keepalive", action="store_true", help="keep model file pages resident until Ctrl-C")
    parser.add_argument("--skip-camera", action="store_true", help="do not briefly warm and release cameras")
    args = parser.parse_args()

    started_wall = now()
    stages: list[dict[str, Any]] = []
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    READY_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    write_status("loading", stages, started_wall)
    emit("preload_started", pid=os.getpid(), root=str(ROOT), keepalive=args.keepalive)

    held_model_blocks: list[bytes] = []
    try:
        run_stage(stages, "python_dependencies", import_dependencies)
        paths = run_stage(stages, "resolve_model_files", model_paths)
        page_holder: dict[str, list[bytes]] = {}

        def cache_model_files() -> dict[str, Any]:
            blocks, metadata = read_model_files(paths)
            page_holder["blocks"] = blocks
            return metadata

        run_stage(stages, "model_file_pages", cache_model_files)
        held_model_blocks = page_holder["blocks"]
        run_stage(stages, "ros_rclpy_and_action_types", warm_ros)
        run_stage(stages, "retinaface_rknn", warm_face_detector)
        run_stage(stages, "facenet_rknn", warm_face_embedding)
        fitness_module, fitness_config = load_fitness_config()
        run_stage(stages, "yolo_rknn3", lambda: warm_yolo(fitness_module, fitness_config))
        run_stage(stages, "reid_rknn", lambda: warm_reid(fitness_module, fitness_config))
        run_stage(stages, "mediapipe_pose", warm_pose)
        if not args.skip_camera:
            run_stage(stages, "front_camera", lambda: warm_camera("/dev/video22"), required=False)
            run_stage(stages, "back_camera", lambda: warm_camera("/dev/video31"), required=False)

        READY_FILE.write_text(str(now()), encoding="ascii")
        write_status("ready" if args.keepalive else "complete", stages, started_wall)
        emit(
            "preload_ready",
            elapsed_sec=round(time.perf_counter() - STARTED_MONOTONIC, 3),
            rss_mb=rss_mb(),
            model_pages_mb=round(sum(len(v) for v in held_model_blocks) / 1048576.0, 2),
            cameras_released=True,
            rknn_contexts_released=True,
        )

        if args.keepalive:
            stopped = False

            def request_stop(_signum, _frame):
                nonlocal stopped
                stopped = True

            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            while not stopped:
                time.sleep(0.5)
            write_status("stopped", stages, started_wall)
            emit("preload_stopped")
        return 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_status("failed", stages, started_wall, error)
        emit("preload_failed", error=error, traceback=traceback.format_exc())
        return 1
    finally:
        READY_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
