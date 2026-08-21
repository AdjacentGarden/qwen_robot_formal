#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "reproduce" / "dependencies.lock.json").read_text(encoding="utf-8"))
PARSER = argparse.ArgumentParser(description="Static Qwen robot snapshot verification")
PARSER.add_argument("--full", action="store_true", help="verify every file listed in reproduce/SHA256SUMS")
ARGS = PARSER.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


required = [
    ROOT / "run.sh",
    ROOT / "realtime_chat.py",
    ROOT / "skill_host.py",
    ROOT / "robot_stack.sh",
    ROOT / "robot_skills" / "resident_runtime_server.py",
    ROOT / "robot_skills" / "navigation_goto" / "run.py",
    ROOT / "robot_skills" / "push_up" / "run.py",
    ROOT / "robot_skills" / "push_up" / "models" / "pose_landmark_full_norm255.rknn",
    ROOT / "android_app" / "web" / "index.html",
    ROOT / "android_app" / "robot_bridge" / "bridge.py",
    ROOT / "android_app" / "android" / "app" / "build.gradle",
    ROOT / "reproduce" / "vendor" / "qwen_robot_project" / "new_project" / "executor.py",
    ROOT / "reproduce" / "vendor" / "self_program" / "skill_function_specs",
    ROOT / "reproduce" / "vendor" / "new_project" / "new_project" / "reminder_cli.py",
]

errors: list[str] = []
for path in required:
    if not path.exists():
        errors.append(f"missing: {path}")

for relative, expected in LOCK.get("checksums", {}).items():
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing checksum target: {relative}")
        continue
    actual = sha256(path)
    if actual != expected:
        errors.append(f"checksum mismatch: {relative}: {actual}")

full_checked = 0
if ARGS.full:
    manifest = ROOT / "reproduce" / "SHA256SUMS"
    if not manifest.is_file():
        errors.append("missing: reproduce/SHA256SUMS")
    else:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            expected, relative = line.split("  ", 1)
            path = ROOT / relative
            full_checked += 1
            if not path.is_file():
                errors.append(f"missing manifest target: {relative}")
            elif sha256(path) != expected:
                errors.append(f"manifest checksum mismatch: {relative}")

secret_paths = [
    ROOT / "robot_skills" / "config" / "modelscope.env",
    ROOT / "robot_skills" / "face_data" / "faces.db",
    ROOT / "android_app" / "release" / "ideal-robot-qwen-2.2.1-realtime-link.apk",
]
for path in secret_paths:
    if path.exists():
        errors.append(f"private runtime file must not be tracked in snapshot: {path.relative_to(ROOT)}")

if errors:
    print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
    sys.exit(1)

print(
    json.dumps(
        {
            "ok": True,
            "root": str(ROOT),
            "checked_required_paths": len(required),
            "checked_hashes": len(LOCK.get("checksums", {})),
            "checked_full_manifest_files": full_checked,
            "note": "Static verification only; no project or hardware process was started.",
        },
        ensure_ascii=False,
        indent=2,
    )
)
