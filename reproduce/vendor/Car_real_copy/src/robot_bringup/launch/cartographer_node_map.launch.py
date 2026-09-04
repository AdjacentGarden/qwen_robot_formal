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
    # exploration_share = get_package_share_directory('exploration')

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
            '-collect_metrics',
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

    # Start the lifecycle map saver with Cartographer, before exploration and
    # before constraint building can saturate low-end hardware. The save node
    # later reuses this already-discovered service instead of spawning a CLI.
    map_saver_node = Node(
        package='nav2_map_server',
        executable='map_saver_server',
        name='map_saver',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'save_map_timeout': LaunchConfiguration('map_save_timeout_s'),
            'free_thresh_default': 0.25,
            'occupied_thresh_default': 0.65,
            'map_subscribe_transient_local': True,
        }],
    )

    map_saver_lifecycle_manager = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager_map_saver',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'autostart': True,
            'node_names': ['map_saver'],
        }],
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

    # frontier_explorer = TimerAction(
    #     period=15.0,
    #     actions=[
    #         Node(
    #             package='exploration',
    #             executable='frontier_explorer',
    #             name='frontier_explorer',
    #             output='screen',
    #             parameters=[
    #                 explore_params,
    #                 {
    #                     'nav_action_name': 'navigate_to_pose',
    #                     'cmd_vel_topic': cmd_vel_topic,
    #                     'enable_return_home': True,
    #                     'num_random_goals': 0,
    #                 },
    #             ],
    #         )
    #     ],
    #     condition=IfCondition(enable_frontier_exploration),
    # )

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
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('enable_auto_navigation', default_value='false'),
        DeclareLaunchArgument('enable_frontier_exploration', default_value='false'),
        DeclareLaunchArgument('cmd_vel_topic', default_value='/cmd_vel'),
        DeclareLaunchArgument('map_resolution', default_value='0.05'),
        DeclareLaunchArgument('map_save_timeout_s', default_value='120.0'),
        DeclareLaunchArgument(
            'nav2_params_dir',
            default_value=os.path.join(car_nav2_share, 'param_map'),
        ),
        # DeclareLaunchArgument(
        #     'explore_params',
        #     default_value=os.path.join(exploration_share, 'config', 'explore_params.yaml'),
        # ),
        DeclareLaunchArgument(
            'rviz_config',
            default_value=default_rviz_config,
        ),
        
        cartographer_node,
        occupancy_grid_node,
        map_saver_node,
        map_saver_lifecycle_manager,
        # nav2_navigation,
        # frontier_explorer,
        rviz_node,
    ])
