#!/usr/bin/env python3
"""Hash only; never import the existing applications or change their state."""
import hashlib
import json
from pathlib import Path
import sys

roots = [Path('/home/test/Car_real_copy'), Path('/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test')]
excluded = {'runtime','build','install','log','logs','.git','__pycache__','backups','node_modules','.venv','face_data'}
result = {}
for root in roots:
    for p in root.rglob('*'):
        if not p.is_file() or p.is_symlink() or set(p.relative_to(root).parts) & excluded:
            continue
        if p.suffix.lower() in {'.py','.json','.yaml','.yml','.sh','.cpp','.hpp','.h','.xml','.toml','.md','.txt','.js','.html','.css'}:
            result[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
Path(sys.argv[1]).write_text(json.dumps(result,indent=2))
print(json.dumps({'files':len(result),'manifest':sys.argv[1]}))
