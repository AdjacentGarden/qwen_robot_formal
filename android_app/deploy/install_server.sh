#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if python3 -m venv "$ROOT/.venv" 2>/dev/null; then
  "$ROOT/.venv/bin/pip" install --upgrade pip
  "$ROOT/.venv/bin/pip" install -r "$ROOT/server/requirements.txt"
else
  rm -rf "$ROOT/.venv"
  mkdir -p "$ROOT/vendor"
  python3 -m pip install --target "$ROOT/vendor" -r "$ROOT/server/requirements.txt"
fi
mkdir -p "$ROOT/config" "$ROOT/runtime" "$ROOT/data/videos" "$ROOT/data/thumbs"
echo "server dependencies installed"
