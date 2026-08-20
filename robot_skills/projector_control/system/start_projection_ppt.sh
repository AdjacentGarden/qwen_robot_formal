#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-android_0}"
IMAGE1="${IMAGE1:-/sdcard/Pictures/test-1.jpg}"
IMAGE2="${IMAGE2:-/sdcard/Pictures/test-2.jpg}"
INTERVAL="${INTERVAL:-3}"
START_PREP="${START_PREP:-/ty3}"
LOOP_SCRIPT="${LOOP_SCRIPT:-/data/local/tmp/ppt_projection_loop.sh}"
MODE="${1:-${MODE:-single}}"

[[ "$MODE" == "loop" || "$MODE" == "single" ]] || { echo "MODE must be loop or single" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }

run_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

if [[ "$MODE" == "single" ]]; then
  run_root docker exec "$CONTAINER_NAME" sh -c "test -f '$IMAGE1'"
else
  run_root docker exec "$CONTAINER_NAME" sh -c "test -f '$IMAGE1' && test -f '$IMAGE2'"
fi

pid="$(run_root pidof surfaceflinger | awk '{print $1}')"
[[ -n "${pid:-}" ]] || { echo "surfaceflinger pid not found" >&2; exit 1; }

run_root nsenter -t "$pid" -m -p -- /system/bin/settings put system accelerometer_rotation 0
run_root nsenter -t "$pid" -m -p -- /system/bin/settings put system user_rotation 2
run_root nsenter -t "$pid" -m -p -- /system/bin/cmd window user-rotation lock 2
run_root nsenter -t "$pid" -m -p -- /system/bin/cmd window fixed-to-user-rotation enabled
run_root nsenter -t "$pid" -m -p -- /system/bin/cmd window set-ignore-orientation-request true

if [[ -x "$START_PREP" ]]; then
  run_root "$START_PREP"
fi

if [[ "$MODE" == "single" ]]; then
  run_root docker exec "$CONTAINER_NAME" sh -c "pkill -f '[p]pt_projection_loop.sh' 2>/dev/null || true; settings put global policy_control immersive.full=*; pm grant com.android.gallery3d android.permission.READ_MEDIA_IMAGES 2>/dev/null || true; pm grant com.android.gallery3d android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true; am force-stop com.android.gallery3d; am start -n com.android.gallery3d/.app.GalleryActivity -a android.intent.action.VIEW -d file://$IMAGE1 -t image/jpeg"
  echo "Started single-image PPT projection in container $CONTAINER_NAME"
  exit 0
fi

run_root docker exec "$CONTAINER_NAME" sh -c "cat > '$LOOP_SCRIPT' <<'EOF'
settings put global policy_control immersive.full=*
pm grant com.android.gallery3d android.permission.READ_MEDIA_IMAGES 2>/dev/null || true
pm grant com.android.gallery3d android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true

while true; do
  am start -S -n com.android.gallery3d/.app.GalleryActivity -a android.intent.action.VIEW -d file://$IMAGE1 -t image/jpeg
  sleep $INTERVAL
  am start -S -n com.android.gallery3d/.app.GalleryActivity -a android.intent.action.VIEW -d file://$IMAGE2 -t image/jpeg
  sleep $INTERVAL
done
EOF
chmod 755 '$LOOP_SCRIPT'
"

run_root docker exec -d "$CONTAINER_NAME" sh -c "sh '$LOOP_SCRIPT' >/data/local/tmp/ppt_projection.log 2>&1"
echo "Started PPT projection loop in container $CONTAINER_NAME"
