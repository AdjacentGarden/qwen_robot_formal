#!/usr/bin/env python3
"""Make the copied project self-contained without touching its source project."""

from __future__ import annotations

from pathlib import Path
import os
import re


ROOT = Path(__file__).resolve().parent
SOURCE = "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills"
TARGET = os.environ.get(
    "QWEN_ROBOT_SKILLS_ROOT",
    "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills",
).rstrip("/")
REPLACEMENTS = [
    (SOURCE, TARGET),
    (
        "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/push_up/models",
        f"{TARGET}/push_up/models",
    ),
    (
        "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/pet_tracking/assets/model",
        f"{TARGET}/pet_tracking/assets/model",
    ),
    (
        "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_recognition/assets/model",
        f"{TARGET}/face_recognition/assets/model",
    ),
    ("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_data", f"{TARGET}/face_data"),
    ("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime", f"{TARGET}/runtime"),
]

changed = []
for path in ROOT.rglob("*"):
    if not path.is_file():
        continue
    relative = path.relative_to(ROOT)
    if relative.parts and relative.parts[0] in {"runtime", "audit"}:
        continue
    if path.suffix.lower() not in {".py", ".sh", ".json", ".md"} and path.name not in {"README", ".gitignore"}:
        continue
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    updated = original
    for old, new in REPLACEMENTS:
        if old == SOURCE:
            updated = re.sub(re.escape(old) + r"(?!_resident)", new, updated)
        else:
            updated = updated.replace(old, new)
    if updated != original:
        path.write_text(updated, encoding="utf-8")
        changed.append(str(relative))

print(f"rewrote {len(changed)} files under {ROOT}")
for item in changed:
    print(item)
