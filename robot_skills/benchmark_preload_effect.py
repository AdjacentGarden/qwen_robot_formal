#!/usr/bin/env python3
"""A/B/A timing benchmark for the standalone V11 skill preloader.

Safety policy: only argparse --help, model zero-input checks, camera reads,
ROS client construction, dry-runs, and appliance status queries are allowed.
No actuator-changing command is present in this file.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "preload"
PRELOADER = ROOT / "preload_all.sh"


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, "ts": round(time.time(), 3), **payload}, ensure_ascii=False), flush=True)


def run_once(command: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return {
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "returncode": completed.returncode,
            "timed_out": False,
            "stdout_tail": completed.stdout[-300:],
            "stderr_tail": completed.stderr[-300:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "returncode": None,
            "timed_out": True,
            "stdout_tail": str(exc.stdout or "")[-300:],
            "stderr_tail": str(exc.stderr or "")[-300:],
        }


def run_repeated(name: str, command: list[str], cwd: Path, repetitions: int, timeout: float) -> dict[str, Any]:
    samples = []
    for index in range(repetitions):
        result = run_once(command, cwd, timeout)
        samples.append(result)
        emit(
            "benchmark_sample",
            name=name,
            sample=index + 1,
            repetitions=repetitions,
            elapsed_ms=result["elapsed_ms"],
            returncode=result["returncode"],
            timed_out=result["timed_out"],
        )
    valid = [v["elapsed_ms"] for v in samples if not v["timed_out"]]
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd),
        "samples": samples,
        "valid_count": len(valid),
        "median_ms": round(statistics.median(valid), 3) if valid else None,
        "min_ms": round(min(valid), 3) if valid else None,
        "max_ms": round(max(valid), 3) if valid else None,
    }


def skill_import_specs() -> list[dict[str, Any]]:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    specs = []
    for item in manifest["skills"]:
        name = str(item["name"])
        script = ROOT / name / "run.py"
        if script.is_file():
            specs.append(
                {
                    "name": f"import::{name}",
                    "command": [sys.executable, str(script), "--help"],
                    "cwd": script.parent,
                    "timeout": 15.0,
                    "kind": "skill_import",
                }
            )
    return specs


def probe_specs() -> list[dict[str, Any]]:
    camera_code = (
        "import cv2,sys; d=sys.argv[1]; c=cv2.VideoCapture(d); "
        "assert c.isOpened(), d; c.set(cv2.CAP_PROP_FRAME_WIDTH,640); "
        "c.set(cv2.CAP_PROP_FRAME_HEIGHT,480); n=0; "
        "\nfor _ in range(8):\n ok,f=c.read(); n += int(bool(ok and f is not None))\n"
        "c.release(); assert n>0, (d,n)"
    )
    ros_code = (
        "import rclpy,os; from rclpy.action import ActionClient; "
        "from nav2_msgs.action import NavigateToPose; rclpy.init(); "
        "n=rclpy.create_node('preload_bench_'+str(os.getpid())); "
        "c=ActionClient(n,NavigateToPose,'navigate_to_pose'); "
        "c.destroy(); n.destroy_node(); rclpy.shutdown()"
    )
    return [
        {
            "name": "probe::all_models_no_camera",
            "command": [sys.executable, str(ROOT / "benchmark_model_probe.py"), "all"],
            "cwd": ROOT,
            "timeout": 30.0,
            "kind": "model_probe",
        },
        {
            "name": "probe::face_models",
            "command": [sys.executable, str(ROOT / "benchmark_model_probe.py"), "face"],
            "cwd": ROOT,
            "timeout": 20.0,
            "kind": "model_probe",
        },
        {
            "name": "probe::mediapipe_pose",
            "command": [sys.executable, str(ROOT / "benchmark_model_probe.py"), "pose"],
            "cwd": ROOT,
            "timeout": 15.0,
            "kind": "model_probe",
        },
        {
            "name": "probe::yolo_reid",
            "command": [
                sys.executable,
                str(ROOT / "push_up" / "pipeline.py"),
                "--config",
                str(ROOT / "push_up" / "config.json"),
                "check",
            ],
            "cwd": ROOT / "push_up",
            "timeout": 20.0,
            "kind": "model_probe",
        },
        {
            "name": "probe::front_camera_8_frames",
            "command": [sys.executable, "-c", camera_code, "/dev/video22"],
            "cwd": ROOT,
            "timeout": 10.0,
            "kind": "camera_probe",
        },
        {
            "name": "probe::back_camera_8_frames",
            "command": [sys.executable, "-c", camera_code, "/dev/video31"],
            "cwd": ROOT,
            "timeout": 10.0,
            "kind": "camera_probe",
        },
        {
            "name": "probe::ros_action_client",
            "command": [sys.executable, "-c", ros_code],
            "cwd": ROOT,
            "timeout": 10.0,
            "kind": "ros_probe",
        },
        {
            "name": "probe::light_status_read_only",
            "command": ["bash", str(ROOT / "light_control" / "run.sh"), "status"],
            "cwd": ROOT / "light_control",
            "timeout": 15.0,
            "kind": "network_probe",
        },
        {
            "name": "probe::feeder_status_read_only",
            "command": ["bash", str(ROOT / "feeder_control" / "run.sh"), "status"],
            "cwd": ROOT / "feeder_control",
            "timeout": 15.0,
            "kind": "network_probe",
        },
        {
            "name": "probe::environment_dry_run",
            "command": [
                "bash",
                str(ROOT / "environment_perception" / "run.sh"),
                "--purpose",
                "general",
                "--camera",
                "front",
                "--dry-run",
            ],
            "cwd": ROOT / "environment_perception",
            "timeout": 10.0,
            "kind": "dry_run_probe",
        },
        {
            "name": "probe::navigation_list",
            "command": ["bash", str(ROOT / "navigation_list" / "run.sh"), "list"],
            "cwd": ROOT / "navigation_list",
            "timeout": 10.0,
            "kind": "dry_run_probe",
        },
    ]


def phase(name: str, specs: list[dict[str, Any]], repetitions: int) -> dict[str, Any]:
    emit("benchmark_phase_started", phase=name, cases=len(specs), repetitions=repetitions)
    started = time.perf_counter()
    cases = {}
    for index, spec in enumerate(specs):
        emit("benchmark_case_started", phase=name, index=index + 1, total=len(specs), name=spec["name"])
        cases[spec["name"]] = {
            "kind": spec["kind"],
            **run_repeated(spec["name"], spec["command"], spec["cwd"], repetitions, spec["timeout"]),
        }
    result = {"name": name, "elapsed_sec": round(time.perf_counter() - started, 3), "cases": cases}
    emit("benchmark_phase_finished", phase=name, elapsed_sec=result["elapsed_sec"])
    return result


def preloader(action: str) -> None:
    completed = subprocess.run(
        ["bash", str(PRELOADER), action],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    emit("preloader_action", action=action, returncode=completed.returncode, output=completed.stdout[-500:])
    if completed.returncode != 0:
        raise RuntimeError(f"preloader {action} failed: {completed.stdout}")


def summarize(phases: dict[str, Any]) -> list[dict[str, Any]]:
    names = sorted(phases["preloader_on"]["cases"])
    rows = []
    for name in names:
        before = phases["preloader_off_before"]["cases"][name]
        during = phases["preloader_on"]["cases"][name]
        after = phases["preloader_off_after"]["cases"][name]
        off_values = [v for v in (before["median_ms"], after["median_ms"]) if isinstance(v, (int, float))]
        off = statistics.median(off_values) if off_values else None
        on = during["median_ms"]
        delta = (on - off) if isinstance(on, (int, float)) and isinstance(off, (int, float)) else None
        percent = (delta / off * 100.0) if isinstance(delta, (int, float)) and off else None
        rows.append(
            {
                "name": name,
                "kind": during["kind"],
                "off_median_ms": round(off, 3) if off is not None else None,
                "on_median_ms": on,
                "delta_ms": round(delta, 3) if delta is not None else None,
                "change_percent": round(percent, 2) if percent is not None else None,
                "off_before_median_ms": before["median_ms"],
                "off_after_median_ms": after["median_ms"],
                "on_returncodes": [v["returncode"] for v in during["samples"]],
                "on_timeouts": sum(1 for v in during["samples"] if v["timed_out"]),
            }
        )
    return rows


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# V11 Skill 预加载 A/B/A 实测报告",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "测试顺序：预加载关闭 → 预加载常驻 → 再次关闭。关闭数据取前后两阶段中位数，减少系统缓存和温度漂移影响。",
        "",
        "| 测试项 | 类型 | 关闭时中位数(ms) | 开启时中位数(ms) | 变化(ms) | 变化率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["summary"]:
        values = [row.get(k) for k in ("off_median_ms", "on_median_ms", "delta_ms", "change_percent")]
        fmt = ["—" if v is None else f"{v:.2f}" for v in values]
        lines.append(f"| {row['name']} | {row['kind']} | {fmt[0]} | {fmt[1]} | {fmt[2]} | {fmt[3]}% |")
    lines.extend(
        [
            "",
            "负变化表示开启预加载后更快，正变化表示更慢。所有测试均未发送硬件动作。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--imports-only", action="store_true")
    args = parser.parse_args()
    repetitions = max(1, min(args.repetitions, 10))
    specs = skill_import_specs()
    if not args.imports_only:
        specs += probe_specs()

    RUNTIME.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    phases: dict[str, Any] = {}
    preloader("stop")
    try:
        phases["preloader_off_before"] = phase("preloader_off_before", specs, repetitions)
        preloader("daemon")
        phases["preloader_on"] = phase("preloader_on", specs, repetitions)
        preloader("stop")
        phases["preloader_off_after"] = phase("preloader_off_after", specs, repetitions)
    finally:
        try:
            preloader("stop")
        except Exception as exc:
            emit("cleanup_warning", error=str(exc))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "safety": {
            "motion_commands": False,
            "navigation_goals": False,
            "head_commands": False,
            "appliance_state_changes": False,
            "tts_audio": False,
            "camera_reads_only": True,
            "network_status_reads_only": True,
        },
        "repetitions_per_phase": repetitions,
        "phases": phases,
        "summary": summarize(phases),
    }
    json_path = RUNTIME / f"benchmark_{stamp}.json"
    md_path = RUNTIME / f"benchmark_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    (RUNTIME / "benchmark_latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (RUNTIME / "benchmark_latest.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    emit("benchmark_complete", json=str(json_path), markdown=str(md_path), cases=len(report["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
