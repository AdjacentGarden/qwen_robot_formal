import os
from functools import lru_cache
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _workspace_from_path(path: Path):
    """Return the colcon workspace containing this robot_bringup install/source."""
    resolved = path.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == "install":
            candidate = parent.parent
            if (candidate / "src" / "robot_bringup").is_dir():
                return candidate
    for parent in (resolved, *resolved.parents):
        if (parent / "src" / "robot_bringup").is_dir():
            return parent
    return None


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    # AMENT_PREFIX_PATH is ordered by overlay priority. Prefer the workspace
    # whose robot_bringup package is active in the currently sourced shell.
    for prefix_text in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep):
        if not prefix_text:
            continue
        prefix = Path(prefix_text)
        package_marker = (
            prefix / "share" / "ament_index" / "resource_index" /
            "packages" / "robot_bringup"
        )
        if not package_marker.is_file():
            continue
        workspace = _workspace_from_path(prefix)
        if workspace is not None:
            return workspace

    # Also support direct source execution and installations where the active
    # ament environment is unavailable but this file remains inside the workspace.
    workspace = _workspace_from_path(Path(__file__))
    if workspace is not None:
        return workspace

    raise RuntimeError(
        "Cannot locate the colcon workspace containing src/robot_bringup. "
        "Source that workspace's install/setup.bash before launching."
    )


