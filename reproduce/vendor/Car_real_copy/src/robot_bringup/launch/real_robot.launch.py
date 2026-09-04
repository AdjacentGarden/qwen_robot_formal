import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')
    car_nav2_share = get_package_share_directory('car_nav2')
    exploration_share = get_package_share_directory('exploration')

    default_rviz_config = os.path.join(bringup_share, 'config', 'default.rviz')

    use_sim_time = LaunchConfiguration('use_sim_time')
    use_rviz = LaunchConfiguration('use_rviz')
    enable_auto_navigation = LaunchConfiguration('enable_auto_navigation')
    enable_frontier_exploration = LaunchConfiguration('enable_frontier_exploration')
    use_rf2o_in_ekf = LaunchConfiguration('use_rf2o_in_ekf')
    cmd_vel_topic = LaunchConfiguration('cmd_vel_topic')
    base_frame = LaunchConfiguration('base_frame')
    odom_frame = LaunchConfiguration('odom_frame')
    lidar_serial_port = LaunchConfiguration('lidar_serial_port')
    chassis_serial_port = LaunchConfiguration('chassis_serial_port')
    chassis_baudrate = LaunchConfiguration('chassis_baudrate')
    wheel_track = LaunchConfiguration('wheel_track')
    wheel_diameter = LaunchConfiguration('wheel_diameter')
    motor_speed_topic = LaunchConfiguration('motor_speed_topic')
    motor_speed_unit = LaunchConfiguration('motor_speed_unit')
    motor_speed_scale = LaunchConfiguration('motor_speed_scale')
    left_motor_id = LaunchConfiguration('left_motor_id')
    right_motor_id = LaunchConfiguration('right_motor_id')
    motor_speed_timeout = LaunchConfiguration('motor_speed_timeout')
    wheel_linear_direction = LaunchConfiguration('wheel_linear_direction')
    left_wheel_linear_direction = LaunchConfiguration('left_wheel_linear_direction')
    right_wheel_linear_direction = LaunchConfiguration('right_wheel_linear_direction')
    wheel_angular_direction = LaunchConfiguration('wheel_angular_direction')
    enable_speed_closed_loop = LaunchConfiguration('enable_speed_closed_loop')
    speed_kp = LaunchConfiguration('speed_kp')
    speed_ki = LaunchConfiguration('speed_ki')
    speed_kd = LaunchConfiguration('speed_kd')
    lidar_frame = LaunchConfiguration('lidar_frame')
    imu_frame = LaunchConfiguration('imu_frame')
    map_resolution = LaunchConfiguration('map_resolution')
    nav2_params_dir = LaunchConfiguration('nav2_params_dir')
    explore_params = LaunchConfiguration('explore_params')
    rviz_config = LaunchConfiguration('rviz_config')

    imu_args = {
        'imu_i2c_bus': LaunchConfiguration('imu_i2c_bus'),
        'imu_device_addr': LaunchConfiguration('imu_device_addr'),
        'imu_sample_period': LaunchConfiguration('imu_sample_period'),
        'imu_odr_hz': LaunchConfiguration('imu_odr_hz'),
        'imu_wait_data_ready': LaunchConfiguration('imu_wait_data_ready'),
        'imu_reject_min_accel_norm': LaunchConfiguration('imu_reject_min_accel_norm'),
        'imu_reject_max_accel_norm': LaunchConfiguration('imu_reject_max_accel_norm'),
        'imu_reject_max_gyro_rad_s': LaunchConfiguration('imu_reject_max_gyro_rad_s'),
        'imu_axis_map': LaunchConfiguration('imu_axis_map'),
        'imu_publish_orientation': LaunchConfiguration('imu_publish_orientation'),
        'imu_publish_euler': LaunchConfiguration('imu_publish_euler'),
        'imu_euler_topic': LaunchConfiguration('imu_euler_topic'),
        'imu_euler_from_accel': LaunchConfiguration('imu_euler_from_accel'),
        'imu_euler_yaw_zero': LaunchConfiguration('imu_euler_yaw_zero'),
        'imu_print_debug': LaunchConfiguration('imu_print_debug'),
    }

    base_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(bringup_share, 'launch', 'real_robot_base.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'cmd_vel_topic': cmd_vel_topic,
            'lidar_serial_port': lidar_serial_port,
            'chassis_serial_port': chassis_serial_port,
            'chassis_baudrate': chassis_baudrate,
            'wheel_track': wheel_track,
            'wheel_diameter': wheel_diameter,
            'motor_speed_topic': motor_speed_topic,
            'motor_speed_unit': motor_speed_unit,
            'motor_speed_scale': motor_speed_scale,
            'left_motor_id': left_motor_id,
            'right_motor_id': right_motor_id,
            'enable_speed_closed_loop': enable_speed_closed_loop,
            'speed_kp': speed_kp,
            'speed_ki': speed_ki,
            'speed_kd': speed_kd,
            'lidar_frame': lidar_frame,
            'imu_frame': imu_frame,
            **imu_args,
        }.items(),
    )

    odometry_launch = TimerAction(
        period=LaunchConfiguration('odometry_start_delay'),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(bringup_share, 'launch', 'real_robot_odometry.launch.py')
                ),
                launch_arguments={
                    'use_sim_time': use_sim_time,
                    'use_rf2o_in_ekf': use_rf2o_in_ekf,
                    'base_frame': base_frame,
                    'odom_frame': odom_frame,
                    'wheel_track': wheel_track,
                    'wheel_diameter': wheel_diameter,
                    'motor_speed_topic': motor_speed_topic,
                    'motor_speed_unit': motor_speed_unit,
                    'left_motor_id': left_motor_id,
                    'right_motor_id': right_motor_id,
                    'motor_speed_timeout': motor_speed_timeout,
                    'wheel_linear_direction': wheel_linear_direction,
                    'left_wheel_linear_direction': left_wheel_linear_direction,
                    'right_wheel_linear_direction': right_wheel_linear_direction,
                    'wheel_angular_direction': wheel_angular_direction,
                }.items(),
            )
        ],
    )

    mapping_nav_launch = TimerAction(
        period=LaunchConfiguration('mapping_start_delay'),
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(bringup_share, 'launch', 'real_robot_mapping_nav.launch.py')
                ),
                launch_arguments={
                    'use_sim_time': use_sim_time,
                    'use_rviz': use_rviz,
                    'enable_auto_navigation': enable_auto_navigation,
                    'enable_frontier_exploration': enable_frontier_exploration,
                    'cmd_vel_topic': cmd_vel_topic,
                    'map_resolution': map_resolution,
                    'nav2_params_dir': nav2_params_dir,
                    'explore_params': explore_params,
                    'rviz_config': rviz_config,
                }.items(),
            )
        ],
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('enable_auto_navigation', default_value='true'),
        DeclareLaunchArgument('enable_frontier_exploration', default_value='true'),
        DeclareLaunchArgument('use_rf2o_in_ekf', default_value='true'),
        DeclareLaunchArgument('cmd_vel_topic', default_value='/cmd_vel'),
        DeclareLaunchArgument('base_frame', default_value='base_footprint'),
        DeclareLaunchArgument('odom_frame', default_value='odom'),
        DeclareLaunchArgument('lidar_serial_port', default_value='/dev/ttyS8'),
        DeclareLaunchArgument('chassis_serial_port', default_value='/dev/ttyS0'),
        DeclareLaunchArgument('chassis_baudrate', default_value='115200'),
        DeclareLaunchArgument('wheel_track', default_value='0.2948'),
        DeclareLaunchArgument('wheel_diameter', default_value='0.07'),
        DeclareLaunchArgument('motor_speed_topic', default_value='/motor_speed'),
        DeclareLaunchArgument('motor_speed_unit', default_value='rpm'),
        DeclareLaunchArgument('motor_speed_scale', default_value='1.0'),
        DeclareLaunchArgument('left_motor_id', default_value='1'),
        DeclareLaunchArgument('right_motor_id', default_value='2'),
        DeclareLaunchArgument('motor_speed_timeout', default_value='0.5'),
        DeclareLaunchArgument('wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('left_wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('right_wheel_linear_direction', default_value='1.0'),
        DeclareLaunchArgument('wheel_angular_direction', default_value='-1.0'),
        DeclareLaunchArgument('enable_speed_closed_loop', default_value='true'),
        DeclareLaunchArgument('speed_kp', default_value='6.0'),
        DeclareLaunchArgument('speed_ki', default_value='1.0'),
        DeclareLaunchArgument('speed_kd', default_value='0.0'),
        DeclareLaunchArgument('lidar_frame', default_value='laser_link'),
        DeclareLaunchArgument('imu_frame', default_value='imu_link'),
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
        DeclareLaunchArgument('imu_i2c_bus', default_value='4'),
        DeclareLaunchArgument('imu_device_addr', default_value='0x6A'),
        DeclareLaunchArgument('imu_sample_period', default_value='0.02'),
        DeclareLaunchArgument('imu_odr_hz', default_value='208'),
        DeclareLaunchArgument('imu_wait_data_ready', default_value='false'),
        DeclareLaunchArgument('imu_reject_min_accel_norm', default_value='6.0'),
        DeclareLaunchArgument('imu_reject_max_accel_norm', default_value='13.0'),
        DeclareLaunchArgument('imu_reject_max_gyro_rad_s', default_value='8.0'),
        DeclareLaunchArgument('imu_axis_map', default_value='-y,-x,-z'),
        DeclareLaunchArgument('imu_publish_orientation', default_value='true'),
        DeclareLaunchArgument('imu_publish_euler', default_value='true'),
        DeclareLaunchArgument('imu_euler_topic', default_value='/imu/euler_deg'),
        DeclareLaunchArgument('imu_euler_from_accel', default_value='true'),
        DeclareLaunchArgument('imu_euler_yaw_zero', default_value='false'),
        DeclareLaunchArgument('imu_print_debug', default_value='true'),
        DeclareLaunchArgument('odometry_start_delay', default_value='2.0'),
        DeclareLaunchArgument('mapping_start_delay', default_value='5.0'),
        base_launch,
        odometry_launch,
        mapping_nav_launch,
    ])
