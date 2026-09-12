#!/usr/bin/env bash
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/humble/setup.bash
source /home/test/Car_real_copy/install/setup.bash
export ROS_DOMAIN_ID=83
export ROS_LOCALHOST_ONLY=1
export ROS_LOG_DIR="$root/runtime/ros_logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root/src:$root/vendor:${PYTHONPATH:-}"
exec "$@"
