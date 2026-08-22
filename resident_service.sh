#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$ROOT/runtime/resident_service"
PID_FILE="$STATE/service.pid"
LOG_FILE="$STATE/service.log"
CONTROL_SOCKET="$ROOT/runtime/app_control.sock"
LOCK_FILE="$STATE/service.lock"
STATE_FILE="$STATE/service_state.json"
ACTION="${1:-status}"
START_TIMEOUT="${QWEN_SERVICE_START_TIMEOUT:-720}"

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  local cmdline
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"$ROOT/run.sh"* ]]
}

write_state() {
  local state="$1" pid="${2:-0}" temporary
  temporary="$STATE_FILE.$$.tmp"
  printf '{"state":"%s","pid":%s,"updated_at":%s}\n' \
    "$state" "$pid" "$(date +%s)" >"$temporary"
  mv -f "$temporary" "$STATE_FILE"
}

write_pid() {
  local pid="$1" temporary
  temporary="$PID_FILE.$$.tmp"
  printf '%s\n' "$pid" >"$temporary"
  mv -f "$temporary" "$PID_FILE"
}

cleanup_if_owned() {
  local expected_pid="$1" current_pid
  current_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$current_pid" == "$expected_pid" ]]; then
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
  fi
}

acquire_operation_lock() {
  mkdir -p "$STATE" "$ROOT/runtime"
  exec 9>"$LOCK_FILE"
  flock -n 9
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
  start|start-voice-only)
    mkdir -p "$STATE" "$ROOT/runtime"
    if ! acquire_operation_lock; then
      pid="$(cat "$PID_FILE" 2>/dev/null || printf '0')"
      printf '{"ok":true,"state":"starting","pid":%s,"detail":"startup_operation_in_progress"}\n' "$pid"
      exit 0
    fi
    if alive; then
      pid="$(cat "$PID_FILE")"
      if [[ -S "$CONTROL_SOCKET" ]]; then
        status="$(control_status 2>/dev/null || true)"
        if [[ "$status" == *'"connected": true'* ]] \
          && { [[ "$status" == *'"accepting_local_voice": true'* ]] \
            || [[ "$status" == *'"enabled": false'* ]]; }; then
          write_state running "$pid"
          echo "$status"
          exit 0
        fi
      fi
      write_state starting "$pid"
      printf '{"ok":true,"state":"starting","pid":%s,"detail":"existing_startup_preserved"}\n' "$pid"
      exit 0
    fi
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
    run_args=(--execute-skills)
    if [[ "$ACTION" == "start-voice-only" ]]; then
      run_args+=(--no-auto-robot-stack)
    fi
    nohup setsid bash "$ROOT/run.sh" "${run_args[@]}" 9>&- >>"$LOG_FILE" 2>&1 </dev/null &
    pid=$!
    write_pid "$pid"
    write_state starting "$pid"
    # The lock protects only the state transition. A second start can now
    # acquire it, observe this live PID and return "starting" without spawning
    # another stack. Stop is likewise never blocked for the full startup time.
    flock -u 9
    deadline=$((SECONDS + START_TIMEOUT))
    while (( SECONDS < deadline )); do
      if ! kill -0 "$pid" 2>/dev/null; then
        tail -n 160 "$LOG_FILE" >&2 || true
        acquire_operation_lock || true
        cleanup_if_owned "$pid"
        write_state failed "$pid"
        exit 1
      fi
      if [[ -S "$CONTROL_SOCKET" ]]; then
        status="$(control_status 2>/dev/null || true)"
        # A cloud websocket alone is not enough: when the local microphone is
        # enabled, wait until the audio stream has read a real non-zero signal.
        # A deliberately disabled local mic remains a valid ready state because
        # App voice must continue to work.
        if [[ "$status" == *'"connected": true'* ]] \
          && { [[ "$status" == *'"accepting_local_voice": true'* ]] \
            || [[ "$status" == *'"enabled": false'* ]]; }; then
          echo "$status"
          acquire_operation_lock || true
          if [[ "$(cat "$PID_FILE" 2>/dev/null || true)" == "$pid" ]]; then
            write_state running "$pid"
          fi
          exit 0
        fi
      fi
      sleep 0.25
    done
    echo "Qwen realtime resident startup timed out" >&2
    kill -TERM -- "-$pid" 2>/dev/null || true
    acquire_operation_lock || true
    cleanup_if_owned "$pid"
    write_state failed "$pid"
    exit 1
    ;;
  stop)
    if ! acquire_operation_lock; then
      echo '{"ok":false,"state":"busy","error":"service_operation_in_progress"}' >&2
      exit 1
    fi
    if alive; then
      pid="$(cat "$PID_FILE")"
      write_state stopping "$pid"
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      deadline=$((SECONDS + 90))
      while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
      if kill -0 "$pid" 2>/dev/null; then
        echo "Qwen realtime resident did not stop cleanly" >&2
        exit 1
      fi
    fi
    rm -f "$PID_FILE" "$CONTROL_SOCKET"
    write_state stopped 0
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
      if [[ -r "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
      else
        echo '{"ok":false,"state":"stopped"}'
      fi
      exit 1
    fi
    ;;
  log)
    tail -n "${2:-160}" "$LOG_FILE"
    ;;
  *)
    echo "usage: bash $0 {start|start-voice-only|stop|restart|status|log [lines]}" >&2
    exit 2
    ;;
esac
