#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
EXPECTED_TY3_SHA256="b2ed66ee1731aee957780f0e6701367bedbce6cc5609185c3f0f9a4191386f3f"
[[ "$(sha256sum /ty3 | awk '{print $1}')" == "$EXPECTED_TY3_SHA256" ]] || {
  echo "Refusing to install: /ty3 checksum changed" >&2
  exit 1
}

sudo install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin
sudo install -o root -g root -m 0755 /ty3 /usr/local/libexec/robot-projection-prep
sudo install -o root -g root -m 0755 "$DIR/system/robot-start-exercise-projection" /usr/local/sbin/robot-start-exercise-projection
sudo install -o root -g root -m 0755 "$DIR/system/robot-meeting-projection" /usr/local/sbin/robot-meeting-projection
sudo install -o root -g root -m 0755 "$DIR/system/start_projection_ppt.sh" /home/test/start_projection_ppt.sh
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s\n' 'test ALL=(root) NOPASSWD: /usr/local/sbin/robot-start-exercise-projection, /usr/local/sbin/robot-meeting-projection *, /home/test/start_projection_ppt.sh single' > "$tmp"
sudo install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/robot-exercise-projection
sudo visudo -cf /etc/sudoers.d/robot-exercise-projection
echo "Projection helper installed"
