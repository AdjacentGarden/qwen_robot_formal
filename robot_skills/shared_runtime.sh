#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/runtime/shared_runtime"
PID_FILE="$STATE/server.pid"
SOCKET="$STATE/inference.sock"
LOG="$STATE/server.log"
ACTION="${1:-start}"
mkdir -p "$STATE"

set +u
source /opt/ros/humble/setup.bash
if [[ -f /home/test/car_real_copy_zhenghang/install/setup.bash ]]; then
  source /home/test/car_real_copy_zhenghang/install/setup.bash
fi
if [[ -f /home/test/new_project_optimized_v11_navsafe/startup/ros_transport_env.sh ]]; then
  # Standalone Car_real_copy terminals use the ROS system default unless the
  # operator explicitly selects another transport for the whole stack.
  export V8_DDS_TRANSPORT="${V8_DDS_TRANSPORT:-system_default}"
  source /home/test/new_project_optimized_v11_navsafe/startup/ros_transport_env.sh
fi
set -u

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(tr -cd '0-9' < "$PID_FILE")"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= | grep -Fq "$ROOT/shared_runtime_server.py" || return 1
  printf '%s' "$pid"
}

case "$ACTION" in
  start)
    if pid="$(running_pid)"; then echo "共享运行时已经运行，PID=$pid"; exit 0; fi
    exec env PYTHONUNBUFFERED=1 python3 "$ROOT/shared_runtime_server.py"
    ;;
  daemon)
    if pid="$(running_pid)"; then echo "共享运行时已经运行，PID=$pid"; exit 0; fi
    rm -f "$SOCKET"
    nohup env PYTHONUNBUFFERED=1 python3 "$ROOT/shared_runtime_server.py" >"$LOG" 2>&1 &
    pid=$!
    for _ in $(seq 1 120); do
      if [[ -S "$SOCKET" ]] && python3 "$ROOT/shared_runtime_client.py" ping >/dev/null 2>&1; then
        echo "共享运行时已就绪，PID=$pid，日志：$LOG"
        exit 0
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "共享运行时启动失败：" >&2
        tail -100 "$LOG" >&2 || true
        exit 1
      fi
      sleep 0.5
    done
    echo "共享运行时启动超时：$LOG" >&2
    exit 1
    ;;
  status)
    if pid="$(running_pid)"; then
      echo "共享运行时正在运行，PID=$pid"
      python3 "$ROOT/shared_runtime_client.py" ping
    else
      echo "共享运行时未运行"
    fi
    [[ ! -f "$STATE/status.json" ]] || python3 -m json.tool "$STATE/status.json"
    ;;
  stop)
    if ! pid="$(running_pid)"; then
      echo "共享运行时未运行"
      rm -f "$PID_FILE" "$SOCKET"
      exit 0
    fi
    kill -INT "$pid"
    for _ in $(seq 1 40); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "共享运行时未能及时停止，PID=$pid" >&2
      exit 1
    fi
    echo "共享运行时已停止"
    ;;
  *)
    echo "用法：bash shared_runtime.sh {start|daemon|status|stop}" >&2
    exit 2
    ;;
esac
