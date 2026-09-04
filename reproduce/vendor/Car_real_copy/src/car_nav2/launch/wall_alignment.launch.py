import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory('car_nav2')
    params_file = LaunchConfiguration('params_file')

    return LaunchDescription([
        DeclareLaunchArgument(
            'params_file',
            default_value=os.path.join(share, 'param', 'wall_alignment.yaml'),
        ),
        Node(
            package='car_nav2',
            executable='wall_alignment_node',
            name='wall_alignment',
            output='screen',
            parameters=[params_file],
        ),
    ])
