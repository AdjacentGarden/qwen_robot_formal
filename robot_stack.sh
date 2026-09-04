#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAR_ROOT="${CAR_REAL_WS:-/home/test/Car_real_copy}"
RUN_DIR="$ROOT/runtime/robot_stack"
LOG_DIR="$RUN_DIR/logs"
MANAGER_PID_FILE="$RUN_DIR/manager.pid"
MANAGER_LOG_FILE="$LOG_DIR/manager.log"
HEALTH_MONITOR="$ROOT/ros_health_monitor.py"
STARTUP_HEAD_GUARD="$ROOT/robot_skills/car_real_startup_guard.py"
HEALTH_PID_FILE="$RUN_DIR/health_monitor.pid"
HEALTH_STATE_FILE="$RUN_DIR/health.json"
HEALTH_LOG_FILE="$LOG_DIR/health_monitor.log"
MANAGER_READY_TIMEOUT="${QWEN_MANAGER_READY_TIMEOUT:-330}"
MANAGER_START_ATTEMPTS="${QWEN_MANAGER_START_ATTEMPTS:-2}"
MANAGER_RETRY_DELAY="${QWEN_MANAGER_RETRY_DELAY_SEC:-5}"
EXISTING_MANAGER_READY_TIMEOUT="${QWEN_EXISTING_MANAGER_READY_TIMEOUT:-75}"
INSTALLED_MAP="$CAR_ROOT/install/robot_bringup/share/robot_bringup/map/map.pbstream"
mkdir -p "$RUN_DIR" "$LOG_DIR"
MANAGER_REUSED=0

source_ros() {
  set +u
  source /opt/ros/humble/setup.bash
  source "$CAR_ROOT/install/setup.bash"
  set -u
}

process_group_has_members() {
  local pgid="$1"
  ps -eo pgid= | awk -v target="$pgid" '$1 == target { found=1 } END { exit !found }'
}

pid_matches() {
  local pid="$1" pattern="$2" cmdline
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"$pattern"* ]]
}

managed_manager_pid() {
  local pid
  [[ -s "$MANAGER_PID_FILE" ]] || return 1
  pid="$(<"$MANAGER_PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pid_matches "$pid" "mapping_navigation_manager.py" || return 1
  printf '%s\n' "$pid"
}

health_monitor_pid() {
  local pid
  [[ -s "$HEALTH_PID_FILE" ]] || return 1
  pid="$(<"$HEALTH_PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pid_matches "$pid" "$HEALTH_MONITOR" || return 1
  printf '%s\n' "$pid"
}

health_value() {
  python3 - "$HEALTH_STATE_FILE" "$1" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    for part in sys.argv[2].split("."):
        value = value[part]
    if isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)
except Exception:
    raise SystemExit(1)
PY
}

validate_plan() {
  local required size
  for required in \
    /opt/ros/humble/setup.bash \
    "$CAR_ROOT/install/setup.bash" \
    "$CAR_ROOT/install/robot_bringup/lib/robot_bringup/mapping_navigation_manager.py" \
    "$CAR_ROOT/install/motion_controller/share/motion_controller/msg/NavGoal.idl" \
    "$HEALTH_MONITOR" \
    "$STARTUP_HEAD_GUARD"; do
    [[ -f "$required" ]] || { echo "缺少 Manager 依赖：$required" >&2; return 1; }
  done
  [[ -f "$INSTALLED_MAP" ]] || {
    echo "拒绝启动：Car_real_copy 已安装定位地图不存在；禁止 Manager 回退到自动建图" >&2
    return 1
  }
  size="$(wc -c <"$INSTALLED_MAP")"
  ((size > 10240)) || {
    echo "拒绝启动：Car_real_copy 已安装定位地图过小（${size} bytes）；禁止自动建图" >&2
    return 1
  }
}

ensure_health_monitor() {
  local pid deadline temporary
  if pid="$(health_monitor_pid 2>/dev/null)"; then
    echo "[health] Manager 健康检查器已运行，PID=$pid"
    return 0
  fi
  rm -f "$HEALTH_PID_FILE" "$HEALTH_STATE_FILE"
  setsid python3 "$HEALTH_MONITOR" --run-dir "$RUN_DIR" >>"$HEALTH_LOG_FILE" 2>&1 </dev/null &
  pid=$!
  temporary="$HEALTH_PID_FILE.$$.tmp"
  printf '%s\n' "$pid" >"$temporary"
  mv -f "$temporary" "$HEALTH_PID_FILE"
  deadline=$((SECONDS + 8))
  while ((SECONDS < deadline)); do
    kill -0 "$pid" 2>/dev/null || { tail -n 60 "$HEALTH_LOG_FILE" >&2 || true; return 1; }
    [[ -r "$HEALTH_STATE_FILE" ]] && return 0
    sleep 0.2
  done
  echo "Manager 健康检查器未生成状态" >&2
  return 1
}

