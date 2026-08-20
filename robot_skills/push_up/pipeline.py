#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from rknn3lite.api import RKNN3Lite
from rknnlite.api import RKNNLite


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
COCO_PERSON_CLASS_ID = 0


class ReIDTestError(RuntimeError):
    pass


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, "timestamp": round(time.time(), 3), **payload}, ensure_ascii=False), flush=True)


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    for key in ("models", "camera", "detector", "reid", "paths"):
        if not isinstance(value.get(key), dict):
            raise ReIDTestError(f"missing config object: {key}")
    return value


def safe_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    if not result:
        raise ReIDTestError("invalid profile name")
    return result[:64]


def normalized(vector: Any) -> np.ndarray:
    result = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(result))
    if norm <= 1e-9:
        raise ReIDTestError("empty ReID embedding")
    return result / norm


@dataclass
class Detection:
    confidence: float
    bbox: tuple[int, int, int, int]

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bbox
        return max(0, x2 - x1) * max(0, y2 - y1)


class PersonDetector:
    INPUT_W = 640
    INPUT_H = 640

    def __init__(self, model: str, weight: str, confidence: float, core_mask: int, device_id: str = "0002:21:00.0"):
        if not Path(model).is_file() or not Path(weight).is_file():
            raise ReIDTestError("person detector model or weight is missing")
        self.confidence = float(confidence)
        self.rknn = RKNN3Lite()
        ret = self.rknn.load_rknn(model, weight)
        if ret != 0:
            raise ReIDTestError(f"detector load_rknn failed: {ret}")
        ids = self.rknn.get_devices_id()
        if not ids:
            raise ReIDTestError("RKNN3 device not found")
        selected_device = str(device_id or "0002:21:00.0").encode("ascii")
        ret = self.rknn.init_runtime(target="rk3588", core_mask=int(core_mask), device_id=selected_device)
        if ret != 0:
            raise ReIDTestError(f"detector init_runtime failed: {ret}")

    def detect(self, frame: np.ndarray) -> list[Detection]:
        height, width = frame.shape[:2]
        scale = min(self.INPUT_W / width, self.INPUT_H / height)
        new_w, new_h = int(width * scale), int(height * scale)
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.INPUT_H, self.INPUT_W, 3), 114, dtype=np.uint8)
        pad_x, pad_y = (self.INPUT_W - new_w) // 2, (self.INPUT_H - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        input_data = np.expand_dims(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB), 0)
        outputs = self.rknn.inference(inputs=[input_data])
        if not outputs:
            return []
        raw = np.asarray(outputs[0], dtype=np.float32)
        if raw.ndim == 3 and raw.shape[0] == 1:
            raw = raw[0]
        if raw.ndim != 2 or raw.shape[1] < 6:
            raise ReIDTestError(f"unexpected detector output shape: {raw.shape}")
        raw = raw[np.any(raw != 0, axis=1)]
        result: list[Detection] = []
        for row in raw:
            confidence, class_id = float(row[0]), int(row[1])
            if confidence < self.confidence or class_id != COCO_PERSON_CLASS_ID:
                continue
            x1 = int(np.clip((row[2] - pad_x) / scale, 0, width))
            y1 = int(np.clip((row[3] - pad_y) / scale, 0, height))
            x2 = int(np.clip((row[4] - pad_x) / scale, 0, width))
            y2 = int(np.clip((row[5] - pad_y) / scale, 0, height))
            if x2 > x1 and y2 > y1:
                result.append(Detection(confidence, (x1, y1, x2, y2)))
        return result

    def release(self) -> None:
        self.rknn.release()


