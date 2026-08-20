#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT/../.." && pwd)"
BRIDGE="$PROJECT_ROOT/android_app/robot_bridge/robot_bridge.sh"
CORE="$PROJECT_ROOT/resident_service.sh"

case "${1:-status}" in
  start)
    relay_ready=false
    for relay in http://100.125.188.94:8765 http://10.249.188.197:8765; do
      if curl -fsS --max-time 2 "$relay/api/health" >/dev/null; then
        relay_ready=true
        break
      fi
    done
    $relay_ready || {
      echo "[app] 中转服务器暂时不可达；机器人核心仍会启动，桥接进程将在后台重连。" >&2
    }
    bash "$BRIDGE" start
    bash "$CORE" start
    echo "千问流式语音、机器人 Skill 与 Android App 桥接已启动。"
    ;;
  stop)
    bash "$CORE" stop
    bash "$BRIDGE" stop
    ;;
  restart)
    bash "$CORE" restart
    ;;
  status)
    bash "$CORE" status || true
    ;;
  bridge-start) bash "$BRIDGE" start ;;
  bridge-stop) bash "$BRIDGE" stop ;;
  *) echo "usage: bash $0 {start|stop|restart|status|bridge-start|bridge-stop}" >&2; exit 2 ;;
esac
