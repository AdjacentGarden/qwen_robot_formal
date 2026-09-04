#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_DIR="$(dirname "${SCRIPT_DIR}")"

install -d -m 0755 /usr/local/lib/robot-imu
install -m 0755 "${SCRIPT_DIR}/imu_exclusive_control.sh" \
    /usr/local/lib/robot-imu/imu_exclusive_control.sh
install -m 0644 "${PACKAGE_DIR}/systemd/robot-imu-exclusive.service" \
    /etc/systemd/system/robot-imu-exclusive.service

systemctl daemon-reload
systemctl enable --now robot-imu-exclusive.service
systemctl --no-pager status robot-imu-exclusive.service
