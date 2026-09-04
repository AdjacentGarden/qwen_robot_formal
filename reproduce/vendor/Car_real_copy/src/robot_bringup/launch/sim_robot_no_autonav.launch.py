import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    robot_description_share = get_package_share_directory('robot_description')

    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rf2o_in_ekf = LaunchConfiguration('use_rf2o_in_ekf')
    enable_odometry = LaunchConfiguration('enable_odometry')
    base_frame = LaunchConfiguration('base_frame')
    odom_frame = LaunchConfiguration('odom_frame')

    robot_description_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(robot_description_share, 'launch', 'gazebo.launch.py')
        ),
        launch_arguments={'use_sim_time': use_sim_time}.items(),
    )

    rf2o_laser_odometry_node = Node(
        package='rf2o_laser_odometry',
        executable='rf2o_laser_odometry_node',
        name='rf2o_laser_odometry',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
            'laser_scan_topic': '/scan',
            'odom_topic': '/odom_rf2o',
            'publish_tf': False,
            'base_frame_id': base_frame,
            'odom_frame_id': odom_frame,
            'init_pose_from_topic': '',
            'init_pose_from_imu_topic': '/imu',
            'motion_filter_linear_m': 0.01,
            'motion_filter_angular_rad': 0.01,
            'freq': 12.0,
        }],
        arguments=['--ros-args', '--log-level', 'WARN'],
        condition=IfCondition(enable_odometry),
    )

    ekf_filter_node_without_rf2o = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'ekf_external_imu.yaml'),
            {'use_sim_time': use_sim_time, 'publish_tf': True},
        ],
        remappings=[('odometry/filtered', '/odom')],
        condition=IfCondition(PythonExpression([
            "'", enable_odometry, "'.lower() == 'true' and '",
            use_rf2o_in_ekf, "'.lower() == 'false'",
        ])),
    )

    ekf_filter_node_with_rf2o = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'ekf_external_imu_rf2o.yaml'),
            {'use_sim_time': use_sim_time, 'publish_tf': True},
        ],
        remappings=[('odometry/filtered', '/odom')],
        condition=IfCondition(PythonExpression([
            "'", enable_odometry, "'.lower() == 'true' and '",
            use_rf2o_in_ekf, "'.lower() == 'true'",
        ])),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='true'),
        DeclareLaunchArgument('use_rf2o_in_ekf', default_value='true'),
        DeclareLaunchArgument(
            'enable_odometry',
            default_value='true',
            description='Start RF2O and EKF in this launch process.',
        ),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('odom_frame', default_value='odom'),
        robot_description_launch,
        rf2o_laser_odometry_node,
        ekf_filter_node_without_rf2o,
        ekf_filter_node_with_rf2o,
    ])
