import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    controller_share = get_package_share_directory('controller')
    # 使用协方差配置 置信系数，用给ekf权重
    # 以后需要从源文件 修改
    # covariance_config = os.path.join(bringup_share, 'config', 'odom_covariance_params.yaml')
        
    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rf2o_in_ekf = LaunchConfiguration('use_rf2o_in_ekf')
    base_frame = LaunchConfiguration('base_frame')
    odom_frame = LaunchConfiguration('odom_frame')
    wheel_track = LaunchConfiguration('wheel_track')
    wheel_diameter = LaunchConfiguration('wheel_diameter')
    motor_speed_topic = LaunchConfiguration('motor_speed_topic')
    motor_speed_unit = LaunchConfiguration('motor_speed_unit')
    left_motor_id = LaunchConfiguration('left_motor_id')
    right_motor_id = LaunchConfiguration('right_motor_id')
    motor_speed_timeout = LaunchConfiguration('motor_speed_timeout')
    wheel_linear_direction = LaunchConfiguration('wheel_linear_direction')
    left_wheel_linear_direction = LaunchConfiguration('left_wheel_linear_direction')
    right_wheel_linear_direction = LaunchConfiguration('right_wheel_linear_direction')
    wheel_angular_direction = LaunchConfiguration('wheel_angular_direction')

    odom_publisher_node = Node(
        package='controller',
        executable='odom_publisher',
        name='odom_publisher',
        output='screen',
        parameters=[
            #这是放缩系数 
            os.path.join(controller_share, 'config', 'calibrate_params.yaml'),
            # covariance_config,  # 加载协方差配置
            {
                'base_frame_id': base_frame,
                'odom_frame_id': odom_frame,
                'pub_odom_topic': True,
                'use_wheel_speed_feedback': True,
                'motor_speed_topic': motor_speed_topic,
                'motor_speed_unit': motor_speed_unit,
                'left_motor_id': left_motor_id,
                'right_motor_id': right_motor_id,
                'motor_speed_timeout': motor_speed_timeout,
                # 调整d 当作系数放缩 一下 手动赋值
                'wheel_diameter': 0.061,
                'wheel_track': 0.27,
                'wheel_linear_direction': 1.,
                'left_wheel_linear_direction': 1.,
                'right_wheel_linear_direction': 1.,
                'wheel_angular_direction': -1.,
            },
        ],
    )

    rf2o_laser_odometry_node = Node(
        package='rf2o_laser_odometry',
        executable='rf2o_laser_odometry_node',
        name='rf2o_laser_odometry',
        output='screen',
        parameters=[
          # covariance_config,  # 加载协方差配置
          {
            'laser_scan_topic' : '/scan',
            'odom_topic' : '/odom_rf2o',
            'publish_tf' : False,
            'base_frame_id' : 'base_footprint',
            'odom_frame_id' : 'odom',
            'init_pose_from_topic' : '',
            'init_pose_from_imu_topic' : '/imu',
            'motion_filter_linear_m' : 0.01,
            'motion_filter_angular_rad' : 0.01,
            'freq' : 12.0,
            
            
            # 'wheel_linear_direction': -1,
            }],
        
        
    )
    


    ekf_filter_node = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'ekf_external_all.yaml'),
            {'use_sim_time': use_sim_time},
            {'publish_tf': False},
        ],
        remappings=[('odometry/filtered', '/odom')],
        # condition=IfCondition(use_rf2o_in_ekf),
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('use_rf2o_in_ekf', default_value='true'),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('odom_frame', default_value='odom'),
        DeclareLaunchArgument('wheel_track', default_value='0.2948'),
        DeclareLaunchArgument('wheel_diameter', default_value='0.07'),
        DeclareLaunchArgument('motor_speed_topic', default_value='/motor_speed'),
        DeclareLaunchArgument('motor_speed_unit', default_value='rpm'),
        DeclareLaunchArgument('left_motor_id', default_value='1'),
        DeclareLaunchArgument('right_motor_id', default_value='2'),
        DeclareLaunchArgument('motor_speed_timeout', default_value='0.5'),
        DeclareLaunchArgument('wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('left_wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('right_wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('wheel_angular_direction', default_value='1.0'),
        odom_publisher_node,
        rf2o_laser_odometry_node,
        ekf_filter_node,
    ])
