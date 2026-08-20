#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/runtime/resident_service"
PID_FILE="$STATE/service.pid"
LOG_FILE="$STATE/service.log"
CONTROL_SOCKET="$ROOT/runtime/app_control.sock"
ACTION="${1:-status}"

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

control_status() {
  python3 - "$CONTROL_SOCKET" <<'PY'
import json, socket, sys
path = sys.argv[1]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
    conn.settimeout(2.0)
    conn.connect(path)
    conn.sendall(b'{"op":"status"}\n')
    raw = conn.makefile("rb").readline(1024 * 1024)
value = json.loads(raw.decode("utf-8"))
print(json.dumps(value, ensure_ascii=False))
raise SystemExit(0 if value.get("ok") else 1)
PY
}

case "$ACTION" in
  start)
    mkdir -p "$STATE" "$ROOT/runtime"
    if alive && [[ -S "$CONTROL_SOCKET" ]]; then
      status="$(control_status 2>/dev/null || true)"
      if [[ -n "$status" ]]; then
        echo "$status"
        exit 0
      fi
    fi
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
    nohup setsid bash "$ROOT/run.sh" --execute-skills >>"$LOG_FILE" 2>&1 </dev/null &
    pid=$!
    echo "$pid" >"$PID_FILE"
    deadline=$((SECONDS + 180))
    while (( SECONDS < deadline )); do
      if ! kill -0 "$pid" 2>/dev/null; then
        tail -n 160 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE" "$CONTROL_SOCKET"
        exit 1
      fi
      if [[ -S "$CONTROL_SOCKET" ]]; then
        status="$(control_status 2>/dev/null || true)"
        if [[ "$status" == *'"connected": true'* ]]; then
          echo "$status"
          exit 0
        fi
      fi
      sleep 0.25
    done
    echo "Qwen realtime resident startup timed out" >&2
    kill -TERM -- "-$pid" 2>/dev/null || true
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
    exit 1
    ;;
  stop)
    if alive; then
      pid="$(cat "$PID_FILE")"
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      deadline=$((SECONDS + 90))
      while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
      if kill -0 "$pid" 2>/dev/null; then
        echo "Qwen realtime resident did not stop cleanly" >&2
        exit 1
      fi
    fi
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
    echo '{"ok":true,"state":"stopped"}'
    ;;
  restart)
    bash "$ROOT/resident_service.sh" stop
    bash "$ROOT/resident_service.sh" start
    ;;
  status)
    if alive && [[ -S "$CONTROL_SOCKET" ]]; then
      control_status
    elif alive; then
      printf '{"ok":false,"state":"starting","pid":%s}\n' "$(cat "$PID_FILE")"
      exit 1
    else
      echo '{"ok":false,"state":"stopped"}'
      exit 1
    fi
    ;;
  log)
    tail -n "${2:-160}" "$LOG_FILE"
    ;;
  *)
    echo "usage: bash $0 {start|stop|restart|status|log [lines]}" >&2
    exit 2
    ;;
esac
