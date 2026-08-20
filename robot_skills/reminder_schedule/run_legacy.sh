#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export SINGLE_FUNCTION_SPEECH_EVENTS="${SINGLE_FUNCTION_SPEECH_EVENTS:-1}"
export SINGLE_FUNCTION_ROOT="${SINGLE_FUNCTION_ROOT:-$(cd "$DIR/.." && pwd)}"
export SINGLE_FUNCTION_RUNTIME_DIR="${SINGLE_FUNCTION_RUNTIME_DIR:-$SINGLE_FUNCTION_ROOT/runtime/agent_0625}"
exec python3 "$DIR/run.py" "$@"
