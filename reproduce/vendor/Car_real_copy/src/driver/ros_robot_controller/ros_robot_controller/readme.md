cd ..
cd home/test/Car_real

colcon build 

source install/setup.bash
ros2 run ros_robot_controller ros_robot_controller

python3 home/test/Car_real/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py
