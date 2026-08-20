#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export V8_NAVIGATION_POINTS_DB="${V8_NAVIGATION_POINTS_DB:-/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/points/named_points.json}"

TRANSPORT_ENV_SH="${V8_TRANSPORT_ENV_SH:-/home/test/new_project_optimized_v11_navsafe/startup/ros_transport_env.sh}"
if [ -f "$TRANSPORT_ENV_SH" ]; then
    # Keep this standalone entry point on the same RMW/transport as the V8 stack.
    source "$TRANSPORT_ENV_SH"
fi

for arg in "$@"; do
    if [ "$arg" = "--dry-run" ] || [ "$arg" = "list" ] || [ "$arg" = "points" ] || [ "$arg" = "show" ]; then
        exec python3 "$DIR/run.py" "$@"
    fi
done

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
