# Car Real：建图、导航与多控制源运动管理

这是一个 ROS 2 Humble 工作空间，包含 RF2O、EKF、Cartographer、Nav2、自动探索、墙面对齐、底盘运动仲裁和 AI 外部控制模拟器。

本文档分为五部分：

1. 快速开始
2. 建图与导航流程管理器
3. 运动控制器
4. AI 控制模拟器
5. 自动测试与验收

# 1. 快速开始

## 1.1 环境与构建

环境要求：

- Ubuntu 22.04
- ROS 2 Humble
- Python 3.10
- 已安装 Cartographer、Nav2、Gazebo、robot_localization 等依赖

首次构建：

```bash
cd /home/garry/car_real
source /opt/ros/humble/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

修改本项目后可只构建主要包：

```bash
colcon build --symlink-install --packages-select \
  motion_controller car_nav2 robot_bringup robot_system_test
source install/setup.bash
```

每个新终端都需要加载环境：

```bash
cd /home/garry/car_real
source install/setup.bash
```

## 1.2 默认启动：有地图直接导航，无地图自动建图

当前流程管理器的内置基础组是 Gazebo，因此仿真使用：

```bash
source install/setup.bash
ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
  -p use_sim_time:=false \
  -p use_rviz:=true \
  -p init:=true
```

`init` 默认为 `false`：

```text
存在有效 map.pbstream
→ 跳过建图
→ 启动 Cartographer 定位
→ 启动 Nav2
→ 等待 motion_controller 就绪
→ NAVIGATION

不存在有效 map.pbstream
→ 记录警告
→ 自动执行建图、探索、保存、定位和导航全流程
```

管理器检查定位实际加载的文件：

```text
install/robot_bringup/share/robot_bringup/map/map.pbstream
```

低性能真机建议关闭本机 RViz，避免 Cartographer、Nav2 lifecycle 服务和 TF 回调因
CPU/GPU 争用超时：

```bash
ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
  -p use_sim_time:=false \
  -p use_rviz:=false
```

管理器不会使用固定延迟启动 Nav2。它会等待 Cartographer 节点与服务、有效 `/map` 和
`map -> odom` TF 连续稳定后再启动 mapping-mode Nav2。单独运行
`real_robot_automap.launch.py enable_auto_navigation:=true` 时也使用同一类就绪门控，
不再使用固定 5 秒 `TimerAction`。

## 1.3 强制重新建图

即使已有地图，也强制执行完整建图流程：

```bash
ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
  -p init:=true \
  -p use_sim_time:=true \
  -p use_rviz:=true
```

## 1.4 外部真机基础组

如果雷达、底盘控制器、robot_state_publisher 等真机基础节点已经由外部程序启动，必须避免重复启动基础组和 RF2O/EKF。

基础组应关闭其内置里程计，然后让管理器负责 RF2O/EKF：

```bash
ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
  -p init:=false \
  -p use_sim_time:=false \
  -p use_rviz:=true
```

真机必须使用系统时间，即 `use_sim_time:=false`；只有存在有效 `/clock` 的 Gazebo 或数据回放才使用 `true`。

## 1.5 清理残留任务

不要同时手动启动管理器内部负责的 Cartographer、Nav2、RF2O 或 EKF。终端崩溃后可以先预览再清理：

```bash
ros2 run robot_bringup cleanup_robot_mission.py --dry-run
ros2 run robot_bringup cleanup_robot_mission.py
```

清理范围包括独立启动的 `motion_controller` 及隔离测试遗留的
`/motion_controller_test`。清理完成后脚本会停止 ROS 2 daemon；若立即执行
`ros2 node list`，DDS 可能需要数秒移除旧 graph 记录。

## 1.6 Quick Start 测试

基础节点启动后，快速检查节点、topic 发布者/订阅者数量、频率、数据延迟和积压：

```bash
source install/setup.bash

# 单硬件/基础数据链路测试，默认持续 15 秒
ros2 run robot_system_test robot_graph_test.py

# 所有导航节点启动稳定后的整体测试
ros2 run robot_system_test robot_graph_test.py \
  --profile integration \
  --duration 30
