#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import statistics
import struct
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_SOCKET = ROOT / "runtime" / "shared_runtime" / "inference.sock"


def recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = conn.recv(size)
        if not chunk:
            raise ConnectionError("server closed connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def request(socket_path: Path, header: dict[str, Any], payload: bytes = b"") -> tuple[dict[str, Any], float]:
    header = dict(header)
    header["payload_len"] = len(payload)
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    started = time.perf_counter()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(max(20.0, float(header.get("timeout", 0.0)) + 15.0))
        conn.connect(str(socket_path))
        conn.sendall(struct.pack("!I", len(encoded)) + encoded + payload)
        size = struct.unpack("!I", recv_exact(conn, 4))[0]
        response = json.loads(recv_exact(conn, size).decode("utf-8"))
    return response, (time.perf_counter() - started) * 1000.0


def frame_request(socket_path: Path, op: str, frame, **options):
    header = {"op": op, "shape": list(frame.shape), **options}
    return request(socket_path, header, frame.tobytes(order="C"))


def stats(values):
    return {"min_ms": round(min(values), 3), "median_ms": round(statistics.median(values), 3), "max_ms": round(max(values), 3), "mean_ms": round(statistics.mean(values), 3)}


def command_benchmark(args) -> int:
    import numpy as np

    frames = {
        "face_detect": np.zeros((320, 320, 3), dtype=np.uint8),
        "face_embed": np.zeros((160, 160, 3), dtype=np.uint8),
        "yolo": np.zeros((480, 640, 3), dtype=np.uint8),
        "reid": np.zeros((256, 128, 3), dtype=np.uint8),
        "pose": np.zeros((480, 640, 3), dtype=np.uint8),
    }
    selected = list(frames) if args.operation == "all" else [args.operation]
    report = {}
    for op in selected:
        for _ in range(args.warmup):
            response, _ = frame_request(args.socket, op, frames[op], return_landmarks=False)
            if not response.get("ok"):
                raise RuntimeError(response)
        client_values = []
        server_values = []
        for _ in range(args.runs):
            response, elapsed = frame_request(args.socket, op, frames[op], return_landmarks=False)
            if not response.get("ok"):
                raise RuntimeError(response)
            client_values.append(elapsed)
            server_values.append(float(response["server_elapsed_ms"]))
        report[op] = {"client_round_trip": stats(client_values), "server_inference": stats(server_values), "runs": args.runs}
    print(json.dumps({"ok": True, "benchmark": report}, ensure_ascii=False, indent=2))
    return 0


def command_camera(args) -> int:
    import cv2

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {args.device}")
    frame = None
    try:
        for _ in range(8):
            ok, value = cap.read()
            if ok and value is not None:
                frame = value
    finally:
        cap.release()
    if frame is None:
        raise RuntimeError("camera returned no frame")
    results = {}
    for op in ("face_pipeline", "yolo", "pose"):
        response, elapsed = frame_request(args.socket, op, frame, return_landmarks=False)
        results[op] = {"round_trip_ms": round(elapsed, 3), "response": response}
    print(json.dumps({"ok": True, "device": args.device, "results": results}, ensure_ascii=False, indent=2))
    return 0


def print_response(response: dict[str, Any], elapsed: float) -> int:
    print(json.dumps({"client_round_trip_ms": round(elapsed, 3), **response}, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


def command_chassis(args) -> int:
    if args.direction == "stop":
        response, elapsed = request(args.socket, {"op": "chassis_stop"})
        return print_response(response, elapsed)
    linear = 0.0
    angular = 0.0
    if args.direction == "forward":
        linear = abs(args.speed)
    elif args.direction == "backward":
        linear = -abs(args.speed)
    elif args.direction == "left":
        angular = abs(args.angular_speed)
    elif args.direction == "right":
        angular = -abs(args.angular_speed)
    response, elapsed = request(
        args.socket,
        {
            "op": "chassis_move",
            "linear_x": linear,
            "angular_z": angular,
            "duration": args.duration,
            "discovery_timeout": args.discovery_timeout,
            "allow_no_subscriber": args.allow_no_subscriber,
        },
    )
    return print_response(response, elapsed)


def command_navigation(args) -> int:
    header: dict[str, Any] = {
        "op": "navigation_goal",
        "timeout": args.timeout,
        "server_wait_timeout": args.server_wait_timeout,
        "goal_response_timeout": args.goal_response_timeout,
    }
    if args.point:
        header["point"] = args.point
    else:
        if args.x is None or args.y is None:
            raise SystemExit("导航需要点位名，或者同时提供 --x 和 --y")
        header.update({"x": args.x, "y": args.y, "yaw": args.yaw, "frame_id": args.frame_id})
    response, elapsed = request(args.socket, header)
    return print_response(response, elapsed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("ros-ready")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--operation", choices=("all", "face_detect", "face_embed", "yolo", "reid", "pose"), default="all")
    bench.add_argument("--runs", type=int, default=50)
    bench.add_argument("--warmup", type=int, default=3)
    camera = sub.add_parser("camera-test")
    camera.add_argument("--device", default="/dev/video22")
    chassis = sub.add_parser("chassis")
    chassis.add_argument("direction", choices=("forward", "backward", "left", "right", "stop"))
    chassis.add_argument("--speed", type=float, default=0.10)
    chassis.add_argument("--angular-speed", type=float, default=0.25)
    chassis.add_argument("--duration", type=float, default=0.5)
    chassis.add_argument("--discovery-timeout", type=float, default=1.0)
    chassis.add_argument("--allow-no-subscriber", action="store_true")
    navigation = sub.add_parser("nav")
    navigation.add_argument("point", nargs="?")
    navigation.add_argument("--x", type=float)
    navigation.add_argument("--y", type=float)
    navigation.add_argument("--yaw", type=float, default=0.0)
    navigation.add_argument("--frame-id", default="map")
    navigation.add_argument("--timeout", type=float, default=120.0)
    navigation.add_argument("--server-wait-timeout", type=float, default=5.0)
    navigation.add_argument("--goal-response-timeout", type=float, default=8.0)
    sub.add_parser("nav-cancel")
    args = parser.parse_args()

    if args.command == "ping":
        response, elapsed = request(args.socket, {"op": "ping"})
        print(json.dumps({"round_trip_ms": round(elapsed, 3), **response}, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1
    if args.command == "ros-ready":
        response, elapsed = request(args.socket, {"op": "ros_ready"})
        print(json.dumps({"round_trip_ms": round(elapsed, 3), **response}, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1
    if args.command == "benchmark":
        return command_benchmark(args)
    if args.command == "camera-test":
        return command_camera(args)
    if args.command == "chassis":
        return command_chassis(args)
    if args.command == "nav":
        return command_navigation(args)
    if args.command == "nav-cancel":
        response, elapsed = request(args.socket, {"op": "navigation_cancel"})
        return print_response(response, elapsed)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
