#!/usr/bin/env python3
"""Measure fresh client-process wall time against the persistent runtime."""

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CLIENT = ROOT / "shared_runtime_client.py"
OUT = ROOT / "runtime" / "shared_runtime" / "client_process_benchmark.json"


def measure(arguments, runs=10):
    values = []
    returncodes = []
    for _ in range(runs):
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, str(CLIENT), *arguments],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        values.append((time.perf_counter() - started) * 1000.0)
        returncodes.append(completed.returncode)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout)
    return {
        "runs": runs,
        "min_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "max_ms": round(max(values), 3),
        "mean_ms": round(statistics.mean(values), 3),
        "returncodes": returncodes,
    }


def main():
    tests = {
        "ping": ["ping"],
        "ros_ready": ["ros-ready"],
        "face_detect": ["benchmark", "--operation", "face_detect", "--runs", "1", "--warmup", "0"],
        "face_embed": ["benchmark", "--operation", "face_embed", "--runs", "1", "--warmup", "0"],
        "yolo": ["benchmark", "--operation", "yolo", "--runs", "1", "--warmup", "0"],
        "reid": ["benchmark", "--operation", "reid", "--runs", "1", "--warmup", "0"],
        "pose": ["benchmark", "--operation", "pose", "--runs", "1", "--warmup", "0"],
        "all_five_operations": ["benchmark", "--operation", "all", "--runs", "1", "--warmup", "0"],
    }
    result = {"generated_at": time.time(), "tests": {}}
    for name, arguments in tests.items():
        result["tests"][name] = measure(arguments)
        print(json.dumps({"name": name, **result["tests"][name]}, ensure_ascii=False), flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
