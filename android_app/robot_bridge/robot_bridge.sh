#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT/../.." && pwd)"
PROJECT="${ROBOT_PROJECT:-$PROJECT_ROOT}"
ENV_FILE="${ROBOT_BRIDGE_ENV:-/home/test/.config/robot_android_bridge.env}"
PID_FILE="$ROOT/runtime/bridge.pid"
LOG_FILE="$ROOT/runtime/bridge.log"
mkdir -p "$ROOT/runtime"

alive() {
  [[ -s "$PID_FILE" ]] || return 1
  local pid cmdline
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"robot_bridge.sh supervise"* || "$cmdline" == *"robot_bridge.sh foreground"* || "$cmdline" == *"android_app/robot_bridge/bridge.py"* ]]
}

load_environment() {
  [[ -r "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 2; }
  set -a
  source "$ENV_FILE"
  set +a
  set +u
  source /opt/ros/humble/setup.bash
  source /home/test/Car_real_copy/install/setup.bash
  set -u
  export ROBOT_PROJECT="$PROJECT"
}

supervise() {
  trap 'exit 0' TERM INT
  while true; do
    set +e
    python3 "$ROOT/bridge.py"
    rc=$?
    set -e
    echo "$(date -Is) robot bridge worker exited: rc=$rc; restarting in 0.8s" >&2
    sleep 0.8
  done
}

start() {
  if alive; then
    echo "robot bridge already running: pid=$(cat "$PID_FILE")"
    return
  fi
  rm -f "$PID_FILE"
  load_environment
  nohup setsid bash "$0" supervise >"$LOG_FILE" 2>&1 </dev/null &
  echo $! >"$PID_FILE"
  sleep 1
  alive || { tail -n 80 "$LOG_FILE"; return 3; }
  echo "robot bridge started: pid=$(cat "$PID_FILE")"
}

foreground() {
  load_environment
  echo "$$" >"$PID_FILE"
  # Let systemd own the worker directly.  exec keeps the PID stable, avoids an
  # orphaned child when the worker crashes, and makes Restart=always immediate.
  exec python3 "$ROOT/bridge.py"
}

stop() {
  if alive; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
  echo "robot bridge stopped"
}

status() {
  if alive; then echo "robot bridge running: pid=$(cat "$PID_FILE")"; else echo "robot bridge stopped"; fi
  tail -n 10 "$LOG_FILE" 2>/dev/null || true
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  supervise) supervise ;;
  foreground) foreground ;;
  *) echo "usage: bash $0 {start|stop|restart|status|foreground}"; exit 2 ;;
esac
