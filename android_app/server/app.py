#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
VIDEOS = DATA / "videos"
THUMBS = DATA / "thumbs"
MAP_FILE = DATA / "map.png"
STATE_FILE = DATA / "state.json"
VIDEO_INDEX = DATA / "videos.json"
for directory in (DATA, VIDEOS, THUMBS):
    directory.mkdir(parents=True, exist_ok=True)

APP_TOKEN = os.environ.get("ROBOT_APP_TOKEN", "")
ROBOT_TOKEN = os.environ.get("ROBOT_BRIDGE_TOKEN", "")
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_BYTES", str(80 * 1024 * 1024)))
MAX_VOICE_BYTES = int(os.environ.get("MAX_VOICE_BYTES", str(4 * 1024 * 1024)))
ROBOT_STALE_SECONDS = float(os.environ.get("ROBOT_STALE_SECONDS", "12.0"))
ROBOT_DISCONNECT_GRACE_SECONDS = float(os.environ.get("ROBOT_DISCONNECT_GRACE_SECONDS", "3.0"))
SERVER_HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("SERVER_HEARTBEAT_INTERVAL_SECONDS", "1.0"))
APP_SEND_TIMEOUT_SECONDS = float(os.environ.get("APP_SEND_TIMEOUT_SECONDS", "0.8"))


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def require_configured() -> None:
    if not APP_TOKEN or not ROBOT_TOKEN:
        raise RuntimeError("ROBOT_APP_TOKEN and ROBOT_BRIDGE_TOKEN must be configured")


