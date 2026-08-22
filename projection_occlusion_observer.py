#!/usr/bin/env python3
"""Qwen-only meeting-projection occlusion observer.

This module is deliberately read-only with respect to robot hardware. It reads
the resident camera's shared-memory feed and the projector state file, calls a
separate Qwen Omni Realtime session, and emits speech events through the main
conversation's existing speaker queue.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import cv2
import numpy as np
import websockets

from robot_skills.resident_camera_ipc import open_capture


DEFAULT_CONFIG_PATH = Path(__file__).with_name("projection_occlusion.json")
DEFAULT_ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3-omni-flash-realtime"
DEFAULT_ALERT = "您挡住投影了，麻烦您往旁边挪一挪。"
DEFAULT_REPEAT_ALERT = "投影画面还被挡着，麻烦您再往旁边挪一点。"

VISION_INSTRUCTIONS = """
你是会议投影遮挡观察器。输入图片中的绿色四边形和半透明绿色区域代表投影有效区域。
只判断是否有真实人物的身体明显进入绿色区域，使投影内容落到人身上或被人体遮挡。
人物只在区域外、很小的手部短暂进入、绿色边框、墙面图案、家具、阴影或曝光变化，
都不能判定为遮挡。无法确定时 blocked 必须为 false。只输出一行 JSON，不要解释：
{"blocked":false,"confidence":0.0,"person_position":"none|left|center|right","suggested_move":"none|left|right","reason":"简短原因"}
""".strip()


@dataclass(frozen=True)
class Detection:
    blocked: bool
    confidence: float
    person_position: str
    suggested_move: str
    reason: str
    latency_ms: float = 0.0


@dataclass(frozen=True)
class Alert:
    kind: str
    text: str
    detection: Detection


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("projection_occlusion_config_not_object")
    roi = value.get("roi")
    if not isinstance(roi, list) or len(roi) != 4:
        raise ValueError("projection_occlusion_roi_requires_four_points")
    normalized = []
    for point in roi:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("projection_occlusion_roi_invalid_point")
        x, y = float(point[0]), float(point[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("projection_occlusion_roi_out_of_range")
        normalized.append((x, y))
    value["roi"] = normalized
    return value


def projector_session_active(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(isinstance(value, dict) and value.get("session_active"))


def annotate_projection_roi(
    frame: np.ndarray,
    roi: Iterable[tuple[float, float]],
) -> np.ndarray:
    if frame is None or frame.size == 0:
        raise ValueError("empty_projection_frame")
    height, width = frame.shape[:2]
    points = np.array(
        [[round(x * (width - 1)), round(y * (height - 1))] for x, y in roi],
        dtype=np.int32,
    )
    overlay = frame.copy()
    cv2.fillPoly(overlay, [points], (0, 160, 0))
    output = cv2.addWeighted(overlay, 0.18, frame, 0.82, 0.0)
    cv2.polylines(output, [points], True, (0, 255, 0), 4, cv2.LINE_AA)
    anchor = tuple(points[0])
    cv2.putText(
        output,
        "PROJECTED IMAGE AREA",
        (int(anchor[0]), max(24, int(anchor[1]) - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    return output


def encode_jpeg(frame: np.ndarray, max_bytes: int = 190_000) -> bytes:
    resized = frame
    height, width = resized.shape[:2]
    if max(height, width) > 960:
        scale = 960.0 / max(height, width)
        resized = cv2.resize(resized, (round(width * scale), round(height * scale)))
    encoded = None
    for quality in (88, 80, 72, 64, 56):
        ok, candidate = cv2.imencode(
            ".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
        if not ok:
            continue
        encoded = candidate
        if len(candidate) <= max_bytes:
            return candidate.tobytes()
    if encoded is None:
        raise RuntimeError("projection_jpeg_encode_failed")
    return encoded.tobytes()


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        with contextlib.suppress(json.JSONDecodeError):
            value, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(value, dict):
                return value
    raise ValueError("projection_model_response_has_no_json")


def parse_detection(text: str, latency_ms: float = 0.0) -> Detection:
    value = _extract_json(text)
    raw_blocked = value.get("blocked", False)
    blocked = (
        raw_blocked.strip().lower() in {"true", "1", "yes", "是"}
        if isinstance(raw_blocked, str)
        else bool(raw_blocked)
    )
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    position = str(value.get("person_position") or "none").lower()
    move = str(value.get("suggested_move") or "none").lower()
    if position not in {"none", "left", "center", "right"}:
        position = "none"
    if move not in {"none", "left", "right"}:
        move = "none"
    return Detection(
        blocked=blocked,
        confidence=confidence,
        person_position=position,
        suggested_move=move,
        reason=str(value.get("reason") or "").strip()[:160],
        latency_ms=latency_ms,
    )


class OcclusionStateMachine:
    def __init__(
        self,
        *,
        blocked_required: int = 2,
        clear_required: int = 2,
        confidence_threshold: float = 0.70,
        immediate_confidence_threshold: float = 0.92,
        repeat_seconds: float = 12.0,
    ) -> None:
        self.blocked_required = max(1, int(blocked_required))
        self.clear_required = max(1, int(clear_required))
        self.confidence_threshold = float(confidence_threshold)
        self.immediate_confidence_threshold = max(
            self.confidence_threshold,
            float(immediate_confidence_threshold),
        )
        self.repeat_seconds = max(3.0, float(repeat_seconds))
        self.blocked_streak = 0
        self.clear_streak = 0
        self.is_blocked = False
        self.last_alert_at = float("-inf")

    def reset(self) -> None:
        self.blocked_streak = 0
        self.clear_streak = 0
        self.is_blocked = False
        self.last_alert_at = float("-inf")

    def update(self, detection: Detection, now: float | None = None) -> list[Alert]:
        current = time.monotonic() if now is None else float(now)
        positive = detection.blocked and detection.confidence >= self.confidence_threshold
        if positive:
            self.blocked_streak += 1
            self.clear_streak = 0
            immediate = detection.confidence >= self.immediate_confidence_threshold
            if not self.is_blocked and (
                immediate or self.blocked_streak >= self.blocked_required
            ):
                self.is_blocked = True
                self.last_alert_at = current
                return [Alert("blocked", DEFAULT_ALERT, detection)]
            if self.is_blocked and current - self.last_alert_at >= self.repeat_seconds:
                self.last_alert_at = current
                return [Alert("blocked_repeat", DEFAULT_REPEAT_ALERT, detection)]
            return []
        self.clear_streak += 1
        self.blocked_streak = 0
        if self.is_blocked and self.clear_streak >= self.clear_required:
            self.is_blocked = False
            return [Alert("clear", "", detection)]
        return []


async def _websocket_connect(url: str, api_key: str):
    keyword = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )
    return await websockets.connect(
        url,
        open_timeout=10,
        close_timeout=3,
        max_size=12 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
        **{
            keyword: {
                "Authorization": f"Bearer {api_key}",
                "x-dashscope-dataInspection": "disable",
            }
        },
    )


class QwenVisionSession:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = 15.0,
        rotate_after: int = 3,
    ) -> None:
        separator = "&" if "?" in endpoint else "?"
        self.url = endpoint + separator + "model=" + model
        self.api_key = api_key
        self.timeout = float(timeout)
        self.rotate_after = min(7, max(1, int(rotate_after)))
        self.websocket: Any = None
        self.turns = 0
        self._rotation_task: asyncio.Task[Any] | None = None

    async def _open_session(self):
        websocket = await _websocket_connect(self.url, self.api_key)
        try:
            await self._receive_until({"session.created"}, websocket=websocket)
            await websocket.send(
                json.dumps(
                    {
                        "event_id": "projection_" + uuid.uuid4().hex,
                        "type": "session.update",
                        "session": {
                            "modalities": ["text"],
                            "instructions": VISION_INSTRUCTIONS,
                            "input_audio_format": "pcm",
                            "turn_detection": None,
                        },
                    },
                    ensure_ascii=False,
                )
            )
            await self._receive_until({"session.updated"}, websocket=websocket)
            return websocket
        except BaseException:
            with contextlib.suppress(Exception):
                await websocket.close()
            raise

    def _start_background_rotation(self) -> None:
        if self._rotation_task is None:
            self._rotation_task = asyncio.create_task(
                self._open_session(),
                name="projection-qwen-session-rotation",
            )

    async def close(self) -> None:
        rotation, self._rotation_task = self._rotation_task, None
        if rotation is not None:
            if not rotation.done():
                rotation.cancel()
            result = await asyncio.gather(rotation, return_exceptions=True)
            if result and not isinstance(result[0], BaseException):
                with contextlib.suppress(Exception):
                    await result[0].close()
        if self.websocket is not None:
            with contextlib.suppress(Exception):
                await self.websocket.close()
        self.websocket = None
        self.turns = 0

    async def _receive_until(
        self,
        wanted: set[str],
        *,
        websocket: Any | None = None,
    ) -> dict[str, Any]:
        websocket = self.websocket if websocket is None else websocket
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("projection_qwen_wait_timeout")
            event = json.loads(await asyncio.wait_for(websocket.recv(), remaining))
            if event.get("type") == "error":
                error = event.get("error") or event
                raise RuntimeError(
                    f"{error.get('code') or 'qwen_error'}:{error.get('message') or error}"
                )
            if event.get("type") in wanted:
                return event

    async def _ensure_connected(self) -> None:
        if self.websocket is None:
            self.websocket = await self._open_session()
            self.turns = 0
            return
        rotation = self._rotation_task
        if rotation is None or not rotation.done():
            # Continue using the healthy current session while its replacement
            # connects.  Rotation must never create a blind 10-second window.
            return
        self._rotation_task = None
        try:
            replacement = rotation.result()
        except Exception:
            # A transient connection failure is not a reason to stop seeing.
            # Keep the current session and prepare another replacement later.
            self._start_background_rotation()
            return
        previous, self.websocket = self.websocket, replacement
        self.turns = 0
        asyncio.create_task(previous.close())

    async def detect(self, jpeg: bytes) -> Detection:
        await self._ensure_connected()
        started = time.monotonic()
        silence = base64.b64encode(bytes(32000)).decode("ascii")
        image = base64.b64encode(jpeg).decode("ascii")
        for event in (
            {"type": "input_audio_buffer.append", "audio": silence},
            {"type": "input_image_buffer.append", "image": image},
            {"type": "input_audio_buffer.commit"},
        ):
            await self.websocket.send(json.dumps(event))
        await self._receive_until({"input_audio_buffer.committed"})
        await self.websocket.send(
            json.dumps({"type": "response.create", "response": {"modalities": ["text"]}})
        )
        pieces: list[str] = []
        while True:
            event = await self._receive_until(
                {"response.text.delta", "response.text.done", "response.done"}
            )
            kind = event.get("type")
            if kind == "response.text.delta":
                pieces.append(str(event.get("delta") or ""))
            elif kind == "response.text.done" and not pieces:
                pieces.append(str(event.get("text") or ""))
            elif kind == "response.done":
                break
        self.turns += 1
        if self.turns >= self.rotate_after:
            self._start_background_rotation()
        return parse_detection("".join(pieces), (time.monotonic() - started) * 1000.0)


class ProjectionOcclusionObserver:
    """Watch projector state and run visual checks only during meeting projection."""

    def __init__(
        self,
        api_key: str,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        event_callback: Callable[[dict[str, Any]], bool | None],
        log: Callable[..., None],
        capture_factory: Callable[..., Any] = open_capture,
        session_factory: Callable[..., Any] = QwenVisionSession,
    ) -> None:
        self.config = load_config(config_path)
        self.event_callback = event_callback
        self.log = log
        self.capture_factory = capture_factory
        self.session = session_factory(
            api_key,
            endpoint=str(self.config.get("endpoint") or DEFAULT_ENDPOINT),
            model=str(self.config.get("model") or DEFAULT_MODEL),
            timeout=float(self.config.get("timeout_seconds", 15.0)),
            rotate_after=int(self.config.get("rotate_after", 3)),
        )
        self.state = OcclusionStateMachine(
            blocked_required=int(self.config.get("blocked_confirmations", 2)),
            clear_required=int(self.config.get("clear_confirmations", 2)),
            confidence_threshold=float(self.config.get("confidence_threshold", 0.70)),
            immediate_confidence_threshold=float(
                self.config.get("immediate_confidence_threshold", 0.92)
            ),
            repeat_seconds=float(self.config.get("repeat_seconds", 12.0)),
        )
        self.task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._capture: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.task is not None and not self.task.done(),
            "meeting_active": projector_session_active(Path(self.config["projector_state_file"])),
            "blocked": self.state.is_blocked,
        }

    async def start(self) -> None:
        if not self.enabled or (self.task is not None and not self.task.done()):
            return
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self._run(), name="projection-occlusion-observer")
        self.log("projection_occlusion_started", **self.status())

    async def stop(self) -> None:
        self._stop.set()
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._release_active_resources()
        self.state.reset()
        self.log("projection_occlusion_stopped")

    async def _release_active_resources(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            with contextlib.suppress(Exception):
                capture.release()
        await self.session.close()

    async def _ensure_capture(self):
        if self._capture is None or not self._capture.isOpened():
            if self._capture is not None:
                with contextlib.suppress(Exception):
                    self._capture.release()
            self._capture = await asyncio.to_thread(
                self.capture_factory,
                str(self.config.get("camera") or "/dev/video22"),
            )
        if not self._capture.isOpened():
            raise RuntimeError("projection_camera_unavailable")
        return self._capture

    async def _detect_once(self) -> Detection:
        capture = await self._ensure_capture()
        ok, frame = await asyncio.to_thread(capture.read)
        if not ok or frame is None:
            raise RuntimeError("projection_camera_frame_unavailable")
        annotated = annotate_projection_roi(frame, self.config["roi"])
        return await self.session.detect(encode_jpeg(annotated))

    async def _run(self) -> None:
        state_file = Path(self.config["projector_state_file"])
        interval = max(1.0, float(self.config.get("interval_seconds", 1.0)))
        poll = max(0.1, float(self.config.get("state_poll_seconds", 0.5)))
        last_sample = float("-inf")
        while not self._stop.is_set():
            if not projector_session_active(state_file):
                if self._capture is not None:
                    await self._release_active_resources()
                    self.state.reset()
                    self.log("projection_occlusion_idle")
                await asyncio.sleep(poll)
                continue
            now = time.monotonic()
            if now - last_sample < interval:
                await asyncio.sleep(min(poll, interval - (now - last_sample)))
                continue
            last_sample = now
            try:
                detection = await self._detect_once()
                self.log(
                    "projection_occlusion_detection",
                    blocked=detection.blocked,
                    confidence=detection.confidence,
                    position=detection.person_position,
                    suggested_move=detection.suggested_move,
                    reason=detection.reason,
                    latency_ms=round(detection.latency_ms, 1),
                )
                for alert in self.state.update(detection):
                    if alert.kind == "clear":
                        self.log("projection_occlusion_cleared")
                        continue
                    payload = {
                        "event_id": f"projection-occlusion-{uuid.uuid4().hex}",
                        "skill_name": "projector_control",
                        "kind": "attention",
                        "text": alert.text,
                        "projection_occlusion": True,
                        "confidence": alert.detection.confidence,
                        "position": alert.detection.person_position,
                        "suggested_move": alert.detection.suggested_move,
                    }
                    delivered = bool(self.event_callback(payload))
                    self.log(
                        "projection_occlusion_alert",
                        alert_kind=alert.kind,
                        delivered=delivered,
                        text=alert.text,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(
                    "projection_occlusion_error",
                    error=f"{type(exc).__name__}:{exc}",
                )
                await self._release_active_resources()
                await asyncio.sleep(min(3.0, poll + 0.5))
