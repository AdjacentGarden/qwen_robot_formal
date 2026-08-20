#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAR_ROOT="/home/test/car_real_copy_zhenghang"
RUN_DIR="$ROOT/runtime/robot_stack"
LOG_DIR="$RUN_DIR/logs"
BASE_READY_TIMEOUT="${QWEN_BASE_READY_TIMEOUT:-30}"
BASE_START_ATTEMPTS="${QWEN_BASE_START_ATTEMPTS:-2}"
BASE_RETRY_DELAY="${QWEN_BASE_RETRY_DELAY_SEC:-3}"
ODOM_READY_TIMEOUT="${QWEN_ODOM_READY_TIMEOUT:-30}"
ODOM_START_ATTEMPTS="${QWEN_ODOM_START_ATTEMPTS:-2}"
ODOM_RETRY_DELAY="${QWEN_ODOM_RETRY_DELAY_SEC:-3}"
NAV_READY_TIMEOUT="${QWEN_NAV_READY_TIMEOUT:-75}"
NAV_START_ATTEMPTS="${QWEN_NAV_START_ATTEMPTS:-2}"
NAV_RETRY_DELAY="${QWEN_NAV_RETRY_DELAY_SEC:-3}"
HEAD_LEVEL_TIMEOUT="${QWEN_HEAD_LEVEL_TIMEOUT:-25}"
HEAD_STARTUP_TOLERANCE="${QWEN_HEAD_STARTUP_TOLERANCE_DEG:-7}"
mkdir -p "$RUN_DIR" "$LOG_DIR"

components=(base odometry navigation)
started_this_run=()

source_ros() {
  set +u
  source /opt/ros/humble/setup.bash
  source "$CAR_ROOT/install/setup.bash"
  set -u
}

pid_file() {
  printf '%s/%s.pid\n' "$RUN_DIR" "$1"
}

managed_pid() {
  local file pid
  file="$(pid_file "$1")"
  [[ -s "$file" ]] || return 1
  pid="$(<"$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

component_command() {
  case "$1" in
    base)
      printf '%s\0' ros2 launch robot_bringup real_robot_base.launch.py
      ;;
    odometry)
      printf '%s\0' ros2 launch robot_bringup real_robot_odometry.launch.py
      ;;
    navigation)
      printf '%s\0' ros2 launch robot_bringup real_robot_nav.launch.py use_rviz:=false enable_auto_navigation:=true
      ;;
    *) return 2 ;;
  esac
}

topic_has_publishers() {
  local output
  output="$(timeout 3 ros2 topic info "$1" 2>/dev/null || true)"
  grep -Eq 'Publisher count: [1-9][0-9]*' <<<"$output"
}

topic_has_subscribers() {
  local output
  output="$(timeout 3 ros2 topic info "$1" 2>/dev/null || true)"
  grep -Eq 'Subscription count: [1-9][0-9]*' <<<"$output"
}

topic_has_fresh_sample() {
  timeout 3 ros2 topic echo "$1" --once >/dev/null 2>&1
}

service_exists() {
  local output
  output="$(timeout 4 ros2 service list 2>/dev/null || true)"
  grep -qx "$1" <<<"$output"
}

action_exists() {
  local output
  output="$(timeout 4 ros2 action list 2>/dev/null || true)"
  grep -qx "$1" <<<"$output"
}

process_group_has_members() {
  local pgid="$1"
  ps -eo pgid= | awk -v target="$pgid" '$1 == target { found=1 } END { exit !found }'
}

probe_ready() {
  case "$1" in
    base)
      topic_has_subscribers /cmd_vel &&
        topic_has_fresh_sample /scan_raw &&
        topic_has_fresh_sample /imu
      ;;
    odometry)
      topic_has_fresh_sample /odom
      ;;
    navigation)
      service_exists /map_server/get_state &&
        service_exists /planner_server/get_state &&
        service_exists /bt_navigator/get_state &&
        action_exists /compute_path_to_pose &&
        action_exists /navigate_to_pose
      ;;
    *) return 2 ;;
  esac
}