class Hub:
    def __init__(self) -> None:
        self.robot: Optional[WebSocket] = None
        self.apps: Set[WebSocket] = set()
        persisted = load_json(STATE_FILE, {})
        self.pose = persisted.get("pose")
        self.map_meta = persisted.get("map")
        self.robot_status: Dict[str, Any] = {
            "online": False,
            "last_seen": persisted.get("robot_last_seen"),
            "mode": "offline",
        }
        self.last_event = persisted.get("last_event")
        self.program = persisted.get("program")
        persisted_microphone = persisted.get("microphone")
        self.microphone: Dict[str, Any] = (
            dict(persisted_microphone)
            if isinstance(persisted_microphone, dict)
            else {
                "enabled": True,
                "accepting_local_voice": False,
                "app_voice_enabled": True,
            }
        )
        self.task: Dict[str, Any] = {"active": False, "planning": False, "queued": 0}
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.voice_streams: Dict[str, Dict[str, Any]] = {}
        self.last_persist_monotonic = 0.0
        self.last_broadcast_monotonic = 0.0
        self.robot_last_seen_monotonic = 0.0
        self.robot_generation = 0
        self.robot_disconnect_task: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "type": "state",
            "server_time": time.time(),
            "robot": dict(self.robot_status),
            "program": self.program,
            "microphone": self.microphone,
            "task": self.task,
            "pose": self.pose,
            "map": ({**self.map_meta, "url": "/api/map"} if self.map_meta and MAP_FILE.exists() else None),
            "last_event": self.last_event,
            "videos": load_json(VIDEO_INDEX, []),
        }

    def persist(self) -> None:
        atomic_json(STATE_FILE, {
            "pose": self.pose,
            "map": self.map_meta,
            "robot_last_seen": self.robot_status.get("last_seen"),
            "last_event": self.last_event,
            "program": self.program,
            "microphone": self.microphone,
        })
        self.last_persist_monotonic = time.monotonic()

    def persist_if_due(self, force: bool = False) -> None:
        if force or time.monotonic() - self.last_persist_monotonic >= 1.0:
            self.persist()

    def remember_forwarded(self, command: Dict[str, Any]) -> None:
        command_id = str(command.get("id") or "")[:80]
        if not command_id:
            return
        self.commands[command_id] = {
            "id": command_id,
            "action": str(command.get("action") or ""),
            "state": "forwarded",
            "updated_at": time.time(),
        }
        self.prune_commands()

    def remember_result(self, message: Dict[str, Any]) -> None:
        command_id = str(message.get("id") or "")[:80]
        if not command_id:
            return
        self.commands[command_id] = {
            "id": command_id,
            "action": str(message.get("action") or ""),
            "state": "completed",
            "updated_at": time.time(),
            "message": message,
        }
        self.prune_commands()

    def prune_commands(self) -> None:
        if len(self.commands) <= 200:
            return
        oldest = sorted(self.commands.values(), key=lambda item: float(item.get("updated_at") or 0))[:-160]
        for item in oldest:
            self.commands.pop(str(item.get("id") or ""), None)

    def start_voice_stream(self, command_id: str, owner: int, mime_type: str) -> None:
        now = time.time()
        for stream_id, stream in tuple(self.voice_streams.items()):
            if now - float(stream.get("updated_at") or now) > 35.0:
                self.voice_streams.pop(stream_id, None)
        if len(self.voice_streams) >= 8 and command_id not in self.voice_streams:
            raise ValueError("too_many_voice_streams")
        self.voice_streams[command_id] = {
            "owner": owner, "mime_type": mime_type, "data": bytearray(),
            "started_at": now, "updated_at": now,
        }

    def append_voice_stream(self, command_id: str, owner: int, encoded: str) -> int:
        stream = self.voice_streams.get(command_id)
        if stream is None or int(stream.get("owner") or 0) != owner:
            raise ValueError("voice_stream_not_found")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("invalid_voice_chunk") from exc
        if not chunk:
            return len(stream["data"])
        if len(stream["data"]) + len(chunk) > MAX_VOICE_BYTES:
            self.voice_streams.pop(command_id, None)
            raise ValueError("voice_stream_too_large")
        stream["data"].extend(chunk)
        stream["updated_at"] = time.time()
        return len(stream["data"])

    def finish_voice_stream(self, command_id: str, owner: int) -> Dict[str, Any]:
        stream = self.voice_streams.pop(command_id, None)
        if stream is None or int(stream.get("owner") or 0) != owner:
            raise ValueError("voice_stream_not_found")
        if not stream["data"]:
            raise ValueError("empty_voice_stream")
        return stream

    def abort_voice_streams(self, owner: int, command_id: str = "") -> None:
        for stream_id, stream in tuple(self.voice_streams.items()):
            if int(stream.get("owner") or 0) == owner and (not command_id or stream_id == command_id):
                self.voice_streams.pop(stream_id, None)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        clients = tuple(self.apps)

        async def send_one(client: WebSocket) -> Optional[WebSocket]:
            try:
                await asyncio.wait_for(
                    client.send_text(text),
                    timeout=APP_SEND_TIMEOUT_SECONDS,
                )
                return None
            except Exception:
                return client

        # One slow or suspended phone must never hold up telemetry for every
        # other App client or stop the relay from reading the robot socket.
        stale = await asyncio.gather(*(send_one(client) for client in clients))
        for client in (item for item in stale if item is not None):
            self.apps.discard(client)
            try:
                await asyncio.wait_for(client.close(code=4003), timeout=0.3)
            except Exception:
                pass
        self.last_broadcast_monotonic = time.monotonic()


