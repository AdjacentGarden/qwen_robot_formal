#!/usr/bin/env python3
"""Non-intrusive runtime monitor for RPLIDAR serial failures.

During normal operation this tool never opens the monitored serial device.  It
records kernel UART counters, opens/closes of the device node, current owners,
system load, ROS /scan activity, and the ModemManager journal in one JSONL log.
If requested, it reads the configured baud rate once *after* a UART error is
already observed so that healthy operation is not perturbed by the monitor.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import fcntl
import glob
import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_ATTRIB = 0x00000004
IN_NONBLOCK = os.O_NONBLOCK
TCGETS2 = 0x802C542A
TERMIOS2_FORMAT = "IIIIB19BII"
TERMIOS2_SIZE = struct.calcsize(TERMIOS2_FORMAT)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started_mono = time.monotonic()
        self.handle = path.open("w", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def write(self, event: str, **values: Any) -> None:
        item = {
            "wall_time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "elapsed_s": round(time.monotonic() - self.started_mono, 6),
            "event": event,
            **values,
        }
        line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.handle.write(line + "\n")

    def close(self) -> None:
        with self.lock:
            self.handle.close()


def run_capture(command: List[str], timeout: float = 5.0) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "output": result.stdout.rstrip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "error": str(exc)}


def read_cmdline(pid: int) -> str:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        return data.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def device_owners(device: str) -> List[Dict[str, Any]]:
    owners: Dict[int, Dict[str, Any]] = {}
    try:
        target = os.path.realpath(device)
    except OSError:
        target = device
    for link in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            linked = os.readlink(link)
            if linked != device and os.path.realpath(linked) != target:
                continue
            pid = int(link.split("/")[2])
            owners.setdefault(pid, {"pid": pid, "cmdline": read_cmdline(pid), "fds": []})
            owners[pid]["fds"].append(int(link.rsplit("/", 1)[1]))
        except (OSError, ValueError):
            continue
    return sorted(owners.values(), key=lambda item: item["pid"])


def matching_processes() -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {"manager": [], "rplidar": []}
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(path.split("/")[2])
            cmdline = read_cmdline(pid)
        except ValueError:
            continue
        if not cmdline:
            continue
        item = {"pid": pid, "cmdline": cmdline}
        if "mapping_navigation_manager.py" in cmdline:
            result["manager"].append(item)
        if "/rplidar_node" in cmdline:
            result["rplidar"].append(item)
    return result


def serial_index(port: str) -> int:
    match = re.fullmatch(r"ttyS(\d+)", os.path.basename(port))
    if not match:
        raise ValueError(
            f"{port!r} is not ttyS<N>; pass --serial-index for another device type")
    return int(match.group(1))


def uart_counters(index: int) -> Dict[str, int]:
    prefix = f"{index}:"
    with open("/proc/tty/driver/serial", encoding="utf-8") as handle:
        line = next((row.strip() for row in handle if row.startswith(prefix)), "")
    if not line:
        raise RuntimeError(f"serial index {index} not found in /proc/tty/driver/serial")
    result: Dict[str, int] = {}
    for key in ("tx", "rx", "fe", "pe", "brk", "oe"):
        match = re.search(rf"(?:^| ){key}:(\d+)", line)
        result[key] = int(match.group(1)) if match else 0
    return result


def read_baud_once(port: str) -> Dict[str, Any]:
    """Open only after a detected fault and read termios2 without changing it."""
    fd: Optional[int] = None
    try:
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        data = bytearray(TERMIOS2_SIZE)
        fcntl.ioctl(fd, TCGETS2, data, True)
        values = struct.unpack(TERMIOS2_FORMAT, data)
        return {"input_baud": values[-2], "output_baud": values[-1]}
    except OSError as exc:
        return {"error": str(exc)}
    finally:
        if fd is not None:
            os.close(fd)


def cpu_snapshot(previous: Optional[Tuple[int, int]]) -> Tuple[Dict[str, Any], Tuple[int, int]]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    usage: Optional[float] = None
    if previous is not None:
        delta_total = total - previous[0]
        delta_idle = idle - previous[1]
        if delta_total > 0:
            usage = round(100.0 * (delta_total - delta_idle) / delta_total, 2)
    temperatures: List[float] = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            value = float(Path(path).read_text(encoding="utf-8").strip())
            temperatures.append(value / 1000.0 if value > 1000 else value)
        except (OSError, ValueError):
            continue
    load = os.getloadavg()
    snapshot = {
        "cpu_usage_percent": usage,
        "load_1m": round(load[0], 2),
        "load_5m": round(load[1], 2),
        "load_15m": round(load[2], 2),
        "temperature_max_c": round(max(temperatures), 1) if temperatures else None,
    }
    return snapshot, (total, idle)


class ScanMonitor:
    def __init__(
        self, topic: str, ros_setup: str, workspace_setup: str,
        log: EventLog, stop_event: threading.Event,
    ) -> None:
        self.topic = topic
        self.ros_setup = ros_setup
        self.workspace_setup = workspace_setup
        self.log = log
        self.stop_event = stop_event
        self.lock = threading.Lock()
        self.rate_hz: Optional[float] = None
        self.last_mono: Optional[float] = None
        self.available = False
        self.process: Optional[subprocess.Popen[str]] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="scan-monitor", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        shell_program = (
            'source "$1"; '
            'if [ -f "$2" ]; then source "$2"; fi; '
            'exec ros2 topic hz "$3" --window 20'
        )
        command = [
            "bash", "-c", shell_program, "rplidar-scan-monitor",
            self.ros_setup, self.workspace_setup, self.topic,
        ]
        popen_options: Dict[str, Any] = {}
        run_as = os.environ.get("SUDO_USER", "")
        account = None
        if os.geteuid() == 0 and run_as and run_as != "root":
            try:
                account = pwd.getpwnam(run_as)
                popen_options.update(user=account.pw_uid, group=account.pw_gid)
            except KeyError:
                run_as = ""
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        if account is not None:
            environment.update(
                HOME=account.pw_dir,
                USER=account.pw_name,
                LOGNAME=account.pw_name,
            )
        try:
            self.process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                env=environment,
                **popen_options,
            )
            self.available = True
            self.log.write(
                "ros_scan_started", topic=self.topic, command=command,
                run_as=run_as or str(os.geteuid()))
            assert self.process.stdout is not None
            for line in self.process.stdout:
                text = line.strip()
                self.log.write("ros_topic_hz", topic=self.topic, line=text)
                match = re.search(r"average rate:\s*([0-9.]+)", text)
                if match:
                    with self.lock:
                        self.rate_hz = float(match.group(1))
                        self.last_mono = time.monotonic()
                if self.stop_event.is_set():
                    break
            returncode = self.process.poll()
            if not self.stop_event.is_set():
                self.log.write("ros_scan_stopped", returncode=returncode, topic=self.topic)
        except OSError as exc:
            self.log.write("ros_scan_unavailable", error=str(exc), topic=self.topic)

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            age = None if self.last_mono is None else now - self.last_mono
            return {
                "available": self.available,
                "topic": self.topic,
                "rate_hz": self.rate_hz,
                "last_message_age_s": round(age, 3) if age is not None else None,
            }

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


class JournalMonitor:
    def __init__(self, unit: str, log: EventLog, stop_event: threading.Event) -> None:
        self.unit = unit
        self.log = log
        self.stop_event = stop_event
        self.process: Optional[subprocess.Popen[str]] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="journal-monitor", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        command = [
            "journalctl", "-fu", self.unit, "-o", "short-iso-precise",
            "--no-pager", "-n", "50",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            self.log.write("journal_started", unit=self.unit, command=command)
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.log.write("journal", unit=self.unit, line=line.rstrip())
                if self.stop_event.is_set():
                    break
        except OSError as exc:
            self.log.write("journal_error", unit=self.unit, error=str(exc))

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


class DeviceOpenMonitor:
    def __init__(self, device: str) -> None:
        self.device = device
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(IN_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        mask = IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE | IN_ATTRIB
        self.watch = libc.inotify_add_watch(self.fd, device.encode(), mask)
        if self.watch < 0:
            error = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(error, f"inotify_add_watch failed for {device}")

    def events(self) -> List[Dict[str, Any]]:
        ready, _, _ = select.select([self.fd], [], [], 0)
        if not ready:
            return []
        data = os.read(self.fd, 65536)
        events: List[Dict[str, Any]] = []
        offset = 0
        while offset + 16 <= len(data):
            _watch, mask, cookie, name_len = struct.unpack_from("iIII", data, offset)
            offset += 16 + name_len
            kinds = []
            if mask & IN_OPEN:
                kinds.append("open")
            if mask & (IN_CLOSE_WRITE | IN_CLOSE_NOWRITE):
                kinds.append("close")
            if mask & IN_ATTRIB:
                kinds.append("attrib")
            events.append({"mask": mask, "cookie": cookie, "kinds": kinds})
        return events

    def close(self) -> None:
        os.close(self.fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyS8")
    parser.add_argument("--serial-index", type=int)
    parser.add_argument("--scan-topic", default="/scan")
    parser.add_argument("--ros-setup", default="/opt/ros/humble/setup.bash")
    default_workspace_setup = str(Path(__file__).resolve().parents[3] / "install/setup.bash")
    parser.add_argument("--workspace-setup", default=default_workspace_setup)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="heartbeat interval in seconds (default: 5)")
    parser.add_argument("--poll-interval", type=float, default=0.1,
                        help="UART error counter poll interval (default: 0.1)")
    parser.add_argument("--output-dir", default="logs/rplidar_runtime_monitor")
    parser.add_argument("--journal-unit", default="ModemManager")
    parser.add_argument("--enable-modemmanager-debug", action="store_true",
                        help="ask ModemManager to enable DEBUG logging for this boot")
    parser.add_argument("--read-baud-on-error", action="store_true",
                        help="open the port once after an error starts and read termios2")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds; 0 means run until Ctrl-C")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval <= 0 or args.poll_interval <= 0 or args.duration < 0:
        raise SystemExit("intervals must be positive and duration must not be negative")
    index = args.serial_index if args.serial_index is not None else serial_index(args.port)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%z")
    log_path = output_dir / f"rplidar_runtime_{stamp}.jsonl"
    log = EventLog(log_path)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(f"Log: {log_path}", flush=True)
    log.write(
        "start",
        argv=sys.argv,
        pid=os.getpid(),
        uid=os.getuid(),
        port=args.port,
        serial_index=index,
        scan_topic=args.scan_topic,
        ros_setup=args.ros_setup,
        workspace_setup=args.workspace_setup,
        hostname=os.uname().nodename,
        kernel=os.uname().release,
        non_intrusive_during_healthy_operation=True,
    )
    for name, command in (
        ("udev", ["udevadm", "info", "-q", "property", "-n", args.port]),
        ("modem_list", ["mmcli", "-L"]),
        ("modem_service", ["systemctl", "is-active", f"{args.journal_unit}.service"]),
    ):
        log.write("command_snapshot", name=name, **run_capture(command))
    if args.enable_modemmanager_debug:
        log.write(
            "modemmanager_debug_request",
            **run_capture(["mmcli", "--set-logging=DEBUG"]),
        )

    try:
        open_monitor = DeviceOpenMonitor(args.port)
    except OSError as exc:
        log.write("device_watch_error", error=str(exc), port=args.port)
        open_monitor = None

    scan_monitor = ScanMonitor(
        args.scan_topic, args.ros_setup, args.workspace_setup, log, stop_event)
    journal_monitor = JournalMonitor(args.journal_unit, log, stop_event)
    scan_monitor.start()
    journal_monitor.start()

    previous_cpu: Optional[Tuple[int, int]] = None
    previous_counters = uart_counters(index)
    log.write(
        "baseline",
        uart=previous_counters,
        owners=device_owners(args.port),
        processes=matching_processes(),
    )
    started = time.monotonic()
    next_heartbeat = started
    fault_active = False

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                stop_event.set()
                break

            if open_monitor is not None:
                for event in open_monitor.events():
                    log.write(
                        "device_event",
                        port=args.port,
                        owners=device_owners(args.port),
                        **event,
                    )

            current = uart_counters(index)
            delta = {key: current[key] - previous_counters[key] for key in current}
            if delta["fe"] or delta["pe"] or delta["brk"] or delta["oe"]:
                values: Dict[str, Any] = {
                    "uart": current,
                    "delta": delta,
                    "owners": device_owners(args.port),
                    "processes": matching_processes(),
                }
                if args.read_baud_on_error and not fault_active:
                    values["baud"] = read_baud_once(args.port)
                log.write("uart_error", **values)
                if not fault_active:
                    print(
                        f"FAULT {datetime.now().astimezone().isoformat(timespec='seconds')} "
                        f"delta={delta} log={log_path}",
                        flush=True,
                    )
                fault_active = True
            previous_counters = current

            if now >= next_heartbeat:
                system, previous_cpu = cpu_snapshot(previous_cpu)
                scan = scan_monitor.snapshot()
                log.write(
                    "heartbeat",
                    uart=current,
                    system=system,
                    scan=scan,
                    owners=device_owners(args.port),
                    processes=matching_processes(),
                    fault_active=fault_active,
                )
                print(
                    f"OK {datetime.now().astimezone().isoformat(timespec='seconds')} "
                    f"rx={current['rx']} fe={current['fe']} oe={current['oe']} "
                    f"scan_hz={scan['rate_hz']} load={system['load_1m']}",
                    flush=True,
                )
                next_heartbeat = now + args.interval

            stop_event.wait(args.poll_interval)
    except Exception as exc:
        log.write("fatal_error", error=repr(exc))
        raise
    finally:
        stop_event.set()
        scan_monitor.stop()
        journal_monitor.stop()
        if open_monitor is not None:
            open_monitor.close()
        log.write(
            "stop",
            uart=uart_counters(index),
            owners=device_owners(args.port),
            processes=matching_processes(),
        )
        log.close()
        print(f"Stopped. Log: {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
