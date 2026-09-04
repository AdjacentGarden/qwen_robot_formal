#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPRO="$ROOT/reproduce"
INSTALL_USER_FILES=0
INSTALL_SYSTEM_FILES=0

usage() {
  echo "Usage: $0 [--user-files] [--system-files]"
  echo "Copies reproducibility snapshots only. It never starts ROS or hardware."
}

for arg in "$@"; do
  case "$arg" in
    --user-files) INSTALL_USER_FILES=1 ;;
    --system-files) INSTALL_SYSTEM_FILES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$INSTALL_USER_FILES" -eq 0 && "$INSTALL_SYSTEM_FILES" -eq 0 ]]; then
  usage
  exit 0
fi

if [[ "$ROOT" != "/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test" ]]; then
  echo "Repository must be located at /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test" >&2
  exit 1
fi

if [[ "$INSTALL_SYSTEM_FILES" -eq 1 ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "--system-files requires root; rerun with sudo and the same repository path." >&2
    exit 1
  fi
  install -d /usr/local/libexec /usr/local/sbin
  install -m 0755 "$REPRO/system_files/usr_local_libexec/"* /usr/local/libexec/
  install -m 0755 "$REPRO/system_files/usr_local_sbin/"* /usr/local/sbin/
  install -m 0644 "$REPRO/system_files/systemd/ideal-robot-app-bridge.service" /etc/systemd/system/
  systemctl daemon-reload
  echo "System files installed, but no service was started."
  exit 0
fi

if [[ "$(id -un)" != "test" ]]; then
  echo "Run user-space installation as the robot user 'test'." >&2
  exit 1
fi

install_tree() {
  local source="$1"
  local target="$2"
  [[ -d "$source" ]] || { echo "Missing snapshot: $source" >&2; exit 1; }
  mkdir -p "$target"
  cp -a "$source"/. "$target"/
}

install_tree "$REPRO/vendor/qwen_robot_project" /home/test/qwen_robot_project
install_tree "$REPRO/vendor/self_program" /home/test/self_program
install_tree "$REPRO/vendor/new_project" /home/test/new_project
install_tree "$REPRO/vendor/new_project_optimized_v11_navsafe" /home/test/new_project_optimized_v11_navsafe
install_tree "$REPRO/vendor/refine_0508" /home/test/refine_0508

# Formal execution uses ROOT/robot_skills. The compatibility dry-run runtime expects
# /home/test/single_function, so a missing installation can safely point to the same
# immutable skill snapshot.
if [[ ! -e /home/test/single_function ]]; then
  ln -s "$ROOT/robot_skills" /home/test/single_function
fi

if [[ "$INSTALL_USER_FILES" -eq 1 ]]; then
  for name in dian.py start_projection_ppt.sh stop_projection_ppt.sh start_projection_video.sh; do
    install -m 0755 "$REPRO/home_test_root/$name" "/home/test/$name"
  done
fi

mkdir -p "$ROOT/runtime" "$ROOT/robot_skills/runtime" "$ROOT/robot_skills/face_data"
echo "Snapshot files installed. No service, ROS node, Skill, or hardware was started."
echo "Now create private configuration files from reproduce/config_templates/."
