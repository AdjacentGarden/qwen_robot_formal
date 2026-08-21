#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-android_0}"
VIDEO_PATH_CONTAINER="${1:-/sdcard/Movies/exercise.mp4}"
START_PREP="${START_PREP:-/ty3}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found" >&2
  exit 1
fi



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

sudo docker exec "$CONTAINER_NAME" sh -c " \
appops set android.rk.RockVideoPlayer MANAGE_EXTERNAL_STORAGE allow 2>/dev/null || true; \
appops set android.rk.RockVideoPlayer READ_EXTERNAL_STORAGE allow 2>/dev/null || true; \
appops set android.rk.RockVideoPlayer READ_MEDIA_VIDEO allow 2>/dev/null || true; \
appops set android.rk.RockVideoPlayer READ_MEDIA_AUDIO allow 2>/dev/null || true; \
am force-stop android.rk.RockVideoPlayer; \
am start -n android.rk.RockVideoPlayer/.VideoPlayActivity -a android.intent.action.VIEW -d file://$VIDEO_PATH_CONTAINER -t video/mp4"

echo "Started projection in container $CONTAINER_NAME"
