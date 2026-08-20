#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import queue
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from rknnlite.api import RKNNLite

from pipeline import (
    Detection,
    PersonDetector,
    ReIDTestError,
    RknnReID,
    crop_person,
    emit,
    identity_score,
    load_config,
    normalized,
    open_source,
    safe_name,
    valid_people,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
_STOP_REQUESTED = False
_ACTIVE_SKILL = "push_up"
_RESIDENT_DETECTOR_OVERRIDE = None
_RESIDENT_REID_OVERRIDE = None
_RESIDENT_FACE_OVERRIDE = None
_RESIDENT_CAPTURE_FACTORY = None
_RESIDENT_EVENT_CALLBACK = None


def _request_stop(_signum: int | None = None, _frame: Any = None) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _skill_event(kind: str, text: str = "", **payload: Any) -> None:
    data = {
        "event": "skill_ready" if kind == "ready" else "skill_event",
        "skill_name": _ACTIVE_SKILL,
        "kind": kind,
        "text": text,
        "emitted_monotonic": round(time.monotonic(), 6),
        **payload,
    }
    print(json.dumps(data, ensure_ascii=False), flush=True)
    callback = _RESIDENT_EVENT_CALLBACK
    if callable(callback):
        callback(dict(data))


def _write_state(path: str | None, **payload: Any) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"skill": _ACTIVE_SKILL, "updated_at": time.time(), **payload}
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def _wait_start_gate(path: str | None) -> bool:
    if not path:
        return not _STOP_REQUESTED
    gate = Path(path)
    while not _STOP_REQUESTED:
        if gate.exists():
            return True
        time.sleep(0.02)
    return False


def _spoken_count(value: int) -> str:
    digits = "零一二三四五六七八九"
    value = max(0, int(value))

    def under_ten_thousand(number: int) -> str:
        if number == 0:
            return digits[0]
        output = ""
        pending_zero = False
        for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
            digit, number = divmod(number, divisor)
            if digit:
                if pending_zero and output:
                    output += "零"
                if not (divisor == 10 and digit == 1 and not output):
                    output += digits[digit]
                output += unit
                pending_zero = False
            elif output and number and divisor > 1:
                pending_zero = True
        return output

    if value < 10_000:
        return under_ten_thousand(value)
    high, low = divmod(value, 10_000)
    spoken = under_ten_thousand(high) + "万"
    if low:
        if low < 1000:
            spoken += "零"
        spoken += under_ten_thousand(low)
    return spoken


def _spoken_repetition_count(value: int) -> str:
    value = max(0, int(value))
    if value == 2:
        return "两个"
    return f"{_spoken_count(value)}个"


def _spoken_live_count(value: int) -> str:
    """Use phonetic context around short numerals during live counting."""
    value = max(0, int(value))
    if value == 0:
        return "零个"
    return f"第{_spoken_count(value)}个"


class FitnessFramingEventPolicy:
    """Debounce incomplete-body hints so they never flood live count speech."""

    def __init__(self, *, hold_seconds: float = 1.2, repeat_seconds: float = 10.0):
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.repeat_seconds = max(1.0, float(repeat_seconds))
        self.issue_started_at: float | None = None
        self.last_emitted_at = float("-inf")
        self.episode = 0

    def observe(self, pose_result: dict[str, Any], person_bbox: tuple[float, float, float, float] | None, frame_size: tuple[int, int], now: float) -> dict[str, Any] | None:
        visible = int(pose_result.get("visible_points", 0))
        required = max(1, int(pose_result.get("required_points", 8)))
        ratio = visible / required
        if bool(pose_result.get("valid")) and ratio >= 0.70:
            self.issue_started_at = None
            return None
        if self.issue_started_at is None:
            self.issue_started_at = now
            self.episode += 1
            return None
        if now - self.issue_started_at < self.hold_seconds or now - self.last_emitted_at < self.repeat_seconds:
            return None
        self.last_emitted_at = now
        reason = str(pose_result.get("reason") or "incomplete_pose")
        direction, text = "enter_and_center", "我暂时没看全你的动作，请站到画面中间，让全身都出现在镜头里。"
        if person_bbox is not None:
            left, top, right, bottom = person_bbox
            width, height = frame_size
            if left <= width * 0.02:
                direction, text = "move_image_right", "你的身体有一部分出了画面左侧，往右挪一点我就能继续数了。"
            elif right >= width * 0.98:
                direction, text = "move_image_left", "你的身体有一部分出了画面右侧，往左挪一点我就能继续数了。"
            # A side-view push-up naturally touches the lower image edge.  The
            # bottom edge alone is therefore not evidence that the person is
            # too close; treating it as such produced misleading guidance in
            # otherwise usable workout frames.  The top edge still indicates
            # that the upper body is likely cropped.
            elif top <= height * 0.02:
                direction, text = "move_farther", "目前全身还没有完整入镜，稍微离摄像头远一点。"
            elif reason == "no_pose":
                # The person detector and identity tracker can still have a
                # complete, centred target while a pose backend briefly loses
                # its landmarks.  Calling that an out-of-frame condition made
                # users move away from an already correct position and did not
                # help counting recover.
                direction, text = "hold_pose", "我已经看到你了，请保持当前动作一小会儿，我正在重新确认姿态。"
            elif ratio >= 0.40:
                direction, text = "adjust_position", "还有几个关键关节点没拍全，请前后左右挪一点，保持全身在画面里。"
        return {"text": text, "guidance": direction, "visible_points": visible, "required_points": required, "visible_ratio": round(ratio, 3), "reason": reason, "episode": self.episode}


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
    def __init__(self, model_path: str, core: int):
        path = Path(model_path)
        if not path.is_file():
            raise ReIDTestError(f"face model missing: {path}")
        self.rknn = RKNNLite(verbose=False)
        ret = self.rknn.load_rknn(str(path))
        if ret != 0:
            raise ReIDTestError(f"face model load failed: {ret}")
        masks = {1: RKNNLite.NPU_CORE_0, 2: RKNNLite.NPU_CORE_1, 3: RKNNLite.NPU_CORE_2}
        ret = self.rknn.init_runtime(core_mask=masks.get(int(core), RKNNLite.NPU_CORE_1))
        if ret != 0:
            raise ReIDTestError(f"face model runtime init failed: {ret}")

    def feature(self, face: np.ndarray) -> np.ndarray:
        if face is None or face.size == 0:
            raise ReIDTestError("empty face crop")
        resized = cv2.resize(face, (160, 160), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = ((rgb.astype(np.float32) - 127.5) / 128.0)[None, :, :, :]
        outputs = self.rknn.inference(inputs=[tensor.astype(np.float32)])
        if not outputs:
            raise ReIDTestError("face model returned no output")
        return normalized(outputs[0])

    def release(self) -> None:
        self.rknn.release()


def build_person_detector(config: dict[str, Any]) -> PersonDetector:
    if _RESIDENT_DETECTOR_OVERRIDE is not None:
        return _RESIDENT_DETECTOR_OVERRIDE
    models = config["models"]
    detector_cfg = config["detector"]
    return PersonDetector(
        models["person_detector"],
        models["person_detector_weight"],
        detector_cfg["confidence"],
        detector_cfg["npu_core_mask"],
        detector_cfg.get("device_id", "0002:21:00.0"),
    )


def build_detector_reid(config: dict[str, Any]) -> tuple[PersonDetector, RknnReID]:
    if _RESIDENT_DETECTOR_OVERRIDE is not None and _RESIDENT_REID_OVERRIDE is not None:
        return _RESIDENT_DETECTOR_OVERRIDE, _RESIDENT_REID_OVERRIDE
    detector = build_person_detector(config)
    try:
        reid = RknnReID(config["models"]["person_reid"], config["reid"]["npu_core"])
    except Exception:
        detector.release()
        raise
    return detector, reid


def load_registered_faces(config: dict[str, Any]) -> list[dict[str, Any]]:
    database = Path(config["face"]["database"])
    if not database.is_file():
        raise ReIDTestError(f"face database missing: {database}")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT name, vector, person_id FROM face_vectors WHERE name != ? ORDER BY name",
            ("__pending_face__",),
        ).fetchall()
    registered: list[dict[str, Any]] = []
    for name, blob, person_id in rows:
        try:
            registered.append(
                {
                    "name": str(name),
                    "person_id": str(person_id or name),
                    "vector": normalized(pickle.loads(blob)),
                }
            )
        except Exception as exc:
            emit("face_database_row_skipped", name=str(name), error=str(exc))
    if not registered:
        raise ReIDTestError(f"face database has no registered identities: {database}")
    return registered


def recognize_registered_face(
    feature: np.ndarray,
    registered: list[dict[str, Any]],
    threshold: float,
    required_margin: float,
) -> dict[str, Any] | None:
    scored = sorted(
        ((float(np.dot(feature, item["vector"])), item) for item in registered),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored:
        return None
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    margin = best_score - second_score if len(scored) > 1 else 1.0
    if best_score < threshold or margin < required_margin:
        return None
    return {
        "name": best["name"],
        "person_id": best["person_id"],
        "score": best_score,
        "second_score": second_score,
        "margin": margin,
    }


def face_crop(frame: np.ndarray, relative_box: Any) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    height, width = frame.shape[:2]
    x = int(relative_box.xmin * width)
    y = int(relative_box.ymin * height)
    w = int(relative_box.width * width)
    h = int(relative_box.height * height)
    pad_x, pad_y = int(w * 0.18), int(h * 0.18)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(width, x + w + pad_x), min(height, y + h + pad_y)
    return frame[y1:y2, x1:x2], (x1, y1, x2, y2)


def seek(capture: cv2.VideoCapture, seconds: float) -> None:
    if seconds > 0:
        capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)


def is_live_source(source: str) -> bool:
    value = str(source or "")
    return value.isdigit() or value.startswith("/dev/video")


def source_finished(source: str, capture: cv2.VideoCapture, end: float | None) -> bool:
    if end is not None and capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 >= end:
        return True
    return False


def command_check(config: dict[str, Any]) -> int:
    ensure_mediapipe_protobuf_compat()
    started = time.perf_counter()
    registered = load_registered_faces(config)
    detector, reid = build_detector_reid(config)
    face = FaceEmbeddingModel(config["models"]["face_embedding"], config["face"]["npu_core"])
    pose = mp.solutions.pose.Pose(static_image_mode=True, model_complexity=0)
    try:
        face_feature = face.feature(np.zeros((160, 160, 3), dtype=np.uint8))
        reid_feature = reid.feature(np.zeros((256, 128, 3), dtype=np.uint8))
        emit(
            "check_complete",
            ok=True,
            initialization_seconds=round(time.perf_counter() - started, 3),
            face_dimension=int(face_feature.size),
            reid_dimension=int(reid_feature.size),
            face_database=config["face"]["database"],
            registered_names=[item["name"] for item in registered],
            hardware_control=False,
        )
        return 0
    finally:
        pose.close()
        face.release()
        reid.release()
        detector.release()


def bbox_spatial_similarity(
    previous: tuple[int, int, int, int] | None,
    current: tuple[int, int, int, int],
    frame_width: int,
    maximum_shift: float,
) -> float:
    if previous is None:
        return 0.0
    pcx = (previous[0] + previous[2]) * 0.5
    pcy = (previous[1] + previous[3]) * 0.5
    ccx = (current[0] + current[2]) * 0.5
    ccy = (current[1] + current[3]) * 0.5
    distance = math.hypot(ccx - pcx, ccy - pcy) / max(1.0, float(frame_width))
    return float(np.clip(1.0 - distance / max(1e-6, maximum_shift), 0.0, 1.0))


def predict_bbox(
    previous: tuple[int, int, int, int] | None,
    older: tuple[int, int, int, int] | None,
    frame: np.ndarray,
    velocity_scale: float,
) -> tuple[int, int, int, int] | None:
    """Predict one frame ahead without allowing an implausible jump.

    Appearance alone is unreliable while two people cross.  A short constant-
    velocity prediction gives the association step a stable physical prior while
    keeping the registered face/body gallery immutable.
    """
    if previous is None or older is None:
        return previous
    height, width = frame.shape[:2]
    pcx = (previous[0] + previous[2]) * 0.5
    pcy = (previous[1] + previous[3]) * 0.5
    ocx = (older[0] + older[2]) * 0.5
    ocy = (older[1] + older[3]) * 0.5
    box_width = max(2.0, float(previous[2] - previous[0]))
    box_height = max(2.0, float(previous[3] - previous[1]))
    max_step = max(8.0, 0.35 * box_width)
    dx = float(np.clip((pcx - ocx) * velocity_scale, -max_step, max_step))
    dy = float(np.clip((pcy - ocy) * velocity_scale, -max_step, max_step))
    cx = float(np.clip(pcx + dx, box_width * 0.5, width - box_width * 0.5))
    cy = float(np.clip(pcy + dy, box_height * 0.5, height - box_height * 0.5))
    return (
        int(round(cx - box_width * 0.5)),
        int(round(cy - box_height * 0.5)),
        int(round(cx + box_width * 0.5)),
        int(round(cy + box_height * 0.5)),
    )


