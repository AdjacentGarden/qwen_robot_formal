import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    imu_share = get_package_share_directory('imu_cartographer_publisher')
    robot_description_share = get_package_share_directory('robot_description')
    rplidar_share = get_package_share_directory('rplidar_ros')

    use_sim_time = LaunchConfiguration('use_sim_time')
    lidar_serial_port = LaunchConfiguration('lidar_serial_port')
    lidar_frame = LaunchConfiguration('lidar_frame')
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
    left_motor_id = LaunchConfiguration('left_motor_id')
    right_motor_id = LaunchConfiguration('right_motor_id')
    motor_speed_scale = LaunchConfiguration('motor_speed_scale')
    enable_speed_closed_loop = LaunchConfiguration('enable_speed_closed_loop')
    speed_kp = LaunchConfiguration('speed_kp')
    speed_ki = LaunchConfiguration('speed_ki')
    speed_kd = LaunchConfiguration('speed_kd')

    robot_description_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(robot_description_share, 'launch', 'robot_description_test.launch.py')
        ),
        launch_arguments={'use_sim_time': use_sim_time}.items(),
    )

    c1_lidar_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(rplidar_share, 'launch', 'rplidar_c1_launch.py')
        ),
        launch_arguments={
            'channel_type': 'serial',
            'serial_port': lidar_serial_port,
            'serial_baudrate': '460800',
            'frame_id': lidar_frame,
            'inverted': 'false',
            'angle_compensate': 'true',
            'scan_mode': 'Standard',
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

    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('cmd_vel_topic', default_value='/cmd_vel'),
        DeclareLaunchArgument('lidar_serial_port', default_value='/dev/ttyS8'),
        DeclareLaunchArgument('chassis_serial_port', default_value='/dev/ttyS0'),
        DeclareLaunchArgument('chassis_baudrate', default_value='115200'),
        DeclareLaunchArgument('wheel_track', default_value='0.2948'),
        DeclareLaunchArgument('wheel_diameter', default_value='0.07'),
        DeclareLaunchArgument('motor_speed_topic', default_value='/motor_speed'),
        DeclareLaunchArgument('motor_speed_unit', default_value='rpm'),
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
        DeclareLaunchArgument('head_command_horizontal_deg', default_value='185.'),
        DeclareLaunchArgument('head_kp_angle', default_value='1.0'),
        DeclareLaunchArgument('head_kp_rate', default_value='2.0'),
        DeclareLaunchArgument('head_angle_deadband_deg', default_value='4.0'),
        DeclareLaunchArgument('head_angle_restart_deg', default_value='7.0'),
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
        DeclareLaunchArgument('head_max_motor_speed', default_value='40.0'),
        DeclareLaunchArgument('head_max_desired_rate_dps', default_value='5.0'),
        robot_description_launch,
        c1_lidar_launch,
        external_imu_launch,
        robot_controller_node,
        wheel_joint_state_publisher_node,
    ])
