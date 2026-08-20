#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test"
queries=(
  "天黑了，帮我打开客厅的灯"
  "来陪我一起做俯卧撑吧"
  "看看豆豆在干嘛，它该吃饭了"
  "投影一下会议内容"
  "看看面前的人是谁"
  "给我讲一个简短的笑话"
  "不看了，我已经坐了一天了"
)
for query in "${queries[@]}"; do
  echo "=== $query"
  output="$(cd "$ROOT" && timeout 55 bash run.sh --tool-test-text "$query" --tool-test-output runtime/app_voice_smoke.wav 2>&1)"
  printf '%s\n' "$output" | grep -E '^\[本地Skill\]|"ok"|"transcripts"' | head -4 || true
done
