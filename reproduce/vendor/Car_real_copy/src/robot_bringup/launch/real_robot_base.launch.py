import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    controller_share = get_package_share_directory('controller')
    imu_share = get_package_share_directory('imu_cartographer_publisher')
    robot_description_share = get_package_share_directory('robot_description')
    rplidar_share = get_package_share_directory('rplidar_ros')

    use_sim_time = LaunchConfiguration('use_sim_time')
    lidar_serial_port = LaunchConfiguration('lidar_serial_port')
    lidar_frame = LaunchConfiguration('lidar_frame')
    lidar_quality_check_enabled = LaunchConfiguration('lidar_quality_check_enabled')
    lidar_quality_guard_enabled = LaunchConfiguration('lidar_quality_guard_enabled')
    lidar_quality_min_points = LaunchConfiguration('lidar_quality_min_points')
    lidar_quality_min_valid_points = LaunchConfiguration('lidar_quality_min_valid_points')
    lidar_quality_min_scan_duration = LaunchConfiguration('lidar_quality_min_scan_duration')
    lidar_quality_max_scan_duration = LaunchConfiguration('lidar_quality_max_scan_duration')
    lidar_quality_min_angle_coverage_deg = LaunchConfiguration(
        'lidar_quality_min_angle_coverage_deg')
    lidar_quality_max_angle_gap_deg = LaunchConfiguration(
        'lidar_quality_max_angle_gap_deg')
    lidar_quality_restart_after_bad_scans = LaunchConfiguration(
        'lidar_quality_restart_after_bad_scans')
    imu_frame = LaunchConfiguration('imu_frame')
    imu_odr_hz = LaunchConfiguration('imu_odr_hz')
    imu_publish_orientation = LaunchConfiguration('imu_publish_orientation')
    imu_publish_euler = LaunchConfiguration('imu_publish_euler')
    imu_euler_topic = LaunchConfiguration('imu_euler_topic')
    imu_euler_from_accel = LaunchConfiguration('imu_euler_from_accel')
    imu_euler_yaw_zero = LaunchConfiguration('imu_euler_yaw_zero')
    imu_wait_data_ready = LaunchConfiguration('imu_wait_data_ready')
    imu_reject_min_accel_norm = LaunchConfiguration('imu_reject_min_accel_norm')
    imu_reject_max_accel_norm = LaunchConfiguration('imu_reject_max_accel_norm')
    imu_reject_max_gyro_rad_s = LaunchConfiguration('imu_reject_max_gyro_rad_s')
    cmd_vel_topic = LaunchConfiguration('cmd_vel_topic')
    chassis_serial_port = LaunchConfiguration('chassis_serial_port')
    chassis_baudrate = LaunchConfiguration('chassis_baudrate')
    wheel_track = LaunchConfiguration('wheel_track')
    wheel_diameter = LaunchConfiguration('wheel_diameter')
    motor_speed_topic = LaunchConfiguration('motor_speed_topic')
    motor_speed_unit = LaunchConfiguration('motor_speed_unit')
    motor_speed_timeout = LaunchConfiguration('motor_speed_timeout')
    left_motor_id = LaunchConfiguration('left_motor_id')
    right_motor_id = LaunchConfiguration('right_motor_id')
    motor_speed_scale = LaunchConfiguration('motor_speed_scale')
    enable_speed_closed_loop = LaunchConfiguration('enable_speed_closed_loop')
    speed_kp = LaunchConfiguration('speed_kp')
    speed_ki = LaunchConfiguration('speed_ki')
    speed_kd = LaunchConfiguration('speed_kd')

    robot_description_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(robot_description_share, 'launch', 'robot_description.launch.py')
        ),
        launch_arguments={'use_sim_time': use_sim_time}.items(),
    )

    c1_lidar_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(rplidar_share, 'launch', 'rplidar_c1_launch.py')
        ),
        launch_arguments={
            'serial_port': lidar_serial_port,
            'serial_baudrate': '460800',
            'frame_id': lidar_frame,
            'inverted': 'false',
            'angle_compensate': 'true',
            'scan_mode': 'Standard',
            'quality_check_enabled': lidar_quality_check_enabled,
            'quality_guard_enabled': lidar_quality_guard_enabled,
            'quality_min_points': lidar_quality_min_points,
            'quality_min_valid_points': lidar_quality_min_valid_points,
            'quality_min_scan_duration': lidar_quality_min_scan_duration,
            'quality_max_scan_duration': lidar_quality_max_scan_duration,
            'quality_min_angle_coverage_deg': lidar_quality_min_angle_coverage_deg,
            'quality_max_angle_gap_deg': lidar_quality_max_angle_gap_deg,
            'quality_restart_after_bad_scans': lidar_quality_restart_after_bad_scans,
        }.items(),
    )

    external_imu_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(imu_share, 'launch', 'imu_cartographer_publisher.launch.py')
        ),
        launch_arguments={
            'topic': '/imu',
            'raw_topic': '/imu/raw',
            'raw_counts_topic': '/imu/raw_counts',
            'euler_topic': imu_euler_topic,
            'frame_id': imu_frame,
            'raw_frame_id': 'imu_sensor_link',
            'i2c_bus': LaunchConfiguration('imu_i2c_bus'),
            'device_addr': LaunchConfiguration('imu_device_addr'),
            'sample_period': LaunchConfiguration('imu_sample_period'),
            'odr_hz': imu_odr_hz,
            'wait_data_ready': imu_wait_data_ready,
            'reject_min_accel_norm': LaunchConfiguration('imu_reject_min_accel_norm'),
            'reject_max_accel_norm': LaunchConfiguration('imu_reject_max_accel_norm'),
            'reject_max_gyro_rad_s': imu_reject_max_gyro_rad_s,
            'axis_map': LaunchConfiguration('imu_axis_map'),
            'publish_orientation': imu_publish_orientation,
            'publish_euler': imu_publish_euler,
            'euler_from_accel': imu_euler_from_accel,
            'euler_yaw_zero': imu_euler_yaw_zero,
            'print_debug': LaunchConfiguration('imu_print_debug'),
        }.items(),
    )

    robot_controller_node = Node(
        package='ros_robot_controller',
        executable='ros_robot_controller',
        name='ros_robot_controller',
        output='screen',
        parameters=[{
            'device': chassis_serial_port,
            'baudrate': chassis_baudrate,
            'imu_frame': imu_frame,
            'publish_imu': False,
            'head_imu_topic': '/imu/raw',
            'head_imu_timeout': LaunchConfiguration('head_imu_timeout'),
            'head_calibration_samples': LaunchConfiguration('head_calibration_samples'),
            'head_command_horizontal_deg': LaunchConfiguration('head_command_horizontal_deg'),
            'head_horizontal_roll_deg': LaunchConfiguration('head_horizontal_roll_deg'),
            'head_kp_angle': LaunchConfiguration('head_kp_angle'),
            'head_kp_rate': LaunchConfiguration('head_kp_rate'),
            'head_angle_deadband_deg': LaunchConfiguration('head_angle_deadband_deg'),
            'head_angle_restart_deg': LaunchConfiguration('head_angle_restart_deg'),
            'head_settle_rate_dps': LaunchConfiguration('head_settle_rate_dps'),
            'head_settle_enter_hold_sec': LaunchConfiguration('head_settle_enter_hold_sec'),
            'head_settle_exit_hold_sec': LaunchConfiguration('head_settle_exit_hold_sec'),
            'head_motion_restart_rate_dps': LaunchConfiguration('head_motion_restart_rate_dps'),
            'head_motion_restart_hold_sec': LaunchConfiguration(
                'head_motion_restart_hold_sec'),
            'head_arrival_brake_engage_rate_dps': LaunchConfiguration(
                'head_arrival_brake_engage_rate_dps'),
            'head_arrival_brake_release_rate_dps': LaunchConfiguration(
                'head_arrival_brake_release_rate_dps'),
            'head_motor_deadband': LaunchConfiguration('head_motor_deadband'),
            'head_max_motor_speed': LaunchConfiguration('head_max_motor_speed'),
            'head_max_desired_rate_dps': LaunchConfiguration('head_max_desired_rate_dps'),
            'head_command_timeout_sec': LaunchConfiguration('head_command_timeout_sec'),
            'cmd_vel_topic': cmd_vel_topic,
            'wheel_track': wheel_track,
            'wheel_diameter': wheel_diameter,
            'motor_speed_topic': motor_speed_topic,
            'motor_speed_unit': motor_speed_unit,
            'motor_speed_scale': motor_speed_scale,
            'left_feedback_sign': -1.0,
            'right_feedback_sign': 1.0,
            'enable_speed_closed_loop': enable_speed_closed_loop,
            'speed_kp': speed_kp,
            'speed_ki': speed_ki,
            'speed_kd': speed_kd,
        }],
    )

    odom_publisher_node = Node(
        package='controller',
        executable='odom_publisher',
        name='odom_publisher',
        output='screen',
        parameters=[
            os.path.join(controller_share, 'config', 'calibrate_params.yaml'),
            {
                'base_frame_id': 'base_footprint',
                'odom_frame_id': 'odom',
                'pub_odom_topic': True,
                'use_wheel_speed_feedback': True,
                'motor_speed_topic': motor_speed_topic,
                'motor_speed_unit': motor_speed_unit,
                'left_motor_id': left_motor_id,
                'right_motor_id': right_motor_id,
                'motor_speed_timeout': motor_speed_timeout,
                'wheel_diameter': 0.061,
                'wheel_track': 0.27,
                'wheel_linear_direction': 1.0,
                'angular_direction': 1.0,
            },
        ],
    )

    wheel_joint_state_publisher_node = Node(
        package='controller',
        executable='wheel_joint_state_publisher',
        name='wheel_joint_state_publisher',
        output='screen',
        parameters=[{
            'motor_speed_topic': motor_speed_topic,
            'motor_speed_unit': motor_speed_unit,
            'left_motor_id': left_motor_id,
            'right_motor_id': right_motor_id,
            'left_wheel_joint_direction': -1.0,
            'right_wheel_joint_direction': -1.0,
            'wheel_radius': 0.035,
        }],
    )
    
    tof_publisher_node = Node(
      package='tof_publisher',                    # 包名
      executable='tof_publisher_node',            # 可执行文件名
      name='tof_publisher_node',                  # 节点名称
      output='screen',
    )

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('cmd_vel_topic', default_value='/cmd_vel'),
        DeclareLaunchArgument('lidar_serial_port', default_value='/dev/ttyS8'),
        DeclareLaunchArgument('lidar_quality_check_enabled', default_value='false'),
        DeclareLaunchArgument('lidar_quality_guard_enabled', default_value='false'),
        DeclareLaunchArgument('lidar_quality_min_points', default_value='60'),
        DeclareLaunchArgument('lidar_quality_min_valid_points', default_value='60'),
        DeclareLaunchArgument('lidar_quality_min_scan_duration', default_value='0.03'),
        DeclareLaunchArgument('lidar_quality_max_scan_duration', default_value='0.30'),
        DeclareLaunchArgument(
            'lidar_quality_min_angle_coverage_deg', default_value='270.0'),
        DeclareLaunchArgument('lidar_quality_max_angle_gap_deg', default_value='90.0'),
        DeclareLaunchArgument('lidar_quality_restart_after_bad_scans', default_value='3'),
        DeclareLaunchArgument('chassis_serial_port', default_value='/dev/ttyS0'),
        DeclareLaunchArgument('chassis_baudrate', default_value='115200'),
        DeclareLaunchArgument('wheel_track', default_value='0.2948'),
        DeclareLaunchArgument('wheel_diameter', default_value='0.07'),
        DeclareLaunchArgument('motor_speed_topic', default_value='/motor_speed'),
        DeclareLaunchArgument('motor_speed_unit', default_value='rpm'),
        DeclareLaunchArgument('motor_speed_timeout', default_value='0.5'),
        DeclareLaunchArgument('left_motor_id', default_value='1'),
        DeclareLaunchArgument('right_motor_id', default_value='2'),
        DeclareLaunchArgument('motor_speed_scale', default_value='1.0'),
        DeclareLaunchArgument('enable_speed_closed_loop', default_value='true'),
        DeclareLaunchArgument('speed_kp', default_value='6.0'),
        DeclareLaunchArgument('speed_ki', default_value='1.0'),
        DeclareLaunchArgument('speed_kd', default_value='0.0'),
        DeclareLaunchArgument('lidar_frame', default_value='laser_link'),
        DeclareLaunchArgument('imu_frame', default_value='imu_link'),
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
        DeclareLaunchArgument('head_imu_timeout', default_value='0.25'),
        DeclareLaunchArgument('head_calibration_samples', default_value='100'),
        DeclareLaunchArgument(
            'head_command_horizontal_deg',
            default_value='185.',
            description='Existing /step_motor_angle value that represents the current horizontal head pose.',
        ),
        DeclareLaunchArgument(
            'head_horizontal_roll_deg',
            default_value='180.0',
            description='Nominal horizontal IMU roll; controller adds its installation-offset constant.',
        ),
        DeclareLaunchArgument('head_kp_angle', default_value='1.0'),
        DeclareLaunchArgument('head_kp_rate', default_value='2.0'),
        DeclareLaunchArgument('head_angle_deadband_deg', default_value='2.0'),
        DeclareLaunchArgument('head_angle_restart_deg', default_value='4.0'),
        DeclareLaunchArgument('head_settle_rate_dps', default_value='0.5'),
        DeclareLaunchArgument('head_settle_enter_hold_sec', default_value='0.50'),
        DeclareLaunchArgument('head_settle_exit_hold_sec', default_value='0.20'),
        DeclareLaunchArgument('head_motion_restart_rate_dps', default_value='1.0'),
        DeclareLaunchArgument('head_motion_restart_hold_sec', default_value='0.05'),
        DeclareLaunchArgument(
            'head_arrival_brake_engage_rate_dps', default_value='2.0'),
        DeclareLaunchArgument(
            'head_arrival_brake_release_rate_dps', default_value='0.50'),
        DeclareLaunchArgument('head_motor_deadband', default_value='8.0'),
        DeclareLaunchArgument('head_max_motor_speed', default_value='100.0'),
        DeclareLaunchArgument('head_max_desired_rate_dps', default_value='50.0'),
        DeclareLaunchArgument('head_command_timeout_sec', default_value='5.0'),
        robot_description_launch,
        c1_lidar_launch,
        robot_controller_node,
        odom_publisher_node,
        external_imu_launch,
        wheel_joint_state_publisher_node,
        tof_publisher_node,      # 添加 ToF 节点
    ])
