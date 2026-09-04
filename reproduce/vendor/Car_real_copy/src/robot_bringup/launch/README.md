1. 底盘控制
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_base.launch.py

不使用imu
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_base_wo_imu.launch.py
2. odom pub
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_odometry.launch.py

cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_odometry_wo_tf.launch.py

3. 建图 cartographer 
原始版本
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup cartographer_node.launch.py
不发布tf
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup cartographer_node_test1.launch.py
发布tf
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup cartographer_node_test2.launch.py

不使用imu
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup cartographer_node_test3.launch.py

不使用imu 使用odom
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup cartographer_node_test4.launch.py

4. 导航
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_nav.launch.py


test0 最原始版 倒置imu 
ros2 launch robot_bringup real_robot_base.launch.py 
ros2 launch robot_bringup real_robot_odometry.launch.py
ros2 launch robot_bringup cartographer_node.launch.py

test1 基本和test0 一致 优化了cartographer的lua配置文件 小房间定位成功状态 基本0误差
ros2 launch robot_bringup real_robot_base.launch.py 
ros2 launch robot_bringup real_robot_odometry.launch.py
ros2 launch robot_bringup cartographer_node_test1.launch.py

test11 在1 基础上订阅/odom 发布map -> imu_link
ros2 launch robot_bringup real_robot_base.launch.py 
ros2 launch robot_bringup real_robot_odometry_wo_tf.launch.py
ros2 launch robot_bringup cartographer_node_test11.launch.py

test2  废弃，

test3 正imu  cartographer 不使用imu no 里程计 有10cm误差
ros2 launch robot_bringup real_robot_base_wo_imu.launch.py
<!-- ros2 launch robot_bringup real_robot_odometry.launch.py -->
ros2 launch robot_bringup cartographer_node_test3.launch.py

test4 正imu  cartographer 不使用imu 用 里程计 rf2o方向会反，
ros2 launch robot_bringup real_robot_base_wo_imu.launch.py
ros2 launch robot_bringup real_robot_odometry.launch.py
ros2 launch robot_bringup cartographer_node_test4.launch.py


ros2 service call /finish_trajectory cartographer_ros_msgs/srv/FinishTrajectory "{trajectory_id: 0}"

ros2 service call /write_state cartographer_ros_msgs/srv/WriteState "{filename: '/home/test/Car_real_copy/src/robot_bringup/map/813.pbstream', include_unfinished_submaps: false}"


导航流程
cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_base.launch.py

cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_odometry.launch.py

cd Car_real_copy
source install/setup.bash
ros2 launch robot_bringup real_robot_nav.launch.py


手动测试tof
要让键盘节点发布到 /cmd_vel_smoothed，你需要使用 ROS 2 的话题重映射功能。核心命令是：

ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args --remap cmd_vel:=/cmd_vel_smoothed