```

成功时返回码为 `0`；任一必需项目失败时返回码为 `1`。完整测试方法、真机安全要求和
配置说明见第 5 部分。

# 2. 建图与导航流程管理器

主程序：

```text
src/robot_bringup/tools/mapping_navigation_manager.py
```

## 2.1 主要功能

`mapping_navigation_manager` 是独立常驻进程，负责：

- 检查 ROS graph，拒绝与残留 Cartographer、Nav2、RF2O、EKF 或运动控制器重复运行。
- 启动并监督基础组、RF2O、EKF 和 `motion_controller`。
- 根据 `init` 和地图文件状态决定直接导航还是完整建图。
- 建图时启动 Cartographer、mapping 模式 Nav2、地图保存节点和 frontier explorer。
- 校验地图大小、更新时间、YAML 引用以及 source/install SHA-256。
- 建图完成后清理旧接口，按需重启 RF2O/EKF，再启动 Cartographer 定位和 Nav2。
- 监督 Nav2 lifecycle、TF、传感器新鲜度、子进程退出和运动控制器反馈。
- 发生严重错误时发布零速度、关闭本次启动的进程并进入 `SAFE_STOP`。

每个子流程使用独立 POSIX 进程组，停止建图或 Nav2 不会误杀管理器本身。

## 2.2 流程结构

```text
mapping_navigation_manager
  ├─ 基础组：Gazebo/真机基础节点、robot_state_publisher、ros2_control、雷达
  ├─ 里程计组：RF2O、EKF
  ├─ 运动组：motion_controller
  ├─ 建图组：Cartographer mapping、occupancy grid
  ├─ 建图导航组：car_nav2_map、param_map
  ├─ 探索组：地图保存节点、frontier explorer
  └─ 定位导航组：Cartographer localization、Nav2 navigation
```

完整建图流程：

```text
检查启动冲突
→ 等待 scan、odom、TF、底盘控制器、RF2O、EKF、motion_controller
→ 启动 Cartographer 建图
→ 启动 mapping Nav2
→ 等待 motion_controller 就绪
→ 自动探索和保存地图
→ 校验地图
→ 清理建图接口
→ 重启 RF2O/EKF
→ 启动 Cartographer 定位
→ 启动 Nav2
→ 等待 motion_controller 就绪
→ NAVIGATION
```

## 2.3 `init` 与地图判断

| 条件 | 行为 |
| --- | --- |
| `init=true` | 无条件执行完整建图流程 |
| `init=false` 且安装地图有效 | 跳过建图，直接定位导航 |
| `init=false` 且地图缺失或过小 | 写入警告并自动执行完整建图流程 |

现有地图用于直接导航时，安装目录中的 `map.pbstream` 必须存在且大于 10 KiB。源码地图缺失或源码、安装地图哈希不同会记录警告，但当前运行仍使用有效的安装地图。

## 2.4 状态、事件与日志

查看管理器状态：

```bash
ros2 topic echo /mapping_manager/state
ros2 topic echo /mapping_manager/event
ros2 topic echo /mapping_manager/attempt
```

主要状态：

| 状态 | 含义 |
| --- | --- |
| `BOOT` | 检查参数、地图和 ROS graph 冲突 |
| `WAIT_BASE` | 等待传感器、TF、里程计、底盘与运动控制器 |
| `WAIT_MAPPING_READY` | 等待 Cartographer 建图就绪 |
| `WAIT_MAPPING_NAVIGATION_READY` | 等待 mapping Nav2 和运动输出链路就绪 |
| `WAIT_SAVER` | 等待地图保存节点 |
| `WAIT_EXPLORER` | 等待自动探索节点 |
| `MAPPING` | 自动探索建图中 |
| `VALIDATING_MAP` | 校验新地图文件 |
| `WAIT_MAPPING_STOPPED` | 等待旧建图接口完全消失 |
| `WAIT_ODOMETRY_STOPPED` | 等待 RF2O/EKF 停止 |
| `WAIT_ODOMETRY_RESTART` | 等待 RF2O/EKF 重启稳定 |
| `WAIT_LOCALIZATION_READY` | 等待 Cartographer 定位、地图与 TF |
| `WAIT_NAVIGATION_READY` | 等待 Nav2 和 motion_controller 就绪且无冲突 |
| `NAVIGATION` | 定位、Nav2 和运动控制链路可用 |
| `SAFE_STOP` | 严重故障，已关闭任务进程并归零速度 |

主日志：

```text
/home/garry/car_real/logs/mapping_manager.log
```

各子流程日志位于：

```text
/home/garry/car_real/logs/
```

管理器会记录运动控制器状态变化，例如：

```text
motion_controller feedback: unknown -> idle; conflict=False
Localization, Nav2 and motion_controller are active;
motion_controller=idle, conflict=False
```

多个控制源同时发送非零命令时记录 WARNING。

## 2.5 地图输出

```text
src/robot_bringup/map/map.pbstream
install/robot_bringup/share/robot_bringup/map/map.pbstream

