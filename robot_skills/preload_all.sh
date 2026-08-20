#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$ROOT/runtime/preload"
PID_FILE="$STATE_DIR/preload.pid"
READY_FILE="$STATE_DIR/ready"
LOG_FILE="$STATE_DIR/preload.log"
ACTION="${1:-start}"

mkdir -p "$STATE_DIR"

if [[ -f /opt/ros/humble/setup.bash ]]; then
  set +u
  source /opt/ros/humble/setup.bash
  if [[ -f /home/test/car_real_copy_zhenghang/install/setup.bash ]]; then
    source /home/test/car_real_copy_zhenghang/install/setup.bash
  fi
  set -u
fi

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(tr -cd '0-9' < "$PID_FILE")"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= | grep -Fq "$ROOT/preload_runtime.py" || return 1
  printf '%s' "$pid"
}

case "$ACTION" in
  start)
    if pid="$(running_pid)"; then
      echo "预加载器已经运行，PID=$pid"
      exit 0
    fi
    exec env PYTHONUNBUFFERED=1 python3 "$ROOT/preload_runtime.py" --keepalive
    ;;
  once)
    exec env PYTHONUNBUFFERED=1 python3 "$ROOT/preload_runtime.py"
    ;;
  daemon)
    if pid="$(running_pid)"; then
      echo "预加载器已经运行，PID=$pid"
      exit 0
    fi
    rm -f "$READY_FILE"
    nohup env PYTHONUNBUFFERED=1 python3 "$ROOT/preload_runtime.py" --keepalive >"$LOG_FILE" 2>&1 &
    pid=$!
    for _ in $(seq 1 120); do
      if [[ -f "$READY_FILE" ]]; then
        echo "预加载完成，PID=$pid，日志：$LOG_FILE"
        exit 0
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "预加载失败，日志如下：" >&2
        tail -80 "$LOG_FILE" >&2 || true
        exit 1
      fi
      sleep 0.5
    done
    echo "等待预加载完成超时，查看：$LOG_FILE" >&2
    exit 1
    ;;
  status)
    if pid="$(running_pid)"; then
      echo "预加载器正在运行，PID=$pid"
    else
      echo "预加载器未运行"
    fi
    if [[ -f "$STATE_DIR/status.json" ]]; then
      python3 -m json.tool "$STATE_DIR/status.json"
    fi
    ;;
  stop)
    if ! pid="$(running_pid)"; then
      echo "预加载器未运行"
      rm -f "$PID_FILE" "$READY_FILE"
      exit 0
    fi
    kill -INT "$pid"
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "预加载器未能及时退出，PID=$pid" >&2
      exit 1
    fi
    echo "预加载器已停止"
    ;;
  *)
    echo "用法：bash preload_all.sh {start|once|daemon|status|stop}" >&2
    exit 2
    ;;
esac
