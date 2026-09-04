import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node


def generate_launch_description():
    car_nav2_dir = get_package_share_directory('car_nav2')
    nav2_bt_dir = get_package_share_directory('nav2_bt_navigator')

    params_dir = LaunchConfiguration('params_dir')
    use_sim_time = LaunchConfiguration('use_sim_time')
    autostart = LaunchConfiguration('autostart')

    param_files = [
        PathJoinSubstitution([params_dir, 'nav2_bt_navigator.yaml']),
        PathJoinSubstitution([params_dir, 'nav2_controller_rpp.yaml']),
        PathJoinSubstitution([params_dir, 'nav2_costmaps.yaml']),
        PathJoinSubstitution([params_dir, 'nav2_planner_smoother.yaml']),
        PathJoinSubstitution([params_dir, 'nav2_behaviors.yaml']),
        PathJoinSubstitution([params_dir, 'nav2_waypoint_velocity.yaml']),
        # PathJoinSubstitution([params_dir, 'collision_monitor.yaml']),
    ]

    configured_params = param_files + [{
        'use_sim_time': use_sim_time,
        'default_nav_to_pose_bt_xml': os.path.join(
            nav2_bt_dir, 'behavior_trees',
            'navigate_w_recovery_and_replanning_only_if_path_becomes_invalid.xml'),
        'default_nav_through_poses_bt_xml': os.path.join(
            nav2_bt_dir, 'behavior_trees', 'navigate_through_poses_w_replanning_and_recovery.xml'),
    }]

    lifecycle_nodes = [
        'controller_server',
        'planner_server',
        'behavior_server',
        'bt_navigator',
        'velocity_smoother',
        # 'collision_monitor',
    ]

    return LaunchDescription([
        DeclareLaunchArgument(
            'params_dir',
            default_value=os.path.join(car_nav2_dir, 'param'),
            description='Directory containing split Nav2 parameter files.',
        ),
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='true',
            description='Use simulation clock. Set false for the real robot.',
        ),
        DeclareLaunchArgument(
            'autostart',
            default_value='true',
            description='Automatically configure and activate Nav2 lifecycle nodes.',
        ),

        Node(
            package='nav2_controller',
            executable='controller_server',
            output='screen',
            parameters=configured_params,
            remappings=[('cmd_vel', 'cmd_vel_nav')],
        ),
        Node(
            package='nav2_planner',
            executable='planner_server',
            name='planner_server',
            output='screen',
            parameters=configured_params,
        ),
        Node(
            package='nav2_behaviors',
            executable='behavior_server',
            name='behavior_server',
            output='screen',
            parameters=configured_params,
            # Recovery behaviors enter the Nav2 smoothing/arbitration chain.
            remappings=[('cmd_vel', 'cmd_vel_nav')],
        ),
        Node(
            package='nav2_bt_navigator',
            executable='bt_navigator',
            name='bt_navigator',
            output='screen',
            parameters=configured_params,
        ),
        Node(
            package='nav2_velocity_smoother',
            executable='velocity_smoother',
            name='velocity_smoother',
            output='screen',
            parameters=configured_params,
            # remappings=[('cmd_vel', 'cmd_vel_nav'), ('cmd_vel_smoothed', 'cmd_vel_smoothed')],
            # 最终由 motion_controller 在 Nav2 与墙面对齐之间进行仲裁。
            remappings=[('cmd_vel', 'cmd_vel_nav'),
                        ('cmd_vel_smoothed', 'cmd_vel_nav_smoothed')],
        ),
        # Node(
        #     package='nav2_collision_monitor',
        #     executable='collision_monitor',
        #     name='collision_monitor',
        #     output='screen',
        #     parameters=configured_params,
        # ),
        Node(
            package='nav2_lifecycle_manager',
            executable='lifecycle_manager',
            name='lifecycle_manager_navigation',
            output='screen',
            parameters=[{
                'use_sim_time': use_sim_time,
                'autostart': autostart,
                'bond_timeout': 4.0,
                'node_names': lifecycle_nodes,
            }],
        ),
    ])