src/car_nav2/maps/exploration_map.yaml
src/car_nav2/maps/exploration_map.pgm
install/car_nav2/share/car_nav2/maps/exploration_map.yaml
install/car_nav2/share/car_nav2/maps/exploration_map.pgm
```

定位入口只加载 `map.pbstream`，不会使用旧的 `map_best.pbstream`。

## 2.6 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `init` | `false` | 强制完整建图；为 false 时优先使用已有地图 |
| `use_sim_time` | `false` | Gazebo/带 `/clock` 回放使用 true，真机使用 false |
| `use_rviz` | `true` | 是否启动 RViz |
| `use_rf2o_in_ekf` | `true` | EKF 是否融合 `/odom_rf2o` |
| `start_base` | `true` | 是否由管理器启动基础组 |
| `restart_odometry_on_transition` | `true` | 建图切换定位时是否重启 RF2O/EKF |
| `stable_duration_s` | `4.0` | 就绪条件需要连续稳定的时间 |
| `base_timeout_s` | `60.0` | 基础组启动超时 |
| `mapping_timeout_s` | `60.0` | 建图 Cartographer/Nav2 启动超时 |
| `localization_timeout_s` | `60.0` | Cartographer 定位启动超时 |
| `nav2_timeout_s` | `60.0` | Nav2 lifecycle 和运动链路启动超时 |
| `max_nav2_restarts` | `1` | Nav2 激活失败后的重试次数 |
| `mission_timeout_s` | `1800.0` | 单次建图任务总超时 |
| `cleanup_timeout_s` | `30.0` | 等待旧 ROS graph 接口消失的时间 |
| `max_rebuilds` | `0` | 地图质量不足时允许完整重建的次数 |

## 2.7 停止与故障排查

按一次 `Ctrl+C`，管理器会依次使用 SIGINT、SIGTERM，必要时使用 SIGKILL 清理自己启动的进程。

一直停在 `WAIT_BASE` 时检查：

```bash
ros2 topic hz /scan
ros2 topic hz /odom
ros2 run tf2_ros tf2_echo odom base_footprint
ros2 run tf2_ros tf2_echo base_footprint laser_link
ros2 control list_controllers
ros2 topic echo /motion_controller/status
```

一直停在 `WAIT_NAVIGATION_READY` 时检查：

```bash
ros2 lifecycle get /controller_server
ros2 lifecycle get /planner_server
ros2 lifecycle get /bt_navigator
ros2 topic echo /motion_controller/status
ros2 topic echo /motion_controller/control_conflict
```

更详细的分组启动说明见 [`src/robot_bringup/launch/README.md`](src/robot_bringup/launch/README.md)。

# 3. 运动控制器

代码和配置：

```text
src/motion_controller/src/motion_controller_node.cpp
src/motion_controller/config/motion_controller.yaml
src/motion_controller/README.md
```

## 3.1 功能

`motion_controller` 是底盘最终速度仲裁器。Nav2、墙面对齐和 AI 外部系统不能直接控制最终 `/cmd_vel`，而是分别向独立输入话题发布请求。非零速度表示请求运动，零速度或输入超时表示自动释放。

```text
Nav2        → /cmd_vel_nav_smoothed ─┐
墙面对齐    → /cmd_vel_wall_align ────┼→ motion_controller → /cmd_vel → 底盘
AI 外部系统 → /cmd_vel_external ──────┘
```

主要保护：

- 不维护长期控制权，不需要预先选择控制源。
- 空闲时自动接受最先到达的单路非零请求。
- 活动源归零或超时后自动释放，并保持一段切换停车时间。
- 当前输入超时后立即归零。
- 对最终线速度、角速度和加速度进行硬限制。
- AI 输入有独立的更低速度上限。
- NaN/Inf 输入按零处理。
- 已有活动源时拒绝其他非零请求，并输出 WARN、占用源和被拒绝源。
- 可配置冲突时只警告，或立即停止最终输出。

软件 STOP 不能代替硬件急停和电机驱动器通信看门狗。

## 3.2 主要话题

| 方向 | 话题 | 类型 | 说明 |
| --- | --- | --- | --- |
| 输入 | `/cmd_vel_nav_smoothed` | `geometry_msgs/msg/Twist` | Nav2 平滑速度 |
| 输入 | `/cmd_vel_wall_align` | `geometry_msgs/msg/Twist` | 墙面对齐速度 |
| 输入 | `/cmd_vel_external` | `geometry_msgs/msg/Twist` | AI 外部速度 |
| 输出 | `/cmd_vel` | `geometry_msgs/msg/Twist` | 唯一底盘最终速度 |
| 反馈 | `/motion_controller/status` | `std_msgs/msg/String` | 当前模式、活动源或超时状态 |
| 反馈 | `/motion_controller/control_conflict` | `std_msgs/msg/Bool` | 是否存在多个非零控制源 |
| 反馈 | `/motion_controller/warning` | `std_msgs/msg/String` | 占用源、被拒绝源等具体原因 |
| 输入 | `/motion_controller/nav_goal` | `geometry_msgs/msg/Pose2D` | AI/上层发送 XY+yaw；theta 为 rad |
| 输入 | `/motion_controller/nav_goal_with_options` | `motion_controller/msg/NavGoal` | XY+yaw及自动对墙选项 |
| 反馈 | `/motion_controller/nav_goal_status` | `std_msgs/msg/String` | 导航目标转发和执行结果 |
| 反馈 | `/motion_controller/wall_alignment_status` | `std_msgs/msg/String` | 墙面对齐执行状态 |

## 3.3 自动请求与停止服务

Nav2、墙面对齐和 AI 不需要调用选择服务。向各自输入话题发送非零 `Twist` 即请求运动，发送零速度或停止发布直到超时即释放请求。

`select_navigation`、`select_wall_alignment`、`select_external` 仅为旧程序兼容保留，调用后不会锁定或切换控制权。

安全停止所有输出：

```bash
ros2 service call /motion_controller/stop \
  std_srvs/srv/Trigger "{}"
