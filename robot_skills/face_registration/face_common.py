#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import pickle
import re
import sqlite3
import time
from pathlib import Path

from retinaface_rknn import RetinaFaceRKNN, align_face, padded_face_crop, valid_face_geometry


MODEL_NAME = "facenet_vggface2_160.rknn"
DETECTOR_MODEL_NAME = "RetinaFace_resnet50_320_fp.rknn"
EMBEDDING_VERSION = "facenet_vggface2_160_retinaface_aligned_v1"
PENDING_FACE_KEY = "__pending_face__"


def _ensure_mediapipe_protobuf_compat():
    try:
        from google.protobuf import message_factory
    except Exception:
        return
    if hasattr(message_factory, "GetMessageClass"):
        return
    factory = message_factory.MessageFactory()
    if not hasattr(factory, "GetPrototype"):
        return

    def _get_message_class(descriptor):
        return factory.GetPrototype(descriptor)

    message_factory.GetMessageClass = _get_message_class


def _truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _json_emit(ok, status, skill, action, result=None, error=None, started_at=None):
    metrics = {"ts": round(time.time(), 3)}
    if started_at is not None:
        metrics["elapsed_sec"] = round(time.time() - started_at, 3)
    print(json.dumps({
        "ok": bool(ok),
        "status": status,
        "skill": skill,
        "action": action,
        "result": result or {},
        "error": error,
        "metrics": metrics,
    }, ensure_ascii=False))




def _single_function_emit_ready(skill_name, text):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    print(json.dumps({
        "event": "skill_ready",
        "skill_name": skill_name,
        "kind": "ready",
        "text": text,
    }, ensure_ascii=False), flush=True)


def _single_function_wait_start_gate(start_gate_path):
    if not start_gate_path:
        return True
    gate = Path(start_gate_path)
    while not gate.exists():
        time.sleep(0.05)
    return True


def _safe_name(text):
    text = str(text or "session").strip() or "session"
    text = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", text)
    return text[:40] or "session"


