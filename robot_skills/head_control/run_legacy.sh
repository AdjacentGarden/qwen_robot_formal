#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export SINGLE_FUNCTION_ROOT="$DIR"
export SINGLE_FUNCTION_RUNTIME_DIR="${SINGLE_FUNCTION_RUNTIME_DIR:-$DIR/runtime}"
mkdir -p "$SINGLE_FUNCTION_RUNTIME_DIR"

export AMENT_TRACE_SETUP_FILES="${AMENT_TRACE_SETUP_FILES:-}"
if [ "${ROBOT_ROS_ENV_READY:-0}" != "1" ]; then
    set +u
    if [ -f /opt/ros/humble/setup.bash ]; then
        source /opt/ros/humble/setup.bash
    fi
    CAR_REAL_WS="${CAR_REAL_WS:-/home/test/Car_real_copy}"
    if [ -f "$CAR_REAL_WS/install/setup.bash" ]; then
        source "$CAR_REAL_WS/install/setup.bash"
    fi
    set -u
fi

exec python3 "$DIR/run.py" "$@"
