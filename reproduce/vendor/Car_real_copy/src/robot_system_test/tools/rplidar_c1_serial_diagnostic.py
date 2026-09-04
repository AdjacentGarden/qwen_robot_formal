#!/usr/bin/env python3
"""Direct RPLIDAR C1 serial-protocol diagnostic (no ROS /scan involved).

The program opens the serial port exclusively, sends the native Slamtec device
info/health/legacy scan commands, saves every received byte, and independently
decodes the 5-byte standard measurement stream.  It is intended to distinguish
serial/device corruption from a fault introduced later by rplidar_ros.

The standard 5-byte measurement format has structural sync/check bits, but no
packet checksum or sequence number.  Consequently this tool can prove malformed
or missing serial data, while a clean run cannot mathematically rule out every
single-byte substitution that still happens to form a valid measurement.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import struct
import sys
import termios
import time
from typing import BinaryIO, Optional, TextIO


SYNC = 0xA5
ANS_SYNC = b"\xA5\x5A"
CMD_STOP = 0x25
CMD_SCAN = 0x20
CMD_FORCE_SCAN = 0x21
CMD_GET_INFO = 0x50
CMD_GET_HEALTH = 0x52
CMD_MOTOR_SPEED = 0xA8
ANS_INFO = 0x04
ANS_HEALTH = 0x06
ANS_MEASUREMENT = 0x81


def command_packet(command: int, payload: bytes = b"") -> bytes:
    if not payload:
        return bytes((SYNC, command))
    if len(payload) > 255 or not command & 0x80:
        raise ValueError("payload command must have bit 7 set and at most 255 bytes")
    packet = bytes((SYNC, command, len(payload))) + payload
    checksum = 0
    for value in packet:
        checksum ^= value
    return packet + bytes((checksum,))


class EventLog:
    def __init__(self, path: Path) -> None:
        self.started_mono = time.monotonic()
        self.handle = path.open("w", encoding="utf-8", buffering=1)

    def write(self, event: str, **values) -> None:
        item = {
            "wall_time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "elapsed_s": round(time.monotonic() - self.started_mono, 6),
            "event": event,
            **values,
        }
        self.handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self.handle.close()


class SerialCapture:
    def __init__(
        self, port: str, baudrate: int, raw_file: BinaryIO, events: EventLog,
        log_chunks: bool,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.raw_file = raw_file
        self.events = events
        self.log_chunks = log_chunks
        self.fd: Optional[int] = None
        self.raw_offset = 0
        self.total_rx = 0
        self.total_tx = 0

    def open(self) -> None:
        if not hasattr(termios, f"B{self.baudrate}"):
            raise RuntimeError(f"Python termios does not support baud {self.baudrate}")
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(
                f"cannot lock {self.port}; stop rplidar_node and other serial readers first: {exc}")

        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        speed = getattr(termios, f"B{self.baudrate}")
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        try:
            fcntl.ioctl(self.fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_DTR))
            dtr = "low"
        except OSError as exc:
            dtr = f"unchanged:{exc}"
        self.events.write(
            "serial_open", port=self.port, baudrate=self.baudrate, dtr=dtr,
            pid=os.getpid(),
        )

    def write(self, data: bytes, label: str) -> None:
        if self.fd is None:
            raise RuntimeError("serial port is not open")
        sent = 0
        deadline = time.monotonic() + 1.0
        while sent < len(data):
            try:
                count = os.write(self.fd, data[sent:])
                sent += count
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"serial write timeout for {label}")
                select.select([], [self.fd], [], 0.05)
        termios.tcdrain(self.fd)
        self.total_tx += sent
        self.events.write("serial_tx", label=label, count=sent, hex=data.hex())

    def read(self, timeout: float, phase: str) -> bytes:
        if self.fd is None:
            raise RuntimeError("serial port is not open")
        readable, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not readable:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        if not data:
            return b""
        offset = self.raw_offset
        self.raw_file.write(data)
        self.raw_offset += len(data)
        self.total_rx += len(data)
        if self.log_chunks:
            preview = data.hex() if len(data) <= 128 else data[:64].hex() + "..." + data[-64:].hex()
            self.events.write(
                "serial_rx_chunk", phase=phase, raw_offset=offset,
                count=len(data), hex_preview=preview,
            )
        return data

    def drain(self, seconds: float, phase: str) -> int:
        deadline = time.monotonic() + seconds
        count = 0
        while time.monotonic() < deadline:
            data = self.read(min(0.02, deadline - time.monotonic()), phase)
            count += len(data)
        if count:
            self.events.write("serial_drain", phase=phase, count=count)
        return count

    def close(self) -> None:
        if self.fd is not None:
            try:
                termios.tcdrain(self.fd)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


class ProtocolReader:
    def __init__(self, serial: SerialCapture) -> None:
        self.serial = serial
        self.buffer = bytearray()

    def take(self, count: int, timeout: float, phase: str) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timeout reading {count} bytes during {phase}; got {len(self.buffer)}")
            self.buffer.extend(self.serial.read(min(0.1, remaining), phase))
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def descriptor(self, timeout: float, phase: str) -> dict:
        deadline = time.monotonic() + timeout
        skipped = bytearray()
        while True:
            index = self.buffer.find(ANS_SYNC)
            if index >= 0:
                skipped.extend(self.buffer[:index])
                del self.buffer[:index]
                break
            if len(self.buffer) > 1:
                skipped.extend(self.buffer[:-1])
                del self.buffer[:-1]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"response descriptor sync not found during {phase}")
            self.buffer.extend(self.serial.read(min(0.1, remaining), phase))
        raw = self.take(7, max(0.01, deadline - time.monotonic()), phase)
        size_mode = int.from_bytes(raw[2:6], "little")
        result = {
            "raw_hex": raw.hex(),
            "payload_size": size_mode & 0x3FFFFFFF,
            "send_mode": size_mode >> 30,
            "answer_type": raw[6],
            "preamble_skipped": len(skipped),
        }
        if skipped:
            result["preamble_hex"] = bytes(skipped[-64:]).hex()
        self.serial.events.write("response_descriptor", phase=phase, **result)
        return result


@dataclass
class Revolution:
    index: int
    started: float
    points: int = 0
    valid_distance_points: int = 0
    qualities: list[int] = field(default_factory=list)
    angles: list[float] = field(default_factory=list)
    angle_wraps: int = 0


class StandardNodeParser:
    def __init__(
        self, events: EventLog, node_writer: Optional[csv.writer],
        min_points: int, max_revolution_gap: float, baudrate: int,
    ) -> None:
        self.events = events
        self.node_writer = node_writer
        self.min_points = min_points
        self.max_revolution_gap = max_revolution_gap
        # UART transmits one start bit, eight data bits and one stop bit.
        self.seconds_per_byte = 10.0 / baudrate
        self.buffer = bytearray()
        self.nodes = 0
        self.valid_distance_nodes = 0
        self.skipped_bytes = 0
        self.resync_events = 0
        self.angle_wraps = 0
        self.unexpected_angle_regressions = 0
        self.sync_nodes = 0
        self.revolutions = 0
        self.bad_revolutions = 0
        self.revolution_hz: list[float] = []
        self.max_angle_gaps: list[float] = []
        self.current: Optional[Revolution] = None
        self.last_angle: Optional[float] = None

    @staticmethod
    def candidate(data: bytearray, offset: int = 0) -> bool:
        if len(data) - offset < 2:
            return False
        first = data[offset]
        sync = first & 1
        inverse = (first >> 1) & 1
        return sync != inverse and (data[offset + 1] & 1) == 1

    def feed(self, data: bytes, now: float, raw_offset: int) -> None:
        self.buffer.extend(data)
        skipped_here = 0
        while len(self.buffer) >= 5:
            if not self.candidate(self.buffer):
                del self.buffer[0]
                self.skipped_bytes += 1
                skipped_here += 1
                continue
            raw = bytes(self.buffer[:5])
            del self.buffer[:5]
            if skipped_here:
                self.resync_events += 1
                self.events.write(
                    "stream_resync", skipped_bytes=skipped_here,
                    total_skipped_bytes=self.skipped_bytes,
                    approximate_raw_offset=raw_offset - len(self.buffer) - 5,
                    next_node_hex=raw.hex(),
                )
                skipped_here = 0
            # A read() timestamp is the end of its chunk. Reconstruct each
            # measurement's approximate wire time so a large kernel read does
            # not make an entire revolution appear to have zero duration.
            node_time = now - len(self.buffer) * self.seconds_per_byte
            self._node(raw, node_time)

    def _node(self, raw: bytes, now: float) -> None:
        first = raw[0]
        sync = bool(first & 1)
        quality = first >> 2
        angle_word = int.from_bytes(raw[1:3], "little")
        distance_word = int.from_bytes(raw[3:5], "little")
        angle = (angle_word >> 1) / 64.0
        distance_mm = distance_word / 4.0
        self.nodes += 1
        if distance_word:
            self.valid_distance_nodes += 1
        if sync:
            self.sync_nodes += 1
            if self.current is not None:
                self._finish_revolution(now)
            self.current = Revolution(self.revolutions + 1, now)
        elif self.last_angle is not None and angle + 1.0 < self.last_angle:
            # On C1 the start flag can occur in one of the last samples near
            # 360 degrees, with the numeric angle wrapping on a following
            # non-start sample. That is one normal wrap inside the interval
            # between two start flags, not corruption. Only a second wrap in
            # the same interval (or a wrap before the first start flag) is
            # structurally unexpected.
            self.angle_wraps += 1
            unexpected = self.current is None or self.current.angle_wraps >= 1
            if self.current is not None:
                self.current.angle_wraps += 1
            if unexpected:
                self.unexpected_angle_regressions += 1
                self.events.write(
                    "unexpected_angle_regression",
                    previous_angle_deg=round(self.last_angle, 4),
                    angle_deg=round(angle, 4), raw_hex=raw.hex(),
                    node_index=self.nodes,
                )
        self.last_angle = angle
        if self.current is not None:
            self.current.points += 1
            self.current.valid_distance_points += int(distance_word != 0)
            self.current.qualities.append(quality)
            self.current.angles.append(angle)
        if self.node_writer is not None:
            self.node_writer.writerow([
                self.nodes, f"{now:.9f}", int(sync), quality,
                f"{angle:.6f}", f"{distance_mm:.3f}", raw.hex(),
            ])

    def _finish_revolution(self, now: float) -> None:
        assert self.current is not None
        rev = self.current
        duration = now - rev.started
        angles = sorted(rev.angles)
        gaps: list[float] = []
        if len(angles) >= 2:
            gaps.extend(b - a for a, b in zip(angles, angles[1:]))
            gaps.append((angles[0] + 360.0) - angles[-1])
        max_gap = max(gaps) if gaps else 360.0
        coverage = 360.0 - max_gap
        hz = 1.0 / duration if duration > 0 else math.inf
        bad = (
            rev.points < self.min_points or duration > self.max_revolution_gap
            or duration < 0.02 or coverage < 270.0
        )
        self.revolutions += 1
        self.bad_revolutions += int(bad)
        self.revolution_hz.append(hz)
        self.max_angle_gaps.append(max_gap)
        self.events.write(
            "revolution", index=self.revolutions, duration_s=round(duration, 6),
            hz=round(hz, 4), points=rev.points,
            valid_distance_points=rev.valid_distance_points,
            angle_coverage_deg=round(coverage, 4), max_angle_gap_deg=round(max_gap, 4),
            quality_min=min(rev.qualities) if rev.qualities else None,
            quality_max=max(rev.qualities) if rev.qualities else None,
            angle_wraps=rev.angle_wraps,
            bad=bad,
        )

    def summary(self) -> dict:
        return {
            "decoded_nodes": self.nodes,
            "valid_distance_nodes": self.valid_distance_nodes,
            "sync_nodes": self.sync_nodes,
            "completed_revolutions": self.revolutions,
            "bad_revolutions": self.bad_revolutions,
            "resync_events": self.resync_events,
            "skipped_bytes": self.skipped_bytes,
            "unparsed_tail_bytes": len(self.buffer),
            "unparsed_tail_hex": bytes(self.buffer).hex(),
            "angle_wraps": self.angle_wraps,
            "unexpected_angle_regressions": self.unexpected_angle_regressions,
            # Compatibility with older summary consumers. This now counts
            # only unexpected regressions, rather than normal C1 wraps.
            "angle_regressions_without_sync": self.unexpected_angle_regressions,
            "revolution_hz_min": min(self.revolution_hz) if self.revolution_hz else None,
            "revolution_hz_max": max(self.revolution_hz) if self.revolution_hz else None,
            "max_observed_angle_gap_deg": max(self.max_angle_gaps) if self.max_angle_gaps else None,
        }


def decode_info(payload: bytes) -> dict:
    if len(payload) != 20:
        return {"raw_hex": payload.hex(), "error": f"expected 20 bytes, got {len(payload)}"}
    firmware = int.from_bytes(payload[1:3], "little")
    return {
        "model": payload[0],
        "firmware": f"{firmware >> 8}.{firmware & 0xff}",
        "hardware": payload[3],
        "serial_number": payload[4:20].hex().upper(),
        "raw_hex": payload.hex(),
    }


def decode_health(payload: bytes) -> dict:
    if len(payload) != 3:
        return {"raw_hex": payload.hex(), "error": f"expected 3 bytes, got {len(payload)}"}
    return {
        "status": payload[0],
        "status_name": {0: "OK", 1: "WARNING", 2: "ERROR"}.get(payload[0], "UNKNOWN"),
        "error_code": int.from_bytes(payload[1:3], "little"),
        "raw_hex": payload.hex(),
    }


def query(
    serial: SerialCapture, reader: ProtocolReader, command: int,
    expected_type: int, expected_size: int, label: str,
) -> bytes:
    serial.write(command_packet(command), label)
    descriptor = reader.descriptor(2.0, label)
    if descriptor["answer_type"] != expected_type:
        raise RuntimeError(
            f"{label}: answer type 0x{descriptor['answer_type']:02x}, expected 0x{expected_type:02x}")
    if descriptor["payload_size"] != expected_size:
        raise RuntimeError(
            f"{label}: payload size {descriptor['payload_size']}, expected {expected_size}")
    return reader.take(expected_size, 2.0, label)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Direct RPLIDAR C1 serial integrity test; does not use ROS /scan")
    parser.add_argument("--port", default="/dev/ttyS8")
    parser.add_argument("--baudrate", type=int, default=460800)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--output-dir", default="logs")
    parser.add_argument("--max-byte-gap", type=float, default=0.50)
    parser.add_argument("--max-revolution-gap", type=float, default=0.35)
    parser.add_argument("--min-revolution-hz", type=float, default=5.0)
    parser.add_argument("--min-points-per-revolution", type=int, default=60)
    parser.add_argument("--force-scan", action="store_true")
    parser.add_argument(
        "--no-motor-command", action="store_true",
        help="do not send C1 RPM start/stop commands (normally leave this off)")
    parser.add_argument(
        "--log-nodes", action="store_true",
        help="also write one CSV row per decoded point; produces a large file")
    parser.add_argument(
        "--log-chunks", action="store_true",
        help="also write metadata for every OS serial read; raw .bin is always complete")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0 or args.baudrate <= 0:
        raise SystemExit("duration and baudrate must be positive")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output_dir).expanduser().resolve() / f"rplidar_c1_serial_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    events = EventLog(output / "events.jsonl")
    raw_file = (output / "serial_rx.bin").open("wb", buffering=1024 * 1024)
    node_handle: Optional[TextIO] = None
    node_writer: Optional[csv.writer] = None
    if args.log_nodes:
        node_handle = (output / "nodes.csv").open("w", encoding="utf-8", newline="", buffering=1024 * 1024)
        node_writer = csv.writer(node_handle)
        node_writer.writerow([
            "node_index", "monotonic_s", "sync", "quality",
            "angle_deg", "distance_mm", "raw_hex",
        ])

    serial = SerialCapture(args.port, args.baudrate, raw_file, events, args.log_chunks)
    reader = ProtocolReader(serial)
    parser = StandardNodeParser(
        events, node_writer, args.min_points_per_revolution,
        args.max_revolution_gap, args.baudrate)
    stop_requested = False
    scan_started = False
    summary: dict = {
        "tool": "rplidar_c1_serial_diagnostic",
        "port": args.port,
        "baudrate": args.baudrate,
        "requested_duration_s": args.duration,
        "output_directory": str(output),
        "uses_ros_scan": False,
        "protocol_limit": (
            "The standard 5-byte stream has sync/check bits but no checksum or sequence number."
        ),
    }

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    exit_code = 2
    started = time.monotonic()
    try:
        serial.open()
        serial.write(command_packet(CMD_STOP), "stop_before_test")
        time.sleep(0.10)
        serial.drain(0.10, "preflight_drain")

        info_payload = query(serial, reader, CMD_GET_INFO, ANS_INFO, 20, "device_info")
        summary["device_info"] = decode_info(info_payload)
        events.write("device_info", **summary["device_info"])

        health_payload = query(serial, reader, CMD_GET_HEALTH, ANS_HEALTH, 3, "device_health")
        summary["device_health"] = decode_health(health_payload)
        events.write("device_health", **summary["device_health"])
        if summary["device_health"].get("status") == 2:
            raise RuntimeError(
                f"lidar reports internal health error {summary['device_health'].get('error_code')}")

        if not args.no_motor_command:
            # C1 is an RPM-controlled model. 600 rpm matches the SDK fallback/default.
            serial.write(command_packet(CMD_MOTOR_SPEED, struct.pack("<H", 600)), "motor_start_600rpm")
            time.sleep(0.10)

        scan_command = CMD_FORCE_SCAN if args.force_scan else CMD_SCAN
        serial.write(command_packet(scan_command), "start_standard_scan")
        descriptor = reader.descriptor(3.0, "scan_descriptor")
        summary["scan_descriptor"] = descriptor
        if descriptor["answer_type"] != ANS_MEASUREMENT:
            raise RuntimeError(
                "classic scan did not return the standard 5-byte stream: "
                f"answer_type=0x{descriptor['answer_type']:02x}, size={descriptor['payload_size']}. "
                "Raw bytes were preserved, but this run cannot use the standard-node decoder.")
        if descriptor["payload_size"] != 5 or descriptor["send_mode"] != 1:
            raise RuntimeError(
                f"unexpected scan descriptor size/mode: {descriptor['payload_size']}/{descriptor['send_mode']}")
        scan_started = True

        scan_start = time.monotonic()
        deadline = scan_start + args.duration
        last_rx: Optional[float] = None
        max_gap = 0.0
        gaps: list[dict] = []
        window_started = scan_start
        window_bytes = 0
        window_reads = 0

        if reader.buffer:
            initial = bytes(reader.buffer)
            reader.buffer.clear()
            now = time.monotonic()
            parser.feed(initial, now, serial.raw_offset)
            last_rx = now
            window_bytes += len(initial)
            window_reads += 1

        while not stop_requested and time.monotonic() < deadline:
            now = time.monotonic()
            data = serial.read(min(0.10, deadline - now), "scan_stream")
            now = time.monotonic()
            if data:
                if last_rx is not None:
                    gap = now - last_rx
                    max_gap = max(max_gap, gap)
                    if gap > args.max_byte_gap:
                        item = {"at_s": now - scan_start, "duration_s": gap}
                        gaps.append(item)
                        events.write("serial_byte_gap", **item)
                last_rx = now
                parser.feed(data, now, serial.raw_offset)
                window_bytes += len(data)
                window_reads += 1
            if now - window_started >= 1.0:
                elapsed = now - window_started
                events.write(
                    "rx_window", duration_s=round(elapsed, 6), bytes=window_bytes,
                    reads=window_reads, bytes_per_second=round(window_bytes / elapsed, 2),
                    decoded_nodes=parser.nodes, revolutions=parser.revolutions,
                    skipped_bytes=parser.skipped_bytes,
                )
                # Keep at most roughly one second of captured evidence in the
                # Python userspace buffer if the test or machine later fails.
                raw_file.flush()
                window_started = now
                window_bytes = 0
                window_reads = 0

        actual_duration = max(0.001, time.monotonic() - scan_start)
        protocol = parser.summary()
        summary.update({
            "actual_scan_duration_s": actual_duration,
            "serial_rx_bytes": serial.total_rx,
            "serial_tx_bytes": serial.total_tx,
            "scan_stream_max_read_gap_s": max_gap,
            "scan_stream_gaps_over_limit": gaps,
            "protocol": protocol,
        })
        revolution_hz = protocol["completed_revolutions"] / actual_duration
        summary["observed_revolution_hz"] = revolution_hz
        malformed = (
            not protocol["decoded_nodes"]
            or protocol["skipped_bytes"] > 0
            or protocol["unexpected_angle_regressions"] > 0
            or protocol["bad_revolutions"] > 0
            or bool(gaps)
            or revolution_hz < args.min_revolution_hz
        )
        if malformed:
            summary["verdict"] = "RAW_SERIAL_OR_LIDAR_DATA_FAULT"
            summary["explanation"] = (
                "异常已出现在不经过 ROS /scan 的原始串口字节或 Slamtec 标准协议结构中；"
                "优先检查 C1、供电、串口线、RK3588 UART/USB 串口驱动和端口占用。"
            )
            exit_code = 1
        else:
            summary["verdict"] = "RAW_SERIAL_STREAM_HEALTHY"
            summary["explanation"] = (
                "本次窗口内原始串口流和独立协议解析正常。若同一时段 rplidar_ros 仍失败，"
                "更可能是 SDK 扫描模式/解析、驱动重启逻辑或其下游软件问题。偶发硬件问题仍需长时间复现。"
            )
            exit_code = 0
    except Exception as exc:
        summary["verdict"] = "SETUP_OR_DEVICE_COMMUNICATION_FAILURE"
        summary["explanation"] = str(exc)
        summary["exception_type"] = type(exc).__name__
        events.write("fatal", exception_type=type(exc).__name__, message=str(exc))
        exit_code = 2
    finally:
        if serial.fd is not None:
            try:
                if scan_started:
                    serial.write(command_packet(CMD_STOP), "stop_after_test")
                    time.sleep(0.05)
                if not args.no_motor_command:
                    serial.write(
                        command_packet(CMD_MOTOR_SPEED, struct.pack("<H", 0)),
                        "motor_stop")
            except Exception as exc:
                events.write("cleanup_error", message=str(exc))
        summary["total_elapsed_s"] = time.monotonic() - started
        summary["interrupted"] = stop_requested
        summary["serial_rx_bytes_total"] = serial.total_rx
        summary["serial_tx_bytes_total"] = serial.total_tx
        summary.setdefault("protocol", parser.summary())
        events.write("summary", **summary)
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raw_file.flush()
        raw_file.close()
        if node_handle is not None:
            node_handle.flush()
            node_handle.close()
        serial.close()
        events.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"logs: {output}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
