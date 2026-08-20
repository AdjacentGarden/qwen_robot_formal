#!/usr/bin/env python3
"""The only process allowed to open resident V4L2 camera devices."""

from __future__ import annotations

import contextlib
import json
import mmap
import os
import signal
import struct
import threading
import time
from pathlib import Path

import cv2

from resident_camera_ipc import (
    HEADER, HEADER_SIZE, MAGIC, SLOT_META, SLOT_META_SIZE, VERSION,
)


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "runtime" / "resident"
PID_FILE = STATE / "camera.pid"
STATUS_FILE = STATE / "camera_status.json"
MANIFEST_FILE = STATE / "cameras.json"
SLOTS = 4


def load_cameras() -> dict:
    defaults = {
        "front": {"device": "/dev/video22", "width": 640, "height": 480, "fps": 15.0},
        "back": {"device": "/dev/video31", "width": 640, "height": 480, "fps": 15.0},
    }
    path = ROOT / "config" / "hardware.json"
    try:
        configured = json.loads(path.read_text(encoding="utf-8")).get("cameras") or {}
    except Exception:
        configured = {}
    result = {}
    for name, fallback in defaults.items():
        item = {**fallback, **(configured.get(name) or {})}
        item = {key: item[key] for key in ("device", "width", "height", "fps")}
        item["device"] = str(item["device"])
        item["width"] = int(item["width"])
        item["height"] = int(item["height"])
        item["fps"] = float(item["fps"])
        item["aliases"] = [name, item["device"]]
        item["shared_memory_path"] = f"/dev/shm/v11_resident_camera_{name}.bin"
        result[name] = item
    return result


class CameraRing:
    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.path = Path(config["shared_memory_path"])
        self.max_bytes = int(config["width"] * config["height"] * 3)
        self.total_bytes = HEADER_SIZE + SLOTS * SLOT_META_SIZE + SLOTS * self.max_bytes
        self.file = None
        self.map = None
        self.sequence = 0
        self.generation = int(time.monotonic_ns())
        self.status = "starting"
        self.error = None
        self.frames = 0
        self.reopens = 0
        self.last_frame_ns = 0
        self.cap = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"camera-{name}", daemon=False)
        self._create_map()

    def _create_map(self):
        self.path.unlink(missing_ok=True)
        self.file = self.path.open("w+b", buffering=0)
        self.file.truncate(self.total_bytes)
        os.chmod(self.path, 0o640)
        self.map = mmap.mmap(self.file.fileno(), self.total_bytes, access=mmap.ACCESS_WRITE)
        self._write_header(0, 0, state=0)

    def _write_header(self, latest_seq: int, latest_slot: int, state: int):
        HEADER.pack_into(
            self.map, 0, MAGIC, VERSION, SLOTS, self.max_bytes,
            int(self.config["width"]), int(self.config["height"]), 3,
            int(float(self.config["fps"]) * 1000), int(state),
            int(latest_seq), int(latest_slot), int(self.generation), int(time.monotonic_ns()),
        )

    def _open(self):
        cap = cv2.VideoCapture(self.config["device"])
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"camera_open_failed:{self.config['device']}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config["height"])
        cap.set(cv2.CAP_PROP_FPS, self.config["fps"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap = cap
        self.reopens += 1

    def _publish(self, frame):
        height, width = frame.shape[:2]
        channels = 1 if frame.ndim == 2 else frame.shape[2]
        if channels == 1:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            channels = 3
        if frame.dtype.name != "uint8" or not frame.flags.c_contiguous:
            frame = frame.copy(order="C")
        raw = memoryview(frame).cast("B")
        if len(raw) > self.max_bytes:
            frame = cv2.resize(frame, (self.config["width"], self.config["height"]))
            height, width, channels = frame.shape
            raw = memoryview(frame).cast("B")
        self.sequence += 1
        slot = self.sequence % SLOTS
        meta_offset = HEADER_SIZE + slot * SLOT_META_SIZE
        data_offset = HEADER_SIZE + SLOTS * SLOT_META_SIZE + slot * self.max_bytes
        odd = self.sequence * 2 - 1
        SLOT_META.pack_into(self.map, meta_offset, odd, 0, 0, 0, 0, 0)
        self.map[data_offset:data_offset + len(raw)] = raw
        timestamp_ns = time.monotonic_ns()
        SLOT_META.pack_into(
            self.map, meta_offset, self.sequence * 2, timestamp_ns,
            int(width), int(height), int(channels), int(len(raw)),
        )
        self.last_frame_ns = timestamp_ns
        self.frames += 1
        self._write_header(self.sequence, slot, state=1)

    def _close_capture(self):
        if self.cap is not None:
            with contextlib.suppress(Exception):
                self.cap.release()
            self.cap = None

    def _loop(self):
        failures = 0
        while not self.stop.is_set():
            try:
                if self.cap is None:
                    self.status = "opening"
                    self._open()
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    raise RuntimeError(f"camera_read_failed:{self.config['device']}")
                failures = 0
                self.error = None
                self.status = "ready"
                self._publish(frame)
            except Exception as exc:
                failures += 1
                self.error = f"{type(exc).__name__}:{exc}"
                self.status = "unavailable"
                self._write_header(self.sequence, self.sequence % SLOTS, state=0)
                self._close_capture()
                self.stop.wait(min(2.0, 0.1 * (2 ** min(failures, 4))))
        self._close_capture()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3.0)
        self._close_capture()
        if self.map is not None:
            with contextlib.suppress(Exception):
                self._write_header(self.sequence, self.sequence % SLOTS, state=0)
                self.map.flush()
                self.map.close()
            self.map = None
        if self.file is not None:
            with contextlib.suppress(Exception):
                self.file.close()
            self.file = None
        self.path.unlink(missing_ok=True)

    def snapshot(self):
        age_ms = None if not self.last_frame_ns else round((time.monotonic_ns() - self.last_frame_ns) / 1e6, 3)
        return {
            "device": self.config["device"], "state": self.status, "error": self.error,
            "frames": self.frames, "sequence": self.sequence, "age_ms": age_ms,
            "reopens": self.reopens, "width": self.config["width"],
            "height": self.config["height"], "fps": self.config["fps"],
            "shared_memory_path": str(self.path),
        }


def atomic_json(path: Path, payload: dict):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    cameras = load_cameras()
    manifest = {"version": VERSION, "broker_pid": os.getpid(), "created_at": time.time(), "cameras": cameras}
    atomic_json(MANIFEST_FILE, manifest)
    rings = {name: CameraRing(name, config) for name, config in cameras.items()}
    stopping = threading.Event()

    def stop(_sig, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    for ring in rings.values():
        ring.start()
    try:
        while not stopping.wait(0.2):
            snapshots = {name: ring.snapshot() for name, ring in rings.items()}
            ready = sum(item["state"] == "ready" for item in snapshots.values())
            atomic_json(STATUS_FILE, {
                "state": "ready" if ready == len(rings) else "degraded",
                "pid": os.getpid(), "updated_at": time.time(), "ready_count": ready,
                "camera_count": len(rings), "cameras": snapshots,
            })
        return 0
    finally:
        for ring in rings.values():
            ring.close()
        atomic_json(STATUS_FILE, {"state": "stopped", "pid": os.getpid(), "updated_at": time.time()})
        MANIFEST_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

