from __future__ import annotations

import math
import time
from itertools import product
from pathlib import Path

import cv2
import numpy as np
from rknnlite.api import RKNNLite


MODEL_SIZE = 320
ALIGN_SIZE = 160
ALIGN_TEMPLATE = np.asarray(
    [
        [54.7066, 73.8519],
        [105.0454, 73.5734],
        [80.0360, 102.4809],
        [59.3561, 131.9507],
        [100.4957, 131.7201],
    ],
    dtype=np.float32,
)


def _letterbox(image: np.ndarray):
    height, width = image.shape[:2]
    ratio = min(MODEL_SIZE / width, MODEL_SIZE / height)
    resized_width = max(1, int(round(width * ratio)))
    resized_height = max(1, int(round(height * ratio)))
    resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    canvas = np.full((MODEL_SIZE, MODEL_SIZE, 3), 114, dtype=np.uint8)
    offset_x = (MODEL_SIZE - resized_width) // 2
    offset_y = (MODEL_SIZE - resized_height) // 2
    canvas[offset_y : offset_y + resized_height, offset_x : offset_x + resized_width] = resized
    return canvas[..., ::-1], ratio, offset_x, offset_y


def _prior_box() -> np.ndarray:
    anchors: list[float] = []
    for sizes, step in zip(((16, 32), (64, 128), (256, 512)), (8, 16, 32)):
        feature_size = math.ceil(MODEL_SIZE / step)
        for row, col in product(range(feature_size), range(feature_size)):
            for size in sizes:
                anchors.extend(
                    (
                        (col + 0.5) * step / MODEL_SIZE,
                        (row + 0.5) * step / MODEL_SIZE,
                        size / MODEL_SIZE,
                        size / MODEL_SIZE,
                    )
                )
    return np.asarray(anchors, dtype=np.float32).reshape(-1, 4)


PRIORS = _prior_box()


def _decode_boxes(locations: np.ndarray) -> np.ndarray:
    boxes = np.concatenate(
        (
            PRIORS[:, :2] + locations[:, :2] * 0.1 * PRIORS[:, 2:],
            PRIORS[:, 2:] * np.exp(locations[:, 2:] * 0.2),
        ),
        axis=1,
    )
    boxes[:, :2] -= boxes[:, 2:] / 2
    boxes[:, 2:] += boxes[:, :2]
    return boxes


def _decode_landmarks(predictions: np.ndarray) -> np.ndarray:
    points = []
    for offset in range(0, 10, 2):
        points.append(PRIORS[:, :2] + predictions[:, offset : offset + 2] * 0.1 * PRIORS[:, 2:])
    return np.concatenate(points, axis=1)


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = 0.4) -> list[int]:
    if not len(boxes):
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1 + 1) * np.maximum(0.0, y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        index = int(order[0])
        keep.append(index)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[index], x1[rest])
        yy1 = np.maximum(y1[index], y1[rest])
        xx2 = np.minimum(x2[index], x2[rest])
        yy2 = np.minimum(y2[index], y2[rest])
        intersection = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        overlap = intersection / np.maximum(areas[index] + areas[rest] - intersection, 1e-6)
        order = rest[np.where(overlap <= threshold)[0]]
    return keep