hub = Hub()
# The protected section contains only synchronous index/file operations.  A
# process-local lock avoids binding an asyncio.Lock at import time, which can
# fail when the module is loaded before an event loop (Python 3.9 tests and
# some ASGI launchers) and is safe because no await occurs while held.
video_lock = threading.RLock()
app = FastAPI(title="理想机器人 Android 中继", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    require_configured()
    app.state.server_heartbeat_task = asyncio.create_task(server_heartbeat_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "server_heartbeat_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def server_heartbeat_loop() -> None:
    """Keep App-to-relay health independent from robot telemetry."""
    while True:
        await asyncio.sleep(SERVER_HEARTBEAT_INTERVAL_SECONDS)
        if hub.apps and time.monotonic() - hub.last_broadcast_monotonic >= SERVER_HEARTBEAT_INTERVAL_SECONDS * 0.8:
            await hub.broadcast({
                "type": "server_heartbeat",
                "server_time": time.time(),
                "robot": dict(hub.robot_status),
            })


def app_authorized(token: str) -> bool:
    return bool(APP_TOKEN) and token == APP_TOKEN


def robot_authorized(token: str) -> bool:
    return bool(ROBOT_TOKEN) and token == ROBOT_TOKEN


def validate_command(raw: Dict[str, Any]) -> Dict[str, Any]:
    action = str(raw.get("action") or "")
    command_id = str(raw.get("id") or uuid.uuid4().hex)
    if action == "manual_move":
        direction = str(raw.get("direction") or "")
        if direction not in {"forward", "backward", "left", "right", "stop"}:
            raise ValueError("invalid_direction")
        return {
            "type": "command", "id": command_id, "action": action,
            "direction": direction,
            "duration": min(0.45, max(0.10, float(raw.get("duration", 0.25)))),
            "linear_speed": min(0.18, max(0.05, float(raw.get("linear_speed", 0.12)))),
            "angular_speed": min(0.65, max(0.15, float(raw.get("angular_speed", 0.42)))),
        }
    if action == "light":
        state = str(raw.get("state") or "")
        if state not in {"on", "off"}:
            raise ValueError("invalid_light_state")
        return {"type": "command", "id": command_id, "action": action, "state": state}
    if action == "feed":
        grams = int(raw.get("grams", 10))
        if grams < 10 or grams > 100 or grams % 10:
            raise ValueError("invalid_feed_grams")
        return {"type": "command", "id": command_id, "action": action, "grams": grams}
    if action == "navigate":
        x, y = float(raw.get("x")), float(raw.get("y"))
        yaw = float(raw.get("yaw", 0.0))
        if not (-100.0 <= x <= 100.0 and -100.0 <= y <= 100.0):
            raise ValueError("navigation_target_out_of_range")
        return {"type": "command", "id": command_id, "action": action, "x": x, "y": y, "yaw": yaw}
    if action == "microphone_set":
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("microphone_enabled_must_be_boolean")
        return {
            "type": "command",
            "id": command_id,
            "action": action,
            "enabled": enabled,
        }
    if action in {"stop", "request_state", "program_start", "program_stop", "program_status"}:
        return {"type": "command", "id": command_id, "action": action}
    raise ValueError("unsupported_action")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/app/")


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    now = time.time()
    last_seen = float(hub.robot_status.get("last_seen") or 0.0)
    return {
        "ok": True,
        "robot_online": bool(hub.robot),
        "robot_last_seen_age_ms": round(max(0.0, now - last_seen) * 1000.0, 1) if last_seen else None,
        "app_clients": len(hub.apps),
        "time": now,
    }


@app.get("/api/state")
async def state(token: str) -> Dict[str, Any]:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return hub.snapshot()


@app.get("/api/map")
async def map_image(token: str) -> FileResponse:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not MAP_FILE.exists():
        raise HTTPException(status_code=404, detail="map_unavailable")
    return FileResponse(str(MAP_FILE), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/videos")
async def video_list(token: str) -> Dict[str, Any]:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True, "videos": load_json(VIDEO_INDEX, [])}


@app.get("/api/commands/{command_id}")
async def command_status(command_id: str, token: str) -> Dict[str, Any]:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", command_id)[:80]
    command = hub.commands.get(normalized)
    if command is None:
        raise HTTPException(status_code=404, detail="command_not_found")
    return {"ok": True, "command": command}


def find_video(video_id: str) -> Dict[str, Any]:
    for item in load_json(VIDEO_INDEX, []):
        if str(item.get("id")) == video_id:
            return item
    raise HTTPException(status_code=404, detail="video_not_found")


@app.get("/api/videos/{video_id}/file")
async def video_file(video_id: str, token: str) -> FileResponse:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    item = find_video(video_id)
    return FileResponse(str(VIDEOS / item["filename"]), media_type="video/mp4", filename=item["filename"])


@app.get("/api/videos/{video_id}/thumb")
async def video_thumb(video_id: str, token: str) -> FileResponse:
    if not app_authorized(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    item = find_video(video_id)
    thumb = THUMBS / item["thumbnail"]
    if not thumb.exists():
        raise HTTPException(status_code=404, detail="thumbnail_unavailable")
    return FileResponse(str(thumb), media_type="image/jpeg")


@app.post("/api/robot/videos")
async def robot_video_upload(request: Request) -> JSONResponse:
    if not robot_authorized(request.headers.get("x-robot-token", "")):
        raise HTTPException(status_code=401, detail="unauthorized")
    length = int(request.headers.get("content-length") or 0)
    if length <= 0 or length > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="invalid_video_size")
    body = await request.body()
    if len(body) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="video_too_large")
    requested_name = request.headers.get("x-video-name", "doudou.mp4")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", requested_name)
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"
    video_id = uuid.uuid4().hex[:16]
    filename = f"{video_id}_{safe_name}"
    destination = VIDEOS / filename
    temporary = destination.with_suffix(".upload")
    temporary.write_bytes(body)
    temporary.replace(destination)
    thumbnail_name = f"{video_id}.jpg"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-ss", "1.0", "-i", str(destination),
        "-frames:v", "1", "-vf", "scale=640:-2", str(THUMBS / thumbnail_name),
    ], check=False, timeout=20)
    item = {
        "id": video_id,
        "filename": filename,
        "thumbnail": thumbnail_name,
        "title": urllib.parse.unquote(request.headers.get("x-video-title", "豆豆找到了")),
        "created_at": float(request.headers.get("x-video-time") or time.time()),
        "duration_sec": float(request.headers.get("x-video-duration") or 5.0),
        "size_bytes": len(body),
        "pose": load_json_header(request.headers.get("x-video-pose")),
        "category": request.headers.get("x-video-category", "pet"),
        "exercise": request.headers.get("x-video-exercise", ""),
        "exercise_label": urllib.parse.unquote(request.headers.get("x-video-exercise-label", "")),
        "count": int(request.headers.get("x-video-count") or 0),
        "identity": urllib.parse.unquote(request.headers.get("x-video-identity", "")),
        "session_state": request.headers.get("x-video-session-state", "completed"),
    }
    with video_lock:
        items = load_json(VIDEO_INDEX, [])
        items.insert(0, item)
        retained, expired = items[:100], items[100:]
        atomic_json(VIDEO_INDEX, retained)
        for old in expired:
            for path in (VIDEOS / str(old.get("filename", "")), THUMBS / str(old.get("thumbnail", ""))):
                if path.is_file():
                    path.unlink()
    await hub.broadcast({"type": "video_available", "video": item})
    return JSONResponse({"ok": True, "video": item})