def diverse_append(gallery: list[np.ndarray], feature: np.ndarray, limit: int, minimum_distance: float) -> bool:
    if gallery and max(float(np.dot(feature, item)) for item in gallery) > 1.0 - minimum_distance:
        return False
    gallery.append(feature.copy())
    if len(gallery) > limit:
        del gallery[0 : len(gallery) - limit]
    return True


def recent_append(gallery: list[np.ndarray], feature: np.ndarray, limit: int) -> None:
    """Append every trusted observation to a bounded short-term tracklet."""
    gallery.append(feature.copy())
    if len(gallery) > limit:
        del gallery[0 : len(gallery) - limit]


def bbox_overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> tuple[float, float]:
    x1, y1 = max(first[0], second[0]), max(first[1], second[1])
    x2, y2 = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    first_area = max(1, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1, (second[2] - second[0]) * (second[3] - second[1]))
    union = first_area + second_area - intersection
    return intersection / max(1, union), intersection / max(1, min(first_area, second_area))


def deduplicate_people(people: list[Detection], config: dict[str, Any]) -> list[Detection]:
    """Remove detector duplicates without merging two genuinely separate people."""
    reid_cfg = config["reid"]
    kept: list[Detection] = []
    for person in sorted(people, key=lambda item: (item.confidence, item.area), reverse=True):
        duplicate = False
        for existing in kept:
            iou, containment = bbox_overlap(person.bbox, existing.bbox)
            if iou >= float(reid_cfg["duplicate_iou_threshold"]) or containment >= float(
                reid_cfg["duplicate_containment_threshold"]
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(person)
    return kept


def bbox_edge_contacts(bbox: tuple[int, int, int, int], frame: np.ndarray, tolerance: int = 2) -> int:
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    return sum((x1 <= tolerance, y1 <= tolerance, x2 >= width - tolerance, y2 >= height - tolerance))


@dataclass
class Candidate:
    person: Detection
    feature: np.ndarray
    anchor_score: float
    rolling_anchor_score: float
    adaptive_score: float
    tracklet_score: float
    prone_score: float
    wide_body: bool
    identity_score: float
    spatial_score: float
    association_score: float


class AnonymousTargetTracker:
    """Track one visible exercise subject without face or ReID inference.

    This mode is intentionally available only after an explicit user opt-out.
    It uses detector geometry and short-term box continuity, so it never loads
    or calls either identity model.  In a multi-person frame the initial target
    is the large, central person; later frames prefer physical continuity.
    """

    def __init__(self) -> None:
        self.previous_bbox: tuple[int, int, int, int] | None = None
        self.reacquisitions = 0
        self.switch_rejections = 0
        self.tracklet_gallery: list[np.ndarray] = []
        self.tracklet_updates = 0
        self.adaptive_gallery: list[np.ndarray] = []
        self.adaptive_updates = 0
        self.rolling_anchor_updates = 0

    @staticmethod
    def _candidate(person: Detection, score: float) -> Candidate:
        return Candidate(
            person=person,
            feature=np.empty((0,), dtype=np.float32),
            anchor_score=0.0,
            rolling_anchor_score=0.0,
            adaptive_score=0.0,
            tracklet_score=0.0,
            prone_score=0.0,
            wide_body=False,
            identity_score=0.0,
            spatial_score=score,
            association_score=score,
        )

    def update(
        self,
        frame: np.ndarray,
        people: list[Detection],
        _reid: RknnReID | None = None,
    ) -> tuple[Candidate | None, list[Candidate], dict[str, Any]]:
        if not people:
            return None, [], {
                "state": "missing",
                "policy": "anonymous",
                "face_recognition": False,
                "reid": False,
            }
        height, width = frame.shape[:2]
        candidates: list[Candidate] = []
        for person in people:
            if self.previous_bbox is not None:
                score = bbox_spatial_similarity(
                    self.previous_bbox, person.bbox, width, 1.0
                )
            else:
                x1, y1, x2, y2 = person.bbox
                center_x = (x1 + x2) / 2.0
                center_y = (y1 + y2) / 2.0
                center_distance = math.hypot(
                    (center_x - width / 2.0) / max(1.0, width),
                    (center_y - height / 2.0) / max(1.0, height),
                )
                area_ratio = person.area / max(1.0, float(width * height))
                score = max(0.0, 1.0 - center_distance) + min(0.5, area_ratio)
            candidates.append(self._candidate(person, float(score)))
        selected = max(candidates, key=lambda item: item.association_score)
        if self.previous_bbox is None and self.reacquisitions:
            self.reacquisitions += 1
        self.previous_bbox = selected.person.bbox
        return selected, candidates, {
            "state": "anonymous_locked",
            "policy": "anonymous",
            "face_recognition": False,
            "reid": False,
            "people": len(people),
        }


def locked_continuity_candidates(
    candidates: list[Candidate],
    config: dict[str, Any],
) -> tuple[list[Candidate], bool, bool, float]:
    """Narrow a locked multi-person track to the physically continuous body.

    Appearance can change sharply while the target bends.  A permissive
    minimum spatial gate alone lets a bystander compete and steal the rolling
    identity gallery.  When one candidate has a clearly reliable predicted
    position, discard candidates that are materially farther from that motion
    path before comparing appearance.  If prediction itself is weak, retain
    the old association behavior so crossings and reacquisition still work.
    """
    spatial_gate = float(config.get("locked_spatial_gate", 0.42))
    continuous = [item for item in candidates if item.spatial_score >= spatial_gate]
    if not continuous:
        return candidates, False, False, -1.0
    max_spatial = max(item.spatial_score for item in continuous)
    focus_active = False
    if (
        len(continuous) > 1
        and max_spatial >= float(config.get("locked_spatial_focus_min_score", 0.55))
    ):
        floor = max_spatial - float(config.get("locked_spatial_focus_slack", 0.18))
        focused = [item for item in continuous if item.spatial_score >= floor]
        if focused and len(focused) < len(continuous):
            continuous = focused
            focus_active = True
    return (
        sorted(continuous, key=lambda item: item.association_score, reverse=True),
        True,
        focus_active,
        max_spatial,
    )


class ContinuousIdentityTracker:
    def __init__(self, profile: dict[str, Any], config: dict[str, Any]):
        self.cfg = config["reid"]
        self.anchor_centroid = profile["reid_centroid"]
        self.anchor_gallery = profile["reid_gallery"]
        # Keep the registered identity anchor immutable, but maintain a second
        # short-term anchor that can follow trusted posture changes (standing ->
        # bending -> prone).  The immutable anchor remains the recovery/safety
        # reference and prevents the rolling anchor from silently changing ID.
        self.rolling_anchor_centroid = self.anchor_centroid.copy()
        self.adaptive_gallery: list[np.ndarray] = []
        self.tracklet_gallery: list[np.ndarray] = [item.copy() for item in self.anchor_gallery]
        # A dedicated posture gallery avoids comparing a prone person only
        # against the face-confirmed upright body samples.  It is populated
        # exclusively from an already locked, spatially continuous track.
        self.prone_gallery: list[np.ndarray] = []
        self.locked = False
        self.stable_frames = 0
        self.lost_frames = 0
        self.previous_bbox: tuple[int, int, int, int] | None = None
        self.older_bbox: tuple[int, int, int, int] | None = None
        self.frame_index = 0
        self.switch_rejections = 0
        self.reacquisitions = 0
        self.tracklet_updates = 0
        self.adaptive_updates = 0
        self.rolling_anchor_updates = 0
        self.prone_updates = 0
        self.prone_seed_streak = 0
        self.selected_streak = 0

        # Face acquisition has already authenticated both the identity and the
        # corresponding person box.  Preserve that physical track when the
        # counting tracker takes over; otherwise the first standing-to-prone
        # transition is treated as a brand-new, untrusted identity and the
        # posture bridge can never become active.
        authenticated_bbox = profile.get("authenticated_bbox")
        if authenticated_bbox is not None:
            self.bootstrap_authenticated_target(authenticated_bbox)

    def bootstrap_authenticated_target(self, bbox: Any) -> None:
        """Seed continuity from the last face-authenticated person box.

        This does not relax the global ReID threshold.  It only marks the
        already face-authenticated physical track as locked, so the existing
        spatial, ambiguity and multi-person guards remain authoritative.
        """
        values = tuple(int(value) for value in bbox)
        if len(values) != 4:
            raise ValueError(f"authenticated_bbox must contain four values: {bbox!r}")
        self.locked = True
        self.stable_frames = max(1, int(self.cfg.get("stable_accept_frames", 2)))
        self.lost_frames = 0
        self.previous_bbox = values
        self.older_bbox = values
        self.selected_streak = 1

    def _reset_tracklet(self) -> None:
        self.tracklet_gallery = [item.copy() for item in self.anchor_gallery]
        self.rolling_anchor_centroid = self.anchor_centroid.copy()
        # Once the physical track is truly lost, posture-adaptive samples are
        # no longer safe identity evidence.  Reacquisition must start from the
        # immutable face-confirmed body anchor instead of a possibly polluted
        # short-term gallery.
        self.adaptive_gallery.clear()
        self.prone_gallery.clear()
        self.prone_seed_streak = 0

    def _expire_lost_lock_if_allowed(self) -> None:
        if self.lost_frames <= int(self.cfg["lost_grace_frames"]):
            return
        # A registered-face session must not silently degrade into ordinary
        # body ReID after a temporary detector miss.  Preserve the last trusted
        # trajectory and its unpolluted posture galleries so the same physical
        # person can reconnect when they return; unrelated people remain lost.
        if bool(self.cfg.get("preserve_authenticated_lock_on_loss", True)):
            return
        self.locked = False
        self.previous_bbox = None
        self.older_bbox = None
        self._reset_tracklet()

    def update(self, frame: np.ndarray, people: list[Detection], reid: RknnReID) -> tuple[Candidate | None, list[Candidate], dict[str, Any]]:
        self.frame_index += 1
        top_k = int(self.cfg["gallery_top_k"])
        adaptive_matrix = np.stack(self.adaptive_gallery) if self.adaptive_gallery else None
        tracklet_matrix = np.stack(self.tracklet_gallery)
        prone_matrix = np.stack(self.prone_gallery) if self.prone_gallery else None
        predicted_bbox = predict_bbox(
            self.previous_bbox,
            self.older_bbox,
            frame,
            float(self.cfg.get("motion_prediction_scale", 0.80)),
        )
        candidates: list[Candidate] = []
        for person in people:
            feature = reid.feature(crop_person(frame, person.bbox, padding=0.08))
            anchor_score = identity_score(feature, self.anchor_centroid, self.anchor_gallery, top_k)
            rolling_anchor_score = float(np.dot(self.rolling_anchor_centroid, feature))
            adaptive_score = float(np.max(adaptive_matrix @ feature)) if adaptive_matrix is not None else -1.0
            tracklet_score = float(np.max(tracklet_matrix @ feature))
            box_width = max(1, person.bbox[2] - person.bbox[0])
            box_height = max(1, person.bbox[3] - person.bbox[1])
            wide_body = box_width / box_height >= float(self.cfg.get("prone_gallery_min_bbox_aspect", 1.10))
            prone_score = (
                float(np.mean(np.sort(prone_matrix @ feature)[-min(3, len(prone_matrix)):]))
                if prone_matrix is not None and wide_body
                else -1.0
            )
            combined = max(
                anchor_score,
                rolling_anchor_score - float(self.cfg.get("rolling_anchor_score_penalty", 0.01)),
                adaptive_score - float(self.cfg["adaptive_score_penalty"]),
                prone_score - float(self.cfg.get("prone_gallery_score_penalty", 0.0)),
            )
            spatial = bbox_spatial_similarity(
                predicted_bbox,
                person.bbox,
                frame.shape[1],
                float(self.cfg["maximum_center_shift"]),
            )
            if self.locked:
                appearance = max(combined, tracklet_score - float(self.cfg["tracklet_score_penalty"]))
                association = appearance + float(self.cfg["spatial_weight"]) * spatial
            else:
                # A fresh/recovered lock is identity authentication, not
                # short-term tracking.  Dynamic galleries are deliberately not
                # allowed to choose a person while unlocked.
                association = anchor_score
            candidates.append(
                Candidate(
                    person,
                    feature,
                    anchor_score,
                    rolling_anchor_score,
                    adaptive_score,
                    tracklet_score,
                    prone_score,
                    wide_body,
                    combined,
                    spatial,
                    association,
                )
            )
        candidates.sort(key=lambda item: item.association_score, reverse=True)
        comparison_candidates = candidates
        anchor_ranked = sorted(candidates, key=lambda item: item.anchor_score, reverse=True)
        anchor_margin = (
            anchor_ranked[0].anchor_score
            - (anchor_ranked[1].anchor_score if len(anchor_ranked) > 1 else -1.0)
            if anchor_ranked
            else -1.0
        )
        motion_gate_active = False
        spatial_focus_active = False
        locked_max_spatial = -1.0
        identity_override_active = False
        prone_guard_active = False
        if self.locked and candidates:
            (
                comparison_candidates,
                motion_gate_active,
                spatial_focus_active,
                locked_max_spatial,
            ) = locked_continuity_candidates(candidates, self.cfg)
            prone_ranked = sorted(
                (
                    item
                    for item in candidates
                    if item.wide_body
                    and item.prone_score >= 0.0
                    and item.spatial_score >= float(self.cfg.get("prone_guard_min_spatial_score", 0.62))
                ),
                key=lambda item: item.prone_score,
                reverse=True,
            )
            prone_margin = prone_ranked[0].prone_score - (
                prone_ranked[1].prone_score if len(prone_ranked) > 1 else -1.0
            ) if prone_ranked else -1.0
            prone_guard_active = bool(
                len(self.prone_gallery) >= int(self.cfg.get("prone_gallery_min_ready_samples", 5))
                and prone_ranked
                and prone_ranked[0].prone_score >= float(self.cfg.get("prone_guard_min_score", 0.70))
                and prone_margin >= float(self.cfg.get("prone_guard_min_margin", 0.08))
            )
            if (
                not prone_guard_active
                and anchor_ranked[0].anchor_score
                >= float(self.cfg.get("locked_reacquire_min_anchor_score", 0.72))
                and anchor_margin
                >= float(self.cfg.get("locked_reacquire_min_anchor_margin", 0.15))
            ):
                # A decisive immutable-anchor match is stronger evidence than
                # motion after a crossing.  It safely reattaches the original
                # person even if another person inherited the predicted path.
                comparison_candidates = [anchor_ranked[0]] + [
                    item for item in anchor_ranked[1:]
                ]
                identity_override_active = True
        best = comparison_candidates[0] if comparison_candidates else None
        second_identity = max((item.identity_score for item in comparison_candidates[1:]), default=-1.0)
        identity_margin = best.identity_score - second_identity if best else -1.0
        second_association = comparison_candidates[1].association_score if len(comparison_candidates) > 1 else -1.0
        association_margin = best.association_score - second_association if best else -1.0
        candidate_overlap = 0.0
        if best is not None and len(candidates) > 1:
            candidate_overlap = max(
                (
                    bbox_overlap(best.person.bbox, item.person.bbox)[1]
                    for item in candidates
                    if item is not best
                ),
                default=0.0,
            )
        crossing_ambiguous = bool(
            self.locked
            and best is not None
            and len(candidates) > 1
            and candidate_overlap >= float(self.cfg.get("crossing_overlap_threshold", 0.18))
            and (
                association_margin < float(self.cfg.get("crossing_min_association_margin", 0.08))
                or identity_margin < float(self.cfg.get("crossing_min_identity_margin", 0.07))
            )
        )
        locked_accept_spatial_gate = float(
            self.cfg.get(
                "single_candidate_locked_spatial_gate"
                if len(candidates) == 1
                else "locked_spatial_gate",
                0.50 if len(candidates) == 1 else 0.42,
            )
        )
        identity_accept = bool(
            best
            and best.identity_score >= float(self.cfg["accept_threshold"])
            and best.anchor_score >= float(self.cfg["identity_min_anchor_score"])
            and identity_margin >= float(self.cfg["minimum_candidate_margin"])
            and (
                (
                    not self.locked
                    and best.anchor_score
                    >= float(self.cfg.get("session_reacquire_min_anchor_score", 0.72))
                    and anchor_margin
                    >= float(self.cfg.get("session_reacquire_min_anchor_margin", 0.15))
                )
                or (
                    self.locked
                    and (
                        best.spatial_score >= locked_accept_spatial_gate
                        or identity_override_active
                    )
                )
            )
        )
        required_tracklet_score = float(self.cfg["tracklet_accept_threshold"]) + min(
            self.lost_frames,
            int(self.cfg["tracklet_max_gap_frames"]),
        ) * float(self.cfg["tracklet_gap_score_increment"])
        tracklet_accept = bool(
            best
            and self.locked
            and self.lost_frames <= int(self.cfg["tracklet_max_gap_frames"])
            and best.tracklet_score >= required_tracklet_score
            and best.anchor_score >= float(self.cfg["tracklet_min_anchor_score"])
            and best.spatial_score >= max(
                float(self.cfg["tracklet_min_spatial_score"]),
                locked_accept_spatial_gate if len(candidates) == 1 else 0.0,
            )
            and association_margin >= float(self.cfg["tracklet_min_candidate_margin"])
            and best.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
        )
        # A posture change can make an upright-person ReID embedding collapse
        # before the tracklet has learned the prone appearance.  For an already
        # authenticated target, bridge that gap only when there is exactly one
        # candidate and its physical trajectory remains continuous.  This is a
        # continuity rule, not a global lowering of the identity threshold.
        single_posture_bridge_accept = bool(
            best
            and self.locked
            and len(candidates) == 1
            and self.lost_frames <= int(self.cfg.get("posture_bridge_max_gap_frames", 36))
            and best.anchor_score >= float(self.cfg.get("posture_bridge_min_anchor_score", 0.18))
            and best.spatial_score >= float(self.cfg.get("posture_bridge_min_spatial_score", 0.42))
            and best.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
            and not crossing_ambiguous
        )
        multi_posture_bridge_accept = bool(
            best
            and self.locked
            and len(candidates) > 1
            and self.lost_frames <= int(self.cfg.get("multi_posture_bridge_max_gap_frames", 18))
            and best.anchor_score >= float(self.cfg.get("multi_posture_bridge_min_anchor_score", 0.15))
            and max(best.rolling_anchor_score, best.tracklet_score)
            >= float(self.cfg.get("multi_posture_bridge_min_dynamic_score", 0.32))
            and best.spatial_score >= float(self.cfg.get("multi_posture_bridge_min_spatial_score", 0.62))
            and association_margin >= float(self.cfg.get("multi_posture_bridge_min_association_margin", 0.10))
            and identity_margin >= float(self.cfg.get("multi_posture_bridge_min_identity_margin", 0.035))
            and candidate_overlap < float(self.cfg.get("multi_posture_bridge_max_overlap", 0.16))
            and best.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
            and not crossing_ambiguous
        )
        posture_bridge_accept = single_posture_bridge_accept or multi_posture_bridge_accept
        selected: Candidate | None = None
        selection_mode: str | None = None
        was_locked = self.locked
        if crossing_ambiguous:
            self.lost_frames += 1
            self.stable_frames = 0
            self.selected_streak = 0
            self.switch_rejections += 1
            self._expire_lost_lock_if_allowed()
        elif identity_accept:
            self.stable_frames += 1
            self.lost_frames = 0
            if self.stable_frames >= int(self.cfg["stable_accept_frames"]):
                self.locked = True
                selected = best
                selection_mode = "identity"
                if not was_locked:
                    self.reacquisitions += 1
        elif tracklet_accept:
            self.lost_frames = 0
            self.stable_frames += 1
            selected = best
            selection_mode = "tracklet"
        elif posture_bridge_accept:
            self.lost_frames = 0
            self.stable_frames += 1
            selected = best
            selection_mode = "posture_bridge"
        else:
            self.lost_frames += 1
            self.stable_frames = 0
            self.selected_streak = 0
            if best is not None:
                self.switch_rejections += 1
            self._expire_lost_lock_if_allowed()
        if selected is not None:
            self.selected_streak += 1
            self.older_bbox = self.previous_bbox
            self.previous_bbox = selected.person.bbox
            strong_identity_update = bool(
                selection_mode == "identity"
                and selected.anchor_score >= float(self.cfg.get("tracklet_update_min_anchor_score", 0.36))
                and identity_margin >= float(self.cfg.get("tracklet_update_min_identity_margin", 0.06))
            )
            strong_continuity_update = bool(
                selection_mode == "tracklet"
                and self.selected_streak >= int(self.cfg.get("tracklet_update_stable_frames", 4))
                and selected.anchor_score >= float(self.cfg.get("tracklet_update_min_anchor_score", 0.36))
                and selected.tracklet_score >= float(self.cfg.get("tracklet_update_min_tracklet_score", 0.64))
                and selected.spatial_score >= float(self.cfg.get("tracklet_update_min_spatial_score", 0.58))
                and association_margin >= float(self.cfg.get("tracklet_update_min_candidate_margin", 0.06))
            )
            strong_posture_bridge_update = bool(
                selection_mode == "posture_bridge"
                and selected.anchor_score >= float(
                    self.cfg.get(
                        "posture_bridge_min_anchor_score" if len(candidates) == 1
                        else "multi_posture_bridge_min_anchor_score",
                        0.18 if len(candidates) == 1 else 0.15,
                    )
                )
                and selected.spatial_score >= float(self.cfg.get("posture_bridge_update_min_spatial_score", 0.30))
                and selected.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
                and (
                    len(candidates) == 1
                    or (
                        multi_posture_bridge_accept
                        and association_margin
                        >= float(self.cfg.get("multi_anchor_update_min_association_margin", 0.12))
                        and identity_margin >= float(self.cfg.get("multi_anchor_update_min_identity_margin", 0.05))
                        and candidate_overlap < float(self.cfg.get("multi_anchor_update_max_overlap", 0.12))
                    )
                )
            )
            trusted_multi_person_update = bool(
                len(candidates) > 1
                and not crossing_ambiguous
                and selection_mode in {"identity", "tracklet", "posture_bridge"}
                and selected.anchor_score >= float(self.cfg.get("multi_update_min_anchor_score", 0.15))
                and selected.spatial_score
                >= float(self.cfg.get("multi_update_min_spatial_score", 0.70))
                and max(selected.rolling_anchor_score, selected.tracklet_score)
                >= float(self.cfg.get("multi_posture_bridge_min_dynamic_score", 0.32))
                and association_margin
                >= float(self.cfg.get("multi_anchor_update_min_association_margin", 0.12))
                and identity_margin >= float(self.cfg.get("multi_anchor_update_min_identity_margin", 0.05))
                and candidate_overlap < float(self.cfg.get("multi_anchor_update_max_overlap", 0.12))
                and selected.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
            )
            safe_tracklet_update = bool(
                self.locked
                and not crossing_ambiguous
                and selected.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
                and (
                    strong_identity_update
                    or strong_continuity_update
                    or strong_posture_bridge_update
                    or trusted_multi_person_update
                )
                and self.frame_index % int(self.cfg["tracklet_update_interval_frames"]) == 0
            )
            if safe_tracklet_update:
                appended = diverse_append(
                    self.tracklet_gallery,
                    selected.feature,
                    int(self.cfg["tracklet_gallery_size"]),
                    float(self.cfg.get("tracklet_min_feature_distance", 0.015)),
                )
                if appended:
                    self.tracklet_updates += 1
            trusted_prone_observation = bool(
                self.locked
                and selected.wide_body
                and not crossing_ambiguous
                and selected.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
                and selected.spatial_score >= float(
                    self.cfg.get(
                        "prone_gallery_multi_min_spatial_score" if len(candidates) > 1 else "prone_gallery_min_spatial_score",
                        0.70 if len(candidates) > 1 else 0.30,
                    )
                )
                and (
                    len(candidates) == 1
                    or (
                        association_margin >= float(self.cfg.get("prone_gallery_min_association_margin", 0.10))
                        and candidate_overlap < float(self.cfg.get("prone_gallery_max_overlap", 0.16))
                        and (trusted_multi_person_update or strong_posture_bridge_update or strong_continuity_update)
                    )
                )
            )
            if trusted_prone_observation:
                self.prone_seed_streak += 1
            elif not selected.wide_body:
                self.prone_seed_streak = 0
            if (
                trusted_prone_observation
                and self.prone_seed_streak >= int(self.cfg.get("prone_gallery_seed_frames", 3))
                and self.frame_index % int(self.cfg.get("prone_gallery_update_interval_frames", 1)) == 0
            ):
                recent_append(
                    self.prone_gallery,
                    selected.feature,
                    int(self.cfg.get("prone_gallery_size", 24)),
                )
                self.prone_updates += 1
            safe_rolling_anchor_update = bool(
                self.locked
                and not crossing_ambiguous
                and selected.person.confidence >= float(self.cfg["tracklet_min_detection_confidence"])
                and (
                    len(candidates) == 1
                    or strong_posture_bridge_update
                    or trusted_multi_person_update
                    or selected.anchor_score >= float(self.cfg.get("rolling_anchor_multi_person_min_anchor_score", 0.55))
                )
                and selected.spatial_score >= float(self.cfg.get("rolling_anchor_min_spatial_score", 0.24))
                and self.frame_index % int(self.cfg.get("rolling_anchor_update_interval_frames", 1)) == 0
            )
            if safe_rolling_anchor_update:
                alpha = float(self.cfg.get("rolling_anchor_ema_alpha", 0.22))
                # A small immutable-anchor pull bounds long-term drift while the
                # EMA follows rapid, legitimate posture changes.
                anchor_pull = float(self.cfg.get("rolling_anchor_identity_pull", 0.04))
                self.rolling_anchor_centroid = normalized(
                    (1.0 - alpha - anchor_pull) * self.rolling_anchor_centroid
                    + alpha * selected.feature
                    + anchor_pull * self.anchor_centroid
                )
                self.rolling_anchor_updates += 1
            if (
                safe_tracklet_update
                and (
                    (
                        selected.anchor_score >= float(self.cfg["adaptive_update_min_anchor_score"])
                        and selected.tracklet_score >= float(self.cfg["adaptive_update_min_tracklet_score"])
                    )
                    or strong_posture_bridge_update
                    or trusted_multi_person_update
                )
                and association_margin >= float(self.cfg["adaptive_update_min_candidate_margin"])
                and self.selected_streak >= int(self.cfg["adaptive_promotion_stable_frames"])
                and bbox_edge_contacts(selected.person.bbox, frame)
                <= int(self.cfg["adaptive_max_edge_contacts"])
                and self.frame_index % int(self.cfg["adaptive_update_interval_frames"]) == 0
            ):
                appended = diverse_append(
                    self.adaptive_gallery,
                    selected.feature,
                    int(self.cfg["adaptive_gallery_size"]),
                    float(self.cfg["adaptive_min_feature_distance"]),
                )
                if appended:
                    self.adaptive_updates += 1
        if selected is not None:
            freeze_reason = None
        elif best is None:
            freeze_reason = "no_candidate"
        elif crossing_ambiguous:
            freeze_reason = "crossing_ambiguous"
        elif identity_accept:
            freeze_reason = "identity_stabilizing"
        elif self.locked and association_margin < float(self.cfg["tracklet_min_candidate_margin"]):
            freeze_reason = "candidate_ambiguous"
        elif self.locked and best.spatial_score < float(self.cfg["tracklet_min_spatial_score"]):
            freeze_reason = "spatial_discontinuity"
        elif self.locked and best.tracklet_score < required_tracklet_score:
            freeze_reason = "tracklet_below_threshold"
        else:
            freeze_reason = "identity_below_threshold"
        state = "locked" if selected is not None else "stabilizing" if identity_accept else "lost" if was_locked else "searching"
        diagnostics = {
            "state": state,
            "selection_mode": selection_mode,
            "freeze_reason": freeze_reason,
            "best_score": round(best.identity_score, 4) if best else None,
            "anchor_score": round(best.anchor_score, 4) if best else None,
            "rolling_anchor_score": round(best.rolling_anchor_score, 4) if best else None,
            "adaptive_score": round(best.adaptive_score, 4) if best and best.adaptive_score >= 0 else None,
            "tracklet_score": round(best.tracklet_score, 4) if best else None,
            "prone_score": round(best.prone_score, 4) if best and best.prone_score >= 0 else None,
            "margin": round(identity_margin, 4),
            "association_margin": round(association_margin, 4),
            "candidate_overlap": round(candidate_overlap, 4),
            "crossing_ambiguous": crossing_ambiguous,
            "motion_gate_active": motion_gate_active,
            "spatial_focus_active": spatial_focus_active,
            "locked_max_spatial": round(locked_max_spatial, 4) if locked_max_spatial >= 0 else None,
            "locked_accept_spatial_gate": locked_accept_spatial_gate,
            "identity_override_active": identity_override_active,
            "strict_session_reacquire": not was_locked,
            "authenticated_lock_preserved": bool(
                self.locked
                and self.lost_frames > int(self.cfg["lost_grace_frames"])
                and self.cfg.get("preserve_authenticated_lock_on_loss", True)
            ),
            "anchor_margin": round(anchor_margin, 4),
            "prone_guard_active": prone_guard_active,
            "identity_accept": identity_accept,
            "continuity_accept": tracklet_accept,
            "tracklet_accept": tracklet_accept,
            "posture_bridge_accept": posture_bridge_accept,
            "single_posture_bridge_accept": single_posture_bridge_accept,
            "multi_posture_bridge_accept": multi_posture_bridge_accept,
            "stable_frames": self.stable_frames,
            "lost_frames": self.lost_frames,
            "selected_streak": self.selected_streak,
            "tracklet_gallery_size": len(self.tracklet_gallery),
            "tracklet_updates": self.tracklet_updates,
            "adaptive_gallery_size": len(self.adaptive_gallery),
            "adaptive_updates": self.adaptive_updates,
            "rolling_anchor_updates": self.rolling_anchor_updates,
            "prone_gallery_size": len(self.prone_gallery),
            "prone_updates": self.prone_updates,
            "prone_seed_streak": self.prone_seed_streak,
            "selected_bbox": list(selected.person.bbox) if selected is not None else None,
            "candidates": [
                {
                    "bbox": list(item.person.bbox),
                    "anchor": round(item.anchor_score, 4),
                    "rolling": round(item.rolling_anchor_score, 4),
                    "adaptive": round(item.adaptive_score, 4),
                    "tracklet": round(item.tracklet_score, 4),
                    "prone": round(item.prone_score, 4),
                    "wide_body": item.wide_body,
                    "identity": round(item.identity_score, 4),
                    "spatial": round(item.spatial_score, 4),
                    "association": round(item.association_score, 4),
                }
                for item in candidates
            ],
        }
        return selected, candidates, diagnostics


def registered_face_person_matches(
    frame: np.ndarray,
    people: list[Detection],
    face_detector: Any,
    face_model: FaceEmbeddingModel,
    registered: list[dict[str, Any]],
    requested_name: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    face_cfg = config["face"]
    frame_height, frame_width = frame.shape[:2]
    matches: list[dict[str, Any]] = []
    for person in people:
        x1, y1, x2, y2 = person.bbox
        person_width = max(1, x2 - x1)
        person_height = max(1, y2 - y1)
        pad_x = int(person_width * 0.08)
        crop_x1 = max(0, x1 - pad_x)
        crop_x2 = min(frame_width, x2 + pad_x)
        crop_y1 = max(0, y1 - int(person_height * 0.04))
        crop_y2 = min(
            frame_height,
            y1 + int(person_height * float(face_cfg["person_upper_body_ratio"])),
        )
        upper_body = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        if upper_body.size == 0:
            continue
        scale = max(
            1.0,
            float(face_cfg["minimum_detector_crop_width"]) / max(1.0, float(upper_body.shape[1])),
        )
        scale = min(scale, float(face_cfg["maximum_detector_upscale"]))
        detector_image = (
            cv2.resize(upper_body, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            if scale > 1.0
            else upper_body
        )
        result = face_detector.process(cv2.cvtColor(detector_image, cv2.COLOR_BGR2RGB))
        for detection in result.detections or []:
            face_image, face_box = face_crop(detector_image, detection.location_data.relative_bounding_box)
            original_side = min(face_box[2] - face_box[0], face_box[3] - face_box[1]) / scale
            if face_image.size == 0 or original_side < float(face_cfg["minimum_face_side"]):
                continue
            focus = float(
                cv2.Laplacian(cv2.cvtColor(face_image, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            )
            if focus < float(face_cfg["minimum_focus"]):
                continue
            identity = recognize_registered_face(
                face_model.feature(face_image),
                registered,
                float(face_cfg["match_threshold"]),
                float(face_cfg["match_margin"]),
            )
            if identity is None or identity["name"] != requested_name:
                continue
            global_face_box = (
                crop_x1 + int(face_box[0] / scale),
                crop_y1 + int(face_box[1] / scale),
                crop_x1 + int(face_box[2] / scale),
                crop_y1 + int(face_box[3] / scale),
            )
            matches.append(
                {
                    "identity": identity,
                    "person": person,
                    "focus": focus,
                    "face_box": global_face_box,
                }
            )
    deduplicated: list[dict[str, Any]] = []
    for match in sorted(matches, key=lambda item: item["identity"]["score"], reverse=True):
        x1, y1, x2, y2 = match["face_box"]
        center = ((x1 + x2) * 0.5, (y1 + y2) * 0.5)
        side = max(1.0, float(max(x2 - x1, y2 - y1)))
        duplicate_index = None
        for index, existing in enumerate(deduplicated):
            ex1, ey1, ex2, ey2 = existing["face_box"]
            existing_center = ((ex1 + ex2) * 0.5, (ey1 + ey2) * 0.5)
            existing_side = max(1.0, float(max(ex2 - ex1, ey2 - ey1)))
            if math.hypot(center[0] - existing_center[0], center[1] - existing_center[1]) <= 0.35 * max(
                side,
                existing_side,
            ):
                duplicate_index = index
                break
        if duplicate_index is None:
            deduplicated.append(match)
        elif match["person"].area < deduplicated[duplicate_index]["person"].area:
            deduplicated[duplicate_index] = match
    return deduplicated


def acquire_database_identity_gallery(
    capture: cv2.VideoCapture,
    detector: PersonDetector,
    reid: RknnReID,
    requested_name: str,
    registered: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], np.ndarray, dict[str, Any], tuple[int, int, int, int]]:
    ensure_mediapipe_protobuf_compat()
    face_cfg = config["face"]
    registered_names = [item["name"] for item in registered]
    requested = next((item for item in registered if item["name"] == requested_name), None)
    if requested is None:
        raise ReIDTestError(
            f"requested identity is not registered: {requested_name}; registered={registered_names}"
        )
    face_model = _RESIDENT_FACE_OVERRIDE or FaceEmbeddingModel(
        config["models"]["face_embedding"], face_cfg["npu_core"]
    )
    face_detector = mp.solutions.face_detection.FaceDetection(
        model_selection=0,
        min_detection_confidence=float(face_cfg["detection_confidence"]),
    )
    required = int(face_cfg["runtime_body_samples"])
    deadline = time.monotonic() + float(face_cfg["runtime_acquire_timeout_seconds"])
    body_features: list[np.ndarray] = []
    last_authenticated_bbox: tuple[int, int, int, int] | None = None
    diagnostics = {
        "frames": 0,
        "person_detections": 0,
        "face_person_matches": 0,
        "accepted": 0,
        "ambiguous": 0,
    }
    emit(
        "database_face_acquire_started",
        name=requested_name,
        person_id=requested["person_id"],
        database=face_cfg["database"],
        required_body_samples=required,
        registered_names=registered_names,
    )
    try:
        while len(body_features) < required and time.monotonic() < deadline:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            diagnostics["frames"] += 1
            people = deduplicate_people(valid_people(detector.detect(frame), config), config)
            diagnostics["person_detections"] += len(people)
            if not people:
                continue
            matches = registered_face_person_matches(
                frame,
                people,
                face_detector,
                face_model,
                registered,
                requested_name,
                config,
            )
            diagnostics["face_person_matches"] += len(matches)
            matches.sort(key=lambda item: item["identity"]["score"], reverse=True)
            if not matches:
                continue
            best = matches[0]
            second_person_score = matches[1]["identity"]["score"] if len(matches) > 1 else -1.0
            person_margin = (
                best["identity"]["score"] - second_person_score if len(matches) > 1 else 1.0
            )
            if person_margin < float(face_cfg["person_match_margin"]):
                diagnostics["ambiguous"] += 1
                continue
            person = best["person"]
            body_feature = reid.feature(crop_person(frame, person.bbox, padding=0.08))
            if body_features and float(
                np.dot(normalized(np.mean(body_features, axis=0)), body_feature)
            ) < float(face_cfg["runtime_body_consistency_threshold"]):
                diagnostics["ambiguous"] += 1
                continue
            body_features.append(body_feature)
            last_authenticated_bbox = tuple(int(value) for value in person.bbox)
            diagnostics["accepted"] += 1
            emit(
                "database_face_acquire_sample",
                name=requested_name,
                person_id=requested["person_id"],
                current=len(body_features),
                required=required,
                face_score=round(best["identity"]["score"], 4),
                database_margin=round(best["identity"]["margin"], 4),
                person_margin=round(person_margin, 4),
            )
        if len(body_features) < required:
            raise ReIDTestError(
                f"registered-face ReID acquisition failed for {requested_name}: "
                f"{len(body_features)}/{required}; diagnostics={diagnostics}"
            )
        matrix = np.stack(body_features).astype(np.float32)
        if last_authenticated_bbox is None:
            raise ReIDTestError("face authentication completed without a target body box")
        diagnostics["last_authenticated_bbox"] = list(last_authenticated_bbox)
        identity = {"name": requested_name, "person_id": requested["person_id"]}
        emit(
            "database_face_target_locked",
            **identity,
            body_samples=len(matrix),
            diagnostics=diagnostics,
        )
        return identity, matrix, diagnostics, last_authenticated_bbox
    finally:
        face_detector.close()
        if _RESIDENT_FACE_OVERRIDE is None:
            face_model.release()


def joint_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    ba, bc = a - b, c - b
    denominator = float(np.linalg.norm(ba) * np.linalg.norm(bc))
    if denominator <= 1e-9:
        return 0.0
    cosine = float(np.clip(np.dot(ba, bc) / denominator, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


class PushupPhaseTracker:
    """Pure state machine fed by geometry-corrected pose observations."""

    def __init__(self, config: dict[str, Any]):
        self.cfg = config
        self.count = 0
        self.phase: str | None = None
        self.candidate_phase: str | None = None
        self.candidate_frames = 0
        self.filtered_angle: float | None = None
        self.frames_since_rep = 9999
        self.missing_frames = 0
        self.horizontal_frames = 0
        self.horizontal_ready = False
        self.phase_frames = 0

    def _reset_posture(self) -> None:
        self.phase = None
        self.candidate_phase = None
        self.candidate_frames = 0
        self.filtered_angle = None
        self.horizontal_frames = 0
        self.horizontal_ready = False
        self.phase_frames = 0

    def mark_missing(self) -> None:
        self.missing_frames += 1
        self.frames_since_rep += 1
        if self.missing_frames > int(self.cfg["lost_reset_frames"]):
            self._reset_posture()

    def process_observation(
        self,
        raw_angle: float,
        wrist_shoulder_ratio: float,
        torso_angle: float,
        crop_aspect: float,
    ) -> dict[str, Any]:
        self.frames_since_rep += 1
        self.missing_frames = 0
        horizontal = (
            crop_aspect >= float(self.cfg["horizontal_min_crop_aspect"])
            and torso_angle <= float(self.cfg["horizontal_max_torso_angle"])
        )
        if not horizontal:
            self._reset_posture()
            return {
                "count": self.count,
                "phase": self.phase,
                "candidate_phase": None,
                "filtered_angle": None,
                "horizontal": False,
                "horizontal_ready": False,
                "horizontal_frames": 0,
                "incremented": False,
            }

        self.horizontal_frames += 1
        self.horizontal_ready = self.horizontal_frames >= int(self.cfg["horizontal_ready_frames"])
        if self.phase is not None:
            self.phase_frames += 1
        alpha = float(self.cfg["angle_ema_alpha"])
        self.filtered_angle = (
            raw_angle
            if self.filtered_angle is None
            else alpha * raw_angle + (1.0 - alpha) * self.filtered_angle
        )
        phase_candidate = None
        if self.horizontal_ready:
            if (
                wrist_shoulder_ratio <= float(self.cfg["down_wrist_shoulder_ratio"])
                or self.filtered_angle <= float(self.cfg["down_angle"])
            ):
                phase_candidate = "down"
            elif (
                wrist_shoulder_ratio >= float(self.cfg["up_wrist_shoulder_ratio"])
                and self.filtered_angle >= float(self.cfg["up_angle"])
            ):
                phase_candidate = "up"
        if phase_candidate == self.candidate_phase:
            self.candidate_frames += 1
        else:
            self.candidate_phase = phase_candidate
            self.candidate_frames = 1 if phase_candidate else 0
        incremented = False
        required_phase_frames = int(self.cfg["minimum_phase_frames"])
        fast_up_confirmation = bool(
            self.cfg.get("fast_up_confirmation", False)
            and self.phase == "down"
            and phase_candidate == "up"
            and self.filtered_angle is not None
            and self.filtered_angle >= float(self.cfg.get("fast_up_min_angle", 150.0))
            and wrist_shoulder_ratio
            >= float(self.cfg.get("fast_up_min_wrist_shoulder_ratio", 0.77))
            and self.phase_frames >= int(self.cfg.get("fast_up_min_down_phase_frames", 5))
            and self.frames_since_rep >= int(self.cfg["minimum_rep_interval_frames"])
        )
        if fast_up_confirmation:
            required_phase_frames = 1
        if phase_candidate and self.candidate_frames >= required_phase_frames and phase_candidate != self.phase:
            if (
                self.phase == "down"
                and phase_candidate == "up"
                and self.phase_frames
                >= int(self.cfg.get("minimum_down_phase_frames_for_count", 3))
                and self.frames_since_rep >= int(self.cfg["minimum_rep_interval_frames"])
            ):
                self.count += 1
                self.frames_since_rep = 0
                incremented = True
            self.phase = phase_candidate
            self.phase_frames = 1
        return {
            "count": self.count,
            "phase": self.phase,
            "candidate_phase": phase_candidate,
            "filtered_angle": round(float(self.filtered_angle), 2),
            "horizontal": True,
            "horizontal_ready": self.horizontal_ready,
            "horizontal_frames": self.horizontal_frames,
            "required_phase_frames": required_phase_frames,
            "fast_up_confirmation": fast_up_confirmation,
            "phase_frames": self.phase_frames,
            "incremented": incremented,
        }


def _sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-value)
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


class RknnPoseBackend:
    """BlazePose landmark backend validated on the RK3588 push-up clip.

    The existing YOLO/ReID target crop replaces MediaPipe's outer detector and
    rotated ROI graph.  Wide, prone crops are rotated clockwise for the landmark
    network, then transformed back to the crop coordinate system.
    """

    INPUT_SIZE = 256

    def __init__(self, model: Path, presence_threshold: float = 0.5):
        if not model.is_file():
            raise ReIDTestError(f"pose RKNN model missing: {model}")
        self.presence_threshold = float(presence_threshold)
        self.rknn = RKNNLite(verbose=False)
        code = self.rknn.load_rknn(str(model))
        if code != 0:
            raise ReIDTestError(f"pose RKNN load failed: {code}")
        code = self.rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_1)
        if code != 0:
            self.rknn.release()
            raise ReIDTestError(f"pose RKNN runtime init failed: {code}")

    def process(self, rgb: np.ndarray) -> Any:
        height, width = rgb.shape[:2]
        rotate_clockwise = width / max(height, 1) >= 1.10
        working = cv2.rotate(rgb, cv2.ROTATE_90_CLOCKWISE) if rotate_clockwise else rgb
        working_height, working_width = working.shape[:2]
        scale = min(
            self.INPUT_SIZE / max(working_width, 1),
            self.INPUT_SIZE / max(working_height, 1),
        )
        resized_width = max(1, int(round(working_width * scale)))
        resized_height = max(1, int(round(working_height * scale)))
        resized = cv2.resize(
            working,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR,
        )
        canvas = np.zeros((self.INPUT_SIZE, self.INPUT_SIZE, 3), dtype=np.uint8)
        offset_x = (self.INPUT_SIZE - resized_width) // 2
        offset_y = (self.INPUT_SIZE - resized_height) // 2
        canvas[offset_y : offset_y + resized_height, offset_x : offset_x + resized_width] = resized
        outputs = self.rknn.inference(
            inputs=[np.expand_dims(canvas, 0)],
            data_format=["nhwc"],
        )
        raw_landmarks = next(
            (np.asarray(item) for item in outputs if np.asarray(item).size == 195),
            None,
        )
        presence_tensor = next(
            (np.asarray(item) for item in outputs if np.asarray(item).size == 1),
            None,
        )
        if raw_landmarks is None or presence_tensor is None:
            return SimpleNamespace(pose_landmarks=None)
        presence_score = float(presence_tensor.reshape(-1)[0])
        if presence_score < self.presence_threshold:
            return SimpleNamespace(pose_landmarks=None)
        decoded = raw_landmarks.astype(np.float32).reshape(39, 5)
        landmarks = []
        for x, y, z, visibility_logit, presence_logit in decoded[:33]:
            working_x = (float(x) - offset_x) / max(scale, 1e-9)
            working_y = (float(y) - offset_y) / max(scale, 1e-9)
            if rotate_clockwise:
                crop_x = working_y
                crop_y = height - working_x
            else:
                crop_x = working_x
                crop_y = working_y
            landmarks.append(
                SimpleNamespace(
                    x=crop_x / max(width, 1),
                    y=crop_y / max(height, 1),
                    z=float(z) / self.INPUT_SIZE,
                    visibility=_sigmoid(float(visibility_logit)),
                    presence=_sigmoid(float(presence_logit)),
                )
            )
        return SimpleNamespace(
            pose_landmarks=SimpleNamespace(landmark=landmarks),
        )

    def close(self) -> None:
        self.rknn.release()


class PushupCounter:
    def __init__(self, config: dict[str, Any]):
        ensure_mediapipe_protobuf_compat()
        self.cfg = config["pushup"]
        self.pose_backend = str(self.cfg.get("pose_backend", "mediapipe")).strip().lower()
        if self.pose_backend == "rknn":
            model = Path(str(self.cfg.get("pose_rknn_model", "models/pose_landmark_full_norm255.rknn")))
            if not model.is_absolute():
                model = ROOT / model
            self.pose = RknnPoseBackend(
                model,
                presence_threshold=float(self.cfg.get("pose_presence_threshold", 0.5)),
            )
        elif self.pose_backend == "mediapipe":
            self.pose = mp.solutions.pose.Pose(
                static_image_mode=False,
                model_complexity=int(self.cfg["pose_model_complexity"]),
                smooth_landmarks=True,
                min_detection_confidence=float(self.cfg["pose_detection_confidence"]),
                min_tracking_confidence=float(self.cfg["pose_tracking_confidence"]),
            )
        else:
            raise ReIDTestError(f"unsupported push-up pose backend: {self.pose_backend}")
        self.tracker = PushupPhaseTracker(self.cfg)
        self.valid_pose_frames = 0

    @property
    def count(self) -> int:
        return self.tracker.count

    @property
    def phase(self) -> str | None:
        return self.tracker.phase

    def _point(
        self,
        landmarks: list[Any],
        index: int,
        image_size: tuple[int, int],
    ) -> tuple[np.ndarray, float]:
        item = landmarks[index]
        width, height = image_size
        return np.asarray([item.x * width, item.y * height], dtype=np.float32), float(item.visibility)

    def mark_missing(self) -> None:
        self.tracker.mark_missing()

    def process(self, crop: np.ndarray) -> dict[str, Any]:
        result = self.pose.process(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        if result.pose_landmarks is None:
            self.mark_missing()
            return {"valid": False, "count": self.count, "phase": self.phase, "reason": "no_pose", "visible_points": 0, "required_points": 8}
        lm = result.pose_landmarks.landmark
        pose = mp.solutions.pose.PoseLandmark
        height, width = crop.shape[:2]
        image_size = (width, height)
        angles: list[float] = []
        wrist_shoulder_ratios: list[float] = []
        side_visibilities: list[float] = []
        left_shoulder, left_shoulder_visibility = self._point(lm, pose.LEFT_SHOULDER.value, image_size)
        right_shoulder, right_shoulder_visibility = self._point(lm, pose.RIGHT_SHOULDER.value, image_size)
        left_hip, left_hip_visibility = self._point(lm, pose.LEFT_HIP.value, image_size)
        right_hip, right_hip_visibility = self._point(lm, pose.RIGHT_HIP.value, image_size)
        minimum_visibility = float(self.cfg["minimum_landmark_visibility"])
        required_indices = {pose.LEFT_SHOULDER.value, pose.RIGHT_SHOULDER.value, pose.LEFT_HIP.value, pose.RIGHT_HIP.value, pose.LEFT_ELBOW.value, pose.RIGHT_ELBOW.value, pose.LEFT_WRIST.value, pose.RIGHT_WRIST.value}
        visible_points = sum(1 for index in required_indices if float(lm[index].visibility) >= minimum_visibility)
        torso_visibility = min(
            left_shoulder_visibility,
            right_shoulder_visibility,
            left_hip_visibility,
            right_hip_visibility,
        )
        shoulder_midpoint = (left_shoulder + right_shoulder) * 0.5
        hip_midpoint = (left_hip + right_hip) * 0.5
        torso_vector = hip_midpoint - shoulder_midpoint
        torso_length = float(np.linalg.norm(torso_vector))
        if torso_visibility < minimum_visibility or torso_length <= 1e-6:
            self.mark_missing()
            return {"valid": False, "count": self.count, "phase": self.phase, "reason": "torso_not_visible", "visible_points": visible_points, "required_points": len(required_indices)}
        for shoulder, elbow, wrist in (
            (pose.LEFT_SHOULDER.value, pose.LEFT_ELBOW.value, pose.LEFT_WRIST.value),
            (pose.RIGHT_SHOULDER.value, pose.RIGHT_ELBOW.value, pose.RIGHT_WRIST.value),
        ):
            a, va = self._point(lm, shoulder, image_size)
            b, vb = self._point(lm, elbow, image_size)
            c, vc = self._point(lm, wrist, image_size)
            visibility = min(va, vb, vc)
            if visibility >= minimum_visibility:
                angles.append(joint_angle(a, b, c))
                wrist_shoulder_ratios.append(float((c[1] - a[1]) / torso_length))
                side_visibilities.append(visibility)
        if not angles:
            self.mark_missing()
            return {"valid": False, "count": self.count, "phase": self.phase, "reason": "elbows_not_visible", "visible_points": visible_points, "required_points": len(required_indices)}
        raw_angle = float(np.average(angles, weights=side_visibilities))
        wrist_shoulder_ratio = float(np.average(wrist_shoulder_ratios, weights=side_visibilities))
        torso_angle = math.degrees(math.atan2(abs(float(torso_vector[1])), abs(float(torso_vector[0])) + 1e-9))
        crop_aspect = float(width / max(height, 1))
        self.valid_pose_frames += 1
        state = self.tracker.process_observation(raw_angle, wrist_shoulder_ratio, torso_angle, crop_aspect)
        return {
            "valid": True,
            "visible_points": visible_points,
            "required_points": len(required_indices),
            **state,
            "raw_angle": round(raw_angle, 2),
            "wrist_shoulder_ratio": round(wrist_shoulder_ratio, 3),
            "torso_angle": round(torso_angle, 2),
            "crop_aspect": round(crop_aspect, 3),
            "visible_sides": len(angles),
        }

    def release(self) -> None:
        self.pose.close()


class LandmarkExercisePhaseTracker:
    """Debounced two-phase state machine for squat and pull-up repetitions."""

    def __init__(self, exercise: str, config: dict[str, Any]):
        if exercise not in {"squat", "pull_up"}:
            raise ValueError(f"unsupported landmark exercise: {exercise}")
        self.exercise = exercise
        self.cfg = config
        self.count = 0
        self.phase: str | None = None
        self.candidate_phase: str | None = None
        self.candidate_frames = 0
        self.filtered_metric: float | None = None
        self.frames_since_rep = 9999
        self.missing_frames = 0

    def _reset_posture(self) -> None:
        self.phase = None
        self.candidate_phase = None
        self.candidate_frames = 0
        self.filtered_metric = None

    def mark_missing(self) -> None:
        self.missing_frames += 1
        self.frames_since_rep += 1
        if self.missing_frames > int(self.cfg["lost_reset_frames"]):
            self._reset_posture()

    def process_observation(self, raw_metric: float, top_condition: bool = True) -> dict[str, Any]:
        self.frames_since_rep += 1
        self.missing_frames = 0
        alpha = float(self.cfg["angle_ema_alpha"])
        self.filtered_metric = (
            raw_metric
            if self.filtered_metric is None
            else alpha * raw_metric + (1.0 - alpha) * self.filtered_metric
        )
        candidate: str | None = None
        if self.exercise == "squat":
            if self.filtered_metric >= float(self.cfg["up_angle"]):
                candidate = "up"
            elif self.filtered_metric <= float(self.cfg["down_angle"]):
                candidate = "down"
        else:
            if self.filtered_metric <= float(self.cfg["up_angle"]) and top_condition:
                candidate = "up"
            elif self.filtered_metric >= float(self.cfg["down_angle"]):
                candidate = "down"

        if candidate == self.candidate_phase:
            self.candidate_frames += 1
        else:
            self.candidate_phase = candidate
            self.candidate_frames = 1 if candidate else 0
        incremented = False
        if candidate and self.candidate_frames >= int(self.cfg["minimum_phase_frames"]) and candidate != self.phase:
            if (
                self.phase == "down"
                and candidate == "up"
                and self.frames_since_rep >= int(self.cfg["minimum_rep_interval_frames"])
            ):
                self.count += 1
                self.frames_since_rep = 0
                incremented = True
            self.phase = candidate
        return {
            "count": self.count,
            "phase": self.phase,
            "candidate_phase": candidate,
            "filtered_metric": round(float(self.filtered_metric), 2),
            "incremented": incremented,
        }


class LandmarkExerciseCounter:
    """Pose counter sharing the same face-lock and continuous-ReID path as push-up."""

    def __init__(self, exercise: str, config: dict[str, Any]):
        ensure_mediapipe_protobuf_compat()
        self.exercise = exercise
        self.cfg = config[exercise]
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=int(self.cfg["pose_model_complexity"]),
            smooth_landmarks=True,
            min_detection_confidence=float(self.cfg["pose_detection_confidence"]),
            min_tracking_confidence=float(self.cfg["pose_tracking_confidence"]),
        )
        self.tracker = LandmarkExercisePhaseTracker(exercise, self.cfg)
        self.valid_pose_frames = 0

    @property
    def count(self) -> int:
        return self.tracker.count

    @property
    def phase(self) -> str | None:
        return self.tracker.phase

    def _point(self, landmarks: list[Any], index: int) -> tuple[np.ndarray, float]:
        item = landmarks[index]
        return np.asarray([item.x, item.y], dtype=np.float32), float(item.visibility)

    def mark_missing(self) -> None:
        self.tracker.mark_missing()

    def process(self, crop: np.ndarray) -> dict[str, Any]:
        result = self.pose.process(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        if result.pose_landmarks is None:
            self.mark_missing()
            return {"valid": False, "count": self.count, "phase": self.phase, "reason": "no_pose"}
        landmarks = result.pose_landmarks.landmark
        pose = mp.solutions.pose.PoseLandmark
        minimum_visibility = float(self.cfg["minimum_landmark_visibility"])
        angles: list[float] = []
        weights: list[float] = []
        top_condition = True

        if self.exercise == "squat":
            triplets = (
                (pose.LEFT_HIP.value, pose.LEFT_KNEE.value, pose.LEFT_ANKLE.value),
                (pose.RIGHT_HIP.value, pose.RIGHT_KNEE.value, pose.RIGHT_ANKLE.value),
            )
        else:
            triplets = (
                (pose.LEFT_SHOULDER.value, pose.LEFT_ELBOW.value, pose.LEFT_WRIST.value),
                (pose.RIGHT_SHOULDER.value, pose.RIGHT_ELBOW.value, pose.RIGHT_WRIST.value),
            )
        for first, middle, last in triplets:
            a, va = self._point(landmarks, first)
            b, vb = self._point(landmarks, middle)
            c, vc = self._point(landmarks, last)
            visibility = min(va, vb, vc)
            if visibility >= minimum_visibility:
                angles.append(joint_angle(a, b, c))
                weights.append(visibility)
        if not angles:
            self.mark_missing()
            return {"valid": False, "count": self.count, "phase": self.phase, "reason": "joints_not_visible"}

        raw_metric = float(np.average(angles, weights=weights))
        if self.exercise == "pull_up":
            nose, nose_visibility = self._point(landmarks, pose.NOSE.value)
            left_shoulder, left_visibility = self._point(landmarks, pose.LEFT_SHOULDER.value)
            right_shoulder, right_visibility = self._point(landmarks, pose.RIGHT_SHOULDER.value)
            if min(nose_visibility, left_visibility, right_visibility) < minimum_visibility:
                self.mark_missing()
                return {"valid": False, "count": self.count, "phase": self.phase, "reason": "top_landmarks_not_visible"}
            shoulder_y = float((left_shoulder[1] + right_shoulder[1]) * 0.5)
            top_condition = float(nose[1]) < shoulder_y - float(self.cfg.get("nose_above_shoulder_margin", 0.0))

        self.valid_pose_frames += 1
        state = self.tracker.process_observation(raw_metric, top_condition)
        return {
            "valid": True,
            **state,
            "raw_metric": round(raw_metric, 2),
            "visible_sides": len(angles),
            "top_condition": top_condition if self.exercise == "pull_up" else None,
        }

    def release(self) -> None:
        self.pose.close()


def open_writer(path: Path, frame: np.ndarray, fps: float) -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = frame.shape[:2]
    value = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not value.isOpened():
        raise ReIDTestError(f"cannot open output writer: {path}")
    return value


def make_android_compatible_video(source: Path, destination: Path) -> dict[str, Any]:
    """Repackage the clean recording as a WebView-safe H.264 MP4.

    OpenCV's ``mp4v`` writer is broadly readable on desktops but is not a
    required Android WebView codec.  The App copy remains the same unannotated
    recording; only its video codec and MP4 metadata layout are changed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.stem}.{os.getpid()}.tmp{destination.suffix}"
    )
    started = time.monotonic()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-profile:v",
        "baseline",
        "-level:v",
        "3.1",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45.0,
        check=False,
    )
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        detail = completed.stderr.strip()[-800:] or f"ffmpeg_exit_{completed.returncode}"
        raise ReIDTestError(f"app_video_encode_failed: {detail}")
    temporary.replace(destination)
    return {
        "path": str(destination),
        "codec": "h264",
        "pixel_format": "yuv420p",
        "faststart": True,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "bytes": destination.stat().st_size,
    }


class TimelineVideoWriter:
    """Write live frames on a fixed-rate wall-clock timeline.

    Inference throughput is not a camera frame rate. For live sources this writer
    holds the most recent processed frame across every output time slot, producing
    a constant-rate MP4 whose duration follows capture time. File sources use
    direct mode and retain their source-derived frame rate.
    """

    def __init__(
        self,
        path: Path,
        frame: np.ndarray,
        fps: float,
        synchronize_timeline: bool,
    ):
        self.fps = max(1.0, float(fps))
        self.synchronize_timeline = bool(synchronize_timeline)
        self.writer = open_writer(path, frame, self.fps)
        self.output_frames = 0
        self.first_input_timestamp: float | None = None
        self.last_input_timestamp: float | None = None
        self.next_output_timestamp: float | None = None
        self.pending_frame: np.ndarray | None = None
        self.finalized = False

    def write(self, frame: np.ndarray, timestamp: float | None = None) -> None:
        if self.finalized:
            raise ReIDTestError("cannot write after video timeline finalization")
        if not self.synchronize_timeline:
            self.writer.write(frame)
            self.output_frames += 1
            return
        current = float(time.monotonic() if timestamp is None else timestamp)
        if not math.isfinite(current):
            raise ReIDTestError(f"invalid capture timestamp: {current}")
        if self.pending_frame is None:
            self.first_input_timestamp = current
            self.last_input_timestamp = current
            self.next_output_timestamp = current
            self.pending_frame = frame.copy()
            return
        assert self.last_input_timestamp is not None
        assert self.next_output_timestamp is not None
        current = max(current, self.last_input_timestamp)
        interval = 1.0 / self.fps
        while self.next_output_timestamp < current:
            self.writer.write(self.pending_frame)
            self.output_frames += 1
            self.next_output_timestamp += interval
        self.last_input_timestamp = current
        self.pending_frame = frame.copy()

    def finalize(self) -> None:
        if self.finalized:
            return
        if self.synchronize_timeline and self.pending_frame is not None:
            self.writer.write(self.pending_frame)
            self.output_frames += 1
            self.pending_frame = None
        self.finalized = True

    @property
    def capture_duration_seconds(self) -> float | None:
        if self.first_input_timestamp is None or self.last_input_timestamp is None:
            return None
        return max(0.0, self.last_input_timestamp - self.first_input_timestamp)

    @property
    def output_duration_seconds(self) -> float:
        return self.output_frames / self.fps

    def release(self) -> None:
        self.finalize()
        self.writer.release()


class AsyncTimelineVideoWriter:
    """Move MP4 encoding off the capture/inference paths.

    Frames are copied into a bounded queue.  Queue overflow is reported instead
    of blocking inference; the configured multi-second queue is intentionally
    much larger than the measured live writer depth.
    """

    _STOP = object()

    def __init__(
        self,
        path: Path,
        fps: float,
        synchronize_timeline: bool,
        queue_seconds: float = 5.0,
    ):
        self.path = path
        self.fps = max(1.0, float(fps))
        self.synchronize_timeline = bool(synchronize_timeline)
        self.queue: queue.Queue[Any] = queue.Queue(
            maxsize=max(30, int(math.ceil(self.fps * max(1.0, queue_seconds))))
        )
        self.inner: TimelineVideoWriter | None = None
        self.frames_submitted = 0
        self.frames_dropped = 0
        self.maximum_queue_depth = 0
        self.error: BaseException | None = None
        self.closed = False
        self.thread = threading.Thread(
            target=self._run,
            name=f"video-writer-{path.stem}",
            daemon=True,
        )
        self.thread.start()

    def write(self, frame: np.ndarray, timestamp: float | None = None) -> None:
        if self.closed:
            raise ReIDTestError(f"cannot write to closed async video: {self.path}")
        if self.error is not None:
            raise ReIDTestError(f"async video writer failed: {type(self.error).__name__}: {self.error}")
        try:
            self.queue.put_nowait((frame.copy(), timestamp))
            self.frames_submitted += 1
            self.maximum_queue_depth = max(self.maximum_queue_depth, self.queue.qsize())
        except queue.Full:
            self.frames_dropped += 1

    def _run(self) -> None:
        try:
            while True:
                item = self.queue.get()
                try:
                    if item is self._STOP:
                        return
                    frame, timestamp = item
                    if self.inner is None:
                        self.inner = TimelineVideoWriter(
                            self.path,
                            frame,
                            self.fps,
                            self.synchronize_timeline,
                        )
                    self.inner.write(frame, timestamp)
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self.error = exc
        finally:
            if self.inner is not None:
                self.inner.release()

    def release(self) -> None:
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self.queue.put(self._STOP, timeout=0.2)
                break
            except queue.Full:
                if not self.thread.is_alive():
                    break
        self.thread.join(timeout=30.0)
        if self.thread.is_alive():
            raise ReIDTestError(f"async video writer did not stop: {self.path}")
        if self.error is not None:
            raise ReIDTestError(f"async video writer failed: {type(self.error).__name__}: {self.error}")

    @property
    def output_frames(self) -> int:
        return self.inner.output_frames if self.inner is not None else 0

    @property
    def output_duration_seconds(self) -> float:
        return self.inner.output_duration_seconds if self.inner is not None else 0.0

    @property
    def capture_duration_seconds(self) -> float | None:
        return self.inner.capture_duration_seconds if self.inner is not None else None


@dataclass
class LiveFramePacket:
    sequence: int
    frame: np.ndarray
    captured_at: float
    source_position_ms: float


class LatestFrameStream:
    """Continuously capture/record while inference consumes only the newest frame."""

    def __init__(self, capture: Any, raw_writer: AsyncTimelineVideoWriter):
        self.capture = capture
        self.raw_writer = raw_writer
        self.condition = threading.Condition()
        self.latest: LiveFramePacket | None = None
        self.stop_requested = False
        self.finished = False
        self.error: BaseException | None = None
        self.captured_frames = 0
        self.overwritten_frames = 0
        self.first_capture_timestamp: float | None = None
        self.last_capture_timestamp: float | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="fitness-latest-frame-capture",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            while not self.stop_requested:
                ok, frame = self.capture.read()
                if not ok or frame is None:
                    break
                captured_at = time.monotonic()
                source_position_ms = float(self.capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                self.captured_frames += 1
                if self.first_capture_timestamp is None:
                    self.first_capture_timestamp = captured_at
                self.last_capture_timestamp = captured_at
                self.raw_writer.write(frame, captured_at)
                packet = LiveFramePacket(
                    sequence=self.captured_frames,
                    frame=frame,
                    captured_at=captured_at,
                    source_position_ms=source_position_ms,
                )
                with self.condition:
                    if self.latest is not None:
                        self.overwritten_frames += 1
                    self.latest = packet
                    self.condition.notify()
        except BaseException as exc:
            self.error = exc
        finally:
            with self.condition:
                self.finished = True
                self.condition.notify_all()

    def read_latest(self, timeout: float = 0.5) -> LiveFramePacket | None:
        deadline = time.monotonic() + max(0.01, timeout)
        with self.condition:
            while self.latest is None and not self.finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            if self.latest is None:
                return None
            packet = self.latest
            self.latest = None
            return packet

    def stop(self) -> None:
        self.stop_requested = True
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=3.0)
        if self.thread.is_alive():
            raise ReIDTestError("latest-frame capture thread did not stop")
        if self.error is not None:
            raise ReIDTestError(f"latest-frame capture failed: {type(self.error).__name__}: {self.error}")


def draw_candidate(frame: np.ndarray, candidate: Candidate, selected: bool) -> None:
    x1, y1, x2, y2 = candidate.person.bbox
    color = (0, 210, 0) if selected else (0, 180, 255)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    cv2.putText(
        frame,
        f"id={candidate.identity_score:.3f} trk={candidate.tracklet_score:.3f}",
        (x1, max(20, y1 - 6)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        color,
        2,
    )


def command_count(args: argparse.Namespace, config: dict[str, Any]) -> int:
    global _STOP_REQUESTED, _ACTIVE_SKILL
    _STOP_REQUESTED = False
    exercise = str(getattr(args, "exercise", "push_up") or "push_up").strip().lower()
    if exercise not in {"push_up", "squat", "pull_up"}:
        raise ReIDTestError(f"unsupported exercise: {exercise}")
    _ACTIVE_SKILL = exercise
    exercise_config_key = "pushup" if exercise == "push_up" else exercise
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

    initial_count = max(0, int(getattr(args, "initial_count", 0) or 0))
    initial_elapsed = max(0.0, float(getattr(args, "initial_elapsed_seconds", 0.0) or 0.0))
    duration = max(0.0, float(getattr(args, "duration", 0.0) or 0.0))
    identity_policy = str(
        getattr(args, "identity_policy", "face_and_reid") or "face_and_reid"
    ).strip().lower()
    if identity_policy not in {"face_and_reid", "anonymous"}:
        raise ReIDTestError(f"unsupported identity policy: {identity_policy}")
    session_identity = args.name if identity_policy == "face_and_reid" else "anonymous"
    state_file = getattr(args, "state_file", None)
    start_gate = getattr(args, "start_gate", None)
    frame_step = max(1, int(getattr(args, "frame_step", 1) or 1))
    max_frames = max(0, int(getattr(args, "max_frames", 0) or 0))
    output_arg = getattr(args, "output", None)
    end = getattr(args, "end", None)

    detector: PersonDetector | None = None
    reid: RknnReID | None = None
    capture: cv2.VideoCapture | None = None
    counter: PushupCounter | LandmarkExerciseCounter | None = None
    writer: TimelineVideoWriter | AsyncTimelineVideoWriter | None = None
    raw_writer: TimelineVideoWriter | AsyncTimelineVideoWriter | None = None
    latest_stream: LatestFrameStream | None = None
    tracker: ContinuousIdentityTracker | AnonymousTargetTracker | None = None
    locked_identity: dict[str, Any] = {
        "name": session_identity,
        "person_id": session_identity,
    }
    face_acquisition: dict[str, Any] = {}
    runtime = Path(config["paths"]["runtime_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    output = Path(output_arg) if output_arg else runtime / f"{safe_name(session_identity)}_{int(time.time())}.mp4"
    raw_output = output.with_name(f"{output.stem}.raw.mp4")
    app_video_output = output.with_name(f"{output.stem}.app.mp4")
    events_path = output.with_suffix(".jsonl")
    summary_path = output.with_suffix(".summary.json")
    app_video_manifest_path = output.with_suffix(".app_video.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    frame_index = processed_frames = selected_frames = multi_person_frames = 0
    stale_frames = 0
    first_capture_timestamp: float | None = None
    last_capture_timestamp: float | None = None
    session_started = time.monotonic()
    last_state_write = 0.0
    source_frames = 0
    output_fps = float(config.get("output", {}).get("live_fps", config["camera"]["fps"]))
    synchronize_timeline = False

    _write_state(
        state_file,
        state="initializing",
        running=True,
        pid=os.getpid(),
        identity=session_identity,
        identity_policy=identity_policy,
        initial_count=initial_count,
        session_count=0,
        current_count=initial_count,
        count=initial_count,
        elapsed_seconds=initial_elapsed,
        output=str(output),
    )
    try:
        identity_source = str(getattr(args, "identity_source", None) or args.source)
        if identity_policy == "face_and_reid":
            registered = load_registered_faces(config)
            detector, reid = build_detector_reid(config)
        else:
            detector = build_person_detector(config)
            identity_source = str(args.source)
        capture = _RESIDENT_CAPTURE_FACTORY(identity_source) if callable(_RESIDENT_CAPTURE_FACTORY) else open_source(identity_source, config)
        seek(capture, float(getattr(args, "start", 0.0) or 0.0))
        source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if identity_policy == "face_and_reid":
            _skill_event("progress", "请面向摄像头确认身份", state="identity_acquiring", name=args.name)
            _write_state(
                state_file,
                state="identity_acquiring",
                running=True,
                pid=os.getpid(),
                identity=args.name,
                identity_policy=identity_policy,
                initial_count=initial_count,
                session_count=0,
                current_count=initial_count,
                count=initial_count,
                elapsed_seconds=initial_elapsed,
                output=str(output),
            )
            (
                locked_identity,
                runtime_gallery,
                face_acquisition,
                authenticated_bbox,
            ) = acquire_database_identity_gallery(
                capture, detector, reid, args.name, registered, config
            )
            runtime_profile = {
                "name": locked_identity["name"],
                "person_id": locked_identity["person_id"],
                "reid_gallery": runtime_gallery,
                "reid_centroid": normalized(np.mean(runtime_gallery, axis=0)),
            }
            # The box is valid only when authentication and counting use the
            # same camera/source coordinate system.
            if identity_source == str(args.source):
                runtime_profile["authenticated_bbox"] = authenticated_bbox
            tracker = ContinuousIdentityTracker(runtime_profile, config)
        else:
            face_acquisition = {
                "skipped": True,
                "reason": "explicit_user_opt_out",
                "face_recognition": False,
                "reid": False,
            }
            tracker = AnonymousTargetTracker()
        counter = PushupCounter(config) if exercise == "push_up" else LandmarkExerciseCounter(exercise, config)
        framing_policy = FitnessFramingEventPolicy(hold_seconds=float(config.get("fitness_framing", {}).get("hold_seconds", 1.2)), repeat_seconds=float(config.get("fitness_framing", {}).get("repeat_seconds", 10.0)))
        counter.tracker.count = initial_count

        _write_state(
            state_file,
            state="identity_locked",
            running=True,
            pid=os.getpid(),
            identity=locked_identity["name"],
            person_id=locked_identity["person_id"],
            identity_policy=identity_policy,
            initial_count=initial_count,
            session_count=0,
            current_count=initial_count,
            count=initial_count,
            elapsed_seconds=initial_elapsed,
            output=str(output),
        )
        ready_text = {
            "push_up": "准备好了吗？三、二、一，开始。",
            "squat": "准备好了吗？三、二、一，开始。",
            "pull_up": "准备好了吗？三、二、一，开始。",
        }[exercise]
        _skill_event(
            "ready",
            ready_text,
            state="ready",
            identity=locked_identity["name"],
            person_id=locked_identity["person_id"],
            identity_policy=identity_policy,
        )
        if not _wait_start_gate(start_gate):
            _write_state(state_file, state="interrupted", running=False, count=initial_count, current_count=initial_count)
            return 130

        # Identity and counting normally share the rear camera. If a future
        # hardware profile deliberately separates them, switch only after the
        # projection/preparation gate; the counting clock starts afterwards.
        if identity_source != str(args.source):
            capture.release()
            capture = None
            capture = _RESIDENT_CAPTURE_FACTORY(args.source) if callable(_RESIDENT_CAPTURE_FACTORY) else open_source(args.source, config)
            seek(capture, float(getattr(args, "start", 0.0) or 0.0))
            source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        live_source = is_live_source(args.source)
        output_cfg = config.get("output", {})
        synchronize_timeline = live_source and bool(output_cfg.get("timeline_sync", True))
        if live_source:
            output_fps = max(1.0, float(output_cfg.get("live_fps", config["camera"]["fps"])))
            if bool(output_cfg.get("latest_frame_inference", True)):
                raw_writer = AsyncTimelineVideoWriter(
                    raw_output,
                    output_fps,
                    synchronize_timeline,
                    queue_seconds=float(output_cfg.get("writer_queue_seconds", 5.0)),
                )
                latest_stream = LatestFrameStream(capture, raw_writer)
                latest_stream.start()
        else:
            input_fps = float(capture.get(cv2.CAP_PROP_FPS) or config["camera"]["fps"])
            output_fps = max(1.0, input_fps / frame_step)

        session_started = time.monotonic()
        emit(
            "count_started",
            name=locked_identity["name"],
            person_id=locked_identity["person_id"],
            source=args.source,
            identity_policy=identity_policy,
            face_database=(
                config["face"]["database"]
                if identity_policy == "face_and_reid"
                else None
            ),
            initial_count=initial_count,
            hardware_control=False,
        )
        _write_state(
            state_file,
            state="active",
            running=True,
            pid=os.getpid(),
            identity=locked_identity["name"],
            person_id=locked_identity["person_id"],
            initial_count=initial_count,
            session_count=0,
            current_count=initial_count,
            count=initial_count,
            elapsed_seconds=initial_elapsed,
            output=str(output),
            events=str(events_path),
        )

        with events_path.open("w", encoding="utf-8", buffering=1) as events:
            while not _STOP_REQUESTED:
                now = time.monotonic()
                if duration > 0 and now - session_started >= duration:
                    break
                if latest_stream is not None:
                    packet = latest_stream.read_latest(timeout=0.5)
                    if packet is None:
                        if latest_stream.finished:
                            break
                        continue
                    frame = packet.frame
                    captured_at = packet.captured_at
                    frame_index = packet.sequence
                    source_time_seconds = packet.source_position_ms / 1000.0
                    maximum_age_ms = float(output_cfg.get("max_inference_frame_age_ms", 180.0))
                    if (time.monotonic() - captured_at) * 1000.0 > maximum_age_ms:
                        stale_frames += 1
                        continue
                else:
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    captured_at = time.monotonic()
                    if source_finished(args.source, capture, end):
                        break
                    frame_index += 1
                    source_time_seconds = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
                    # File validation retains every source frame. Live capture
                    # is recorded by LatestFrameStream before inference.
                    raw_frame = frame.copy()
                    if raw_writer is None:
                        raw_writer = TimelineVideoWriter(
                            raw_output,
                            raw_frame,
                            output_fps,
                            synchronize_timeline,
                        )
                    raw_writer.write(raw_frame, None)
                if frame_index % frame_step:
                    continue
                if first_capture_timestamp is None:
                    first_capture_timestamp = captured_at
                last_capture_timestamp = captured_at
                processed_frames += 1

                raw_people = valid_people(detector.detect(frame), config)
                people = deduplicate_people(raw_people, config)
                if len(people) > 1:
                    multi_person_frames += 1
                selected, candidates, identity = tracker.update(frame, people, reid)
                if selected is None:
                    counter.mark_missing()
                    pose_result = {
                        "valid": False,
                        "count": counter.count,
                        "phase": counter.phase,
                        "reason": "identity_not_locked",
                    }
                else:
                    selected_frames += 1
                    target_crop = crop_person(
                        frame,
                        selected.person.bbox,
                        padding=float(config[exercise_config_key]["person_crop_padding"]),
                    )
                    pose_result = counter.process(target_crop)
                    if pose_result.get("incremented"):
                        elapsed = initial_elapsed + captured_at - session_started
                        count_emitted = time.monotonic()
                        _skill_event(
                            "count",
                            _spoken_live_count(counter.count),
                            count=counter.count,
                            session_count=counter.count - initial_count,
                            detected_monotonic=round(count_emitted, 6),
                            exercise=exercise,
                        )
                        emit(
                            "fitness_count",
                            name=locked_identity["name"],
                            exercise=exercise,
                            count=counter.count,
                            session_count=counter.count - initial_count,
                            source_time_seconds=round(source_time_seconds, 3),
                            emitted_monotonic=round(count_emitted, 6),
                        )
                        _write_state(
                            state_file,
                            state="active",
                            running=True,
                            pid=os.getpid(),
                            identity=locked_identity["name"],
                            person_id=locked_identity["person_id"],
                            initial_count=initial_count,
                            session_count=counter.count - initial_count,
                            current_count=counter.count,
                            count=counter.count,
                            elapsed_seconds=round(elapsed, 3),
                            output=str(output),
                            events=str(events_path),
                        )
                framing_event = framing_policy.observe(pose_result, tuple(selected.person.bbox) if selected is not None else None, (int(frame.shape[1]), int(frame.shape[0])), captured_at)
                if framing_event is not None:
                    _skill_event("attention", framing_event.pop("text"), attention_kind="fitness_framing", exercise=exercise, **framing_event)
                for candidate in candidates:
                    draw_candidate(frame, candidate, selected is candidate)
                cv2.putText(
                    frame,
                    f"{locked_identity['name']} count={counter.count} id={identity['state']} pose={pose_result.get('phase')}",
                    (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 255),
                    2,
                )
                event = {
                    "frame": frame_index,
                    "source_time_seconds": round(source_time_seconds, 3),
                    "recording_time_seconds": round(captured_at - first_capture_timestamp, 3),
                    "people": len(people),
                    "raw_people": len(raw_people),
                    "identity": identity,
                    "pose": pose_result,
                }
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
                if writer is None:
                    if live_source:
                        writer = AsyncTimelineVideoWriter(
                            output,
                            output_fps,
                            synchronize_timeline,
                            queue_seconds=float(output_cfg.get("writer_queue_seconds", 5.0)),
                        )
                    else:
                        writer = TimelineVideoWriter(output, frame, output_fps, synchronize_timeline)
                writer.write(frame, captured_at if synchronize_timeline else None)

                if captured_at - last_state_write >= 0.5:
                    last_state_write = captured_at
                    _write_state(
                        state_file,
                        state="active",
                        running=True,
                        pid=os.getpid(),
                        identity=locked_identity["name"],
                        person_id=locked_identity["person_id"],
                        initial_count=initial_count,
                        session_count=counter.count - initial_count,
                        current_count=counter.count,
                        count=counter.count,
                        elapsed_seconds=round(initial_elapsed + captured_at - session_started, 3),
                        identity_state=identity.get("state"),
                        identity_freeze_reason=identity.get("freeze_reason"),
                        output=str(output),
                        events=str(events_path),
                    )
                if max_frames and processed_frames >= max_frames:
                    break

        # Finalize and close MP4 containers before creating the upload manifest.
        # Publishing a manifest while OpenCV still owns the file can race the
        # bridge and upload an MP4 whose moov atom has not been written yet.
        if latest_stream is not None:
            latest_stream.stop()
            first_capture_timestamp = latest_stream.first_capture_timestamp
            last_capture_timestamp = latest_stream.last_capture_timestamp
        if writer is not None:
            writer.release()
        if raw_writer is not None:
            raw_writer.release()
        if processed_frames > 0 and selected_frames == 0 and not _STOP_REQUESTED:
            raise ReIDTestError("no_exercise_person_detected")
        app_video_encode: dict[str, Any] | None = None
        app_video_encode_error: str | None = None
        if raw_writer is not None and raw_writer.output_frames > 0 and raw_output.is_file():
            try:
                app_video_encode = make_android_compatible_video(raw_output, app_video_output)
            except Exception as exc:
                app_video_encode_error = f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - session_started
        state = "interrupted" if _STOP_REQUESTED else "completed"
        summary = {
            "ok": True,
            "state": state,
            "exercise": exercise,
            "identity": locked_identity["name"],
            "person_id": locked_identity["person_id"],
            "identity_policy": identity_policy,
            "face_database": (
                config["face"]["database"]
                if identity_policy == "face_and_reid"
                else None
            ),
            "source": args.source,
            "source_total_frames": source_frames,
            "processed_frames": processed_frames,
            "selected_frames": selected_frames,
            "selected_frame_ratio": round(selected_frames / max(1, processed_frames), 4),
            "multi_person_frames": multi_person_frames,
            "initial_count": initial_count,
            "session_count": counter.count - initial_count,
            "current_count": counter.count,
            "count": counter.count,
            "valid_pose_frames": counter.valid_pose_frames,
            "pose_backend": getattr(counter, "pose_backend", "mediapipe"),
            "identity_reacquisitions": tracker.reacquisitions,
            "switch_rejections": tracker.switch_rejections,
            "tracklet_gallery_size": len(tracker.tracklet_gallery),
            "tracklet_updates": tracker.tracklet_updates,
            "adaptive_gallery_size": len(tracker.adaptive_gallery),
            "adaptive_updates": tracker.adaptive_updates,
            "rolling_anchor_updates": tracker.rolling_anchor_updates,
            "face_acquisition": face_acquisition,
            "elapsed_seconds": round(initial_elapsed + elapsed, 3),
            "effective_fps": round(processed_frames / max(elapsed, 1e-6), 3),
            "output_fps": round(output_fps, 3),
            "output_frames": writer.output_frames if writer is not None else 0,
            "output_duration_seconds": round(writer.output_duration_seconds, 3) if writer is not None else 0.0,
            "raw_output": str(raw_output),
            "app_video_output": str(app_video_output) if app_video_encode is not None else None,
            "app_video_encode": app_video_encode,
            "app_video_encode_error": app_video_encode_error,
            "raw_output_frames": raw_writer.output_frames if raw_writer is not None else 0,
            "raw_output_duration_seconds": round(raw_writer.output_duration_seconds, 3) if raw_writer is not None else 0.0,
            "recording_duration_seconds": round(
                max(0.0, last_capture_timestamp - first_capture_timestamp), 3
            ) if first_capture_timestamp is not None and last_capture_timestamp is not None else 0.0,
            "timeline_sync": synchronize_timeline,
            "latest_frame_mode": latest_stream is not None,
            "captured_frames": latest_stream.captured_frames if latest_stream is not None else processed_frames,
            "latest_overwritten_frames": latest_stream.overwritten_frames if latest_stream is not None else 0,
            "stale_inference_frames": stale_frames,
            "raw_writer_dropped_frames": (
                raw_writer.frames_dropped
                if isinstance(raw_writer, AsyncTimelineVideoWriter)
                else 0
            ),
            "annotated_writer_dropped_frames": (
                writer.frames_dropped
                if isinstance(writer, AsyncTimelineVideoWriter)
                else 0
            ),
            "raw_writer_maximum_queue_depth": (
                raw_writer.maximum_queue_depth
                if isinstance(raw_writer, AsyncTimelineVideoWriter)
                else 0
            ),
            "annotated_writer_maximum_queue_depth": (
                writer.maximum_queue_depth
                if isinstance(writer, AsyncTimelineVideoWriter)
                else 0
            ),
            "output": str(output),
            "events": str(events_path),
            "summary": str(summary_path),
            "app_video_manifest": str(app_video_manifest_path),
            "hardware_control": False,
        }
        exercise_name = {
            "push_up": "俯卧撑",
            "squat": "深蹲",
            "pull_up": "引体向上",
        }[exercise]
        if app_video_encode is not None and app_video_output.is_file():
            app_video_manifest = {
                "schema": 1,
                "status": "pending_upload",
                "category": "fitness",
                "title": f"{exercise_name} · {_spoken_repetition_count(counter.count)}",
                "video_path": str(app_video_output),
                "source_video_path": str(raw_output),
                "video_codec": "h264",
                "pixel_format": "yuv420p",
                "faststart": True,
                "duration_sec": round(raw_writer.output_duration_seconds, 3),
                "created_at": time.time(),
                "exercise": exercise,
                "exercise_label": exercise_name,
                "count": counter.count,
                "identity": locked_identity["name"],
                "identity_policy": identity_policy,
                "session_state": state,
            }
            temporary_manifest = app_video_manifest_path.with_suffix(".tmp")
            temporary_manifest.write_text(
                json.dumps(app_video_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_manifest.replace(app_video_manifest_path)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_state(state_file, running=False, pid=None, **summary)
        if _STOP_REQUESTED:
            print(json.dumps({"event": "skill_interrupted", "skill_name": exercise, **summary}, ensure_ascii=False), flush=True)
        else:
            # This event is the authoritative end of counting.  The daemon speaks
            # it immediately, independently of projector/head cleanup, and records
            # it on the step so the later aggregate summary is not repeated.
            _skill_event(
                "complete",
                f"运动结束，你一共完成了{_spoken_repetition_count(counter.count)}{exercise_name}。",
                **summary,
            )
        emit("count_complete", **summary)
        return 0
    except Exception as exc:
        _write_state(
            state_file,
            state="error",
            running=False,
            pid=None,
            identity=session_identity,
            identity_policy=identity_policy,
            initial_count=initial_count,
            current_count=counter.count if counter is not None else initial_count,
            count=counter.count if counter is not None else initial_count,
            error=f"{type(exc).__name__}: {exc}",
            output=str(output),
        )
        raise
    finally:
        if latest_stream is not None:
            latest_stream.stop()
        if writer is not None:
            writer.release()
        if raw_writer is not None:
            raw_writer.release()
        if counter is not None:
            counter.release()
        if capture is not None:
            capture.release()
        if reid is not None and reid is not _RESIDENT_REID_OVERRIDE:
            reid.release()
        if detector is not None and detector is not _RESIDENT_DETECTOR_OVERRIDE:
            detector.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Registered-face anchored continuous-ReID push-up counter experiment"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    count = sub.add_parser("count", help="lock a registered face, then count only that ReID target")
    count.add_argument("--name", required=True)
    count.add_argument(
        "--identity-policy",
        choices=["face_and_reid", "anonymous"],
        default="face_and_reid",
    )
    count.add_argument("--source", help="camera device or video path; defaults to config camera.source")
    count.add_argument("--start", type=float, default=0.0)
    count.add_argument("--end", type=float)
    count.add_argument("--frame-step", type=int, default=1)
    count.add_argument("--max-frames", type=int, default=0)
    count.add_argument("--output")
    count.add_argument("--duration", type=float, default=0.0)
    count.add_argument("--start-gate")
    count.add_argument("--initial-count", type=int, default=0)
    count.add_argument("--initial-elapsed-seconds", type=float, default=0.0)
    count.add_argument("--state-file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(Path(args.config))
    if args.command == "count" and not args.source:
        args.source = str(config["camera"]["source"])
    try:
        if args.command == "check":
            return command_check(config)
        if args.command == "count":
            return command_count(args, config)
        raise ReIDTestError(f"unknown command: {args.command}")
    except KeyboardInterrupt:
        emit("stopped", ok=False, error="keyboard_interrupt")
        return 130
    except Exception as exc:
        emit("error", ok=False, error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
