#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="/home/test/new_project_optimized_v11_navsafe"

export PYTHONPATH="$PROJECT_ROOT:$DIR:${PYTHONPATH:-}"
export PYTHONUNBUFFERED="1"
export PYTHONIOENCODING="utf-8"
export SINGLE_FUNCTION_SPEECH_EVENTS="1"
export ROBOT_PROJECT_CONFIG="${ROBOT_PROJECT_CONFIG:-$PROJECT_ROOT/config/hardware.json}"
export PUSHUP_IDENTITY="${PUSHUP_IDENTITY:-zhangsan}"

exec python3 "$DIR/run.py" "$@"
