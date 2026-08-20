#!/usr/bin/env python3
"""Mechanically replace run.sh files in the copied project only."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
skills = sorted(
    p.name for p in ROOT.iterdir()
    if p.is_dir() and (p / "run.sh").exists() and (p / "run.py").exists()
)
for skill in skills:
    folder = ROOT / skill
    run_sh = folder / "run.sh"
    legacy = folder / "run_legacy.sh"
    if not legacy.exists():
        shutil.copy2(run_sh, legacy)
    run_sh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)\"\n"
        f"exec python3 \"$ROOT/resident_skill_client.py\" {skill!r} \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(run_sh, 0o755)
    meta_path = folder / "skill.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["resident_runtime"] = {
                "enabled": True,
                "launcher": str(ROOT / "resident_runtime.sh"),
                "socket": str(ROOT / "runtime" / "resident" / "skills.sock"),
                "legacy_entrypoint": str(legacy),
            }
            if meta.get("entrypoint"):
                meta["entrypoint"] = str(run_sh)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
print(json.dumps({"ok": True, "root": str(ROOT), "skills": skills, "count": len(skills)}, ensure_ascii=False, indent=2))
