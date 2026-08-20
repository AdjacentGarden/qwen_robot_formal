#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -lt 1 ]; then echo "用法: bash run_skill.sh <skill名称> [参数...]" >&2; exit 2; fi
SKILL="$1"; shift
case "$SKILL" in (*[!A-Za-z0-9_-]*|"") echo "非法 skill 名称: $SKILL" >&2; exit 2;; esac
ENTRY="$DIR/$SKILL/run.sh"
if [ ! -x "$ENTRY" ]; then echo "未知或不可执行的 skill: $SKILL" >&2; exit 2; fi
exec "$ENTRY" "$@"