def _person_id_for_name(name):
    digest = hashlib.sha1(str(name or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"person_{digest}"


class FaceConfig:
    def __init__(self, skill_dir):
        self.skill_dir = Path(skill_dir).resolve()
        self.single_function_dir = self.skill_dir.parent
        self.assets_dir = self.skill_dir / "assets"
        self.runtime_dir = Path(os.getenv("SINGLE_FUNCTION_RUNTIME_DIR", str(self.skill_dir / "runtime")))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.front_camera = os.getenv("FACE_FRONT_CAMERA_ID", os.getenv("FACE_CAMERA_ID", "/dev/video22"))
        self.back_camera = os.getenv("FACE_BACK_CAMERA_ID", "/dev/video31")
        self.camera = os.getenv("FACE_CAMERA_ID", os.getenv("VIDEO_SOURCE", self.front_camera))
        self.width = int(os.getenv("FACE_CAMERA_WIDTH", "640"))
        self.height = int(os.getenv("FACE_CAMERA_HEIGHT", "640"))
        self.camera_open_attempts = max(1, int(os.getenv("FACE_CAMERA_OPEN_ATTEMPTS", "3")))
        self.camera_open_retry_sec = max(0.0, float(os.getenv("FACE_CAMERA_OPEN_RETRY_SEC", "0.25")))
        self.camera_probe_reads = max(1, int(os.getenv("FACE_CAMERA_PROBE_READS", "3")))
        self.model_path = Path(os.getenv("FACE_MODEL_PATH", str(self.assets_dir / "model" / MODEL_NAME)))
        self.detector_model_path = Path(
            os.getenv(
                "FACE_DETECTOR_MODEL_PATH",
                str(self.single_function_dir / "face_recognition" / "assets" / "model" / DETECTOR_MODEL_NAME),
            )
        )
        self.face_data_dir = Path(os.getenv("FACE_DATA_DIR", str(self.single_function_dir / "face_data")))
        self.db_path = Path(os.getenv("FACE_DB_PATH", str(self.face_data_dir / "faces.db")))
        self.identity_event_path = Path(os.getenv("FACE_IDENTITY_EVENT_PATH", str(self.face_data_dir / "identity_event.json")))
        self.image_dir = Path(os.getenv("FACE_IMAGE_DIR", str(self.face_data_dir / "face_images")))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.identity_event_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self.register_timeout = float(os.getenv("FACE_REGISTER_TIMEOUT_SEC", "25.0"))
        self.recognize_timeout = float(os.getenv("FACE_RECOGNIZE_TIMEOUT_SEC", "12.0"))
        self.required_samples = int(os.getenv("FACE_REGISTER_REQUIRED_SAMPLES", "8"))
        self.register_detection_confidence = float(os.getenv("FACE_REGISTER_DETECTION_CONFIDENCE", "0.80"))
        self.recognize_detection_confidence = float(os.getenv("FACE_RECOGNIZE_DETECTION_CONFIDENCE", "0.60"))
        self.min_focus = float(os.getenv("FACE_MIN_FOCUS", os.getenv("FACE_REGISTER_MIN_FOCUS", "8.0")))
        self.min_face_side = int(os.getenv("FACE_MIN_FACE_SIDE_PX", "80"))
        self.recognize_min_face_side = int(os.getenv("FACE_RECOGNIZE_MIN_FACE_SIDE_PX", "32"))
        self.register_sample_interval = float(os.getenv("FACE_REGISTER_SAMPLE_INTERVAL_SEC", "0.30"))
        self.match_threshold = float(os.getenv("FACE_MATCH_THRESHOLD", "0.72"))
        self.match_margin = float(os.getenv("FACE_MATCH_MARGIN_THRESHOLD", "0.08"))
        self.match_confirmations = max(1, int(os.getenv("FACE_MATCH_CONFIRMATIONS", "2")))
        self.save_full_frame = _truthy(os.getenv("FACE_SAVE_FULL_FRAME"), True)
        self.show_window = _truthy(os.getenv("FACE_CAMERA_SHOW_WINDOW"), False)


def _ensure_face_table(conn):
    cursor = conn.cursor()
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS face_vectors "
        "(name TEXT PRIMARY KEY, vector BLOB NOT NULL, person_id TEXT, "
        "sample_count INTEGER DEFAULT 1, updated_at REAL DEFAULT 0)"
    )
    cursor.execute("PRAGMA table_info(face_vectors)")
    existing = {row[1] for row in cursor.fetchall()}
    migrations = {
        "person_id": "ALTER TABLE face_vectors ADD COLUMN person_id TEXT",
        "sample_count": "ALTER TABLE face_vectors ADD COLUMN sample_count INTEGER DEFAULT 1",
        "updated_at": "ALTER TABLE face_vectors ADD COLUMN updated_at REAL DEFAULT 0",
        "aligned_vector": "ALTER TABLE face_vectors ADD COLUMN aligned_vector BLOB",
        "embedding_version": "ALTER TABLE face_vectors ADD COLUMN embedding_version TEXT",
    }
    for column, sql in migrations.items():
        if column not in existing:
            cursor.execute(sql)
    conn.commit()
    return cursor


class RKNNFaceEmbeddingModel:
    def __init__(self, model_path):
        from rknnlite.api import RKNNLite

        self.model_path = str(model_path)
        self.rknn = RKNNLite(verbose=False)
        ret = self.rknn.load_rknn(self.model_path)
        if ret != 0:
            raise RuntimeError(f"RKNN load_rknn failed: {self.model_path}, ret={ret}")
        ret = self.rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0)
        if ret != 0:
            raise RuntimeError(f"RKNN init_runtime failed: {self.model_path}, ret={ret}")

    @staticmethod
    def _preprocess(face_image):
        import cv2
        import numpy as np

        resized = cv2.resize(face_image, (160, 160))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = (rgb.astype(np.float32) - 127.5) / 128.0
        return tensor[None, :, :, :].astype(np.float32)

    def extract(self, face_image):
        import numpy as np

        outputs = self.rknn.inference(inputs=[self._preprocess(face_image)])
        if not outputs:
            raise RuntimeError("RKNN inference returned no output")
        vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        norm = np.linalg.norm(vector)
        if norm > 1e-8:
            vector = vector / norm
        return vector

    def release(self):
        try:
            self.rknn.release()
        except Exception:
            pass


