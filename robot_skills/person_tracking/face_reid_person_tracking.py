#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import threading
import time
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

from fitness_reid_test import (
    DEFAULT_CONFIG, PersonDetector, ReIDTestError, RknnReID, crop_person,
    emit, identity_score, load_config, make_writer, normalized, open_source,
    safe_name, valid_people,
)
from face_reid_fitness_test import (
    FaceEmbeddingModel, acquire_identity, ensure_mediapipe_protobuf_compat,
    load_registered_faces,
)


_STOP_REQUESTED = False
_RESIDENT_DETECTOR_OVERRIDE = None
_RESIDENT_REID_OVERRIDE = None
_RESIDENT_FACE_OVERRIDE = None
_RESIDENT_ROS_FACTORY = None
_RESIDENT_CAPTURE_FACTORY = None
_RESIDENT_HEAD_FACTORY = None


def _request_stop(_signum=None, _frame=None) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


class RosCmdVel:
    def __init__(self, config: dict, execute: bool):
        self.config = config["tracking"]
        self.execute = bool(execute)
        self.closed = False
        self.rclpy = self.node = self.publisher = self.Twist = None
        if not self.execute:
            return
        import rclpy
        from geometry_msgs.msg import Twist
        try:
            from rclpy.signals import SignalHandlerOptions
            if not rclpy.ok():
                rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
        except Exception:
            if not rclpy.ok():
                rclpy.init(args=None)
        self.rclpy, self.Twist = rclpy, Twist
        self.node = rclpy.create_node(f"face_reid_follower_{int(time.time())}")
        self.publisher = self.node.create_publisher(Twist, str(self.config["cmd_vel_topic"]), 10)
        deadline = time.monotonic() + float(self.config["subscriber_wait_seconds"])
        required = int(self.config["required_cmd_vel_subscribers"])
        while time.monotonic() < deadline and self.publisher.get_subscription_count() < required:
            rclpy.spin_once(self.node, timeout_sec=0.05)
        count = self.publisher.get_subscription_count()
        if count < required:
            self.close()
            raise ReIDTestError(f"cmd_vel has insufficient subscribers: {count}")
        self.stop()

    def publish(self, linear: float, angular: float) -> None:
        if not self.execute or self.closed:
            return
        msg = self.Twist()
        msg.linear.x = float(np.clip(linear, -float(self.config["maximum_linear_speed"]), float(self.config["maximum_linear_speed"]))) * float(self.config["linear_sign"])
        msg.angular.z = float(np.clip(angular, -float(self.config["maximum_angular_speed"]), float(self.config["maximum_angular_speed"]))) * float(self.config["angular_sign"])
        self.publisher.publish(msg)
        self.rclpy.spin_once(self.node, timeout_sec=0.0)

    def stop(self) -> None:
        if not self.execute or self.closed:
            return
        for _ in range(int(self.config["stop_publish_repetitions"])):
            self.publish(0.0, 0.0)
            time.sleep(float(self.config["stop_publish_interval_seconds"]))

    def close(self) -> None:
        if self.closed:
            return
        if self.execute:
            try:
                self.stop()
            finally:
                if self.node is not None:
                    self.node.destroy_node()
        self.closed = True