class RknnReID:
    WIDTH = 128
    HEIGHT = 256

    def __init__(self, model: str, core: int):
        if not Path(model).is_file():
            raise ReIDTestError(f"ReID model missing: {model}")
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(model)
        if ret != 0:
            raise ReIDTestError(f"ReID load_rknn failed: {ret}")
        masks = {
            0: None,
            1: RKNNLite.NPU_CORE_0,
            2: RKNNLite.NPU_CORE_1,
            3: RKNNLite.NPU_CORE_2,
            4: RKNNLite.NPU_CORE_0_1_2,
        }
        mask = masks.get(int(core))
        ret = self.rknn.init_runtime(**({"core_mask": mask} if mask is not None else {}))
        if ret != 0:
            raise ReIDTestError(f"ReID init_runtime failed: {ret}")

    def feature(self, crop: np.ndarray) -> np.ndarray:
        if crop is None or crop.size == 0:
            raise ReIDTestError("empty person crop")
        height, width = crop.shape[:2]
        scale = min(self.WIDTH / width, self.HEIGHT / height)
        new_w, new_h = max(1, int(width * scale)), max(1, int(height * scale))
        resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
        x, y = (self.WIDTH - new_w) // 2, (self.HEIGHT - new_h) // 2
        canvas[y:y + new_h, x:x + new_w] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        # The RKNN model input is NHWC.  Supplying NCHW made the runtime
        # transpose every frame and print one warning per inference, flooding
        # the log during identity/ReID tracking.
        data = np.expand_dims(rgb, 0)
        outputs = self.rknn.inference(inputs=[data], data_format=["nhwc"])
        if not outputs:
            raise ReIDTestError("ReID inference returned no output")
        return normalized(outputs[0][0])

    def release(self) -> None:
        self.rknn.release()


def crop_person(frame: np.ndarray, bbox: tuple[int, int, int, int], padding: float = 0.04) -> np.ndarray:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    px, py = int((x2 - x1) * padding), int((y2 - y1) * padding)
    x1, y1 = max(0, x1 - px), max(0, y1 - py)
    x2, y2 = min(width, x2 + px), min(height, y2 + py)
    return frame[y1:y2, x1:x2]


