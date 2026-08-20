#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export SINGLE_FUNCTION_ROOT="$DIR"
export SINGLE_FUNCTION_RUNTIME_DIR="${SINGLE_FUNCTION_RUNTIME_DIR:-$DIR/runtime}"
export FACE_CAMERA_ID="${FACE_CAMERA_ID:-/dev/video22}"
export FACE_CAMERA_WIDTH="${FACE_CAMERA_WIDTH:-640}"
export FACE_CAMERA_HEIGHT="${FACE_CAMERA_HEIGHT:-640}"
export PET_CAMERA_GUI="${PET_CAMERA_GUI:-0}"

if [ "$(id -u)" = "0" ]; then
    echo "[pet_tracking] Do not run this skill as root." >&2
    echo "[pet_tracking] Run as user test: cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills && bash pet_tracking/run.sh --duration 5" >&2
    echo "[pet_tracking] Root creates root-owned runtime files and may not join the same ROS2 runtime cleanly." >&2
    exit 1
fi

mkdir -p "$SINGLE_FUNCTION_RUNTIME_DIR"
if [ ! -w "$SINGLE_FUNCTION_RUNTIME_DIR" ]; then
    echo "[pet_tracking] Runtime dir is not writable: $SINGLE_FUNCTION_RUNTIME_DIR" >&2
    echo "[pet_tracking] Fix: sudo chown -R test:test $SINGLE_FUNCTION_RUNTIME_DIR" >&2
    exit 1
fi

if [ "${PET_TRACKING_SKIP_DEVICE_CHECK:-0}" != "1" ] && [ -e /dev/dma_heap/system ]; then
    if [ ! -r /dev/dma_heap/system ] || [ ! -w /dev/dma_heap/system ]; then
        echo "[pet_tracking] RKNN/NPU device permission denied: /dev/dma_heap/system" >&2
        echo "[pet_tracking] Run: sudo chmod a+rw /dev/dma_heap/system /dev/dri/renderD128" >&2
        echo "[pet_tracking] Or reload/reboot after ensuring /etc/udev/rules.d/99-rknn-dma-heap.rules is installed." >&2
        exit 1
    fi
fi
for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
        exec python3 "$DIR/run.py" "$@"
    fi
done

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
