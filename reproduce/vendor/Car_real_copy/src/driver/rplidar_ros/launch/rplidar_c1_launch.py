#!/usr/bin/env python3

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    serial_port = LaunchConfiguration('serial_port')
    serial_baudrate = LaunchConfiguration('serial_baudrate')
    frame_id = LaunchConfiguration('frame_id')
    inverted = LaunchConfiguration('inverted')
    angle_compensate = LaunchConfiguration('angle_compensate')
    scan_mode = LaunchConfiguration('scan_mode')
    quality_check_enabled = LaunchConfiguration('quality_check_enabled')
    quality_guard_enabled = LaunchConfiguration('quality_guard_enabled')
    quality_min_points = LaunchConfiguration('quality_min_points')
    quality_min_valid_points = LaunchConfiguration('quality_min_valid_points')
    quality_min_scan_duration = LaunchConfiguration('quality_min_scan_duration')
    quality_max_scan_duration = LaunchConfiguration('quality_max_scan_duration')
    quality_min_angle_coverage_deg = LaunchConfiguration('quality_min_angle_coverage_deg')
    quality_max_angle_gap_deg = LaunchConfiguration('quality_max_angle_gap_deg')
    quality_restart_after_bad_scans = LaunchConfiguration(
        'quality_restart_after_bad_scans')

    return LaunchDescription([
        DeclareLaunchArgument(
            'serial_port',
            default_value='/dev/ttyS8',
            description='Serial device connected to the C1'),

        DeclareLaunchArgument(
            'serial_baudrate',
            default_value='460800',
            description='C1 serial baud rate'),
        
        DeclareLaunchArgument(
            'frame_id',
            default_value='laser_link',
            description='LaserScan frame ID'),

        DeclareLaunchArgument(
            'inverted',
            default_value='false',
            description='Specifying whether or not to invert scan data'),

        DeclareLaunchArgument(
            'angle_compensate',
            default_value='true',
            description='Specifying whether or not to enable angle_compensate of scan data'),

        DeclareLaunchArgument(
            'scan_mode',
            default_value='Standard',
            description='Specifying scan mode of lidar'),

        DeclareLaunchArgument('quality_check_enabled', default_value='false'),
        DeclareLaunchArgument('quality_guard_enabled', default_value='false'),
        DeclareLaunchArgument('quality_min_points', default_value='60'),
        DeclareLaunchArgument('quality_min_valid_points', default_value='60'),
        DeclareLaunchArgument('quality_min_scan_duration', default_value='0.03'),
        DeclareLaunchArgument('quality_max_scan_duration', default_value='0.30'),
        DeclareLaunchArgument('quality_min_angle_coverage_deg', default_value='270.0'),
        DeclareLaunchArgument('quality_max_angle_gap_deg', default_value='90.0'),
        DeclareLaunchArgument(
            'quality_restart_after_bad_scans',
            default_value='3'),
        Node(
            package='rplidar_ros',
            executable='rplidar_node',
            name='rplidar_node',
            parameters=[{'channel_type': 'serial',
                         'serial_port': serial_port,
                         'serial_baudrate': serial_baudrate,
                         'frame_id': frame_id,
                         'inverted': inverted,
                         'angle_compensate': angle_compensate,
                         'scan_mode': scan_mode,
                         'quality_check_enabled': quality_check_enabled,
                         'quality_guard_enabled': quality_guard_enabled,
                         'quality_min_points': quality_min_points,
                         'quality_min_valid_points': quality_min_valid_points,
                         'quality_min_scan_duration': quality_min_scan_duration,
                         'quality_max_scan_duration': quality_max_scan_duration,
                         'quality_min_angle_coverage_deg': quality_min_angle_coverage_deg,
                         'quality_max_angle_gap_deg': quality_max_angle_gap_deg,
                         'quality_restart_after_bad_scans': quality_restart_after_bad_scans}],
            output='screen',
            respawn=True,
            respawn_delay=2.0),
    ])
