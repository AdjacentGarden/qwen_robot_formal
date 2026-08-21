#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-android_0}"
LOOP_SCRIPT="${LOOP_SCRIPT:-/data/local/tmp/ppt_projection_loop.sh}"

if command -v sudo >/dev/null 2>&1; then
  sudo docker exec "$CONTAINER_NAME" sh -c "pkill -f '$LOOP_SCRIPT' 2>/dev/null || true" 2>/dev/null || true
  sudo pkill -f "docker exec -i ${CONTAINER_NAME} sh" 2>/dev/null || true
  sudo pkill -f "$LOOP_SCRIPT" 2>/dev/null || true
else
  docker exec "$CONTAINER_NAME" sh -c "pkill -f '$LOOP_SCRIPT' 2>/dev/null || true" 2>/dev/null || true
  pkill -f "docker exec -i ${CONTAINER_NAME} sh" 2>/dev/null || true
  pkill -f "$LOOP_SCRIPT" 2>/dev/null || true
fi

echo "Stopped PPT projection loop in container $CONTAINER_NAME"
