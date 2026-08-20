#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from rknnlite.api import RKNNLite

from fitness_reid_test import (
    DEFAULT_CONFIG,
    PersonDetector,
    ReIDTestError,
    RknnReID,
    crop_person,
    emit,
    identity_score,
    load_config,
    make_writer,
    normalized,
    open_source,
    safe_name,
    valid_people,
)


def ensure_mediapipe_protobuf_compat() -> None:
    try:
        from google.protobuf import message_factory
    except Exception:
        return
    if hasattr(message_factory, "GetMessageClass"):
        return
    factory = message_factory.MessageFactory()
    if hasattr(factory, "GetPrototype"):
        message_factory.GetMessageClass = factory.GetPrototype


class FaceEmbeddingModel:
    def __init__(self, model: str, core: int):
        if not Path(model).is_file():
            raise ReIDTestError(f"face model missing: {model}")
        self.rknn = RKNNLite(verbose=False)
        ret = self.rknn.load_rknn(model)
        if ret != 0:
            raise ReIDTestError(f"face model load failed: {ret}")
        masks = {1: RKNNLite.NPU_CORE_0, 2: RKNNLite.NPU_CORE_1, 3: RKNNLite.NPU_CORE_2}
        ret = self.rknn.init_runtime(core_mask=masks.get(int(core), RKNNLite.NPU_CORE_1))
        if ret != 0:
            raise ReIDTestError(f"face model runtime init failed: {ret}")

    def feature(self, face: np.ndarray) -> np.ndarray:
        resized = cv2.resize(face, (160, 160))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = ((rgb.astype(np.float32) - 127.5) / 128.0)[None, :, :, :]
        outputs = self.rknn.inference(inputs=[tensor.astype(np.float32)])
        if not outputs:
            raise ReIDTestError("face model returned no output")
        return normalized(outputs[0])

    def release(self) -> None:
        self.rknn.release()


def load_registered_faces(path: str) -> list[dict[str, Any]]:
    database = Path(path)
    if not database.is_file():
        raise ReIDTestError(f"face database missing: {database}")
    result = []
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT name, vector, person_id FROM face_vectors WHERE name != ?", ("__pending_face__",)).fetchall()
    for name, blob, person_id in rows:
        try:
            result.append({"name": str(name), "person_id": str(person_id or name), "vector": normalized(pickle.loads(blob))})
        except Exception as exc:
            emit("face_database_row_skipped", name=str(name), error=str(exc))
    if not result:
        raise ReIDTestError("face database has no registered identities")
    return result


def face_crop(frame: np.ndarray, relative_box: Any) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    height, width = frame.shape[:2]
    x = int(relative_box.xmin * width)
    y = int(relative_box.ymin * height)
    w = int(relative_box.width * width)
    h = int(relative_box.height * height)
    pad_x, pad_y = int(w * 0.20), int(h * 0.20)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(width, x + w + pad_x), min(height, y + h + pad_y)
    return frame[y1:y2, x1:x2], (max(0, x), max(0, y), min(width, x + w), min(height, y + h))


def person_for_face(face_box: tuple[int, int, int, int], people: list[Any]) -> Any | None:
    fx1, fy1, fx2, fy2 = face_box
    center_x, center_y = (fx1 + fx2) * 0.5, (fy1 + fy2) * 0.5
    containing = [p for p in people if p.bbox[0] <= center_x <= p.bbox[2] and p.bbox[1] <= center_y <= p.bbox[3]]
    if not containing:
        return None
    return min(containing, key=lambda p: p.area)