ensure_head_level_for_navigation() {
  local output
  echo "[head] 导航启动前恢复平视并等待受保护雷达恢复"
  if ! output="$(timeout "$HEAD_LEVEL_TIMEOUT" python3 "$ROOT/robot_skills/head_control/run.py" level --feedback-tolerance "$HEAD_STARTUP_TOLERANCE" --json 2>&1)"; then
    echo "[head] 平视或雷达恢复失败：$output" >&2
    return 1
  fi
  if ! grep -q '"ok": true' <<<"$output"; then
    echo "[head] 平视结果无效：$output" >&2
    return 1
  fi
  if ! topic_has_fresh_sample /scan; then
    echo "[head] 已平视，但 /scan 没有恢复新数据" >&2
    return 1
  fi
  echo "[head] 已平视，/scan 已恢复"
}

start_component() {
  local name="$1" pid log
  local -a command=()
  if probe_ready "$name"; then
    echo "[$name] 检测到外部 ROS 链路已经就绪，避免重复启动"
    return 0
  fi
  if pid="$(managed_pid "$name")"; then
    echo "[$name] 已由本测试项目启动，PID=$pid"
    return 0
  fi
  rm -f "$(pid_file "$name")"
  mapfile -d '' -t command < <(component_command "$name")
  log="$LOG_DIR/$name.log"
  echo "[$name] 启动：${command[*]}"
  setsid bash -lc '
    set -euo pipefail
    set +u
    source /opt/ros/humble/setup.bash
    source /home/test/car_real_copy_zhenghang/install/setup.bash
    set -u
    exec "$@"
  ' qwen-robot-stack "${command[@]}" >>"$log" 2>&1 < /dev/null &
  pid=$!
  printf '%s\n' "$pid" >"$(pid_file "$name")"
  started_this_run+=("$name")
  sleep 0.5
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[$name] 启动进程提前退出：" >&2
    tail -n 40 "$log" >&2 || true
    rm -f "$(pid_file "$name")"
    return 1
  fi
}

wait_ready() {
  local name="$1" timeout_sec="$2" deadline pid managed_file
  deadline=$((SECONDS + timeout_sec))
  managed_file="$(pid_file "$name")"
  echo "[$name] 等待完整就绪（最多 ${timeout_sec}s）"
  while ((SECONDS < deadline)); do
    if probe_ready "$name"; then
      echo "[$name] 已就绪"
      return 0
    fi
    if pid="$(managed_pid "$name")"; then
      :
    elif [[ -s "$managed_file" ]]; then
      echo "[$name] 启动进程已经退出，请检查 $LOG_DIR/$name.log" >&2
      return 1
    fi
    sleep 0.5
  done
  echo "[$name] 在 ${timeout_sec}s 内未达到就绪条件，停止进入下一层" >&2
  return 1
}

stop_component() {
  local name="$1" file pid deadline
  file="$(pid_file "$name")"
  if ! pid="$(managed_pid "$name")"; then
    rm -f "$file"
    echo "[$name] 没有本测试项目管理的运行进程"
    return 0
  fi
  echo "[$name] 停止本测试项目启动的进程组，PID=$pid"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  deadline=$((SECONDS + 12))
  # ros2 launch can exit before children that share its process group.  Waiting
  # only for the parent left orphaned IMU drivers holding I2C-4 and made the
  # next startup look healthy while no fresh /imu data existed.
  while process_group_has_members "$pid" && ((SECONDS < deadline)); do
    sleep 0.2
  done
  if process_group_has_members "$pid"; then
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 3))
    while process_group_has_members "$pid" && ((SECONDS < deadline)); do
      sleep 0.1
    done
  fi
  if process_group_has_members "$pid"; then
    echo "[$name] 进程组 $pid 仍有残留进程，拒绝静默继续" >&2
    return 1
  fi
  rm -f "$file"
}