```

## 3.4 单独启动与反馈

流程管理器会自动启动运动控制器。只有单独调试时才执行：

```bash
ros2 launch motion_controller motion_controller.launch.py
```

查看状态：

```bash
ros2 topic echo /motion_controller/status
ros2 topic echo /motion_controller/control_conflict
ros2 topic echo /motion_controller/warning
ros2 topic echo /cmd_vel
```

常见状态：

| 状态 | 含义 |
| --- | --- |
| `idle` | 无非零运动请求，最终输出为零 |
| `switching_zero_hold` | 模式切换停车保护中 |
| `accepted_*` | 空闲控制器接受了对应来源的新请求 |
| `active_navigation` | 正在转发 Nav2 速度 |
| `active_wall_alignment` | 正在转发墙面对齐速度 |
| `active_external` | 正在转发 AI 外部速度 |
| `released_*` | 活动源归零或超时，已自动释放 |
| `request_conflict_no_owner` | 空闲时多路请求同时到达，全部拒绝 |
| `stop_latched_waiting_all_inputs_zero` | STOP 后等待所有输入归零再恢复自动仲裁 |

冲突原因同时发布到 `/motion_controller/warning`。例如：

```text
cmd occupied by navigation; rejected: external
```

默认 `stop_on_conflict=false`，已有活动源继续运行，新请求被拒绝；设置为 `true` 后，冲突期间最终输出立即归零。

墙面对齐算法、参数与标定方法见 [`src/car_nav2/WALL_ALIGNMENT.md`](src/car_nav2/WALL_ALIGNMENT.md)。

## 3.5 XY+yaw 导航网关

AI 不需要直接调用 Nav2。它可以向 motion_controller 发送：

```bash
ros2 topic pub --once /motion_controller/nav_goal geometry_msgs/msg/Pose2D \
  "{x: 1.2, y: -0.5, theta: 1.5708}"
