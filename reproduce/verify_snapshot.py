#!/usr/bin/env python3
"""Only verify archived files; never import robot modules or start hardware."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', action='store_true', help='compatibility flag; always verifies the full manifest')
    parser.parse_args()
    errors, count = [], 0
    for row in (ROOT / 'reproduce/SHA256SUMS').read_text().splitlines():
        if not row.strip(): continue
        expected, relative = row.split('  ', 1)
        path = ROOT / relative
        count += 1
        if not path.is_file(): errors.append('missing: ' + relative)
        elif digest(path) != expected: errors.append('checksum mismatch: ' + relative)
    print(json.dumps({'ok': not errors, 'checked': count, 'errors': errors,
                      'scope': 'source snapshot only; verify private Release resources separately'}, ensure_ascii=False, indent=2))
    return bool(errors)

if __name__ == '__main__':
    raise SystemExit(main())
