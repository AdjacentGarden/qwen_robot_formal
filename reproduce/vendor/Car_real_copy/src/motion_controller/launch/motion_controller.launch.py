import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory('motion_controller')
    params_file = LaunchConfiguration('params_file')
    initial_mode = LaunchConfiguration('initial_mode')
    use_sim_time = LaunchConfiguration('use_sim_time')
    direction_reverse = LaunchConfiguration('direction_reverse')

    return LaunchDescription([
        DeclareLaunchArgument(
            'params_file',
            default_value=os.path.join(share, 'config', 'motion_controller.yaml'),
        ),
        DeclareLaunchArgument('initial_mode', default_value='stop'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('direction_reverse', default_value='1.0'),
        Node(
            package='motion_controller',
            executable='motion_controller_node',
            name='motion_controller',
            output='screen',
            parameters=[params_file, {
                'initial_mode': initial_mode,
                'use_sim_time': use_sim_time,
                'direction_reverse': direction_reverse,
            }],
        ),
    ])