```

`x/y` 单位为 m，`theta` 是 `map` 坐标系 yaw，单位为 rad。motion_controller 负责校验、转换并调用 Nav2 `NavigateToPose`。

导航执行期间 `/motion_controller/nav_goal_status` 默认以 1 Hz 发布异步心跳，包含当前阶段、
运行时间和剩余距离。AI 的订阅回调只需保存最新状态，不要在回调中执行耗时推理或同步等待。

```bash
ros2 topic echo /motion_controller/nav_goal_status
ros2 service call /motion_controller/cancel_nav_goal std_srvs/srv/Trigger "{}"
```

导航成功后自动墙面对齐：

```bash
ros2 topic pub --once /motion_controller/nav_goal_with_options \
  motion_controller/msg/NavGoal \
  "{x: 1.2, y: -0.5, yaw: 1.5708, align_to_wall: true}"
```

`align_to_wall` 默认 false。组合任务只有在导航成功且墙面对齐完成后才反馈
`succeeded;wall_aligned=true`。

默认 NavigateToPose 行为树使用
`navigate_w_recovery_and_replanning_only_if_path_becomes_invalid.xml`。系统以 1 Hz 检查
当前剩余路径在 global costmap 中是否仍然有效，但不会周期性调用全局规划器。只有首次收到
目标、目标更新，或 `IsPathValid` 判定当前路径被障碍阻断时才重新执行
`ComputePathToPose`；路径仍可通行时 controller 会继续跟随原路径。

直接请求墙面对齐：

```bash
ros2 service call /motion_controller/align_wall std_srvs/srv/Trigger "{}"
ros2 service call /motion_controller/cancel_wall_alignment std_srvs/srv/Trigger "{}"
ros2 topic echo /motion_controller/wall_alignment_status
```

# 4. AI 控制模拟器

脚本：

```text
/home/garry/car_real/ai_control_sim.py
```

## 4.1 功能

`ai_control_sim.py` 用于模拟 AI 系统控制小车：

- 只向 `/cmd_vel_external` 发布请求，不直接发布最终 `/cmd_vel`。
- 非零命令自动请求运动，归零或命令到期自动释放。
- 默认以 10 Hz 持续发布当前 AI 命令。
- 每条运动命令都具有持续时间，到期自动归零。
- 对线速度和角速度进行本地限幅。
- 监听运动控制器状态和多控制源冲突反馈。
- 支持发送 XY+yaw 导航目标，由 motion_controller 内部转发 Nav2。
- `x`、`quit`、EOF 或 Ctrl+C 只归零 AI 自身请求，不影响 Nav2 或墙面对齐。

## 4.2 启动

先保证 `motion_controller` 已经运行，然后执行：

```bash
cd /home/garry/car_real
source install/setup.bash
./ai_control_sim.py
```

进入交互界面后可直接发送运动命令，非零速度自动请求控制，归零自动释放。

## 4.3 交互命令

| 命令 | 功能 |
| --- | --- |
| `move <vx> <wz> [秒]` | 指定线速度、角速度和持续时间 |
| `goal <x> <y> <yaw弧度> [对墙]` | 发送导航目标；对墙默认 false |
| `cancel_goal` | 通过 motion_controller 取消导航目标 |
| `align_wall` | 不经过导航，直接请求墙面对齐 |
| `cancel_align` | 取消墙面对齐 |
| `lidar_off` | 暂停 `/scan_gated`，屏蔽定位、RF2O 和 Nav2 的雷达输入；C1 与墙面对齐使用的原始 `/scan` 保持运行 |
| `lidar_on` | 恢复向定位、RF2O 和 Nav2 转发雷达数据 |
| `w [秒]` | 前进 |
| `s [秒]` | 后退 |
| `a [秒]` | 原地左转 |
| `d [秒]` | 原地右转 |
| `x` | AI 速度归零并自动释放请求 |
| `status` | 查看控制器状态、冲突和剩余运动时间 |
| `help` | 显示帮助 |
| `quit` | 归零 AI 请求并退出 |

示例：

```text
ai-control> w 2
ai-control> a 1
ai-control> move 0.10 0.15 3
ai-control> goal 1.2 -0.5 1.57 true
ai-control> align_wall
ai-control> x
ai-control> quit
```

## 4.4 ROS 接口

| 方向 | 接口 | 类型/用途 |
| --- | --- | --- |
| 发布 | `/cmd_vel_external` | `geometry_msgs/msg/Twist` |
| 发布 | `/motion_controller/nav_goal_with_options` | `motion_controller/msg/NavGoal` |
| 调用 | `/motion_controller/cancel_nav_goal` | 取消当前导航目标 |
| 调用 | `/motion_controller/align_wall` | 直接墙面对齐 |
| 调用 | `/motion_controller/cancel_wall_alignment` | 取消墙面对齐 |
| 调用 | `/motion_controller/set_lidar_enabled` | 开启或屏蔽雷达数据转发 |
| 订阅 | `/motion_controller/status` | 控制器状态 |
| 订阅 | `/motion_controller/control_conflict` | 多控制源冲突 |
| 订阅 | `/motion_controller/nav_goal_status` | 1 Hz导航状态 |
| 订阅 | `/motion_controller/wall_alignment_status` | 墙面对齐状态 |

## 4.5 参数覆盖

```bash
./ai_control_sim.py --ros-args \
  -p default_linear_velocity:=0.08 \
  -p default_angular_velocity:=0.15 \
  -p shortcut_duration:=1.5 \
  -p max_command_duration:=5.0
