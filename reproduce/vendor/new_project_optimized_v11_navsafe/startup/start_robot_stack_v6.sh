#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/test
export USER=test
export LANG="${LANG:-zh_CN.UTF-8}"

# The supervisor also sources this file in every child shell. Sourcing it here
# keeps direct supervisor startup and systemd startup identical.
source /home/test/new_project_optimized_v11_navsafe/startup/ros_transport_env.sh

mkdir -p /home/test/new_project_optimized_v11_navsafe/runtime/startup
exec /usr/bin/python3 /home/test/new_project_optimized_v11_navsafe/startup/robot_stack_supervisor.py
