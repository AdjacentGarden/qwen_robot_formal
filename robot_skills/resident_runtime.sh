#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/runtime/resident"
PID_FILE="$STATE/server.pid"
LOG_FILE="$STATE/server.log"
SOCKET="$STATE/skills.sock"
PET_PID_FILE="$STATE/pet.pid"
PET_SOCKET="$STATE/pet.sock"
PET_LOG_FILE="$STATE/pet.log"
CAMERA_PID_FILE="$STATE/camera.pid"
CAMERA_STATUS_FILE="$STATE/camera_status.json"
CAMERA_MANIFEST="$STATE/cameras.json"
CAMERA_LOG_FILE="$STATE/camera.log"
ACTION="${1:-status}"
RESIDENT_CPUSET="${PROJECT_INFRA_CPUSET:-0-5}"

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

terminate_pid() {
  local pid="${1:-}"
  local wait_seconds="${2:-8}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  local deadline=$((SECONDS + wait_seconds))
  while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.1; done
  if kill -0 "$pid" 2>/dev/null; then
    echo "process $pid did not stop within ${wait_seconds}s" >&2
    return 1
  fi
}

cleanup_failed_start() {
  local server_pid="${1:-}"
  local pet_pid="${2:-}"
  local camera_pid="${3:-}"
  terminate_pid "$server_pid" 12 || true
  terminate_pid "$pet_pid" 12 || true
  terminate_pid "$camera_pid" 12 || true
  rm -f "$PID_FILE" "$SOCKET" "$PET_PID_FILE" "$PET_SOCKET" "$CAMERA_PID_FILE" "$CAMERA_MANIFEST"
}

case "$ACTION" in
  start)
    mkdir -p "$STATE"
    if alive; then
      echo "resident runtime already running: pid=$(cat "$PID_FILE")"
      exit 0
    fi
    rm -f "$PID_FILE" "$SOCKET" "$PET_PID_FILE" "$PET_SOCKET" "$CAMERA_PID_FILE" "$CAMERA_STATUS_FILE" "$CAMERA_MANIFEST"
    set +u
    source /opt/ros/humble/setup.bash
    source /home/test/Car_real_copy/install/setup.bash
    set -u
    if [[ -r "$ROOT/config/modelscope.env" ]]; then
      set -a
      # Dedicated credential file; values are never printed to logs/status.
      source "$ROOT/config/modelscope.env"
      set +a
    fi
    export PYTHONUNBUFFERED=1
    export PYTHONIOENCODING=utf-8
    export PYTHONPATH="$ROOT:/home/test/new_project_optimized_v11_navsafe:/home/test/new_project:${PYTHONPATH:-}"
    export SINGLE_FUNCTION_ROOT="$ROOT"
    export SINGLE_FUNCTION_RUNTIME_DIR="$ROOT/runtime"
    export FACE_DB_PATH="$ROOT/face_data/faces.db"
    export RESIDENT_CAMERA_MANIFEST="$CAMERA_MANIFEST"
    export RESIDENT_EXTERNAL_PET_WORKER=1
    export PET_TRACKING_RKNN_DEVICE="${PET_TRACKING_RKNN_DEVICE:-0004:41:00.0}"
    nohup taskset -c "$RESIDENT_CPUSET" python3 "$ROOT/resident_camera_broker.py" >>"$CAMERA_LOG_FILE" 2>&1 &
    camera_pid=$!
    echo "$camera_pid" >"$CAMERA_PID_FILE"
    camera_deadline=$((SECONDS + 12))
    while (( SECONDS < camera_deadline )); do
      [[ -f "$CAMERA_MANIFEST" && -f "$CAMERA_STATUS_FILE" ]] && break
      if ! kill -0 "$camera_pid" 2>/dev/null; then
        tail -n 100 "$CAMERA_LOG_FILE" || true
        cleanup_failed_start "" "" "$camera_pid"
        exit 1
      fi
      sleep 0.1
    done
    if [[ ! -f "$CAMERA_MANIFEST" || ! -f "$CAMERA_STATUS_FILE" ]]; then
      echo "resident camera broker startup timed out"
      tail -n 100 "$CAMERA_LOG_FILE" || true
      cleanup_failed_start "" "" "$camera_pid"
      exit 1
    fi
    nohup taskset -c "$RESIDENT_CPUSET" python3 "$ROOT/resident_pet_worker.py" >>"$PET_LOG_FILE" 2>&1 &
    pet_pid=$!
    echo "$pet_pid" >"$PET_PID_FILE"
    pet_deadline=$((SECONDS + 30))
    while (( SECONDS < pet_deadline )); do
      [[ -S "$PET_SOCKET" ]] && break
      if ! kill -0 "$pet_pid" 2>/dev/null; then
        tail -n 100 "$PET_LOG_FILE" || true
        cleanup_failed_start "" "$pet_pid" "$camera_pid"
        exit 1
      fi
      sleep 0.1
    done
    if [[ ! -S "$PET_SOCKET" ]]; then
      echo "resident pet worker startup timed out"
      tail -n 100 "$PET_LOG_FILE" || true
      cleanup_failed_start "" "$pet_pid" "$camera_pid"
      exit 1
    fi
    nohup taskset -c "$RESIDENT_CPUSET" python3 "$ROOT/resident_runtime_server.py" >>"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"
    deadline=$((SECONDS + 45))
    while (( SECONDS < deadline )); do
      if [[ -S "$SOCKET" ]]; then
        echo "resident runtime ready: pid=$pid socket=$SOCKET"
        exit 0
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        tail -n 100 "$LOG_FILE" || true
        cleanup_failed_start "$pid" "$pet_pid" "$camera_pid"
        exit 1
      fi
      sleep 0.1
    done
    echo "resident runtime startup timed out"
    tail -n 100 "$LOG_FILE" || true
    cleanup_failed_start "$pid" "$pet_pid" "$camera_pid"
    exit 1
    ;;
  stop)
    if alive; then
      pid="$(cat "$PID_FILE")"
      terminate_pid "$pid" 12
    fi
    if [[ -f "$PET_PID_FILE" ]]; then
      pet_pid="$(cat "$PET_PID_FILE" 2>/dev/null || true)"
      terminate_pid "$pet_pid" 12
    fi
    if [[ -f "$CAMERA_PID_FILE" ]]; then
      camera_pid="$(cat "$CAMERA_PID_FILE" 2>/dev/null || true)"
      terminate_pid "$camera_pid" 12
    fi
    rm -f "$PID_FILE" "$SOCKET" "$PET_PID_FILE" "$PET_SOCKET" "$CAMERA_PID_FILE" "$CAMERA_MANIFEST"
    echo "resident runtime stopped"
    ;;
  restart)
    "$ROOT/resident_runtime.sh" stop
    "$ROOT/resident_runtime.sh" start
    ;;
  status)
    if alive; then
      python3 "$ROOT/resident_skill_client.py" __status__
    else
      echo '{"state":"stopped"}'
      exit 1
    fi
    ;;
  log)
    tail -n "${2:-100}" "$LOG_FILE"
    ;;
  *)
    echo "usage: bash $0 {start|stop|restart|status|log [lines]}" >&2
    exit 2
    ;;
esac