def recognize_face(feature: np.ndarray, known: list[dict[str, Any]], threshold: float, margin_required: float) -> dict[str, Any] | None:
    scored = sorted(((float(np.dot(feature, item["vector"])), item) for item in known), key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    margin = best_score - second_score if len(scored) > 1 else 1.0
    if best_score < threshold or margin < margin_required:
        return None
    return {"name": best["name"], "person_id": best["person_id"], "score": best_score, "margin": margin}


def angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    ba, bc = a - b, c - b
    denominator = float(np.linalg.norm(ba) * np.linalg.norm(bc))
    if denominator <= 1e-9:
        return 0.0
    cosine = float(np.clip(np.dot(ba, bc) / denominator, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


class ExerciseCounter:
    def __init__(self, exercise: str, config: dict[str, Any]):
        ensure_mediapipe_protobuf_compat()
        self.exercise = exercise
        self.config = config["fitness"]
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            smooth_landmarks=True,
            min_detection_confidence=float(self.config["pose_detection_confidence"]),
            min_tracking_confidence=float(self.config["pose_tracking_confidence"]),
        )
        self.phase = None
        self.count = 0

    def reset_phase(self) -> None:
        self.phase = None

    def _point(self, landmarks: list[Any], index: int) -> np.ndarray | None:
        item = landmarks[index]
        if float(item.visibility) < float(self.config["minimum_landmark_visibility"]):
            return None
        return np.asarray([item.x, item.y], dtype=np.float32)

    def process(self, crop: np.ndarray) -> dict[str, Any]:
        result = self.pose.process(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        if result.pose_landmarks is None:
            return {"valid": False, "count": self.count, "phase": self.phase}
        lm = result.pose_landmarks.landmark
        pose = mp.solutions.pose.PoseLandmark
        required = {
            "ls": self._point(lm, pose.LEFT_SHOULDER.value), "rs": self._point(lm, pose.RIGHT_SHOULDER.value),
            "le": self._point(lm, pose.LEFT_ELBOW.value), "re": self._point(lm, pose.RIGHT_ELBOW.value),
            "lw": self._point(lm, pose.LEFT_WRIST.value), "rw": self._point(lm, pose.RIGHT_WRIST.value),
            "lh": self._point(lm, pose.LEFT_HIP.value), "rh": self._point(lm, pose.RIGHT_HIP.value),
            "lk": self._point(lm, pose.LEFT_KNEE.value), "rk": self._point(lm, pose.RIGHT_KNEE.value),
            "la": self._point(lm, pose.LEFT_ANKLE.value), "ra": self._point(lm, pose.RIGHT_ANKLE.value),
            "nose": self._point(lm, pose.NOSE.value),
        }
        incremented = False
        metric = None
        if self.exercise == "squat":
            if any(required[k] is None for k in ("lh", "lk", "la", "rh", "rk", "ra")):
                return {"valid": False, "count": self.count, "phase": self.phase}
            metric = (angle(required["lh"], required["lk"], required["la"]) + angle(required["rh"], required["rk"], required["ra"])) * 0.5
            if metric >= float(self.config["squat_up_angle"]):
                if self.phase == "down": self.count += 1; incremented = True
                self.phase = "up"
            elif metric <= float(self.config["squat_down_angle"]) and self.phase == "up": self.phase = "down"
        elif self.exercise == "push_up":
            if any(required[k] is None for k in ("ls", "le", "lw", "rs", "re", "rw")):
                return {"valid": False, "count": self.count, "phase": self.phase}
            metric = (angle(required["ls"], required["le"], required["lw"]) + angle(required["rs"], required["re"], required["rw"])) * 0.5
            if metric >= float(self.config["push_up_up_angle"]):
                if self.phase == "down": self.count += 1; incremented = True
                self.phase = "up"
            elif metric <= float(self.config["push_up_down_angle"]) and self.phase == "up": self.phase = "down"
        else:
            if any(required[k] is None for k in ("ls", "le", "lw", "rs", "re", "rw", "nose")):
                return {"valid": False, "count": self.count, "phase": self.phase}
            metric = (angle(required["ls"], required["le"], required["lw"]) + angle(required["rs"], required["re"], required["rw"])) * 0.5
            shoulder_y = float((required["ls"][1] + required["rs"][1]) * 0.5)
            if metric <= float(self.config["pull_up_up_angle"]) and float(required["nose"][1]) < shoulder_y:
                if self.phase == "down": self.count += 1; incremented = True
                self.phase = "up"
            elif metric >= float(self.config["pull_up_down_angle"]): self.phase = "down"
        return {"valid": True, "count": self.count, "phase": self.phase, "metric": round(float(metric), 2), "incremented": incremented}

    def release(self) -> None:
        self.pose.close()


def acquire_identity(frame: np.ndarray, people: list[Any], detector: Any, face_model: FaceEmbeddingModel, known: list[dict[str, Any]], config: dict[str, Any], requested_name: str | None, diagnostics: dict[str, int] | None = None) -> list[dict[str, Any]]:
    face_cfg = config["face"]
    if diagnostics is not None:
        diagnostics["frames"] = diagnostics.get("frames", 0) + 1
        diagnostics["person_detections"] = diagnostics.get("person_detections", 0) + len(people)
    matches = []
    frame_h, frame_w = frame.shape[:2]
    for person in people:
        px1, py1, px2, py2 = person.bbox
        person_w, person_h = px2 - px1, py2 - py1
        pad_x = int(person_w * 0.08)
        crop_x1, crop_x2 = max(0, px1 - pad_x), min(frame_w, px2 + pad_x)
        crop_y1 = max(0, py1 - int(person_h * 0.04))
        crop_y2 = min(frame_h, py1 + int(person_h * float(face_cfg["person_upper_body_ratio"])))
        upper = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        if upper.size == 0:
            continue
        scale = max(1.0, float(face_cfg["minimum_detector_crop_width"]) / max(1.0, float(upper.shape[1])))
        scale = min(scale, float(face_cfg["maximum_detector_upscale"]))
        upper_for_detection = cv2.resize(upper, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1.0 else upper
        results = detector.process(cv2.cvtColor(upper_for_detection, cv2.COLOR_BGR2RGB))
        if diagnostics is not None:
            diagnostics["face_detections"] = diagnostics.get("face_detections", 0) + len(results.detections or [])
        for detection in results.detections or []:
            face, local_box = face_crop(upper_for_detection, detection.location_data.relative_bounding_box)
            box = (
                crop_x1 + int(local_box[0] / scale), crop_y1 + int(local_box[1] / scale),
                crop_x1 + int(local_box[2] / scale), crop_y1 + int(local_box[3] / scale),
            )
            original_side = min(box[2] - box[0], box[3] - box[1])
            if face.size == 0 or original_side < int(face_cfg["minimum_face_side"]):
                if diagnostics is not None:
                    diagnostics["faces_too_small"] = diagnostics.get("faces_too_small", 0) + 1
                continue
            focus = cv2.Laplacian(cv2.cvtColor(face, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            if focus < float(face_cfg["minimum_focus"]):
                if diagnostics is not None:
                    diagnostics["faces_low_focus"] = diagnostics.get("faces_low_focus", 0) + 1
                continue
            feature = face_model.feature(face)
            if diagnostics is not None:
                best_score = max((float(np.dot(feature, item["vector"])) for item in known), default=-1.0)
                diagnostics["best_face_score_milli"] = max(diagnostics.get("best_face_score_milli", -1000), int(round(best_score * 1000)))
            identity = recognize_face(feature, known, float(face_cfg["match_threshold"]), float(face_cfg["match_margin"]))
            if identity is None or (requested_name and identity["name"] != requested_name):
                if diagnostics is not None:
                    diagnostics["faces_not_recognized"] = diagnostics.get("faces_not_recognized", 0) + 1
                continue
            matches.append({"identity": identity, "person": person, "face_box": box})
            if diagnostics is not None:
                diagnostics["accepted_face_person_matches"] = diagnostics.get("accepted_face_person_matches", 0) + 1
    return matches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent face-confirmed ReID exercise counter")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--exercise", required=True, choices=["squat", "push_up", "pull_up"])
    parser.add_argument("--name")
    parser.add_argument("--source")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--output")
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(Path(args.config))
    source = args.source or str(config["camera"]["source"])
    models = config["models"]
    detector = PersonDetector(models["person_detector"], models["person_detector_weight"], config["detector"]["confidence"], config["detector"]["npu_core_mask"])
    reid = RknnReID(models["person_reid"], config["reid"]["npu_core"])
    face_model = FaceEmbeddingModel(models["face_embedding"], config["face"]["npu_core"])
    if args.check:
        face_model.release(); reid.release(); detector.release()
        emit("face_reid_fitness_check", ok=True)
        return 0
    known = load_registered_faces(config["face"]["database"])
    capture = open_source(source, config)
    ensure_mediapipe_protobuf_compat()
    face_detector = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.40)
    target_name = None
    gallery: list[np.ndarray] = []
    required_samples = int(config["face"]["stable_identity_samples"])
    acquire_deadline = time.monotonic() + float(config["face"]["acquire_timeout_seconds"])
    emit("face_acquire_started", requested_name=args.name, registered_names=[item["name"] for item in known])
    acquire_error = None
    try:
        while time.monotonic() < acquire_deadline and len(gallery) < required_samples:
            ok, frame = capture.read()
            if not ok or frame is None: continue
            people = valid_people(detector.detect(frame), config)
            matches = acquire_identity(frame, people, face_detector, face_model, known, config, args.name)
            names = {item["identity"]["name"] for item in matches}
            if len(names) != 1: continue
            match = max(matches, key=lambda item: item["identity"]["score"])
            name = match["identity"]["name"]
            if target_name is not None and name != target_name:
                gallery.clear()
            feature = reid.feature(crop_person(frame, match["person"].bbox))
            if gallery and float(np.dot(normalized(np.mean(gallery, axis=0)), feature)) < float(config["reid"]["reject_threshold"]):
                continue
            target_name = name
            gallery.append(feature)
            emit("face_acquire_sample", name=name, face_score=round(match["identity"]["score"], 4), samples=len(gallery), required=required_samples)
        if len(gallery) < required_samples or target_name is None:
            raise ReIDTestError("no registered face was stably associated with one person")
        gallery_matrix = np.stack(gallery)
        centroid = normalized(np.mean(gallery_matrix, axis=0))
        emit("target_locked", name=target_name, reid_samples=len(gallery))
    except Exception as exc:
        acquire_error = exc
    finally:
        face_detector.close()
        face_model.release()
    if acquire_error is not None:
        capture.release()
        reid.release()
        detector.release()
        emit("error", ok=False, error=f"{type(acquire_error).__name__}: {acquire_error}")
        return 1

    counter = ExerciseCounter(args.exercise, config)
    runtime = Path(config["paths"]["runtime_dir"]); runtime.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else runtime / f"face_reid_{safe_name(target_name)}_{args.exercise}_{int(time.time())}.mp4"
    events_path = output.with_suffix(".jsonl")
    writer = None
    deadline = time.monotonic() + args.seconds
    accept = float(config["reid"]["accept_threshold"])
    margin_required = float(config["reid"]["minimum_candidate_margin"])
    stable_needed = int(config["reid"]["stable_accept_frames"])
    top_k = int(config["reid"]["gallery_top_k"])
    stable = 0
    was_counting = False
    frame_index = 0
    try:
        with events_path.open("w", encoding="utf-8") as events:
            while time.monotonic() < deadline:
                ok, frame = capture.read()
                if not ok or frame is None: continue
                frame_index += 1
                people = valid_people(detector.detect(frame), config)
                candidates = []
                for person in people:
                    feature = reid.feature(crop_person(frame, person.bbox))
                    candidates.append((identity_score(feature, centroid, gallery_matrix, top_k), person, feature))
                candidates.sort(key=lambda item: item[0], reverse=True)
                best = candidates[0] if candidates else None
                second_score = candidates[1][0] if len(candidates) > 1 else -1.0
                margin = best[0] - second_score if best and len(candidates) > 1 else 1.0
                raw_accept = best is not None and best[0] >= accept and margin >= margin_required
                stable = stable + 1 if raw_accept else 0
                counting = raw_accept and stable >= stable_needed
                if was_counting and not counting: counter.reset_phase()
                pose_result = {"valid": False, "count": counter.count, "phase": counter.phase}
                if counting:
                    pose_result = counter.process(crop_person(frame, best[1].bbox, padding=0.10))
                    if pose_result.get("incremented"):
                        emit("exercise_count", name=target_name, exercise=args.exercise, count=counter.count)
                    if frame_index % 12 == 0 and best[0] >= accept + 0.08 and margin >= margin_required + 0.03:
                        gallery_matrix = np.vstack([gallery_matrix[-15:], best[2][None, :]])
                        centroid = normalized(np.mean(gallery_matrix, axis=0))
                state = "counting" if counting else "stabilizing" if raw_accept else "target_uncertain"
                if best is not None:
                    x1, y1, x2, y2 = best[1].bbox
                    color = (0, 200, 0) if counting else (0, 220, 255)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, f"{target_name} {best[0]:.3f}", (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.putText(frame, f"{args.exercise} count={counter.count} {state}", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                payload = {"timestamp": time.time(), "state": state, "identity": target_name, "people": len(people), "best_score": round(best[0], 4) if best else None, "margin": round(margin, 4), "pose": pose_result}
                events.write(json.dumps(payload, ensure_ascii=False) + "\n")
                if writer is None:
                    writer = make_writer(output, frame, capture.get(cv2.CAP_PROP_FPS) or float(config["camera"]["fps"]))
                writer.write(frame)
                was_counting = counting
        emit("face_reid_fitness_complete", ok=True, identity=target_name, exercise=args.exercise, count=counter.count, output=str(output), events=str(events_path))
        return 0
    except Exception as exc:
        emit("error", ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if writer is not None: writer.release()
        counter.release(); capture.release(); reid.release(); detector.release()


if __name__ == "__main__":
    raise SystemExit(main())