rollback() {
  local index
  for ((index=${#started_this_run[@]}-1; index>=0; index--)); do
    stop_component "${started_this_run[index]}" || true
  done
}

start_component_with_retry() {
  local name="$1" timeout_sec="$2" max_attempts="$3" retry_delay="$4"
  local attempt

  if ! [[ "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
    echo "[$name] 非法启动次数：${max_attempts}（必须为正整数）" >&2
    return 2
  fi

  for ((attempt=1; attempt<=max_attempts; attempt++)); do
    if start_component "$name" && wait_ready "$name" "$timeout_sec"; then
      return 0
    fi

    echo "[$name] 第 ${attempt}/${max_attempts} 次启动未就绪，完整停止该组件后重试" >&2
    stop_component "$name"
    if ((attempt < max_attempts)); then
      sleep "$retry_delay"
    fi
  done

  echo "[$name] 连续 ${max_attempts} 次启动均未就绪，终止后续启动" >&2
  return 1
}

start_all() {
  validate_plan
  source_ros
  trap rollback ERR
  start_component_with_retry \
    base "$BASE_READY_TIMEOUT" "$BASE_START_ATTEMPTS" "$BASE_RETRY_DELAY"
  ensure_head_level_for_navigation
  start_component_with_retry \
    odometry "$ODOM_READY_TIMEOUT" "$ODOM_START_ATTEMPTS" "$ODOM_RETRY_DELAY"
  start_component_with_retry \
    navigation "$NAV_READY_TIMEOUT" "$NAV_START_ATTEMPTS" "$NAV_RETRY_DELAY"
  trap - ERR
  echo "机器人底盘、雷达、里程计、定位和导航链路已按顺序就绪"
}

stop_all() {
  local index failed=0
  for ((index=${#components[@]}-1; index>=0; index--)); do
    stop_component "${components[index]}" || failed=1
  done
  return "$failed"
}

status_all() {
  local name pid state
  source_ros
  for name in "${components[@]}"; do
    state="not-ready"
    probe_ready "$name" && state="ready"
    if pid="$(managed_pid "$name")"; then
      echo "[$name] $state (managed pid=$pid)"
    else
      echo "[$name] $state (external or stopped)"
    fi
  done
}

validate_plan() {
  local required
  for required in \
    /opt/ros/humble/setup.bash \
    "$CAR_ROOT/install/setup.bash" \
    "$CAR_ROOT/install/robot_bringup/share/robot_bringup/launch/real_robot_base.launch.py" \
    "$CAR_ROOT/install/robot_bringup/share/robot_bringup/launch/real_robot_odometry.launch.py" \
    "$CAR_ROOT/install/robot_bringup/share/robot_bringup/launch/real_robot_nav.launch.py" \
    "$ROOT/robot_skills/head_control/run.py"; do
    if [[ ! -f "$required" ]]; then
      echo "缺少启动依赖：$required" >&2
      return 1
    fi
  done
}

show_plan() {
  validate_plan
  cat <<EOF
工作区：$CAR_ROOT
启动顺序（plan 模式不会执行）：
  1. base       real_robot_base.launch.py
     等待：/cmd_vel 有订阅者，/scan_raw 和 /imu 均持续收到新消息
     未就绪：完整停止 base 后有限重试（默认 ${BASE_START_ATTEMPTS} 次）
  2. head       自动恢复平视，并确认保护后的 /scan 已恢复
  3. odometry   real_robot_odometry.launch.py
     等待：/odom 持续收到新消息
     未就绪：完整停止 odometry 后有限重试（默认 ${ODOM_START_ATTEMPTS} 次）
  4. navigation real_robot_nav.launch.py（关闭 RViz）
     等待：map/planner/bt_navigator 状态服务、路径规划与导航 action 均可用
     未就绪：完整停止 navigation 后有限重试（默认 ${NAV_START_ATTEMPTS} 次）
     发车前：navigation_goto 再执行一次 ComputePathToPose 路径预检
  5. 上述全部就绪后，run.sh 才启动千问持续对话
停止顺序：navigation -> odometry -> base
EOF
}

case "${1:-}" in
  start) start_all ;;
  stop) stop_all ;;
  status) status_all ;;
  plan) show_plan ;;
  *)
    echo "用法：bash $ROOT/robot_stack.sh {plan|start|status|stop}" >&2
    exit 2
    ;;
esac