```

第一次连接真机时应架空驱动轮，确认前后方向、旋转方向、STOP、输入超时和硬件急停全部正常后再落地测试。

## 4.6 完整流程自动验收

终端 1 启动完整系统：

```bash
source install/setup.bash
ros2 run robot_bringup mapping_navigation_manager.py --ros-args \
  -p use_sim_time:=true \
  -p use_rviz:=true
```

等待定位和 Nav2 就绪后，在终端 2 运行端到端 AI 验收。测试器读取 `/map` 和
`map -> base_footprint`，只从与机器人连通且具有安全净空的自由栅格中按随机种子选点：

```bash
# 仿真，会实际驱动仿真机器人
source install/setup.bash
ros2 run robot_system_test motion_controller_live_auto_test.py --mode sim

# 真机，会实际驱动小车；必须明确确认并准备硬件急停
ros2 run robot_system_test motion_controller_live_auto_test.py \
  --mode real --confirm-real-motion
```

覆盖随机导航、导航取消、导航期间 AI 冲突、短时直行/旋转、STOP、雷达屏蔽/恢复、
运动期间拒绝屏蔽雷达、墙面对齐取消、直接墙面对齐以及导航后自动对墙。默认随机种子固定，
便于复现；可用 `--seed`、`--goal-count`、`--min-goal-distance`、
`--max-goal-distance` 和 `--clearance` 调整测试范围。测试失败返回非零退出码。

# 5. 自动测试与验收

测试代码集中在独立 package：

```text
src/robot_system_test/
├── config/robot_test.yaml
├── tools/robot_graph_test.py
├── tools/motion_controller_auto_test.py
└── tools/motion_controller_live_auto_test.py
```

测试 package 不参与正常 bringup，不会因为启动机器人系统而自动执行测试。

## 5.1 构建测试 package

```bash
cd /home/garry/car_real
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-select robot_system_test
source install/setup.bash
```

## 5.2 单硬件与基础数据链路测试

先启动底盘、雷达、IMU、RF2O/EKF 等基础节点，再运行：

```bash
ros2 run robot_system_test robot_graph_test.py --duration 20
```

默认使用 package 内的 `hardware` 配置，等价于：

```bash
ros2 run robot_system_test robot_graph_test.py \
  --config $(ros2 pkg prefix robot_system_test)/share/robot_system_test/config/robot_test.yaml \
  --profile hardware \
  --duration 20
