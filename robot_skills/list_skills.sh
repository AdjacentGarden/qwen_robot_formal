#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 - "$DIR/manifest.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
print(f"skill_count={d['skill_count']}")
for i,s in enumerate(d["skills"],1): print(f"{i:02d}. {s['name']}")
PY