class MotionController:
    def __init__(self, config: dict):
        self.config = config["tracking"]
        self.linear = self.angular = 0.0
        self.filtered_error = None
        self.filtered_height_ratio = None
        self.turning = False
        self.centered_frames = 0
        self.last_update = None

    @staticmethod
    def step(previous: float, target: float, maximum_step: float) -> float:
        return previous + float(np.clip(target - previous, -maximum_step, maximum_step))

    @staticmethod
    def smooth(previous: float | None, value: float, alpha: float) -> float:
        if previous is None:
            return value
        return previous + alpha * (value - previous)

    def command(self, bbox: tuple[int, int, int, int], shape: tuple[int, ...]):
        height, width = shape[:2]
        x1, y1, x2, y2 = bbox
        raw_error = ((x1 + x2) * 0.5 - width * 0.5) / max(width * 0.5, 1.0)
        raw_height_ratio = (y2 - y1) / max(float(height), 1.0)
        self.filtered_error = self.smooth(
            self.filtered_error,
            raw_error,
            float(self.config["bbox_center_filter_alpha"]),
        )
        self.filtered_height_ratio = self.smooth(
            self.filtered_height_ratio,
            raw_height_ratio,
            float(self.config["bbox_height_filter_alpha"]),
        )

        enter = float(self.config["turn_dead_zone_enter"])
        exit_zone = float(self.config["turn_dead_zone_exit"])
        if self.turning:
            self.turning = abs(self.filtered_error) > exit_zone
        else:
            self.turning = abs(self.filtered_error) > enter

        if self.turning:
            effective_error = np.sign(self.filtered_error) * max(abs(self.filtered_error) - exit_zone, 0.0)
            desired_angular = -float(effective_error) * float(self.config["angular_gain"])
            self.centered_frames = 0
        else:
            desired_angular = 0.0
            self.centered_frames += 1

        height_ratio = self.filtered_height_ratio
        absolute_error = abs(self.filtered_error)
        curve_start = float(self.config["curve_linear_start_error"])
        curve_stop = float(self.config["curve_linear_stop_error"])
        if absolute_error <= curve_start:
            curve_scale = 1.0
        elif absolute_error >= curve_stop:
            curve_scale = 0.0
        else:
            curve_scale = (curve_stop - absolute_error) / max(curve_stop - curve_start, 1e-6)
            curve_scale = max(float(self.config["minimum_curve_linear_scale"]), curve_scale)

        if height_ratio < float(self.config["forward_below_height_ratio"]):
            base_linear = float(self.config["forward_speed"])
        elif height_ratio > float(self.config["backward_above_height_ratio"]):
            base_linear = -float(self.config["backward_speed"])
        else:
            base_linear = 0.0
        desired_linear = base_linear * curve_scale
        translation_allowed = abs(desired_linear) > 1e-6

        now = time.monotonic()
        elapsed = 0.1 if self.last_update is None else float(np.clip(now - self.last_update, 0.03, 0.20))
        self.last_update = now
        linear_rate = (
            float(self.config["maximum_linear_acceleration"])
            if abs(desired_linear) >= abs(self.linear)
            else float(self.config["maximum_linear_deceleration"])
        )
        self.linear = self.step(self.linear, desired_linear, linear_rate * elapsed)
        self.angular = self.step(
            self.angular,
            desired_angular,
            float(self.config["maximum_angular_acceleration"]) * elapsed,
        )
        self.linear = float(np.clip(self.linear, -float(self.config["maximum_linear_speed"]), float(self.config["maximum_linear_speed"])))
        self.angular = float(np.clip(self.angular, -float(self.config["maximum_angular_speed"]), float(self.config["maximum_angular_speed"])))
        return self.linear, self.angular, {
            "raw_horizontal_error": round(raw_error, 4),
            "horizontal_error": round(self.filtered_error, 4),
            "raw_height_ratio": round(raw_height_ratio, 4),
            "height_ratio": round(height_ratio, 4),
            "turning": self.turning,
            "centered_frames": self.centered_frames,
            "translation_allowed": translation_allowed,
            "curve_scale": round(curve_scale, 4),
        }

    def stop(self):
        self.linear = self.angular = 0.0
        self.filtered_error = None
        self.filtered_height_ratio = None
        self.turning = False
        self.centered_frames = 0
        self.last_update = None
        return 0.0, 0.0


def bbox_spatial_similarity(previous: tuple[int, int, int, int] | None, current: tuple[int, int, int, int], frame_width: int) -> float:
    if previous is None:
        return 0.0
    px1, py1, px2, py2 = previous
    cx1, cy1, cx2, cy2 = current
    previous_center = np.asarray([(px1 + px2) * 0.5, (py1 + py2) * 0.5], dtype=np.float32)
    current_center = np.asarray([(cx1 + cx2) * 0.5, (cy1 + cy2) * 0.5], dtype=np.float32)
    center_distance = float(np.linalg.norm(current_center - previous_center)) / max(float(frame_width), 1.0)
    intersection_w = max(0, min(px2, cx2) - max(px1, cx1))
    intersection_h = max(0, min(py2, cy2) - max(py1, cy1))
    intersection = intersection_w * intersection_h
    previous_area = max(1, (px2 - px1) * (py2 - py1))
    current_area = max(1, (cx2 - cx1) * (cy2 - cy1))
    iou = intersection / max(float(previous_area + current_area - intersection), 1.0)
    distance_score = max(0.0, 1.0 - center_distance / 0.28)
    return float(np.clip(0.65 * distance_score + 0.35 * iou, 0.0, 1.0))


