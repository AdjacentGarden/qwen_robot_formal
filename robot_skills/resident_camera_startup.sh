# Sourced by resident_runtime.sh after terminate_pid is defined.
# Only the broker created by this invocation is stopped/restarted.
start_checked_camera_broker() {
  local attempt
  for attempt in 1 2 3; do
    echo "checking front/back cameras: attempt $attempt/3"
    rm -f "$CAMERA_STATUS_FILE" "$CAMERA_MANIFEST"
    nohup taskset -c "$RESIDENT_CPUSET" python3 "$ROOT/resident_camera_broker.py" >>"$CAMERA_LOG_FILE" 2>&1 &
    camera_pid=$!
    echo "$camera_pid" >"$CAMERA_PID_FILE"
    if python3 "$ROOT/resident_camera_startup.py" --state "$STATE" --pid "$camera_pid" --timeout 12; then
      return 0
    fi
    echo "front/back camera check failed; stopping camera broker pid=$camera_pid" >&2
    # No second broker may open cameras before the first one has exited.
    if ! terminate_pid "$camera_pid" 12; then
      echo "camera broker did not exit; refusing an overlapping restart" >&2
      return 1
    fi
    wait "$camera_pid" 2>/dev/null || true
    rm -f "$CAMERA_PID_FILE" "$CAMERA_MANIFEST"
    if (( attempt < 3 )); then
      echo "restarting camera capture service only" >&2
    fi
  done
  echo "camera startup failed after 3 attempts; front/back camera frames required" >&2
  return 1
}