@app.post("/api/app/voice")
async def app_voice_upload(request: Request) -> JSONResponse:
    """Forward one completed press-to-talk recording to the robot.

    The relay intentionally does not persist microphone audio.  It validates
    and forwards the bytes over the already authenticated robot WebSocket.
    """
    if not app_authorized(request.query_params.get("token", "")):
        raise HTTPException(status_code=401, detail="unauthorized")
    robot = hub.robot
    if robot is None:
        raise HTTPException(status_code=409, detail="robot_offline")
    length = int(request.headers.get("content-length") or 0)
    if length <= 0 or length > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="invalid_voice_size")
    body = await request.body()
    if not body or len(body) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="voice_too_large")
    mime_type = str(request.headers.get("content-type") or "audio/webm").split(";", 1)[0].lower()
    if not (mime_type.startswith("audio/") or mime_type == "application/octet-stream"):
        raise HTTPException(status_code=415, detail="unsupported_voice_format")
    command_id = re.sub(
        r"[^A-Za-z0-9_-]", "_", request.headers.get("x-command-id", uuid.uuid4().hex)
    )[:80]
    command = {
        "type": "command",
        "id": command_id,
        "action": "voice_audio",
        "mime_type": mime_type,
        "duration_ms": min(30000, max(0, int(request.headers.get("x-audio-duration-ms") or 0))),
        "audio_base64": base64.b64encode(body).decode("ascii"),
    }
    try:
        await robot.send_json(command)
        hub.remember_forwarded(command)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="robot_link_failed") from exc
    return JSONResponse({"ok": True, "id": command_id, "status": "forwarded", "size_bytes": len(body)})


