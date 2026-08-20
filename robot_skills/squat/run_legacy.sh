#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export SINGLE_FUNCTION_ROOT="$DIR"
export SINGLE_FUNCTION_RUNTIME_DIR="${SINGLE_FUNCTION_RUNTIME_DIR:-$DIR/runtime}"
export PET_CONTROLLER_CLI_PATH="${PET_CONTROLLER_CLI_PATH:-/home/test/Car_real_copy/src/demo/controller_cli.py}"

eval "$(
  python3 - <<'PY'
import json
import os
import shlex

config_path = os.environ.get("ROBOT_PROJECT_CONFIG", "/home/test/new_project/config/hardware.json")
try:
    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
except Exception:
    config = {}

cameras = config.get("cameras", {}) if isinstance(config, dict) else {}
back = os.environ.get("FACE_CAMERA_ID") or cameras.get("back", {}).get("device") or "/dev/video22"
width = os.environ.get("FACE_CAMERA_WIDTH") or cameras.get("back", {}).get("width") or 640
height = os.environ.get("FACE_CAMERA_HEIGHT") or cameras.get("back", {}).get("height") or 480
print(f"export FACE_CAMERA_ID={shlex.quote(str(back))}")
print(f"export FACE_CAMERA_WIDTH={shlex.quote(str(width))}")
print(f"export FACE_CAMERA_HEIGHT={shlex.quote(str(height))}")
PY
)"

exec python3 "$DIR/run.py" "$@"