def _flip_code():
    value = os.getenv("FACE_CAMERA_FLIP", "").strip().lower()
    if value in {"0", "vertical", "v", "true", "yes", "on"}:
        return 0
    if value in {"1", "horizontal", "h"}:
        return 1
    if value in {"-1", "both", "hv", "vh"}:
        return -1
    return None


def _read_frame(cap):
    import cv2

    ok, frame = cap.read()
    if ok and frame is not None:
        code = _flip_code()
        if code is not None:
            frame = cv2.flip(frame, code)
    return ok, frame


def _resolve_camera_source(source, cfg):
    requested = str(source if source is not None else cfg.camera).strip()
    normalized = requested.lower().replace("-", "_").replace(" ", "_")
    if normalized in {"front", "front_camera", "frontcamera", "前", "前摄", "前置", "前置摄像头", "前方"}:
        return requested, cfg.front_camera
    if normalized in {"back", "rear", "back_camera", "rearcamera", "后", "后摄", "后置", "后置摄像头", "后方"}:
        return requested, cfg.back_camera
    if requested.isdigit():
        return requested, f"/dev/video{int(requested)}"
    return requested, requested


def _open_camera(source, cfg):
    import cv2

    requested, resolved = _resolve_camera_source(source, cfg)
    attempts = []
    for attempt in range(1, cfg.camera_open_attempts + 1):
        cap = cv2.VideoCapture(resolved)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        opened = bool(cap.isOpened())
        read_ok = False
        if opened:
            for _ in range(cfg.camera_probe_reads):
                read_ok, frame = cap.read()
                if read_ok and frame is not None:
                    break
                time.sleep(0.05)
        attempts.append({"attempt": attempt, "opened": opened, "read_ok": bool(read_ok)})
        if opened and read_ok:
            return cap, {
                "requested": requested,
                "resolved": str(resolved),
                "attempts": attempts,
                "open_retry_sec": cfg.camera_open_retry_sec,
            }
        cap.release()
        if attempt < cfg.camera_open_attempts and cfg.camera_open_retry_sec:
            time.sleep(cfg.camera_open_retry_sec)
    return cv2.VideoCapture(), {
        "requested": requested,
        "resolved": str(resolved),
        "attempts": attempts,
        "open_retry_sec": cfg.camera_open_retry_sec,
    }


def _crop_face(image, bbox):
    ih, iw, _ = image.shape
    raw_x = int(bbox.xmin * iw)
    raw_y = int(bbox.ymin * ih)
    raw_w = int(bbox.width * iw)
    raw_h = int(bbox.height * ih)
    pad_w = int(raw_w * 0.2)
    pad_h = int(raw_h * 0.2)
    x1 = max(0, raw_x - pad_w)
    y1 = max(0, raw_y - pad_h)
    x2 = min(iw, raw_x + raw_w + pad_w)
    y2 = min(ih, raw_y + raw_h + pad_h)
    return image[y1:y2, x1:x2], (raw_x, raw_y, raw_w, raw_h)


def _new_image_session_dir(cfg, kind, name=None):
    stamp = time.strftime("%H%M%S") + f"_{int((time.time() % 1) * 1000):03d}"
    day = time.strftime("%Y%m%d")
    path = cfg.image_dir / kind / day / f"{stamp}_{_safe_name(name)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_image(path, image):
    if image is None:
        return None
    import cv2

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if cv2.imwrite(str(path), image):
        return str(path)
    return None


def _save_face_images(session_dir, prefix, *, frame=None, face_image=None, annotated_frame=None, cfg=None):
    saved = {}
    base = Path(session_dir)
    prefix = _safe_name(prefix)
    face_path = _save_image(base / f"{prefix}_face.jpg", face_image)
    if face_path:
        saved["face"] = face_path
    if cfg is None or cfg.save_full_frame:
        frame_path = _save_image(base / f"{prefix}_frame.jpg", frame)
        if frame_path:
            saved["frame"] = frame_path
    annotated_path = _save_image(base / f"{prefix}_annotated.jpg", annotated_frame)
    if annotated_path:
        saved["annotated"] = annotated_path
    return saved


def _write_identity_event(cfg, event):
    payload = dict(event or {})
    now = time.time()
    payload.setdefault("timestamp", now)
    payload.setdefault("expires_at", now + 3.0)
    payload.setdefault("source", "face_recognition")
    tmp = cfg.identity_event_path.with_suffix(cfg.identity_event_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cfg.identity_event_path)
    return payload


