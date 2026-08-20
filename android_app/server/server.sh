#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/runtime/server.pid"
LOG_FILE="$ROOT/runtime/server.log"
ENV_FILE="$ROOT/config/server.env"
VENV="$ROOT/.venv"
PYTHON_BIN="$VENV/bin/python"
mkdir -p "$ROOT/runtime" "$ROOT/data/videos" "$ROOT/data/thumbs"

alive() { [[ -s "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
  if alive; then echo "server already running: pid=$(cat "$PID_FILE")"; return 0; fi
  [[ -r "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; return 2; }
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN=python3
    [[ -d "$ROOT/vendor" ]] || { echo "missing runtime; run deploy/install_server.sh" >&2; return 3; }
    export PYTHONPATH="$ROOT/vendor${PYTHONPATH:+:$PYTHONPATH}"
  fi
  set -a; source "$ENV_FILE"; set +a
  cd "$ROOT/server"
  nohup "$PYTHON_BIN" run_server.py >"$LOG_FILE" 2>&1 </dev/null &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 50); do
    curl -fsS "http://127.0.0.1:${BIND_PORT:-8765}/api/health" >/dev/null 2>&1 && {
      echo "server ready: http://10.249.188.197:${BIND_PORT:-8765}/app/"; return 0;
    }
    alive || break
    sleep 0.2
  done
  tail -n 80 "$LOG_FILE" >&2 || true
  return 4
}

stop() {
  if alive; then kill "$(cat "$PID_FILE")" 2>/dev/null || true; fi
  rm -f "$PID_FILE"
  echo "server stopped"
}

status() {
  if alive; then
    echo "server running: pid=$(cat "$PID_FILE")"
    curl -fsS "http://127.0.0.1:${BIND_PORT:-8765}/api/health" || true
  else
    echo "server stopped"
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "usage: bash $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
