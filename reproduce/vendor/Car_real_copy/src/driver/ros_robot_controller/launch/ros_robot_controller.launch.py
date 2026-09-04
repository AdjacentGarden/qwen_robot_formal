from launch_ros.actions import Node
from launch import LaunchDescription, LaunchService
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

def generate_launch_description():
    imu_frame = LaunchConfiguration('imu_frame', default='imu_link')
    publish_imu = LaunchConfiguration('publish_imu', default='false')
    head_imu_topic = LaunchConfiguration('head_imu_topic', default='/imu/raw')
    head_command_horizontal_deg = LaunchConfiguration('head_command_horizontal_deg', default='185.')
    head_kp_angle = LaunchConfiguration('head_kp_angle', default='1.0')
    head_kp_rate = LaunchConfiguration('head_kp_rate', default='2.0')
    head_angle_deadband_deg = LaunchConfiguration('head_angle_deadband_deg', default='2.0')
    head_angle_restart_deg = LaunchConfiguration('head_angle_restart_deg', default='4.0')
    head_settle_rate_dps = LaunchConfiguration('head_settle_rate_dps', default='0.5')
    head_settle_enter_hold_sec = LaunchConfiguration(
        'head_settle_enter_hold_sec', default='0.50')
    head_settle_exit_hold_sec = LaunchConfiguration(
        'head_settle_exit_hold_sec', default='0.20')
    head_motion_restart_rate_dps = LaunchConfiguration(
        'head_motion_restart_rate_dps', default='1.0')
    head_motion_restart_hold_sec = LaunchConfiguration(
        'head_motion_restart_hold_sec', default='0.05')
    head_arrival_brake_engage_rate_dps = LaunchConfiguration(
        'head_arrival_brake_engage_rate_dps', default='2.0')
    head_arrival_brake_release_rate_dps = LaunchConfiguration(
        'head_arrival_brake_release_rate_dps', default='0.50')
    head_motor_deadband = LaunchConfiguration('head_motor_deadband', default='8.0')
    head_max_motor_speed = LaunchConfiguration('head_max_motor_speed', default='60.0')
    head_max_desired_rate_dps = LaunchConfiguration(
        'head_max_desired_rate_dps', default='15.0')
    imu_frame_arg = DeclareLaunchArgument('imu_frame', default_value=imu_frame)
    publish_imu_arg = DeclareLaunchArgument('publish_imu', default_value=publish_imu)
    head_imu_topic_arg = DeclareLaunchArgument('head_imu_topic', default_value=head_imu_topic)
    head_horizontal_arg = DeclareLaunchArgument(
        'head_command_horizontal_deg', default_value=head_command_horizontal_deg)
    head_kp_angle_arg = DeclareLaunchArgument('head_kp_angle', default_value=head_kp_angle)
    head_kp_rate_arg = DeclareLaunchArgument('head_kp_rate', default_value=head_kp_rate)
    head_angle_deadband_arg = DeclareLaunchArgument(
        'head_angle_deadband_deg', default_value=head_angle_deadband_deg)
    head_angle_restart_arg = DeclareLaunchArgument(
        'head_angle_restart_deg', default_value=head_angle_restart_deg)
    head_settle_rate_arg = DeclareLaunchArgument(
        'head_settle_rate_dps', default_value=head_settle_rate_dps)
    head_settle_enter_hold_arg = DeclareLaunchArgument(
        'head_settle_enter_hold_sec', default_value=head_settle_enter_hold_sec)
    head_settle_hold_arg = DeclareLaunchArgument(
        'head_settle_exit_hold_sec', default_value=head_settle_exit_hold_sec)
    head_motion_restart_rate_arg = DeclareLaunchArgument(
        'head_motion_restart_rate_dps', default_value=head_motion_restart_rate_dps)
    head_motion_restart_hold_arg = DeclareLaunchArgument(
        'head_motion_restart_hold_sec', default_value=head_motion_restart_hold_sec)
    head_arrival_brake_engage_rate_arg = DeclareLaunchArgument(
        'head_arrival_brake_engage_rate_dps',
        default_value=head_arrival_brake_engage_rate_dps)
    head_arrival_brake_release_rate_arg = DeclareLaunchArgument(
        'head_arrival_brake_release_rate_dps',
        default_value=head_arrival_brake_release_rate_dps)
    head_motor_deadband_arg = DeclareLaunchArgument(
        'head_motor_deadband', default_value=head_motor_deadband)
    head_max_motor_speed_arg = DeclareLaunchArgument(
        'head_max_motor_speed', default_value=head_max_motor_speed)
    head_max_desired_rate_arg = DeclareLaunchArgument(
        'head_max_desired_rate_dps', default_value=head_max_desired_rate_dps)

    ros_robot_controller_node = Node(
        package='ros_robot_controller',
        executable='ros_robot_controller',
        output='screen',
        parameters=[{
            'imu_frame': imu_frame,
            'publish_imu': publish_imu,
            'head_imu_topic': head_imu_topic,
            'head_command_horizontal_deg': head_command_horizontal_deg,
            'head_kp_angle': head_kp_angle,
            'head_kp_rate': head_kp_rate,
            'head_angle_deadband_deg': head_angle_deadband_deg,
            'head_angle_restart_deg': head_angle_restart_deg,
            'head_settle_rate_dps': head_settle_rate_dps,
            'head_settle_enter_hold_sec': head_settle_enter_hold_sec,
            'head_settle_exit_hold_sec': head_settle_exit_hold_sec,
            'head_motion_restart_rate_dps': head_motion_restart_rate_dps,
            'head_motion_restart_hold_sec': head_motion_restart_hold_sec,
            'head_arrival_brake_engage_rate_dps': head_arrival_brake_engage_rate_dps,
            'head_arrival_brake_release_rate_dps': head_arrival_brake_release_rate_dps,
            'head_motor_deadband': head_motor_deadband,
            'head_max_motor_speed': head_max_motor_speed,
            'head_max_desired_rate_dps': head_max_desired_rate_dps,
        }]
    )

    return LaunchDescription([
        imu_frame_arg,
        publish_imu_arg,
        head_imu_topic_arg,
        head_horizontal_arg,
        head_kp_angle_arg,
        head_kp_rate_arg,
        head_angle_deadband_arg,
        head_angle_restart_arg,
        head_settle_rate_arg,
        head_settle_enter_hold_arg,
        head_settle_hold_arg,
        head_motion_restart_rate_arg,
        head_motion_restart_hold_arg,
        head_arrival_brake_engage_rate_arg,
        head_arrival_brake_release_rate_arg,
        head_motor_deadband_arg,
        head_max_motor_speed_arg,
        head_max_desired_rate_arg,
        ros_robot_controller_node
    ])

if __name__ == '__main__':
    # 创建一个LaunchDescription对象
    ld = generate_launch_description()

    ls = LaunchService()
    ls.include_launch_description(ld)
    ls.run()