def _annotate(image, bbox_tuple, text, color):
    import cv2

    vis = image.copy()
    x, y, w, h = bbox_tuple
    cv2.rectangle(vis, (x, y), (x + w, y + h), color, 2)
    cv2.putText(vis, text, (x, max(20, y - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return vis


def register_face(cfg, name, camera=None, start_gate_path=None):
    import cv2
    import numpy as np

    target_name = str(name or "").strip()
    if not target_name:
        return {"status": "error", "error": "missing_name"}
    if not cfg.model_path.exists():
        return {"status": "error", "error": f"model_not_found: {cfg.model_path}"}
    if not cfg.detector_model_path.exists():
        return {"status": "error", "error": f"detector_model_not_found: {cfg.detector_model_path}"}

    session_dir = _new_image_session_dir(cfg, "registration", target_name)
    saved_images = []
    debug = {"frames": 0, "detected_frames": 0, "multi_face_frames": 0, "too_small_frames": 0, "blurry_frames": 0, "sampled_vectors": 0}
    cap, camera_debug = _open_camera(camera or cfg.camera, cfg)
    debug["camera"] = camera_debug
    if not cap.isOpened():
        return {
            "status": "error",
            "error": "camera_open_failed",
            "camera": camera_debug,
            "image_dir": str(session_dir),
            "images": saved_images,
        }

    model = detector = None
    last_snapshot = None
    collected_aligned = []
    collected_legacy = []
    first_face_seen = False
    last_extract_ts = 0.0
    try:
        model = RKNNFaceEmbeddingModel(cfg.model_path)
        detector = RetinaFaceRKNN(cfg.detector_model_path)
        if start_gate_path:
            _single_function_emit_ready("face_registration", "请面对摄像头，保持不动，我开始注册。")
            _single_function_wait_start_gate(start_gate_path)
        deadline = time.time() + cfg.register_timeout
        no_face_deadline = time.time() + 5.0
        while time.time() < deadline and len(collected_aligned) < cfg.required_samples:
            ok, frame = _read_frame(cap)
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            debug["frames"] += 1
            detections = detector.detect(frame, cfg.register_detection_confidence)
            if not detections:
                if time.time() > no_face_deadline and not first_face_seen:
                    break
                continue
            first_face_seen = True
            debug["detected_frames"] += 1
            valid = [item for item in detections if valid_face_geometry(item, cfg.min_face_side)]
            debug["rejected_detections"] = debug.get("rejected_detections", 0) + len(detections) - len(valid)
            if not valid:
                debug["too_small_frames"] += 1
                continue
            if len(valid) > 1:
                debug["multi_face_frames"] += 1
                continue
            detection = valid[0]
            x1, y1, x2, y2 = detection["box"]
            box = (x1, y1, x2 - x1, y2 - y1)
            debug["last_face_box"] = {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}
            debug["last_detection_score"] = detection["score"]
            debug["last_detector_inference_ms"] = detection["inference_ms"]
            aligned_face = align_face(frame, detection)
            legacy_face = padded_face_crop(frame, detection)
            focus = cv2.Laplacian(cv2.cvtColor(aligned_face, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            debug["last_focus"] = float(focus)
            annotated = _annotate(frame, box, f"sample {len(collected_aligned) + 1}/{cfg.required_samples}", (0, 255, 0))
            last_snapshot = {"frame": frame.copy(), "face": aligned_face.copy(), "annotated": annotated}
            if focus <= cfg.min_focus:
                debug["blurry_frames"] += 1
                continue
            now = time.time()
            if now - last_extract_ts < cfg.register_sample_interval:
                continue
            collected_aligned.append(model.extract(aligned_face))
            collected_legacy.append(model.extract(legacy_face))
            debug["sampled_vectors"] = len(collected_aligned)
            paths = _save_face_images(
                session_dir,
                f"sample_{len(collected_aligned):02d}",
                frame=frame,
                face_image=aligned_face,
                annotated_frame=annotated,
                cfg=cfg,
            )
            if paths:
                saved_images.append(paths)
            last_extract_ts = now

        if not saved_images and last_snapshot is not None:
            paths = _save_face_images(
                session_dir,
                "final",
                frame=last_snapshot["frame"],
                face_image=last_snapshot["face"],
                annotated_frame=last_snapshot["annotated"],
                cfg=cfg,
            )
            if paths:
                saved_images.append(paths)
        debug["image_dir"] = str(session_dir)
        debug["saved_images"] = saved_images

        if len(collected_aligned) < cfg.required_samples:
            return {
                "status": "insufficient_face_samples" if first_face_seen else "no_face",
                "reason": "detected_face_but_not_enough_valid_samples" if first_face_seen else "no_detection",
                "required_samples": cfg.required_samples,
                "sample_count": len(collected_aligned),
                "image_dir": str(session_dir),
                "images": saved_images,
                "debug": debug,
            }

        aligned_vector = np.mean(np.asarray(collected_aligned), axis=0)
        aligned_norm = np.linalg.norm(aligned_vector)
        if aligned_norm > 1e-8:
            aligned_vector = aligned_vector / aligned_norm
        legacy_vector = np.mean(np.asarray(collected_legacy), axis=0)
        legacy_norm = np.linalg.norm(legacy_vector)
        if legacy_norm > 1e-8:
            legacy_vector = legacy_vector / legacy_norm
        with sqlite3.connect(cfg.db_path, check_same_thread=False) as conn:
            cursor = _ensure_face_table(conn)
            cursor.execute(
                "INSERT OR REPLACE INTO face_vectors "
                "(name, vector, person_id, sample_count, updated_at, aligned_vector, embedding_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    target_name,
                    pickle.dumps(legacy_vector),
                    _person_id_for_name(target_name),
                    len(collected_aligned),
                    time.time(),
                    pickle.dumps(aligned_vector),
                    EMBEDDING_VERSION,
                ),
            )
            conn.commit()
        return {
            "status": "success",
            "name": target_name,
            "person_id": _person_id_for_name(target_name),
            "sample_count": len(collected_aligned),
            "embedding_version": EMBEDDING_VERSION,
            "image_dir": str(session_dir),
            "images": saved_images,
            "debug": debug,
        }
    finally:
        cap.release()
        if model is not None:
            model.release()
        if detector is not None:
            detector.release()


def _load_known_faces(cfg):
    with sqlite3.connect(cfg.db_path, check_same_thread=False) as conn:
        cursor = _ensure_face_table(conn)
        cursor.execute("SELECT name, vector, person_id, aligned_vector, embedding_version FROM face_vectors")
        rows = cursor.fetchall()
    faces = []
    for name, legacy_blob, person_id, aligned_blob, embedding_version in rows:
        if name == PENDING_FACE_KEY:
            continue
        blob = aligned_blob if aligned_blob is not None else legacy_blob
        faces.append(
            {
                "name": name,
                "vector": pickle.loads(blob),
                "person_id": person_id or _person_id_for_name(name),
                "embedding_version": embedding_version or "legacy_unaligned",
            }
        )
    return faces


def recognize_face(cfg, camera=None, start_gate_path=None):
    import cv2
    import numpy as np

    known_faces = _load_known_faces(cfg)
    session_dir = _new_image_session_dir(cfg, "recognition", "session")
    saved_images = []
    if not known_faces:
        result = {"status": "empty_db", "image_dir": str(session_dir), "images": saved_images}
        return _write_identity_event(cfg, result)
    if not cfg.model_path.exists():
        return {"status": "error", "error": f"model_not_found: {cfg.model_path}", "image_dir": str(session_dir), "images": saved_images}
    if not cfg.detector_model_path.exists():
        return {"status": "error", "error": f"detector_model_not_found: {cfg.detector_model_path}", "image_dir": str(session_dir), "images": saved_images}

    cap, camera_debug = _open_camera(camera or cfg.camera, cfg)
    if not cap.isOpened():
        return {
            "status": "error",
            "error": "camera_open_failed",
            "camera": camera_debug,
            "image_dir": str(session_dir),
            "images": saved_images,
        }

    model = detector = None
    debug = {
        "frames": 0,
        "detected_frames": 0,
        "blurry_frames": 0,
        "infer_attempts": 0,
        "known_faces": len(known_faces),
        "threshold": cfg.match_threshold,
        "detection_confidence": cfg.recognize_detection_confidence,
        "detector_model": str(cfg.detector_model_path),
        "embedding_model": str(cfg.model_path),
        "match_confirmations": cfg.match_confirmations,
        "camera": camera_debug,
    }
    last_frame = None
    best_payload = None
    confirmed_payload = None
    saw_face = False
    streak_name = None
    streak_count = 0
    try:
        model = RKNNFaceEmbeddingModel(cfg.model_path)
        detector = RetinaFaceRKNN(cfg.detector_model_path)
        if start_gate_path:
            _single_function_emit_ready("face_recognition", "请看向摄像头，我开始识别。")
            _single_function_wait_start_gate(start_gate_path)
        deadline = time.time() + cfg.recognize_timeout
        last_infer_ts = 0.0
        while time.time() < deadline:
            ok, frame = _read_frame(cap)
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            debug["frames"] += 1
            last_frame = frame.copy()
            detections = detector.detect(frame, cfg.recognize_detection_confidence)
            valid = [item for item in detections if valid_face_geometry(item, cfg.recognize_min_face_side)]
            debug["rejected_detections"] = debug.get("rejected_detections", 0) + len(detections) - len(valid)
            if not valid:
                continue
            saw_face = True
            debug["detected_frames"] += 1
            now = time.time()
            if now - last_infer_ts < 0.12:
                continue
            last_infer_ts = now
            frame_candidates = []
            for detection in valid:
                aligned_face = align_face(frame, detection)
                focus = cv2.Laplacian(cv2.cvtColor(aligned_face, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                if focus <= cfg.min_focus:
                    debug["blurry_frames"] += 1
                    continue
                debug["infer_attempts"] += 1
                current_vec = model.extract(aligned_face)
                current_norm = np.linalg.norm(current_vec)
                scores = []
                for known in known_faces:
                    known_norm = np.linalg.norm(known["vector"])
                    score = -1.0 if current_norm <= 1e-8 or known_norm <= 1e-8 else float(np.dot(current_vec, known["vector"]) / (current_norm * known_norm))
                    scores.append({"name": known["name"], "person_id": known["person_id"], "score": score})
                scores.sort(key=lambda item: item["score"], reverse=True)
                best = scores[0]
                second_score = scores[1]["score"] if len(scores) > 1 else -1.0
                margin = best["score"] - second_score if second_score >= 0 else 1.0
                x1, y1, x2, y2 = detection["box"]
                box = (x1, y1, x2 - x1, y2 - y1)
                accepted = best["score"] > cfg.match_threshold and margin >= cfg.match_margin
                label = f"{best['name']} {best['score']:.2f}" if accepted else f"unknown {best['score']:.2f}"
                annotated = _annotate(frame, box, label, (0, 255, 0) if accepted else (0, 0, 255))
                frame_candidates.append(
                    {
                        "best": best,
                        "second_score": second_score,
                        "margin": margin,
                        "detection": detection,
                        "focus": float(focus),
                        "snapshot": {"frame": frame.copy(), "face": aligned_face.copy(), "annotated": annotated},
                    }
                )
            if not frame_candidates:
                continue
            candidate = max(frame_candidates, key=lambda item: item["best"]["score"])
            if best_payload is None or candidate["best"]["score"] > best_payload["best"]["score"]:
                best_payload = candidate
                debug.update(
                    {
                        "best_score": candidate["best"]["score"],
                        "second_score": candidate["second_score"],
                        "margin": candidate["margin"],
                        "best_detection_score": candidate["detection"]["score"],
                        "best_face_box": candidate["detection"]["box"],
                        "best_focus": candidate["focus"],
                    }
                )
            accepted = candidate["best"]["score"] > cfg.match_threshold and candidate["margin"] >= cfg.match_margin
            if accepted:
                name = candidate["best"]["name"]
                streak_count = streak_count + 1 if name == streak_name else 1
                streak_name = name
                debug["confirmation_streak"] = streak_count
                if streak_count >= cfg.match_confirmations:
                    confirmed_payload = candidate
                    break
            else:
                streak_name = None
                streak_count = 0

        selected_payload = confirmed_payload or best_payload
        if confirmed_payload is not None:
            best = confirmed_payload["best"]
            label = best["name"]
            result = {
                "status": "matched",
                "name": best["name"],
                "person_id": best["person_id"],
                "score": best["score"],
                "second_score": confirmed_payload["second_score"],
                "margin": confirmed_payload["margin"],
            }
        elif best_payload and best_payload["best"]["score"] > cfg.match_threshold:
            best = best_payload["best"]
            label = best["name"]
            result = {
                "status": "uncertain",
                "name": best["name"],
                "person_id": best["person_id"],
                "score": best["score"],
                "second_score": best_payload["second_score"],
                "margin": best_payload["margin"],
            }
        elif saw_face:
            label = "unknown"
            result = {
                "status": "unknown",
                "score": best_payload["best"]["score"] if best_payload else None,
                "reason": "face_detected_but_no_registered_identity_matched",
            }
        else:
            label = "no_face"
            result = {"status": "no_face", "reason": "no_detection_during_recognition_window"}

        snapshot = selected_payload.get("snapshot") if selected_payload else None
        paths = _save_face_images(
            session_dir,
            f"final_{label}",
            frame=snapshot["frame"] if snapshot else last_frame,
            face_image=snapshot["face"] if snapshot else None,
            annotated_frame=snapshot["annotated"] if snapshot else None,
            cfg=cfg,
        )
        if paths:
            saved_images.append(paths)
        debug["image_dir"] = str(session_dir)
        debug["saved_images"] = saved_images
        result.update({"image_dir": str(session_dir), "images": saved_images, "debug": debug})
        return _write_identity_event(cfg, result)
    finally:
        cap.release()
        if model is not None:
            model.release()
        if detector is not None:
            detector.release()


def _preflight(skill, argv):
    if "--dry-run" in argv:
        _json_emit(True, "dry_run", skill, "default", {"argv": argv})
        raise SystemExit(0)
    if "--json" in argv:
        argv = [a for a in argv if a != "--json"]
    if "--timeout" in argv:
        idx = argv.index("--timeout")
        if idx + 1 < len(argv):
            os.environ["SINGLE_FUNCTION_TIMEOUT"] = argv[idx + 1]
            del argv[idx:idx + 2]
    for index, arg in list(enumerate(argv)):
        if arg.startswith("--timeout="):
            os.environ["SINGLE_FUNCTION_TIMEOUT"] = arg.split("=", 1)[1]
            del argv[index]
            break
    return argv


def run_registration_cli(skill_dir):
    started = time.time()
    argv = _preflight("face_registration", list(__import__("sys").argv[1:]))
    if os.getenv("SINGLE_FUNCTION_TIMEOUT") and not os.getenv("FACE_REGISTER_TIMEOUT_SEC"):
        os.environ["FACE_REGISTER_TIMEOUT_SEC"] = os.environ["SINGLE_FUNCTION_TIMEOUT"]
    parser = argparse.ArgumentParser(description="Register a face by name.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--camera", default=None)
    parser.add_argument("--start-gate", default=None)
    args = parser.parse_args(argv)
    cfg = FaceConfig(skill_dir)
    result = register_face(cfg, args.name, args.camera, args.start_gate)
    _json_emit(result.get("status") == "success", result.get("status", "error"), "face_registration", "register", result, started_at=started)


def run_recognition_cli(skill_dir):
    started = time.time()
    argv = _preflight("face_recognition", list(__import__("sys").argv[1:]))
    if os.getenv("SINGLE_FUNCTION_TIMEOUT") and not os.getenv("FACE_RECOGNIZE_TIMEOUT_SEC"):
        os.environ["FACE_RECOGNIZE_TIMEOUT_SEC"] = os.environ["SINGLE_FUNCTION_TIMEOUT"]
    parser = argparse.ArgumentParser(description="Recognize a registered face.")
    parser.add_argument("--camera", default=None)
    parser.add_argument("--start-gate", default=None)
    args = parser.parse_args(argv)
    cfg = FaceConfig(skill_dir)
    result = recognize_face(cfg, args.camera, args.start_gate)
    _json_emit(result.get("status") not in {"error"}, result.get("status", "error"), "face_recognition", "recognize", result, started_at=started)
