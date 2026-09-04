"""RF2O and EKF as a restartable process group."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rf2o_in_ekf = LaunchConfiguration('use_rf2o_in_ekf')
    base_frame = LaunchConfiguration('base_frame')
    odom_frame = LaunchConfiguration('odom_frame')

    rf2o = Node(
        package='rf2o_laser_odometry',
        executable='rf2o_laser_odometry_node',
        name='rf2o_laser_odometry',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            # RF2O belongs to the gated localization chain. Wall alignment is
            # the only component that intentionally consumes raw /scan.
            'laser_scan_topic': '/scan_gated',
            'odom_topic': '/odom_rf2o',
            'publish_tf': False,
            'base_frame_id': base_frame,
            'odom_frame_id': odom_frame,
            'init_pose_from_topic': '',
            'init_pose_from_imu_topic': '/imu',
            'motion_filter_linear_m': 0.01,
            'motion_filter_angular_rad': 0.01,
            'freq': 10.0,
        }],
        arguments=['--ros-args', '--log-level', 'WARN'],
    )

    ekf_common = [
        {'use_sim_time': use_sim_time, 'publish_tf': True},
    ]

    ekf_with_rf2o = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'ekf_external_all.yaml'),
            *ekf_common,
        ],
        remappings=[('odometry/filtered', '/odom')],
        condition=IfCondition(use_rf2o_in_ekf),
    )

    ekf_without_rf2o = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'ekf_external_imu.yaml'),
            *ekf_common,
        ],
        remappings=[('odometry/filtered', '/odom')],
        condition=UnlessCondition(use_rf2o_in_ekf),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('use_rf2o_in_ekf', default_value='true'),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('odom_frame', default_value='odom'),
        rf2o,
        ekf_with_rf2o,
        ekf_without_rf2o,
    ])
