#!/usr/bin/env python3
"""Thin CLI used by every resident skill wrapper."""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOCKET_PATH = ROOT / "runtime" / "resident" / "skills.sock"


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = conn.recv(size)
        if not chunk:
            raise ConnectionError("resident runtime closed the connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def request(skill: str, argv: list[str], timeout: float) -> tuple[dict, float]:
    message = (
        {"op": "ping", "payload_len": 0}
        if skill == "__status__"
        else {"op": "skill_run", "skill": skill, "argv": argv, "stream": True, "payload_len": 0}
    )
    payload = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    started = time.perf_counter()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(SOCKET_PATH))
        conn.sendall(struct.pack("!I", len(payload)) + payload)
        saw_stream = False
        while True:
            size = struct.unpack("!I", _recv_exact(conn, 4))[0]
            result = json.loads(_recv_exact(conn, size).decode("utf-8"))
            kind = result.get("type")
            if kind == "stdout":
                saw_stream = True
                sys.stdout.write(str(result.get("data") or ""))
                sys.stdout.flush()
                continue
            if kind == "stderr":
                saw_stream = True
                sys.stderr.write(str(result.get("data") or ""))
                sys.stderr.flush()
                continue
            if kind == "final":
                result.pop("type", None)
                if saw_stream:
                    result["stdout"] = ""
                    result["stderr"] = ""
                    result["_saw_stream"] = True
                break
            # Backward compatibility with the first resident protocol.
            break
    return result, (time.perf_counter() - started) * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("skill")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    known = parser.parse_args()
    timeout = 3600.0
    try:
        result, elapsed_ms = request(known.skill, known.argv, timeout)
    except FileNotFoundError:
        print(json.dumps({
            "ok": False,
            "skill": known.skill,
            "error": "resident_runtime_not_started",
            "hint": f"先执行 bash {ROOT / 'resident_runtime.sh'} start",
        }, ensure_ascii=False), file=sys.stderr)
        return 70
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "skill": known.skill,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), file=sys.stderr)
        return 71

    if known.skill == "__status__" and result.get("ok"):
        status = {"state": "ready", "socket": str(SOCKET_PATH), **(result.get("result") or {})}
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    if not stdout and not result.get("ok") and not result.get("_saw_stream"):
        print(json.dumps({
            "ok": False,
            "skill": known.skill,
            "error": result.get("error", "resident_skill_failed"),
            "client_round_trip_ms": round(elapsed_ms, 3),
        }, ensure_ascii=False), file=sys.stderr)
    return int(result.get("exit_code", 0 if result.get("ok") else 1))


if __name__ == "__main__":
    raise SystemExit(main())