class RetinaFaceRKNN:
    def __init__(self, model_path: str | Path, core_mask=None):
        self.model_path = Path(model_path)
        self.rknn = RKNNLite(verbose=False)
        result = self.rknn.load_rknn(str(self.model_path))
        if result != 0:
            raise RuntimeError(f"RetinaFace load_rknn failed: {result}: {self.model_path}")
        mask = RKNNLite.NPU_CORE_1 if core_mask is None else core_mask
        result = self.rknn.init_runtime(core_mask=mask)
        if result != 0:
            raise RuntimeError(f"RetinaFace init_runtime failed: {result}: {self.model_path}")

    def detect(self, image: np.ndarray, score_threshold: float = 0.6):
        model_image, ratio, offset_x, offset_y = _letterbox(image)
        started = time.perf_counter()
        outputs = self.rknn.inference(inputs=[model_image[None, ...]])
        inference_ms = (time.perf_counter() - started) * 1000.0
        if not outputs or len(outputs) != 3:
            raise RuntimeError("RetinaFace inference returned invalid outputs")
        locations, confidence, landmark_predictions = [np.asarray(value).squeeze(0) for value in outputs]
        boxes = _decode_boxes(locations) * MODEL_SIZE
        landmarks = _decode_landmarks(landmark_predictions) * MODEL_SIZE
        scores = confidence[:, 1]
        selected = np.where(scores >= score_threshold)[0]
        boxes, landmarks, scores = boxes[selected], landmarks[selected], scores[selected]
        if len(scores):
            order = scores.argsort()[::-1]
            boxes, landmarks, scores = boxes[order], landmarks[order], scores[order]
            keep = _nms(boxes, scores)
            boxes, landmarks, scores = boxes[keep], landmarks[keep], scores[keep]
        height, width = image.shape[:2]
        if len(boxes):
            boxes[:, 0::2] = np.clip((boxes[:, 0::2] - offset_x) / ratio, 0, width - 1)
            boxes[:, 1::2] = np.clip((boxes[:, 1::2] - offset_y) / ratio, 0, height - 1)
            landmarks[:, 0::2] = np.clip((landmarks[:, 0::2] - offset_x) / ratio, 0, width - 1)
            landmarks[:, 1::2] = np.clip((landmarks[:, 1::2] - offset_y) / ratio, 0, height - 1)
        detections = []
        for box, points, score in zip(boxes, landmarks, scores):
            detections.append(
                {
                    "score": float(score),
                    "box": [int(round(value)) for value in box],
                    "landmarks": [[float(points[index]), float(points[index + 1])] for index in range(0, 10, 2)],
                    "inference_ms": float(inference_ms),
                }
            )
        return detections

    def release(self):
        self.rknn.release()


def valid_face_geometry(detection: dict, minimum_side: int) -> bool:
    x1, y1, x2, y2 = detection["box"]
    width = x2 - x1
    height = y2 - y1
    if min(width, height) < minimum_side or width <= 0 or height <= 0:
        return False
    ratio = width / max(height, 1)
    if ratio < 0.55 or ratio > 1.45:
        return False
    points = np.asarray(detection["landmarks"], dtype=np.float32)
    if points.shape != (5, 2):
        return False
    left_eye, right_eye, nose, left_mouth, right_mouth = points
    if not (left_eye[0] < right_eye[0] and left_mouth[0] < right_mouth[0]):
        return False
    if not (min(left_eye[1], right_eye[1]) < nose[1] < max(left_mouth[1], right_mouth[1])):
        return False
    if np.any(points[:, 0] < x1 - width * 0.1) or np.any(points[:, 0] > x2 + width * 0.1):
        return False
    if np.any(points[:, 1] < y1 - height * 0.1) or np.any(points[:, 1] > y2 + height * 0.1):
        return False
    return True


def align_face(image: np.ndarray, detection: dict) -> np.ndarray:
    source = np.asarray(detection["landmarks"], dtype=np.float32)
    matrix, _ = cv2.estimateAffinePartial2D(source, ALIGN_TEMPLATE, method=cv2.LMEDS)
    if matrix is None:
        raise RuntimeError("face alignment failed")
    return cv2.warpAffine(
        image,
        matrix,
        (ALIGN_SIZE, ALIGN_SIZE),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def padded_face_crop(image: np.ndarray, detection: dict, padding: float = 0.2) -> np.ndarray:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = detection["box"]
    pad_x = int(round((x2 - x1) * padding))
    pad_y = int(round((y2 - y1) * padding))
    x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    x2, y2 = min(width, x2 + pad_x), min(height, y2 + pad_y)
    return image[y1:y2, x1:x2]