def generate_launch_description():
    workspace = workspace_root()
    return LaunchDescription([
        DeclareLaunchArgument("completion_topic", default_value="exploration_complete"),
        DeclareLaunchArgument("frontier_marker_topic", default_value="/explore/frontiers"),
        DeclareLaunchArgument("result_topic", default_value="/mapping/save_result"),
        DeclareLaunchArgument("finish_trajectory_service", default_value="/finish_trajectory"),
        DeclareLaunchArgument("write_state_service", default_value="/write_state"),
        DeclareLaunchArgument("read_metrics_service", default_value="/read_metrics"),
        DeclareLaunchArgument("map_saver_service", default_value="/map_saver/save_map"),
        DeclareLaunchArgument("map_topic", default_value="/map"),
        DeclareLaunchArgument("exploration_control_service", default_value="/control_exploration"),
        DeclareLaunchArgument("required_exploration_rounds", default_value="2"),
        DeclareLaunchArgument("exploration_restart_delay_s", default_value="2.0"),
        DeclareLaunchArgument("exploration_restart_timeout_s", default_value="30.0"),
        DeclareLaunchArgument(
            "save_map_path",
            default_value=str(workspace / "src" / "car_nav2" / "maps" / "exploration_map"),
        ),
        DeclareLaunchArgument(
            "pbstream_path",
            default_value=str(workspace / "src" / "robot_bringup" / "map" / "map.pbstream"),
        ),
        DeclareLaunchArgument(
            "install_map_path",
            default_value=str(
                workspace / "install" / "car_nav2" / "share" /
                "car_nav2" / "maps" / "exploration_map"
            ),
        ),
        DeclareLaunchArgument(
            "install_pbstream_path",
            default_value=str(
                workspace / "install" / "robot_bringup" / "share" /
                "robot_bringup" / "map" / "map.pbstream"
            ),
        ),
        DeclareLaunchArgument("trajectory_id", default_value="0"),
        DeclareLaunchArgument("settle_delay_s", default_value="8.0"),
        DeclareLaunchArgument("service_availability_timeout_s", default_value="10.0"),
        DeclareLaunchArgument("service_timeout_s", default_value="120.0"),
        DeclareLaunchArgument("return_to_start_before_save", default_value="true"),
        DeclareLaunchArgument("return_to_start_timeout_s", default_value="120.0"),
        DeclareLaunchArgument("return_to_start_server_timeout_s", default_value="10.0"),
        DeclareLaunchArgument("return_to_start_max_attempts", default_value="3"),
        DeclareLaunchArgument("return_to_start_retry_delay_s", default_value="2.0"),
        DeclareLaunchArgument("start_pose_lookup_timeout_s", default_value="5.0"),
        DeclareLaunchArgument("save_on_return_failure", default_value="false"),
        DeclareLaunchArgument("navigate_to_pose_action_name", default_value="navigate_to_pose"),
        DeclareLaunchArgument("global_frame", default_value="map"),
        DeclareLaunchArgument("robot_base_frame", default_value="base_footprint"),
        DeclareLaunchArgument("min_cartographer_score_mean", default_value="0.72"),
        DeclareLaunchArgument("skip_save_if_quality_low", default_value="false"),
        DeclareLaunchArgument("require_quality_score", default_value="true"),
        DeclareLaunchArgument("max_reported_frontiers", default_value="30"),
        DeclareLaunchArgument("debug_quality_logs", default_value="true"),
        DeclareLaunchArgument("quality_debug_log_limit", default_value="20"),
        DeclareLaunchArgument("use_sim_time", default_value="true"),
        Node(
            package="robot_bringup",
            executable="save_map_on_exploration_complete.py",
            name="save_map_on_exploration_complete",
            output="screen",
            parameters=[{
                "completion_topic": LaunchConfiguration("completion_topic"),
                "frontier_marker_topic": LaunchConfiguration("frontier_marker_topic"),
                "result_topic": LaunchConfiguration("result_topic"),
                "finish_trajectory_service": LaunchConfiguration(
                    "finish_trajectory_service"),
                "write_state_service": LaunchConfiguration("write_state_service"),
                "read_metrics_service": LaunchConfiguration("read_metrics_service"),
                "map_saver_service": LaunchConfiguration("map_saver_service"),
                "map_topic": LaunchConfiguration("map_topic"),
                "exploration_control_service": LaunchConfiguration("exploration_control_service"),
                "required_exploration_rounds": LaunchConfiguration("required_exploration_rounds"),
                "exploration_restart_delay_s": LaunchConfiguration("exploration_restart_delay_s"),
                "exploration_restart_timeout_s": LaunchConfiguration(
                    "exploration_restart_timeout_s"),
                "save_map_path": LaunchConfiguration("save_map_path"),
                "pbstream_path": LaunchConfiguration("pbstream_path"),
                "install_map_path": LaunchConfiguration("install_map_path"),
                "install_pbstream_path": LaunchConfiguration("install_pbstream_path"),
                "trajectory_id": LaunchConfiguration("trajectory_id"),
                "settle_delay_s": LaunchConfiguration("settle_delay_s"),
                "service_availability_timeout_s": LaunchConfiguration(
                    "service_availability_timeout_s"),
                "service_timeout_s": LaunchConfiguration("service_timeout_s"),
                "return_to_start_before_save": LaunchConfiguration("return_to_start_before_save"),
                "return_to_start_timeout_s": LaunchConfiguration("return_to_start_timeout_s"),
                "return_to_start_server_timeout_s": LaunchConfiguration(
                    "return_to_start_server_timeout_s"),
                "return_to_start_max_attempts": LaunchConfiguration("return_to_start_max_attempts"),
                "return_to_start_retry_delay_s": LaunchConfiguration(
                    "return_to_start_retry_delay_s"),
                "start_pose_lookup_timeout_s": LaunchConfiguration("start_pose_lookup_timeout_s"),
                "save_on_return_failure": LaunchConfiguration("save_on_return_failure"),
                "navigate_to_pose_action_name": LaunchConfiguration("navigate_to_pose_action_name"),
                "global_frame": LaunchConfiguration("global_frame"),
                "robot_base_frame": LaunchConfiguration("robot_base_frame"),
                "min_cartographer_score_mean": LaunchConfiguration("min_cartographer_score_mean"),
                "skip_save_if_quality_low": LaunchConfiguration("skip_save_if_quality_low"),
                "require_quality_score": LaunchConfiguration("require_quality_score"),
                "max_reported_frontiers": LaunchConfiguration("max_reported_frontiers"),
                "debug_quality_logs": LaunchConfiguration("debug_quality_logs"),
                "quality_debug_log_limit": LaunchConfiguration("quality_debug_log_limit"),
                "use_sim_time": LaunchConfiguration("use_sim_time"),
            }],
        ),
    ])