```

该测试适用于真机和 Gazebo。驱动节点名称允许使用配置中的候选名称，实际数据链路仍检查：

- `/scan`、`/imu`、`/odom` 是否存在预期发布者和订阅者。
- 实际接收频率是否达到配置下限；默认允许 5% 定时调度误差。
- 带 `header.stamp` 消息的 P95 延迟是否超限。
- topic 延迟是否相对其他数据流持续增长，判断 DDS 或回调队列积压。

真机驱动使用 monotonic 时间戳，而测试节点使用 epoch 或仿真 `/clock` 时，会显示
`clock offset normalized`。此时延迟是归一化后的相对延迟和抖动，不代表跨时钟域绝对延迟；
积压检测会扣除各 topic 共有的时钟漂移。

## 5.3 所有节点启动后的整体测试

等待 Cartographer、Nav2、motion controller 和墙面对齐节点全部进入稳定状态后运行：

```bash
ros2 run robot_system_test robot_graph_test.py \
  --profile integration \
  --settle 5 \
  --duration 30
```

整体测试额外检查 Nav2 核心节点、`/cmd_vel` 唯一速度出口、`/cmd_vel_nav_smoothed`、
`/scan_gated`、`/map` 等接口。测试自身创建的订阅不会计入订阅者数量。

`/cmd_vel` 正常非零速度只能由 `motion_controller` 发布；`mapping_navigation_manager`
保留一个仅用于 SAFE_STOP 归零的应急发布者。Nav2 `behavior_server` 必须重映射到
`/cmd_vel_nav`，键盘遥控也不能直接绕过仲裁。需要键盘控制时使用：

```bash
ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args \
  -r cmd_vel:=/cmd_vel_external
```

## 5.4 Motion controller 隔离测试

先停止正常运行的 `/motion_controller`，然后执行：

```bash
# 不连接真实底盘 topic，不会驱动车辆
ros2 run robot_system_test motion_controller_auto_test.py --mode sim

# 验证真机方向参数，同样使用隔离 topic
ros2 run robot_system_test motion_controller_auto_test.py --mode real
```

测试会启动独立控制器和假的 Nav2/墙面对齐端点，覆盖速度转发、输入超时、多源冲突、
雷达门控、导航目标转换、墙面对齐及 STOP。检测到正常控制器仍在运行时会拒绝执行。

## 5.5 完整运动验收

完整运动验收会让仿真机器人或真实车辆运动。先启动完整系统并等待进入导航状态：

```bash
# Gazebo
ros2 run robot_system_test motion_controller_live_auto_test.py --mode sim

# 真机：清空测试区域、架空或确认行驶安全，并准备硬件急停
ros2 run robot_system_test motion_controller_live_auto_test.py \
  --mode real \
  --confirm-real-motion
```

真机模式缺少 `--confirm-real-motion` 时测试会拒绝启动。覆盖项目见 4.6 节。

## 5.6 修改默认检查配置

源码配置位于：

```text
src/robot_system_test/config/robot_test.yaml
```

`publishers` 和 `subscribers` 支持 `min`、`max`；每个 topic 可以配置 `min_hz`、
`max_delay_ms` 和 `max_backlog_growth_ms`。不同机器人型号的节点名或 topic 拓扑不同时，
应修改配置中的候选节点和数量范围，但不要放宽 `/cmd_vel` 最多一个发布者的安全约束。
