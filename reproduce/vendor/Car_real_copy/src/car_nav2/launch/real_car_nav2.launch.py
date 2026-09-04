import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    car_nav2_dir = get_package_share_directory('car_nav2')
    params_dir = LaunchConfiguration('params_dir')
    use_sim_time = LaunchConfiguration('use_sim_time')

    return LaunchDescription([
        DeclareLaunchArgument(
            'params_dir',
            default_value=os.path.join(car_nav2_dir, 'param'),
            description='Directory containing split Nav2 parameter files.',
        ),
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='false',
            description='Use wall time on the real robot.',
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(car_nav2_dir, 'launch', 'car_nav2.launch.py')),
            launch_arguments={
                'params_dir': params_dir,
                'use_sim_time': use_sim_time,
            }.items(),
        ),
    ])
