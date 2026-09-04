#!/usr/bin/env python3
"""Stop residual processes belonging to this robot mapping/navigation mission.

Use this recovery tool after an older manager/launch was killed before it could
clean up. It targets known robot mission commands, not every process containing
the word ``ros2`` on the machine.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import time


COMMAND_FRAGMENTS = (
    'mapping_navigation_manager.py',
    'save_map_on_exploration_complete.py',
    'frontier_explorer',
    'cartographer_node',
    'cartographer_occupancy_grid_node',
    'map_saver_server',
    'rf2o_laser_odometry_node',
    'ekf_node',
    'motion_controller_node',
    'motion_controller_auto_test.py',
    'controller_server',
    'planner_server',
    'bt_navigator',
    'behavior_server',
    'smoother_server',
    'waypoint_follower',
    'velocity_smoother',
    'collision_monitor',
    'lifecycle_manager',
    'map_server',
    'amcl',
    'rviz2',
    'gzserver',
    'gzclient',
    'robot_state_publisher',
    'spawn_entity.py',
)

LAUNCH_FRAGMENTS = (
    'ros2 launch robot_bringup',
    'ros2 launch motion_controller',
    'ros2 launch frontier_exploration_ros2',
    'ros2 launch robot_description',
    'ros2 launch car_nav2',
)


def protected_pids() -> set[int]:
    result = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        try:
            stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
            fields = stat[stat.rfind(')') + 2:].split()
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    return result


def matching_processes() -> dict[int, str]:
    matches = {}
    protected = protected_pids()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in protected:
            continue
        try:
            command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                errors='replace').strip()
        except OSError:
            continue
        if command and any(fragment in command for fragment in
                           COMMAND_FRAGMENTS + LAUNCH_FRAGMENTS):
            matches[pid] = command
    return matches


def signal_matches(sig: signal.Signals, dry_run: bool) -> int:
    matches = matching_processes()
    for pid, command in sorted(matches.items()):
        print(f'{sig.name} pid={pid}: {command}')
        if not dry_run:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
    return len(matches)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Only print matching processes.')
    args = parser.parse_args()

    if args.dry_run:
        signal_matches(signal.SIGINT, True)
        return 0

    for sig, wait_s in ((signal.SIGINT, 5.0), (signal.SIGTERM, 3.0),
                        (signal.SIGKILL, 1.0)):
        if signal_matches(sig, False) == 0:
            break
        time.sleep(wait_s)

    # Clear stale CLI graph cache after all mission processes are gone.
    subprocess.run(['ros2', 'daemon', 'stop'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    remaining = matching_processes()
    if remaining:
        print(f'WARNING: {len(remaining)} mission process(es) remain')
        return 1
    print('Robot mission processes stopped. It is safe to start the manager again.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