def multi_view_score(feature: np.ndarray, gallery: np.ndarray, top_k: int) -> float:
    if gallery is None or len(gallery) == 0:
        return -1.0
    scores = np.sort(gallery @ feature)[::-1]
    count = max(1, min(int(top_k), len(scores)))
    return 0.75 * float(scores[0]) + 0.25 * float(np.mean(scores[:count]))


def add_diverse_feature(gallery: np.ndarray, feature: np.ndarray, limit: int, minimum_distance: float) -> tuple[np.ndarray, bool]:
    if len(gallery):
        nearest_distance = 1.0 - float(np.max(gallery @ feature))
        if nearest_distance < minimum_distance:
            return gallery, False
    if len(gallery) < limit:
        return np.vstack([gallery, feature[None, :]]), True
    similarities = gallery @ gallery.T
    np.fill_diagonal(similarities, -1.0)
    first, second = np.unravel_index(int(np.argmax(similarities)), similarities.shape)
    replacement = max(first, second)
    updated = gallery.copy()
    updated[replacement] = feature
    return updated, True


class HeadPose:
    def __init__(self, config: dict, enabled: bool):
        self.config = config["tracking"]
        self.enabled = bool(enabled)
        self.raised = False

    def run(self, key: str) -> None:
        if not self.enabled:
            return
        command = list(self.config[key])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=float(self.config["head_command_timeout_seconds"]), check=False)
        if completed.returncode != 0:
            raise ReIDTestError(f"head command failed: {completed.stderr.strip() or completed.stdout.strip()}")

    def raise_for_tracking(self) -> None:
        if self.enabled:
            # A motor may already have moved even if closed-loop confirmation
            # later times out.  Mark it displaced first so cleanup still levels
            # the head on every failure path.
            self.raised = True
        self.run("head_up_command")
        if self.enabled:
            time.sleep(float(self.config["head_settle_seconds"]))

    def restore(self) -> None:
        if self.raised:
            try:
                self.run("head_level_command")
            except Exception as exc:
                emit("head_restore_failed", ok=False, error=str(exc))
            finally:
                self.raised = False


def acquire_target(capture, detector, reid, face_model, known, face_detector, config, requested_name):
    gallery, target_name = [], None
    diagnostics = {"frames": 0, "person_detections": 0, "face_detections": 0, "accepted_face_person_matches": 0, "faces_too_small": 0, "faces_low_focus": 0, "faces_not_recognized": 0}
    last_frame = None
    last_report = 0.0
    needed = int(config["face"]["stable_identity_samples"])
    deadline = time.monotonic() + float(config["face"]["acquire_timeout_seconds"])
    emit("face_acquire_started", requested_name=requested_name, registered_names=[item["name"] for item in known])
    while time.monotonic() < deadline and len(gallery) < needed:
        ok, frame = capture.read()
        if not ok or frame is None:
            continue
        last_frame = frame.copy()
        people = valid_people(detector.detect(frame), config)
        matches = acquire_identity(frame, people, face_detector, face_model, known, config, requested_name, diagnostics)
        if time.monotonic() - last_report >= 1.0:
            emit("face_acquire_progress", **diagnostics)
            last_report = time.monotonic()
        names = {item["identity"]["name"] for item in matches}
        if len(names) != 1:
            continue
        match = max(matches, key=lambda item: item["identity"]["score"])
        name = match["identity"]["name"]
        if target_name is not None and name != target_name:
            gallery.clear()
        feature = reid.feature(crop_person(frame, match["person"].bbox))
        if gallery and float(np.dot(normalized(np.mean(gallery, axis=0)), feature)) < float(config["reid"]["reject_threshold"]):
            continue
        target_name = name
        gallery.append(feature)
        emit("face_acquire_sample", name=name, face_score=round(match["identity"]["score"], 4), samples=len(gallery), required=needed)
    if len(gallery) < needed or target_name is None:
        diagnostic_path = Path(config["paths"]["runtime_dir"]) / "latest_face_acquire_failure.jpg"
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        if last_frame is not None:
            cv2.imwrite(str(diagnostic_path), last_frame)
        raise ReIDTestError(f"no registered face was stably associated with one person; diagnostics={diagnostics}; image={diagnostic_path}")
    matrix = np.stack(gallery)
    return target_name, normalized(np.mean(matrix, axis=0)), matrix


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Independent face-confirmed ReID robot person follower")
    value.add_argument("--config", default=str(DEFAULT_CONFIG))
    value.add_argument("--name")
    value.add_argument("--source")
    value.add_argument("--seconds", type=float, default=30.0)
    value.add_argument("--output")
    value.add_argument("--execute", action="store_true", help="publish movement; default is visual-only")
    value.add_argument("--raise-head", action="store_true", help="raise head for visual-only testing; --execute enables this automatically")
    value.add_argument(
        "--skip-head-control",
        action="store_true",
        help="keep the current confirmed head pose while following",
    )
    value.add_argument("--check", action="store_true")
    return value


