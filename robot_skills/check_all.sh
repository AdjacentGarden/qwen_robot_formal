#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 - "$DIR" <<'PY'
import json, pathlib, py_compile, sys
root=pathlib.Path(sys.argv[1]); manifest=json.loads((root/"manifest.json").read_text())
errors=[]
for item in manifest["skills"]:
 d=root/item["name"]
 for required in ("run.sh","skill.json","v11_skill_spec.json","README_START.md"):
  if not (d/required).exists(): errors.append(f"{item['name']}:missing:{required}")
 for p in d.rglob("*.py"):
  try: py_compile.compile(str(p), doraise=True)
  except Exception as e: errors.append(f"{p}:{e}")
print(json.dumps({"ok":not errors,"skill_count":len(manifest["skills"]),"errors":errors},ensure_ascii=False,indent=2))
raise SystemExit(1 if errors else 0)
PY
while IFS= read -r -d "" file; do bash -n "$file"; done < <(find "$DIR" -type f -name "*.sh" -print0)
echo "bash_syntax_ok=true"