stop_health_monitor() {
  local pid deadline
  if ! pid="$(health_monitor_pid 2>/dev/null)"; then
    rm -f "$HEALTH_PID_FILE" "$HEALTH_STATE_FILE"
    return 0
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  deadline=$((SECONDS + 5))
  while kill -0 "$pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 0.1; done
  rm -f "$HEALTH_PID_FILE" "$HEALTH_STATE_FILE"
}

manager_graph_present() {
  pgrep -f 'mapping_navigation_manager[.]py' >/dev/null 2>&1
}

manager_ready() {
  [[ "$(health_value ready.manager 2>/dev/null || true)" == "true" ]]
}

wait_existing_manager_ready() {
  local deadline state event
  deadline=$((SECONDS + EXISTING_MANAGER_READY_TIMEOUT))
  while manager_graph_present && ((SECONDS < deadline)); do
    if manager_ready; then
      return 0
    fi
    state="$(health_value manager.state 2>/dev/null || true)"
    event="$(health_value manager.event 2>/dev/null || true)"
    if [[ "$state" == "SAFE_STOP" ]]; then
      echo "[manager] 已有 Manager 处于 SAFE_STOP：${event:-unknown reason}" >&2
      return 1
    fi
    # A freshly created read-only health monitor needs a short DDS discovery
    # window before it can see the already healthy transient-local state.  Do
    # not tear down that Manager merely because the first sample is incomplete.
    sleep 0.2
  done
  return 1
}

start_manager_once() {
  local pid
  if manager_ready; then
    MANAGER_REUSED=1
    echo "[manager] 检测到现有 Manager 已处于 NAVIGATION，直接复用"
    return 0
  fi
  MANAGER_REUSED=0
  if manager_graph_present; then
    echo "[manager] 检测到已有 Manager，等待健康状态收敛，不叠加第二个实例"
    if wait_existing_manager_ready; then
      MANAGER_REUSED=1
      echo "[manager] 已有 Manager 健康状态确认完成，直接复用"
      return 0
    fi
    echo "[manager] 已有 Manager 在 ${EXISTING_MANAGER_READY_TIMEOUT}s 内仍未就绪，拒绝叠加第二个 Manager" >&2
    return 1
  fi
  rm -f "$MANAGER_PID_FILE"
  echo "[manager] 启动 Car_real_copy MappingNavigationManager（init=false）"
  setsid bash -lc '
    set -euo pipefail
    set +u
    source /opt/ros/humble/setup.bash
    source /home/test/Car_real_copy/install/setup.bash
    set -u
    exec ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
      -p workspace_root:=/home/test/Car_real_copy \
      -p use_sim_time:=false -p use_rviz:=false -p init:=false
  ' >>"$MANAGER_LOG_FILE" 2>&1 </dev/null &
  pid=$!
  printf '%s\n' "$pid" >"$MANAGER_PID_FILE"
  sleep 0.5
  kill -0 "$pid" 2>/dev/null || {
    tail -n 100 "$MANAGER_LOG_FILE" >&2 || true
    rm -f "$MANAGER_PID_FILE"
    return 1
  }
}

ensure_startup_head_level() {
  if [[ "$MANAGER_REUSED" == "1" ]]; then
    echo "[manager] 复用已经完整就绪的 Manager，不重复执行启动期头部动作"
    return 0
  fi
  echo "[manager] 启动期先门控传感器、恢复平视，再等待完整定位"
  timeout 40 python3 "$STARTUP_HEAD_GUARD" --level-angle 185 --timeout 32 \
    >>"$MANAGER_LOG_FILE" 2>&1
}