def load_json_header(value: Optional[str]) -> Any:
    try:
        return json.loads(value) if value else None
    except Exception:
        return None


@app.websocket("/ws/app")
async def app_socket(ws: WebSocket) -> None:
    if not app_authorized(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.apps.add(ws)
    stream_owner = id(ws)
    await ws.send_json(hub.snapshot())
    try:
        while True:
            raw = await ws.receive_json()
            stream_action = str(raw.get("action") or "")
            if stream_action in {"voice_stream_start", "voice_stream_chunk", "voice_stream_end", "voice_stream_abort"}:
                command_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw.get("id") or ""))[:80]
                try:
                    if not command_id:
                        raise ValueError("invalid_command_id")
                    if stream_action == "voice_stream_start":
                        if hub.robot is None:
                            raise ValueError("robot_offline")
                        mime_type = str(raw.get("mime_type") or "audio/webm").split(";", 1)[0].lower()
                        if not mime_type.startswith("audio/"):
                            raise ValueError("unsupported_voice_format")
                        hub.start_voice_stream(command_id, stream_owner, mime_type)
                        await ws.send_json({"type": "command_ack", "ok": True, "status": "voice_stream_ready", "id": command_id})
                    elif stream_action == "voice_stream_chunk":
                        hub.append_voice_stream(command_id, stream_owner, str(raw.get("data_base64") or ""))
                    elif stream_action == "voice_stream_abort":
                        hub.abort_voice_streams(stream_owner, command_id)
                    else:
                        stream = hub.finish_voice_stream(command_id, stream_owner)
                        robot = hub.robot
                        if robot is None:
                            raise ValueError("robot_offline")
                        body = bytes(stream["data"])
                        command = {
                            "type": "command", "id": command_id, "action": "voice_audio",
                            "mime_type": stream["mime_type"],
                            "duration_ms": min(30000, max(0, int(raw.get("duration_ms") or 0))),
                            "audio_base64": base64.b64encode(body).decode("ascii"),
                            "streamed_upload": True,
                        }
                        await robot.send_json(command)
                        hub.remember_forwarded(command)
                        await ws.send_json({
                            "type": "command_ack", "ok": True, "status": "forwarded",
                            "id": command_id, "size_bytes": len(body), "streamed_upload": True,
                        })
                except Exception as exc:
                    hub.abort_voice_streams(stream_owner, command_id)
                    await ws.send_json({"type": "command_ack", "ok": False, "error": str(exc), "id": command_id})
                continue
            try:
                command = validate_command(raw)
            except Exception as exc:
                await ws.send_json({"type": "command_ack", "ok": False, "error": str(exc), "id": raw.get("id")})
                continue
            robot = hub.robot
            if robot is None:
                await ws.send_json({"type": "command_ack", "ok": False, "error": "robot_offline", "id": command["id"]})
                continue
            try:
                await robot.send_json(command)
                hub.remember_forwarded(command)
                await ws.send_json({"type": "command_ack", "ok": True, "status": "forwarded", "id": command["id"]})
            except Exception:
                await ws.send_json({"type": "command_ack", "ok": False, "error": "robot_link_failed", "id": command["id"]})
    except WebSocketDisconnect:
        pass
    finally:
        hub.abort_voice_streams(stream_owner)
        hub.apps.discard(ws)


