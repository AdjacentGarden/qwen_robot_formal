import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _start_nav2_after_gate(event, context, *, nav2_navigation):
    del context
    if event.returncode == 0:
        return [nav2_navigation]
    return []


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    car_nav2_share = get_package_share_directory('car_nav2')

    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rviz = LaunchConfiguration('use_rviz')
    enable_auto_navigation = LaunchConfiguration('enable_auto_navigation')
    map_resolution = LaunchConfiguration('map_resolution')
    nav2_params_dir = LaunchConfiguration('nav2_params_dir')
    rviz_config = LaunchConfiguration('rviz_config')

    cartographer_mapping = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(bringup_share, 'launch', 'cartographer_node_map.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'use_rviz': use_rviz,
            'map_resolution': map_resolution,
            'rviz_config': rviz_config,
            # ========== 添加日志级别 ==========
            'log_level': 'WARN',  # 只显示警告和错误
        }.items(),
    )

    nav2_navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(car_nav2_share, 'launch', 'car_nav2.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'params_dir': nav2_params_dir,
            'autostart': 'true',
        }.items(),
    )

    mapping_ready_gate = Node(
        package='robot_bringup',
        executable='wait_for_mapping_ready.py',
        name='mapping_ready_gate',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'timeout_s': 120.0,
            'stable_duration_s': 3.0,
        }],
        condition=IfCondition(enable_auto_navigation),
    )

    start_navigation_when_ready = RegisterEventHandler(
        OnProcessExit(
            target_action=mapping_ready_gate,
            on_exit=[OpaqueFunction(
                function=_start_nav2_after_gate,
                kwargs={'nav2_navigation': nav2_navigation},
            )],
        )
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='false',
            description='Use simulation time instead of the real robot system clock.',
        ),
        DeclareLaunchArgument(
            'use_rviz',
            default_value='false',
            description='Start RViz with the Cartographer mapping view.',
        ),
        DeclareLaunchArgument(
            'enable_auto_navigation',
            default_value='true',
            description='Start mapping-mode Nav2 inside this launch.',
        ),
        DeclareLaunchArgument('map_resolution', default_value='0.05'),
        DeclareLaunchArgument(
            'nav2_params_dir',
            default_value=os.path.join(car_nav2_share, 'param_map'),
            description='Directory containing the mapping-mode Nav2 parameters.',
        ),
        DeclareLaunchArgument(
            'rviz_config',
            default_value=os.path.join(bringup_share, 'config', 'default.rviz'),
        ),
        cartographer_mapping,
        mapping_ready_gate,
        start_navigation_when_ready,
    ])