def profile_path(name: str, config: dict[str, Any]) -> Path:
    directory = Path(config["paths"]["profiles_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_name(name)}.npz"


def save_profile(name: str, features: list[np.ndarray], config: dict[str, Any]) -> tuple[Path, dict[str, float]]:
    matrix = np.stack(features).astype(np.float32)
    similarities = matrix @ matrix.T
    pair_values = similarities[np.triu_indices(len(matrix), 1)]
    min_pair = float(np.min(pair_values)) if pair_values.size else 1.0
    median_pair = float(np.median(pair_values)) if pair_values.size else 1.0
    minimum = float(config["reid"]["enrollment_min_pair_similarity"])
    if min_pair < minimum:
        raise ReIDTestError(f"enrollment samples are inconsistent: min similarity {min_pair:.3f} < {minimum:.3f}")
    centroid = normalized(np.mean(matrix, axis=0))
    path = profile_path(name, config)
    np.savez_compressed(path, name=np.asarray([name]), centroid=centroid, features=matrix, created_at=np.asarray([time.time()]))
    os.chmod(path, 0o600)
    return path, {"minimum_pair_similarity": round(min_pair, 4), "median_pair_similarity": round(median_pair, 4)}


def load_profile(name: str, config: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    path = profile_path(name, config)
    if not path.is_file():
        raise ReIDTestError(f"profile not found: {path}")
    with np.load(path, allow_pickle=False) as data:
        return normalized(data["centroid"]), np.asarray(data["features"], dtype=np.float32)


def identity_score(feature: np.ndarray, centroid: np.ndarray, gallery: np.ndarray, top_k: int) -> float:
    gallery_scores = np.sort(gallery @ feature)[::-1]
    top_mean = float(np.mean(gallery_scores[:max(1, min(top_k, len(gallery_scores)))]))
    return 0.7 * float(np.dot(centroid, feature)) + 0.3 * top_mean


def open_source(source: str, config: dict[str, Any]) -> cv2.VideoCapture:
    value: Any = int(source) if source.isdigit() else source
    capture = cv2.VideoCapture(value)
    if not capture.isOpened():
        raise ReIDTestError(f"cannot open source: {source}")
    if str(source).startswith("/dev/video") or source.isdigit():
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(config["camera"]["width"]))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(config["camera"]["height"]))
        capture.set(cv2.CAP_PROP_FPS, int(config["camera"]["fps"]))
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(int(config["camera"].get("warmup_frames", 0))):
            capture.read()
    return capture


def valid_people(detections: list[Detection], config: dict[str, Any]) -> list[Detection]:
    minimum_w = int(config["detector"]["min_box_width"])
    minimum_h = int(config["detector"]["min_box_height"])
    return [d for d in detections if d.bbox[2] - d.bbox[0] >= minimum_w and d.bbox[3] - d.bbox[1] >= minimum_h]


def build_models(config: dict[str, Any]) -> tuple[PersonDetector, RknnReID]:
    models, detector_cfg, reid_cfg = config["models"], config["detector"], config["reid"]
    detector = PersonDetector(
        models["person_detector"],
        models["person_detector_weight"],
        detector_cfg["confidence"],
        detector_cfg["npu_core_mask"],
        detector_cfg.get("device_id", "0002:21:00.0"),
    )
    try:
        reid = RknnReID(models["person_reid"], reid_cfg["npu_core"])
    except Exception:
        detector.release()
        raise
    return detector, reid


def command_check(config: dict[str, Any]) -> None:
    started = time.perf_counter()
    detector, reid = build_models(config)
    init_ms = (time.perf_counter() - started) * 1000
    try:
        dummy = np.zeros((256, 128, 3), dtype=np.uint8)
        feature = reid.feature(dummy)
        emit("check", ok=True, initialization_ms=round(init_ms, 2), feature_dimension=int(feature.size), feature_norm=round(float(np.linalg.norm(feature)), 4))
    finally:
        reid.release()
        detector.release()


def command_enroll(args: argparse.Namespace, config: dict[str, Any]) -> None:
    detector, reid = build_models(config)
    capture = open_source(args.source, config)
    features: list[np.ndarray] = []
    frame_index = 0
    deadline = time.monotonic() + args.seconds
    needed = int(config["reid"]["enrollment_samples"])
    interval = max(1, int(config["reid"]["feature_interval_frames"]))
    emit("enrollment_started", name=args.name, required_samples=needed, rule="exactly_one_valid_person")
    try:
        while time.monotonic() < deadline and len(features) < needed:
            ok, frame = capture.read()
            if not ok or frame is None:
                if Path(args.source).is_file():
                    break
                continue
            frame_index += 1
            people = valid_people(detector.detect(frame), config)
            if len(people) != 1 or frame_index % interval:
                continue
            features.append(reid.feature(crop_person(frame, people[0].bbox)))
            emit("enrollment_sample", current=len(features), required=needed, detection_confidence=round(people[0].confidence, 4))
        if len(features) < needed:
            raise ReIDTestError(f"only collected {len(features)}/{needed} valid single-person samples")
        path, quality = save_profile(args.name, features, config)
        emit("enrollment_complete", ok=True, name=args.name, profile=str(path), samples=len(features), **quality)
    finally:
        capture.release()
        reid.release()
        detector.release()


class RealtimeVideoWriter:
    """Writes constant-rate MP4 while preserving wall-clock duration."""

    def __init__(self, path: Path, frame: np.ndarray, fps: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        height, width = frame.shape[:2]
        reported_fps = float(fps or 0.0)
        self.fps = reported_fps if 5.0 <= reported_fps <= 60.0 else 15.0
        self.writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (width, height))
        if not self.writer.isOpened():
            raise ReIDTestError(f"cannot open video writer: {path}")
        self.started_at = time.monotonic()
        self.frames_written = 0
        self.last_frame = None

    def write(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        desired_frames = max(1, int(round((now - self.started_at) * self.fps)) + 1)
        copies = max(0, desired_frames - self.frames_written)
        for _ in range(copies):
            self.writer.write(frame)
        self.frames_written += copies
        self.last_frame = frame.copy()

    def release(self) -> None:
        if self.last_frame is not None:
            desired_frames = max(1, int(round((time.monotonic() - self.started_at) * self.fps)))
            for _ in range(max(0, desired_frames - self.frames_written)):
                self.writer.write(self.last_frame)
                self.frames_written += 1
        self.writer.release()


def make_writer(path: Path, frame: np.ndarray, fps: float) -> RealtimeVideoWriter:
    return RealtimeVideoWriter(path, frame, fps)


def command_identify(args: argparse.Namespace, config: dict[str, Any]) -> None:
    centroid, gallery = load_profile(args.name, config)
    detector, reid = build_models(config)
    capture = open_source(args.source, config)
    runtime = Path(config["paths"]["runtime_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else runtime / f"identify_{safe_name(args.name)}_{int(time.time())}.mp4"
    events_path = output.with_suffix(".jsonl")
    writer = None
    accept_threshold = float(config["reid"]["accept_threshold"])
    reject_threshold = float(config["reid"]["reject_threshold"])
    min_margin = float(config["reid"]["minimum_candidate_margin"])
    stable_needed = int(config["reid"]["stable_accept_frames"])
    lost_grace = int(config["reid"]["lost_grace_frames"])
    top_k = int(config["reid"]["gallery_top_k"])
    stable_count = 0
    lost_count = 0
    accepted_latched = False
    accepted_frames = ambiguous_frames = 0
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else float("inf")
    emit("identification_started", name=args.name, source=args.source, output=str(output))
    try:
        with events_path.open("w", encoding="utf-8") as event_file:
            while time.monotonic() < deadline:
                ok, frame = capture.read()
                if not ok or frame is None:
                    if Path(args.source).is_file():
                        break
                    continue
                people = valid_people(detector.detect(frame), config)
                candidates = []
                for person in people:
                    feature = reid.feature(crop_person(frame, person.bbox))
                    score = identity_score(feature, centroid, gallery, top_k)
                    candidates.append((score, person))
                candidates.sort(key=lambda item: item[0], reverse=True)
                best_score = candidates[0][0] if candidates else -1.0
                second_score = candidates[1][0] if len(candidates) > 1 else -1.0
                margin = best_score - second_score if len(candidates) > 1 else 1.0
                raw_accept = best_score >= accept_threshold and margin >= min_margin
                if raw_accept:
                    stable_count += 1
                    lost_count = 0
                    if stable_count >= stable_needed:
                        accepted_latched = True
                elif accepted_latched and (not candidates or best_score < reject_threshold):
                    lost_count += 1
                    stable_count = 0
                    if lost_count > lost_grace:
                        accepted_latched = False
                else:
                    stable_count = 0
                    lost_count = 0
                    accepted_latched = False
                stable_accept = raw_accept and accepted_latched
                if stable_accept:
                    state = "accepted"
                    accepted_frames += 1
                elif accepted_latched and lost_count <= lost_grace:
                    state = "temporarily_lost"
                elif best_score >= reject_threshold:
                    state = "uncertain"
                    ambiguous_frames += 1
                else:
                    state = "rejected"
                for index, (score, person) in enumerate(candidates):
                    x1, y1, x2, y2 = person.bbox
                    selected = index == 0 and stable_accept
                    color = (0, 200, 0) if selected else (0, 220, 255) if index == 0 and state == "uncertain" else (0, 0, 220)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, f"{score:.3f}", (x1, max(20, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                summary = {"state": state, "people": len(people), "best_score": round(best_score, 4), "second_score": round(second_score, 4), "margin": round(margin, 4), "stable_frames": stable_count, "lost_frames": lost_count}
                event_file.write(json.dumps({"timestamp": time.time(), **summary}, ensure_ascii=False) + "\n")
                cv2.putText(frame, f"target={state} people={len(people)}", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                if writer is None:
                    fps = capture.get(cv2.CAP_PROP_FPS) or float(config["camera"]["fps"])
                    writer = make_writer(output, frame, fps)
                writer.write(frame)
        emit("identification_complete", ok=True, output=str(output), events=str(events_path), accepted_frames=accepted_frames, ambiguous_frames=ambiguous_frames)
    finally:
        if writer is not None:
            writer.release()
        capture.release()
        reid.release()
        detector.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated person ReID experiment for fitness target selection")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--source", default=None)
    enroll.add_argument("--seconds", type=float, default=12.0)
    identify = sub.add_parser("identify")
    identify.add_argument("--name", required=True)
    identify.add_argument("--source", default=None)
    identify.add_argument("--seconds", type=float, default=30.0)
    identify.add_argument("--output")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(Path(args.config))
    if hasattr(args, "source") and not args.source:
        args.source = str(config["camera"]["source"])
    try:
        if args.command == "check":
            command_check(config)
        elif args.command == "enroll":
            command_enroll(args, config)
        elif args.command == "identify":
            command_identify(args, config)
        return 0
    except KeyboardInterrupt:
        emit("stopped", ok=False, error="keyboard_interrupt")
        return 130
    except Exception as exc:
        emit("error", ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
