#!/usr/bin/env bash
set -euo pipefail

ROOT_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1090
source "$ROOT_UNDER_TEST/robot_stack.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

service_exists() { return 0; }
action_exists() { return 0; }
lifecycle_is_active() { return 0; }
map_has_sample() { return 0; }
probe_ready navigation || fail "healthy navigation graph was rejected"

map_has_sample() { return 1; }
if probe_ready navigation; then
  fail "navigation was accepted without a /map sample"
fi

map_has_sample() { return 0; }
lifecycle_is_active() { [[ "$1" != "/map_server" ]]; }
if probe_ready navigation; then
  fail "navigation was accepted while map_server was inactive"
fi

timeout() { printf 'active [3]\n'; }
[[ "$(lifecycle_state /map_server)" == "active" ]] \
  || fail "lifecycle output was not parsed"

calls=()
timeout() { calls+=("$*"); return 0; }
lifecycle_state() { printf 'inactive\n'; }
lifecycle_is_active() { return 0; }
repair_map_server_lifecycle || fail "inactive map_server repair failed"
[[ "${#calls[@]}" == "1" && "${calls[0]}" == *"lifecycle set /map_server activate"* ]] \
  || fail "inactive repair did not issue exactly one activate transition"

calls=()
lifecycle_state() { printf 'unconfigured\n'; }
repair_map_server_lifecycle || fail "unconfigured map_server repair failed"
[[ "${#calls[@]}" == "2" ]] || fail "unconfigured repair must configure then activate"
[[ "${calls[0]}" == *"lifecycle set /map_server configure"* ]] \
  || fail "configure transition missing"
[[ "${calls[1]}" == *"lifecycle set /map_server activate"* ]] \
  || fail "activate transition missing"

echo '{"ok":true,"cases":6,"hardware_commands":0}'
