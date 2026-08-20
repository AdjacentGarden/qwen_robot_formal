#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

set +u
source /opt/ros/humble/setup.bash
if [[ -f /home/test/Car_real_copy/install/setup.bash ]]; then
  source /home/test/Car_real_copy/install/setup.bash
fi
set -u

exec env PYTHONUNBUFFERED=1 python3 "$ROOT/benchmark_preload_effect.py" "$@"
