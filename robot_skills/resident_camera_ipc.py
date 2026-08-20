#!/usr/bin/env python3
"""Low-latency shared-memory camera transport for the resident skill runtime.

Only the camera broker opens V4L2 devices. Model workers map the broker's ring
buffers read-only and expose a small cv2.VideoCapture-compatible facade.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np


MAGIC = b"V11CAM01"
VERSION = 1
HEADER_SIZE = 4096
SLOT_META_SIZE = 64
HEADER = struct.Struct("<8sIIIIIIIIQQQQ")
SLOT_META = struct.Struct("<QQIIII")
DEFAULT_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = DEFAULT_ROOT / "runtime" / "resident" / "cameras.json"


class SharedCameraError(RuntimeError):
    pass


def manifest_path() -> Path:
    return Path(os.getenv("RESIDENT_CAMERA_MANIFEST", str(DEFAULT_MANIFEST)))


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or manifest_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SharedCameraError(f"camera_broker_unavailable:{target}") from exc
    except Exception as exc:
        raise SharedCameraError(f"camera_manifest_invalid:{type(exc).__name__}:{exc}") from exc
    if int(data.get("version", 0)) != VERSION:
        raise SharedCameraError(f"camera_protocol_mismatch:{data.get('version')}!=1")
    return data


def resolve_camera(source: Any, manifest: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]] | None:
    data = manifest or load_manifest()
    text = str(source)
    for name, item in (data.get("cameras") or {}).items():
        aliases = {str(name), str(item.get("device", "")), *(str(v) for v in item.get("aliases", []))}
        if text in aliases:
            return str(name), dict(item)
    return None


class SharedFrameCapture:
    """A read-only, latest-frame VideoCapture-compatible client."""

    def __init__(self, source: Any, read_timeout: float = 1.5, max_stale: float = 2.0):
        self.source = str(source)
        self.read_timeout = max(0.05, float(read_timeout))
        self.max_stale = max(0.1, float(max_stale))
        self._file = None
        self._map = None
        self._entry: dict[str, Any] = {}
        self._last_sequence = 0
        self._first_timestamp_ns: int | None = None
        self._last_timestamp_ns: int | None = None
        self._opened = False
        self._connect()

    def _connect(self) -> None:
        resolved = resolve_camera(self.source)
        if resolved is None:
            raise SharedCameraError(f"camera_not_managed:{self.source}")
        _name, self._entry = resolved
        path = Path(str(self._entry["shared_memory_path"]))
        try:
            self._file = path.open("rb", buffering=0)
            self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception as exc:
            self.release()
            raise SharedCameraError(f"camera_shared_memory_unavailable:{self.source}:{exc}") from exc
        self._validate_header()
        self._opened = True

    def _header(self):
        if self._map is None:
            raise SharedCameraError(f"camera_closed:{self.source}")
        return HEADER.unpack_from(self._map, 0)

    def _validate_header(self):
        values = self._header()
        if values[0] != MAGIC or int(values[1]) != VERSION:
            raise SharedCameraError(f"camera_shared_memory_invalid:{self.source}")
        return values

    def isOpened(self) -> bool:
        if not self._opened or self._map is None:
            return False
        try:
            values = self._validate_header()
            heartbeat_ns = int(values[12])
            return heartbeat_ns > 0 and (time.monotonic_ns() - heartbeat_ns) <= int(self.max_stale * 1e9)
        except Exception:
            return False

    def _try_latest(self, require_new: bool) -> tuple[bool, np.ndarray | None]:
        values = self._validate_header()
        _, _, slots, max_bytes, _, _, _, _, state, latest_seq, latest_slot, _, heartbeat_ns = values
        if int(state) != 1 or int(latest_seq) <= 0:
            return False, None
        if time.monotonic_ns() - int(heartbeat_ns) > int(self.max_stale * 1e9):
            return False, None
        sequence = int(latest_seq)
        if require_new and sequence == self._last_sequence:
            return False, None
        slot = int(latest_slot)
        if slot < 0 or slot >= int(slots):
            return False, None
        meta_offset = HEADER_SIZE + slot * SLOT_META_SIZE
        version1, timestamp_ns, width, height, channels, nbytes = SLOT_META.unpack_from(self._map, meta_offset)
        expected = sequence * 2
        if int(version1) != expected or int(version1) & 1:
            return False, None
        if not (0 < int(nbytes) <= int(max_bytes)):
            return False, None
        data_offset = HEADER_SIZE + int(slots) * SLOT_META_SIZE + slot * int(max_bytes)
        raw = self._map[data_offset:data_offset + int(nbytes)]
        version2 = struct.unpack_from("<Q", self._map, meta_offset)[0]
        if int(version2) != int(version1):
            return False, None
        try:
            # mmap slicing returns immutable ``bytes``.  A NumPy view backed by
            # those bytes is consequently read-only, unlike a frame returned by
            # cv2.VideoCapture.  Several resident consumers annotate frames in
            # place (fitness/person tracking), so honor the VideoCapture contract
            # at this shared boundary and return an owned, C-contiguous buffer.
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                (int(height), int(width), int(channels))
            ).copy(order="C")
        except ValueError:
            return False, None
        self._last_sequence = sequence
        self._last_timestamp_ns = int(timestamp_ns)
        if self._first_timestamp_ns is None:
            self._first_timestamp_ns = int(timestamp_ns)
        return True, frame

    def read(self):
        deadline = time.monotonic() + self.read_timeout
        first = self._last_sequence == 0
        while self._opened and time.monotonic() < deadline:
            try:
                ok, frame = self._try_latest(require_new=not first)
            except (OSError, ValueError, struct.error, SharedCameraError):
                return False, None
            if ok:
                return True, frame
            time.sleep(0.002)
        return False, None

    def get(self, prop: Any) -> float:
        try:
            import cv2
            values = self._validate_header()
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return float(values[4])
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return float(values[5])
            if prop == cv2.CAP_PROP_FPS:
                return float(values[7]) / 1000.0
            if prop == cv2.CAP_PROP_POS_MSEC and self._last_timestamp_ns and self._first_timestamp_ns:
                return (self._last_timestamp_ns - self._first_timestamp_ns) / 1e6
            if prop == cv2.CAP_PROP_FRAME_COUNT:
                return 0.0
        except Exception:
            pass
        return 0.0

    def set(self, _prop: Any, _value: Any) -> bool:
        # Capture format is fixed centrally by the broker. Consumers may resize
        # locally when a model needs a different tensor size.
        return True

    def release(self) -> None:
        self._opened = False
        if self._map is not None:
            try:
                self._map.close()
            except Exception:
                pass
            self._map = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


def open_capture(source: Any, *, allow_file_fallback: bool = True):
    """Open configured cameras through shared memory; keep video-file tests working."""
    text = str(source)
    try:
        resolved = resolve_camera(text)
    except SharedCameraError:
        resolved = None
    if resolved is not None or text.startswith("/dev/video"):
        return SharedFrameCapture(text)
    if not allow_file_fallback:
        raise SharedCameraError(f"camera_not_managed:{text}")
    import cv2
    return cv2.VideoCapture(source)
