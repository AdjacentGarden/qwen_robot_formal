#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export SINGLE_FUNCTION_ROOT="$DIR"
export SINGLE_FUNCTION_RUNTIME_DIR="${SINGLE_FUNCTION_RUNTIME_DIR:-$DIR/runtime}"

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
front = os.environ.get("FRONT_CAMERA_ID") or cameras.get("front", {}).get("device") or "/dev/video22"
back = os.environ.get("BACK_CAMERA_ID") or cameras.get("back", {}).get("device") or "/dev/video22"
print(f"export FRONT_CAMERA_ID={shlex.quote(str(front))}")
print(f"export BACK_CAMERA_ID={shlex.quote(str(back))}")
PY
)"

# ! modelscope api key
export MODELSCOPE_SDK_TOKEN="${MODELSCOPE_SDK_TOKEN:-replace_me}"
exec python3 "$DIR/run.py" "$@"
