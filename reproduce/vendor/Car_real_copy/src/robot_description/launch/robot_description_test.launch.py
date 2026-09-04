import os
import launch
import launch_ros
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('robot_description')
    use_sim_time = LaunchConfiguration('use_sim_time')

    # urdf_file = os.path.join(pkg_share, 'urdf', 'robot_gazebo.urdf')
    default_model_path = pkg_share + '/urdf/robot/rockchip.urdf.xacro'
    # with open(urdf_file, 'r', encoding='utf-8') as f:
    #     robot_description_content = f.read()
    action_declare_arg_mode_path = launch.actions.DeclareLaunchArgument(
        name='model', 
        default_value=str(default_model_path),
        description='URDF 的绝对路径'
    )
    robot_description = launch_ros.parameter_descriptions.ParameterValue(
        launch.substitutions.Command(
            ['xacro ', launch.substitutions.LaunchConfiguration('model')]
        ),
        value_type=str
    )
    robot_state_publisher_node = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        output='screen',
        parameters=[{
            'robot_description': robot_description,
            'use_sim_time': use_sim_time,
        }],
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='false',
            description='Use simulation time',
        ),
        action_declare_arg_mode_path,
        robot_state_publisher_node,
    ])