def main() -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)
    args = parser().parse_args()
    config = load_config(Path(args.config))
    if args.check:
        import rclpy
        from geometry_msgs.msg import Twist
        if not rclpy.ok():
            rclpy.init(args=None)
        node = rclpy.create_node(f"face_reid_follower_check_{int(time.time())}")
        publisher = node.create_publisher(Twist, str(config["tracking"]["cmd_vel_topic"]), 10)
        deadline = time.monotonic() + float(config["tracking"]["subscriber_wait_seconds"])
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        subscriber_count = publisher.get_subscription_count()
        node.destroy_node()
        emit("face_reid_tracking_check", ok=True, movement_enabled=False, topic=config["tracking"]["cmd_vel_topic"], subscribers=subscriber_count)
        return 0
    models = config["models"]
    detector = reid = face_model = capture = face_detector = driver = writer = None
    head_enabled = bool(args.execute or args.raise_head) and not args.skip_head_control
    head = (
        _RESIDENT_HEAD_FACTORY(config, head_enabled)
        if callable(_RESIDENT_HEAD_FACTORY)
        else HeadPose(config, enabled=head_enabled)
    )
    runtime = Path(config["paths"]["runtime_dir"]); runtime.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else runtime / f"face_reid_tracking_{int(time.time())}.mp4"
    events_path = output.with_suffix(".jsonl")
    failure_path = runtime / "latest_failure.json"
    stage = "model_initialization"
    try:
        detector = _RESIDENT_DETECTOR_OVERRIDE or PersonDetector(models["person_detector"], models["person_detector_weight"], config["detector"]["confidence"], config["detector"]["npu_core_mask"])
        reid = _RESIDENT_REID_OVERRIDE or RknnReID(models["person_reid"], config["reid"]["npu_core"])
        face_model = _RESIDENT_FACE_OVERRIDE or FaceEmbeddingModel(models["face_embedding"], config["face"]["npu_core"])
        known = load_registered_faces(config["face"]["database"])
        source = args.source or str(config["camera"]["source"])
        capture = _RESIDENT_CAPTURE_FACTORY(source) if callable(_RESIDENT_CAPTURE_FACTORY) else open_source(source, config)
        driver = _RESIDENT_ROS_FACTORY(config, args.execute) if callable(_RESIDENT_ROS_FACTORY) else RosCmdVel(config, execute=args.execute)
        stage = "head_raise"
        head.raise_for_tracking()
        emit("face_acquire_instruction", text="请站到机器人正前方并面向摄像头")
        stage = "face_acquisition"
        ensure_mediapipe_protobuf_compat()
        face_detector = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.35)
        target_name, anchor_centroid, anchor_gallery = acquire_target(capture, detector, reid, face_model, known, face_detector, config, args.name)
        face_detector.close(); face_detector = None
        if face_model is not _RESIDENT_FACE_OVERRIDE:
            face_model.release()
        face_model = None
        emit("target_locked", name=target_name, anchor_samples=len(anchor_gallery), movement_enabled=bool(args.execute))

        stage = "reid_tracking"
        controller = MotionController(config)
        accept = float(config["reid"]["accept_threshold"])
        margin_required = float(config["reid"]["minimum_candidate_margin"])
        stable_needed = int(config["reid"]["stable_accept_frames"])
        top_k = int(config["reid"]["gallery_top_k"])
        adaptive_limit = int(config["reid"]["adaptive_gallery_size"])
        adaptive_interval = int(config["reid"]["adaptive_update_interval_frames"])
        adaptive_min_score = float(config["reid"]["adaptive_update_min_score"])
        adaptive_min_confidence = float(config["reid"]["adaptive_update_min_detection_confidence"])
        adaptive_min_distance = float(config["reid"]["adaptive_min_feature_distance"])
        adaptive_penalty = float(config["reid"]["adaptive_score_penalty"])
        tracklet_limit = int(config["reid"]["tracklet_gallery_size"])
        tracklet_accept = float(config["reid"]["tracklet_accept_threshold"])
        tracklet_update = float(config["reid"]["tracklet_update_threshold"])
        tracklet_min_spatial = float(config["reid"]["tracklet_min_spatial_similarity"])
        tracklet_spatial_weight = float(config["reid"]["tracklet_association_weight"])
        tracklet_grace = int(config["reid"]["tracklet_grace_frames"])
        tracklet_ambiguous_margin = float(config["reid"]["tracklet_ambiguous_margin"])
        adaptive_gallery = np.empty((0, anchor_gallery.shape[1]), dtype=np.float32)
        adaptive_centroid = None
        tracklet_gallery = anchor_gallery[-min(tracklet_limit, len(anchor_gallery)):].copy()
        stable = 0
        identity_confirmed = False
        lost_track_frames = 0
        frame_index = 0
        last_locked_bbox = None
        deadline = time.monotonic() + args.seconds
        with events_path.open("w", encoding="utf-8") as events:
            while time.monotonic() < deadline and not _STOP_REQUESTED:
                ok, frame = capture.read()
                if not ok or frame is None:
                    controller.stop(); driver.publish(0.0, 0.0); time.sleep(0.01); continue
                frame_index += 1
                people = valid_people(detector.detect(frame), config)
                candidates = []
                for person in people:
                    feature = reid.feature(crop_person(frame, person.bbox))
                    anchor_score = identity_score(feature, anchor_centroid, anchor_gallery, top_k)
                    adaptive_score = multi_view_score(feature, adaptive_gallery, top_k)
                    tracklet_score = multi_view_score(feature, tracklet_gallery, top_k)
                    score = max(anchor_score, adaptive_score - adaptive_penalty)
                    spatial_score = bbox_spatial_similarity(last_locked_bbox, person.bbox, frame.shape[1])
                    continuity_quality = 0.5 * spatial_score + 0.5 * max(tracklet_score, 0.0)
                    association_score = score + (tracklet_spatial_weight * continuity_quality if identity_confirmed else 0.0)
                    candidates.append((association_score, score, anchor_score, adaptive_score, tracklet_score, spatial_score, person, feature))
                if identity_confirmed:
                    candidates.sort(
                        key=lambda item: (
                            item[4] >= tracklet_accept and item[5] >= tracklet_min_spatial,
                            item[0],
                        ),
                        reverse=True,
                    )
                else:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                best = candidates[0] if candidates else None
                continuity_peers = [
                    item for item in candidates[1:]
                    if item[4] >= tracklet_accept and item[5] >= tracklet_min_spatial
                ]
                continuity_margin = best[0] - max((item[0] for item in continuity_peers), default=-1.0) if best else -1.0
                identity_second = max((item[1] for item in candidates[1:]), default=-1.0)
                identity_margin = best[1] - identity_second if best else -1.0
                identity_accept = best is not None and best[1] >= accept and identity_margin >= margin_required
                continuity_accept = bool(
                    best is not None
                    and identity_confirmed
                    and lost_track_frames < tracklet_grace
                    and best[4] >= tracklet_accept
                    and best[5] >= tracklet_min_spatial
                    and continuity_margin >= (tracklet_ambiguous_margin if continuity_peers else 0.0)
                )
                raw_accept = identity_accept or continuity_accept
                stable = stable + 1 if identity_accept else (stable if continuity_accept else 0)
                locked = raw_accept and (identity_confirmed or stable >= stable_needed)
                if locked:
                    identity_confirmed = True
                    lost_track_frames = 0
                    linear, angular, geometry = controller.command(best[6].bbox, frame.shape)
                    driver.publish(linear, angular)
                    spatial_continuity = True
                    if last_locked_bbox is not None:
                        previous_center = np.asarray([(last_locked_bbox[0] + last_locked_bbox[2]) * 0.5, (last_locked_bbox[1] + last_locked_bbox[3]) * 0.5])
                        current_box = best[6].bbox
                        current_center = np.asarray([(current_box[0] + current_box[2]) * 0.5, (current_box[1] + current_box[3]) * 0.5])
                        spatial_continuity = float(np.linalg.norm(current_center - previous_center)) / max(float(frame.shape[1]), 1.0) <= 0.20
                    if best[4] >= tracklet_update and spatial_continuity:
                        tracklet_gallery, _ = add_diverse_feature(tracklet_gallery, best[7], tracklet_limit, adaptive_min_distance * 0.5)
                    trusted_identity = best[1] >= adaptive_min_score
                    trusted_tracklet = continuity_accept and best[4] >= tracklet_update and best[5] >= tracklet_min_spatial + 0.10
                    ambiguity_ok = not continuity_peers or continuity_margin >= max(tracklet_ambiguous_margin, margin_required + 0.02)
                    if frame_index % adaptive_interval == 0 and (trusted_identity or trusted_tracklet) and ambiguity_ok and best[6].confidence >= adaptive_min_confidence and spatial_continuity:
                        adaptive_gallery, added = add_diverse_feature(adaptive_gallery, best[7], adaptive_limit, adaptive_min_distance)
                        if added:
                            adaptive_centroid = normalized(np.mean(adaptive_gallery, axis=0))
                            emit("reid_gallery_updated", adaptive_samples=len(adaptive_gallery), anchor_score=round(best[2], 4), adaptive_score=round(best[3], 4), tracklet_score=round(best[4], 4), spatial_score=round(best[5], 4))
                    last_locked_bbox = best[6].bbox
                    state = "following" if args.execute else "visual_locked"
                else:
                    lost_track_frames += 1
                    linear, angular = controller.stop()
                    driver.publish(0.0, 0.0)
                    geometry = {}
                    state = "stabilizing" if raw_accept else "target_uncertain"
                for index, (_association, score, _anchor_score, _adaptive_score, _tracklet_score, _spatial_score, person, _feature) in enumerate(candidates):
                    x1, y1, x2, y2 = person.bbox
                    color = (0, 200, 0) if index == 0 and locked else (0, 220, 255) if index == 0 and raw_accept else (0, 0, 220)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, f"{score:.3f}", (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.putText(frame, f"{target_name} {state} v={linear:.2f} w={angular:.2f}", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)
                events.write(json.dumps({"timestamp": time.time(), "identity": target_name, "state": state, "people": len(people), "best_score": round(best[1], 4) if best else None, "anchor_score": round(best[2], 4) if best else None, "adaptive_score": round(best[3], 4) if best and best[3] >= 0 else None, "tracklet_score": round(best[4], 4) if best else None, "spatial_score": round(best[5], 4) if best else None, "adaptive_gallery_size": len(adaptive_gallery), "tracklet_gallery_size": len(tracklet_gallery), "identity_accept": identity_accept, "continuity_accept": continuity_accept, "identity_margin": round(identity_margin, 4), "continuity_margin": round(continuity_margin, 4), "lost_track_frames": lost_track_frames, "linear": round(linear, 4), "angular": round(angular, 4), **geometry}, ensure_ascii=False) + "\n")
                if writer is None:
                    writer = make_writer(output, frame, capture.get(cv2.CAP_PROP_FPS) or float(config["camera"]["fps"]))
                writer.write(frame)
        emit(
            "face_reid_tracking_complete",
            ok=True,
            interrupted=_STOP_REQUESTED,
            identity=target_name,
            movement_enabled=bool(args.execute),
            output=str(output),
            events=str(events_path),
        )
        failure_path.unlink(missing_ok=True)
        return 0
    except KeyboardInterrupt:
        emit("stopped", ok=False, error="keyboard_interrupt")
        return 130
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        failure = {
            "timestamp": time.time(),
            "stage": stage,
            "error": error_text,
            "identity": args.name,
            "movement_enabled": bool(args.execute),
            "raise_head": bool(args.raise_head),
        }
        try:
            failure_path.write_text(
                json.dumps(failure, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        emit("error", ok=False, stage=stage, error=error_text, failure=str(failure_path))
        return 1
    finally:
        if driver is not None: driver.close()
        head.restore()
        if writer is not None: writer.release()
        if face_detector is not None: face_detector.close()
        if face_model is not None and face_model is not _RESIDENT_FACE_OVERRIDE: face_model.release()
        if capture is not None: capture.release()
        if reid is not None and reid is not _RESIDENT_REID_OVERRIDE: reid.release()
        if detector is not None and detector is not _RESIDENT_DETECTOR_OVERRIDE: detector.release()


if __name__ == "__main__":
    raise SystemExit(main())
