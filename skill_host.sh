#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/runtime/skill_host"
PID_FILE="$STATE/host.pid"
LOG_FILE="$STATE/host.log"
SOCKET="$ROOT/runtime/skill_host.sock"
ACTION="${1:-status}"

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

case "$ACTION" in
  start)
    mkdir -p "$STATE"
    if alive && [[ -S "$SOCKET" ]]; then
      echo "skill host already running: pid=$(cat "$PID_FILE")"
      exit 0
    fi
    rm -f "$PID_FILE" "$SOCKET"
    nohup python3 "$ROOT/skill_host.py" >>"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"
    deadline=$((SECONDS + 20))
    while (( SECONDS < deadline )); do
      if [[ -S "$SOCKET" ]]; then
        echo "skill host ready: pid=$pid socket=$SOCKET"
        exit 0
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        tail -n 120 "$LOG_FILE" || true
        rm -f "$PID_FILE" "$SOCKET"
        exit 1
      fi
      sleep 0.1
    done
    echo "skill host startup timed out" >&2
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "$PID_FILE" "$SOCKET"
    exit 1
    ;;
  stop)
    if alive; then
      pid="$(cat "$PID_FILE")"
      kill -TERM "$pid" 2>/dev/null || true
      deadline=$((SECONDS + 10))
      while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.1; done
      if kill -0 "$pid" 2>/dev/null; then
        echo "skill host did not stop within 10 seconds" >&2
        exit 1
      fi
    fi
    rm -f "$PID_FILE" "$SOCKET"
    echo "skill host stopped"
    ;;
  status)
    python3 - "$SOCKET" <<'PY'
import json, socket, sys
path = sys.argv[1]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(path)
        conn.sendall(b'{"op":"ping","request_id":"status"}\n')
        print(json.dumps(json.loads(conn.makefile("rb").readline()), ensure_ascii=False, indent=2))
except Exception as exc:
    print(json.dumps({"state": "stopped", "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
    raise SystemExit(1)
PY
    ;;
  log)
    tail -n "${2:-120}" "$LOG_FILE"
    ;;
  *)
    echo "usage: bash $0 {start|stop|status|log [lines]}" >&2
    exit 2
    ;;
esac
