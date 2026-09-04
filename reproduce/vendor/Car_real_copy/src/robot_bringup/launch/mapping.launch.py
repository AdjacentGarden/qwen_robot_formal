from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

import os


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    default_map_output = os.path.join(bringup_share, 'map', 'default.pbstream')

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('use_rf2o_in_ekf', default_value='true'),
        DeclareLaunchArgument('lidar_serial_port', default_value='/dev/ttyS8'),
        DeclareLaunchArgument('map_resolution', default_value='0.05'),
        DeclareLaunchArgument('map_output', default_value=default_map_output),
        DeclareLaunchArgument('map_save_timeout', default_value='20.0'),
        DeclareLaunchArgument('map_auto_save_period', default_value='30.0'),
        DeclareLaunchArgument('map_initial_save_delay', default_value='8.0'),
        DeclareLaunchArgument('map_save_retry_period', default_value='3.0'),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(bringup_share, 'launch', 'real_robot.launch.py')
            ),
            launch_arguments={
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'use_rf2o_in_ekf': LaunchConfiguration('use_rf2o_in_ekf'),
                'lidar_serial_port': LaunchConfiguration('lidar_serial_port'),
                'map_resolution': LaunchConfiguration('map_resolution'),
                'enable_auto_navigation': 'false',
                'enable_frontier_exploration': 'false',
            }.items(),
        ),
        Node(
            package='robot_bringup',
            executable='cartographer_shutdown_saver.py',
            name='cartographer_shutdown_saver',
            output='screen',
            sigterm_timeout='25.0',
            sigkill_timeout='30.0',
            arguments=[
                '--output', LaunchConfiguration('map_output'),
                '--timeout', LaunchConfiguration('map_save_timeout'),
                '--save-period', LaunchConfiguration('map_auto_save_period'),
                '--initial-save-delay', LaunchConfiguration('map_initial_save_delay'),
                '--retry-period', LaunchConfiguration('map_save_retry_period'),
            ],
        ),
    ])
