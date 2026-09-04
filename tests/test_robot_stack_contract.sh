#!/usr/bin/env bash
set -euo pipefail

ROOT_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="$ROOT_UNDER_TEST/robot_stack.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

grep -Fq '/home/test/Car_real_copy' "$STACK" || fail "Car_real_copy workspace missing"
grep -Fq 'mapping_navigation_manager.py' "$STACK" || fail "Manager entrypoint missing"
grep -Fq '/mapping_manager/shutdown' "$STACK" || fail "Manager shutdown interface missing"
grep -Fq 'ready.manager' "$STACK" || fail "Manager readiness gate missing"
grep -Fq 'wait_existing_manager_ready' "$STACK" || fail "existing Manager discovery grace missing"
grep -Fq 'SAFE_STOP' "$STACK" || fail "SAFE_STOP handling missing"
grep -Fq 'map.pbstream' "$STACK" || fail "map fallback preflight missing"

for forbidden in \
  'real_robot_base.launch.py' \
  'real_robot_odometry.launch.py' \
  'real_robot_nav.launch.py' \
  '/home/test/car_real_copy_zhenghang'; do
  if grep -Fq "$forbidden" "$STACK"; then
    fail "split-launch or old workspace remains: $forbidden"
  fi
done

# Sourcing defines functions only. This plan test performs no ROS or hardware action.
# shellcheck disable=SC1090
source "$STACK"
validate_plan() { return 0; }
plan="$(show_plan)"
[[ "$plan" == *"唯一启动入口：MappingNavigationManager"* ]] || fail "plan is not Manager-only"
[[ "$plan" == *"绝不回退自动建图"* ]] || fail "map safety policy missing"

# Exercise the bounded restart state machine with every hardware-facing helper
# replaced before start_all is called.
validate_plan() { return 0; }
source_ros() { return 0; }
ensure_health_monitor() { return 0; }
ensure_startup_head_level() { return 0; }
sleep() { return 0; }
start_calls=0
wait_calls=0
stop_calls=0
health_stop_calls=0
start_manager_once() { start_calls=$((start_calls + 1)); return 0; }
wait_manager_ready() { wait_calls=$((wait_calls + 1)); [[ "$wait_calls" -ge 2 ]]; }
stop_manager() { stop_calls=$((stop_calls + 1)); return 0; }
stop_health_monitor() { health_stop_calls=$((health_stop_calls + 1)); return 0; }
start_all >/dev/null
[[ "$start_calls" == 2 && "$wait_calls" == 2 && "$stop_calls" == 1 ]] \
  || fail "first-failure second-success retry contract broken"

start_calls=0
wait_calls=0
stop_calls=0
health_stop_calls=0
wait_manager_ready() { wait_calls=$((wait_calls + 1)); return 1; }
if start_all >/dev/null 2>&1; then
  fail "two failed Manager attempts were reported as success"
fi
[[ "$start_calls" == 2 && "$wait_calls" == 2 && "$stop_calls" == 2 && "$health_stop_calls" == 1 ]] \
  || fail "bounded failure cleanup contract broken"

echo '{"ok":true,"cases":14,"hardware_commands":0,"wheel_commands":0}'