wait_manager_ready() {
  local deadline state event pid
  deadline=$((SECONDS + MANAGER_READY_TIMEOUT))
  while ((SECONDS < deadline)); do
    state="$(health_value manager.state 2>/dev/null || true)"
    event="$(health_value manager.event 2>/dev/null || true)"
    if [[ "$state" == "SAFE_STOP" ]]; then
      echo "[manager] SAFE_STOP：${event:-unknown reason}" >&2
      return 1
    fi
    if manager_ready; then
      echo "[manager] NAVIGATION 就绪，sensor gate 已恢复，导航接口可用"
      return 0
    fi
    if pid="$(managed_manager_pid 2>/dev/null)"; then :; elif [[ -s "$MANAGER_PID_FILE" ]]; then
      echo "[manager] 启动进程提前退出" >&2
      tail -n 100 "$MANAGER_LOG_FILE" >&2 || true
      return 1
    fi
    sleep 0.5
  done
  echo "[manager] ${MANAGER_READY_TIMEOUT}s 内未完整就绪" >&2
  [[ ! -r "$HEALTH_STATE_FILE" ]] || python3 -m json.tool "$HEALTH_STATE_FILE" >&2 || true
  return 1
}

stop_manager() {
  local pid deadline
  if ! pid="$(managed_manager_pid 2>/dev/null)"; then
    rm -f "$MANAGER_PID_FILE"
    echo "[manager] 没有本项目拥有的 Manager；不关闭外部 Manager"
    return 0
  fi
  source_ros
  timeout 5 ros2 service call /mapping_manager/shutdown std_srvs/srv/Trigger '{}' >/dev/null 2>&1 || true
  deadline=$((SECONDS + 30))
  while process_group_has_members "$pid" && ((SECONDS < deadline)); do sleep 0.2; done
  if process_group_has_members "$pid"; then
    kill -INT -- "-$pid" 2>/dev/null || true
    deadline=$((SECONDS + 8))
    while process_group_has_members "$pid" && ((SECONDS < deadline)); do sleep 0.2; done
  fi
  if process_group_has_members "$pid"; then
    echo "[manager] 专属进程组仍未退出，拒绝静默遗留" >&2
    return 1
  fi
  rm -f "$MANAGER_PID_FILE"
  echo "[manager] 已通过 Manager 统一关闭"
}

start_all() {
  local attempt
  validate_plan
  source_ros
  ensure_health_monitor
  for ((attempt=1; attempt<=MANAGER_START_ATTEMPTS; attempt++)); do
    if start_manager_once && ensure_startup_head_level && wait_manager_ready; then
      echo "Car_real_copy Manager 已统一接管底盘、里程计、定位、导航、头部雷达保护和 SAFE_STOP"
      return 0
    fi
    echo "[manager] 第 ${attempt}/${MANAGER_START_ATTEMPTS} 次未就绪" >&2
    stop_manager || return 1
    ((attempt == MANAGER_START_ATTEMPTS)) || sleep "$MANAGER_RETRY_DELAY"
  done
  stop_health_monitor || true
  return 1
}

stop_all() {
  local failed=0
  stop_manager || failed=1
  stop_health_monitor || failed=1
  return "$failed"
}

status_all() {
  local pid state gate event ownership
  source_ros
  state="$(health_value manager.state 2>/dev/null || echo not_running)"
  gate="$(health_value manager.sensor_gate_state 2>/dev/null || echo unknown)"
  event="$(health_value manager.event 2>/dev/null || true)"
  ownership="external-or-stopped"
  if pid="$(managed_manager_pid 2>/dev/null)"; then ownership="managed pid=$pid"; fi
  echo "[manager] state=$state gate=$gate ($ownership)"
  [[ -z "$event" ]] || echo "[manager] event=$event"
  manager_ready
}

show_plan() {
  validate_plan
  cat <<EOF
工作区：${CAR_ROOT}（只读依赖，本脚本不会修改）
唯一启动入口：MappingNavigationManager
  1. 启动前验证已安装 map.pbstream，失败即停止，绝不回退自动建图
  2. Manager 统一启动 base、RF2O/EKF、motion_controller、定位和 Nav2
  3. 等待 /mapping_manager/state=NAVIGATION、sensor gate=ready 和导航链路就绪
  4. SAFE_STOP 立即阻止全部 Qwen/App 非零运动，不做无限或静默重启
  5. 停止时调用 /mapping_manager/shutdown，仅清理本项目拥有的 Manager 进程组
EOF
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi
case "${1:-}" in
  start) start_all ;;
  stop) stop_all ;;
  status) status_all ;;
  plan) show_plan ;;
  *) echo "用法：bash $ROOT/robot_stack.sh {plan|start|status|stop}" >&2; exit 2 ;;
esac
