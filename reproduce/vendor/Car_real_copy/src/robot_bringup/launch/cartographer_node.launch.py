import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    car_nav2_share = get_package_share_directory('car_nav2')
    exploration_share = get_package_share_directory('exploration')

    default_rviz_config = os.path.join(bringup_share, 'config', 'default.rviz')

    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rviz = LaunchConfiguration('use_rviz')
    enable_auto_navigation = LaunchConfiguration('enable_auto_navigation')
    enable_frontier_exploration = LaunchConfiguration('enable_frontier_exploration')
    cmd_vel_topic = LaunchConfiguration('cmd_vel_topic')
    nav2_params_dir = LaunchConfiguration('nav2_params_dir')
    explore_params = LaunchConfiguration('explore_params')
    rviz_config = LaunchConfiguration('rviz_config')

    cartographer_node = Node(
        package='cartographer_ros',
        executable='cartographer_node',
        name='cartographer_node',
        output='screen',
        parameters=[{'use_sim_time': use_sim_time}],
        arguments=[
            '-configuration_directory', os.path.join(bringup_share, 'config'),
            '-configuration_basename', 'cartographer_2d_real.lua',
        ],
        remappings=[
            ('scan', '/scan_gated'),
            ('odom', '/odom_cartographer_gated'),
            ('imu', '/imu_cartographer_gated'),
        ],
    )

    occupancy_grid_node = Node(
        package='cartographer_ros',
        executable='cartographer_occupancy_grid_node',
        name='cartographer_occupancy_grid_node',
        output='screen',
        parameters=[{'use_sim_time': use_sim_time}],
        arguments=[
            '-resolution', LaunchConfiguration('map_resolution'),
            '-publish_period_sec', '0.5',
        ],
    )

    # nav2_navigation = TimerAction(
    #     period=8.0,
    #     actions=[
    #         IncludeLaunchDescription(
    #             PythonLaunchDescriptionSource(
    #                 os.path.join(car_nav2_share, 'launch', 'car_nav2.launch.py')
    #             ),
    #             launch_arguments={
    #                 'use_sim_time': use_sim_time,
    #                 'params_dir': nav2_params_dir,
    #                 'autostart': 'true',
    #             }.items(),
    #         )
    #     ],
    #     condition=IfCondition(enable_auto_navigation),
    # )

    frontier_explorer = TimerAction(
        period=15.0,
        actions=[
            Node(
                package='exploration',
                executable='frontier_explorer',
                name='frontier_explorer',
                output='screen',
                parameters=[
                    explore_params,
                    {
                        'nav_action_name': 'navigate_to_pose',
                        'cmd_vel_topic': cmd_vel_topic,
                        'enable_return_home': True,
                        'num_random_goals': 0,
                    },
                ],
            )
        ],
        condition=IfCondition(enable_frontier_exploration),
    )

    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        output='screen',
        arguments=['-d', rviz_config],
        parameters=[{'use_sim_time': use_sim_time}],
        condition=IfCondition(use_rviz),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('enable_auto_navigation', default_value='true'),
        DeclareLaunchArgument('enable_frontier_exploration', default_value='true'),
        DeclareLaunchArgument('cmd_vel_topic', default_value='/cmd_vel'),
        DeclareLaunchArgument('map_resolution', default_value='0.05'),
        DeclareLaunchArgument(
            'nav2_params_dir',
            default_value=os.path.join(car_nav2_share, 'param'),
        ),
        DeclareLaunchArgument(
            'explore_params',
            default_value=os.path.join(exploration_share, 'config', 'explore_params.yaml'),
        ),
        DeclareLaunchArgument(
            'rviz_config',
            default_value=default_rviz_config,
        ),
        
        cartographer_node,
        occupancy_grid_node,
        # nav2_navigation,
        # frontier_explorer,
        rviz_node,
    ])
