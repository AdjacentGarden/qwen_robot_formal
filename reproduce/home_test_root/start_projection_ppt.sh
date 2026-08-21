#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-android_0}"
IMAGE1="${IMAGE1:-/sdcard/Pictures/test-1.jpg}"
IMAGE2="${IMAGE2:-/sdcard/Pictures/test-2.jpg}"
INTERVAL="${INTERVAL:-3}"
START_PREP="${START_PREP:-/ty3}"
LOOP_SCRIPT="${LOOP_SCRIPT:-/data/local/tmp/ppt_projection_loop.sh}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found" >&2
  exit 1
fi

sudo docker exec "$CONTAINER_NAME" sh -c "test -f '$IMAGE1' && test -f '$IMAGE2'"

pid="$(sudo pidof surfaceflinger | awk '{print $1}')"
if [[ -z "${pid:-}" ]]; then
  echo "surfaceflinger pid not found" >&2
  exit 1
fi

sudo nsenter -t "$pid" -m -p -- /system/bin/settings put system accelerometer_rotation 0
sudo nsenter -t "$pid" -m -p -- /system/bin/settings put system user_rotation 2
sudo nsenter -t "$pid" -m -p -- /system/bin/cmd window user-rotation lock 2
sudo nsenter -t "$pid" -m -p -- /system/bin/cmd window fixed-to-user-rotation enabled
sudo nsenter -t "$pid" -m -p -- /system/bin/cmd window set-ignore-orientation-request true

if [[ -x "$START_PREP" ]]; then
  sudo "$START_PREP"
fi

sudo docker exec "$CONTAINER_NAME" sh -c "cat > '$LOOP_SCRIPT' <<'EOF'
settings put global policy_control immersive.full=*
pm grant com.android.gallery3d android.permission.READ_MEDIA_IMAGES 2>/dev/null || true
pm grant com.android.gallery3d android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true

while true; do
  am start --activity-clear-top -n com.android.gallery3d/.app.GalleryActivity -a android.intent.action.VIEW -d file://$IMAGE1 -t image/jpeg
  sleep $INTERVAL
  am start --activity-clear-top -n com.android.gallery3d/.app.GalleryActivity -a android.intent.action.VIEW -d file://$IMAGE2 -t image/jpeg
  sleep $INTERVAL
done
EOF
chmod 755 '$LOOP_SCRIPT'
"

sudo docker exec -d "$CONTAINER_NAME" sh -c "sh '$LOOP_SCRIPT' >/data/local/tmp/ppt_projection.log 2>&1"

echo "Started PPT projection loop in container $CONTAINER_NAME"
