#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CAR_REAL_WS="/home/test/car_real_copy_zhenghang"
export CAR_REAL_WS
export PET_CONTROLLER_CLI_PATH="$CAR_REAL_WS/src/demo/controller_cli.py"

if [[ -f runtime/config.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source runtime/config.env
  set +a
fi

KEY_ARGS=()
if [[ -z "${DASHSCOPE_API_KEY:-}" && -f runtime/api_key ]]; then
  KEY_ARGS=(--api-key-file runtime/api_key)
fi

case "${1:-}" in
  --robot-stack-plan)
    exec bash "$ROOT/robot_stack.sh" plan
    ;;
  --robot-stack-status)
    exec bash "$ROOT/robot_stack.sh" status
    ;;
  --robot-stack-start)
    exec bash "$ROOT/robot_stack.sh" start
    ;;
  --robot-stack-stop)
    exec bash "$ROOT/robot_stack.sh" stop
    ;;
esac

AUTO_STACK=0
NO_AUTO_STACK=0
CHAT_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --execute-skills) AUTO_STACK=1; CHAT_ARGS+=("$arg") ;;
    --no-auto-robot-stack) NO_AUTO_STACK=1 ;;
    *) CHAT_ARGS+=("$arg") ;;
  esac
done

STACK_ATTACHED=0
SKILL_RUNTIME_ATTACHED=0
SKILL_HOST_ATTACHED=0
cleanup() {
  local status=$?
  trap - EXIT
  if [[ "$SKILL_HOST_ATTACHED" == "1" ]]; then
    bash "$ROOT/skill_host.sh" stop || true
  fi
  if [[ "$SKILL_RUNTIME_ATTACHED" == "1" ]]; then
    bash "$ROOT/robot_skills/resident_runtime.sh" stop || true
  fi
  if [[ "$STACK_ATTACHED" == "1" ]]; then
    bash "$ROOT/robot_stack.sh" stop || true
  fi
  exit "$status"
}
trap cleanup EXIT

if [[ "$AUTO_STACK" == "1" ]]; then
  conflicting_runtime="$(ps -eo pid=,args= | awk -v own="$ROOT/robot_skills/resident_runtime_server.py" '
    /resident_runtime_server[.]py/ && index($0, own) == 0 { print; exit }
  ')"
  if [[ -n "$conflicting_runtime" ]]; then
    echo "检测到其他项目的常驻 Skill 运行时，为避免相机、NPU 和 ROS 冲突，本项目没有启动：$conflicting_runtime" >&2
    exit 73
  fi
fi

if [[ "$AUTO_STACK" == "1" && "$NO_AUTO_STACK" != "1" ]]; then
  bash "$ROOT/robot_stack.sh" start
  STACK_ATTACHED=1
fi

if [[ "$AUTO_STACK" == "1" ]]; then
  bash "$ROOT/robot_skills/resident_runtime.sh" start
  SKILL_RUNTIME_ATTACHED=1
  bash "$ROOT/skill_host.sh" start
  SKILL_HOST_ATTACHED=1
fi

python3 realtime_chat.py "${KEY_ARGS[@]}" "${CHAT_ARGS[@]}"