@app.websocket("/ws/robot")
async def robot_socket(ws: WebSocket) -> None:
    if not robot_authorized(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.robot_generation += 1
    connection_generation = hub.robot_generation
    if hub.robot_disconnect_task is not None:
        hub.robot_disconnect_task.cancel()
        hub.robot_disconnect_task = None
    previous = hub.robot
    if previous is not None and previous is not ws:
        try:
            await previous.close(code=4001)
        except Exception:
            pass
    hub.robot = ws
    hub.robot_last_seen_monotonic = time.monotonic()
    hub.robot_status.update({"online": True, "last_seen": time.time(), "mode": "connected"})
    await hub.broadcast({"type": "robot_status", "robot": dict(hub.robot_status)})

    async def stale_connection_watchdog() -> None:
        while hub.robot is ws:
            await asyncio.sleep(0.75)
            if time.monotonic() - hub.robot_last_seen_monotonic > ROBOT_STALE_SECONDS:
                await ws.close(code=4002, reason="robot_heartbeat_stale")
                return

    watchdog = asyncio.create_task(stale_connection_watchdog())
    try:
        while True:
            message = await ws.receive_json()
            kind = str(message.get("type") or "")
            hub.robot_last_seen_monotonic = time.monotonic()
            hub.robot_status["last_seen"] = time.time()
            if kind == "telemetry":
                hub.pose = message.get("pose")
                if message.get("program") is not None:
                    hub.program = message.get("program")
                if message.get("task") is not None:
                    hub.task = message.get("task")
                if isinstance(message.get("microphone"), dict):
                    hub.microphone = dict(message["microphone"])
                hub.robot_status["mode"] = message.get("mode", "ready")
                await hub.broadcast({"type": "telemetry", "pose": hub.pose, "robot": dict(hub.robot_status), "program": hub.program, "task": hub.task, "microphone": hub.microphone})
            elif kind == "link_heartbeat":
                if message.get("program") is not None:
                    hub.program = message.get("program")
                if message.get("task") is not None:
                    hub.task = message.get("task")
                if isinstance(message.get("microphone"), dict):
                    hub.microphone = dict(message["microphone"])
                hub.robot_status["mode"] = "ready"
                await hub.broadcast({
                    "type": "link_heartbeat",
                    "robot": dict(hub.robot_status),
                    "program": hub.program,
                    "task": hub.task,
                    "microphone": hub.microphone,
                    "telemetry_age_ms": message.get("telemetry_age_ms"),
                    "timestamp": message.get("timestamp"),
                })
            elif kind == "map":
                import base64
                encoded = message.get("image_base64")
                if not isinstance(encoded, str) or len(encoded) > 12_000_000:
                    continue
                MAP_FILE.write_bytes(base64.b64decode(encoded))
                hub.map_meta = dict(message.get("meta") or {})
                hub.map_meta["updated_at"] = time.time()
                await hub.broadcast({"type": "map_update", "map": {**hub.map_meta, "url": "/api/map"}})
            elif kind == "program_status":
                hub.program = message.get("program")
                await hub.broadcast(message)
            elif kind in {"event", "command_result"}:
                result = message.get("result") or {}
                if isinstance(result, dict) and result.get("program") is not None:
                    hub.program = result.get("program")
                if isinstance(result, dict) and isinstance(result.get("microphone"), dict):
                    hub.microphone = dict(result["microphone"])
                hub.last_event = message
                if kind == "command_result":
                    hub.remember_result(message)
                await hub.broadcast(message)
            elif kind == "hello":
                hub.robot_status.update(message.get("robot") or {})
                if message.get("program") is not None:
                    hub.program = message.get("program")
                await hub.broadcast({"type": "robot_status", "robot": dict(hub.robot_status)})
            hub.persist_if_due(force=kind in {"map", "program_status", "event", "command_result", "hello"})
    except WebSocketDisconnect:
        pass
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        if hub.robot is ws:
            hub.robot = None
            hub.task = {"active": False, "planning": False, "queued": 0}
            hub.robot_status.update({"online": False, "mode": "reconnecting", "last_seen": time.time()})
            hub.persist()
            await hub.broadcast({"type": "robot_status", "robot": dict(hub.robot_status)})

            async def finalize_offline(expected_generation: int) -> None:
                try:
                    await asyncio.sleep(ROBOT_DISCONNECT_GRACE_SECONDS)
                    if hub.robot is None and hub.robot_generation == expected_generation:
                        hub.robot_status.update({"online": False, "mode": "offline", "last_seen": time.time()})
                        hub.persist()
                        await hub.broadcast({"type": "robot_status", "robot": dict(hub.robot_status)})
                finally:
                    if hub.robot_generation == expected_generation:
                        hub.robot_disconnect_task = None

            hub.robot_disconnect_task = asyncio.create_task(finalize_offline(connection_generation))


app.mount("/app", StaticFiles(directory=str(WEB), html=True), name="app")
