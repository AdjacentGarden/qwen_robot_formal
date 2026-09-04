#!/usr/bin/env bash
set -uo pipefail

readonly ROOT="/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test"
readonly TEST_STAMP="$(date +%Y%m%d_%H%M%S)"
readonly RESULT_DIR="$ROOT/runtime/tests"
readonly PLAY_RESULT="$RESULT_DIR/welcome_A_scene_${TEST_STAMP}.json"
readonly SCREENSHOT="$RESULT_DIR/welcome_A_screen_${TEST_STAMP}.png"
readonly CONTAINER_SCREENSHOT="/data/local/tmp/welcome_A_screen_${TEST_STAMP}.png"
readonly VIEWER_PACKAGE="com.adjacentgarden.welcome"

mkdir -p "$RESULT_DIR"

restore_safe_state() {
  bash "$ROOT/robot_skills/welcome_projection/run.sh" stop --json >/dev/null 2>&1 || true
  bash "$ROOT/robot_skills/head_control/run.sh" level --json >/dev/null 2>&1 || true
}
trap restore_safe_state EXIT INT TERM

down_result="$(bash "$ROOT/robot_skills/head_control/run.sh" down --json)" || {
  printf 'HEAD_DOWN=%s\n' "$down_result"
  exit 2
}
printf 'HEAD_DOWN=%s\n' "$down_result"

bash "$ROOT/robot_skills/welcome_projection/run.sh" play --duration 3 --json >"$PLAY_RESULT" &
play_pid=$!

viewer_seen=false
for _attempt in $(seq 1 120); do
  if sudo -n /usr/bin/docker exec android_0 pidof "$VIEWER_PACKAGE" >/dev/null 2>&1; then
    viewer_seen=true
    break
  fi
  sleep 0.03
done

if [[ "$viewer_seen" == true ]]; then
  sleep 0.20
  sudo -n /usr/bin/docker exec android_0 screencap -p "$CONTAINER_SCREENSHOT"
  sudo -n /usr/bin/docker cp "android_0:$CONTAINER_SCREENSHOT" "$SCREENSHOT" >/dev/null
  sudo -n chown test:test "$SCREENSHOT"
fi

wait "$play_pid"
play_rc=$?
play_result="$(cat "$PLAY_RESULT")"
printf 'PLAY=%s\n' "$play_result"
printf 'PLAY_RC=%s\n' "$play_rc"
printf 'VIEWER_SEEN=%s\n' "$viewer_seen"
printf 'SCREENSHOT=%s\n' "$SCREENSHOT"

level_result="$(bash "$ROOT/robot_skills/head_control/run.sh" level --json)" || {
  printf 'HEAD_LEVEL=%s\n' "$level_result"
  exit 3
}
printf 'HEAD_LEVEL=%s\n' "$level_result"

trap - EXIT INT TERM
if sudo -n /usr/bin/docker exec android_0 pidof "$VIEWER_PACKAGE" >/dev/null 2>&1; then
  printf 'VIEWER_AFTER_PLAY=running\n'
  exit 4
fi
printf 'VIEWER_AFTER_PLAY=stopped\n'
[[ "$play_rc" -eq 0 && "$viewer_seen" == true && -s "$SCREENSHOT" ]]
