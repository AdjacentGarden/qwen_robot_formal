# 真机自动运动请求仲裁器

`motion_controller` 统一监督 Nav2、墙面对齐和 AI 外部控制三路速度请求，并且是唯一向底盘最终 `/cmd_vel` 发布命令的节点。

```text
Nav2        → /cmd_vel_nav_smoothed ─┐
墙面对齐    → /cmd_vel_wall_align ────┼→ motion_controller → /cmd_vel → 底盘
AI 外部系统 → /cmd_vel_external ──────┘
```

## 自动仲裁规则

不需要预先申请或长期持有控制权：

1. 超过 `conflict_command_deadband` 的非零速度表示运动请求。
2. 零速度不会占用控制器。
3. 空闲时自动接受最先到达的一路非零请求。
4. 活动源持续更新非零命令时，只有该来源会被转发。
5. 活动源发送零速度或超过 `input_timeout` 未更新时自动释放。
6. 释放后保持 `switch_stop_duration` 的零速度，再允许下一路自动接管。
7. 已有活动源时，其他来源的非零请求会被拒绝，并反馈占用原因。
8. 空闲时多路非零请求同时到达，全部拒绝并保持停车，直到只剩单路请求。

墙面对齐是例外：任务运行期间会主动穿插零速度停车测量。控制器在墙面对齐任务结束、
取消或失败前保持该来源的控制权，不会把测量阶段的零速度误判为任务释放。

例如：

```text
空闲 + AI非零请求
→ accepted_external
→ active_external

AI活动 + Nav2非零请求
→ AI继续运行
→ WARN: cmd occupied by external; rejected: navigation

AI发布零速度
→ released_external
→ switching_zero_hold
→ idle

Nav2仍有非零请求
→ accepted_navigation
→ active_navigation
```

## ROS 接口

| 方向 | 接口 | 类型 | 说明 |
| --- | --- | --- | --- |
| 输入 | `/cmd_vel_nav_smoothed` | `geometry_msgs/msg/Twist` | Nav2 平滑速度请求 |
| 输入 | `/cmd_vel_wall_align` | `geometry_msgs/msg/Twist` | 墙面对齐速度请求 |
| 输入 | `/cmd_vel_external` | `geometry_msgs/msg/Twist` | AI 外部速度请求 |
| 输出 | `/cmd_vel` | `geometry_msgs/msg/Twist` | 唯一底盘最终速度 |
| 反馈 | `/motion_controller/status` | `std_msgs/msg/String` | 空闲、接受、活动、释放等状态 |
| 反馈 | `/motion_controller/control_conflict` | `std_msgs/msg/Bool` | 是否存在请求冲突 |
| 反馈 | `/motion_controller/warning` | `std_msgs/msg/String` | 冲突占用源和被拒绝源 |
| 输入 | `/motion_controller/nav_goal` | `geometry_msgs/msg/Pose2D` | 上层 XY+yaw 导航请求；theta 为 degree，范围 [-180, 180] |
| 输入 | `/motion_controller/nav_goal_with_options` | `motion_controller/msg/NavGoal` | XY+yaw及导航后自动对墙选项 |
| 反馈 | `/motion_controller/nav_goal_status` | `std_msgs/msg/String` | Nav2 接受、执行和最终结果 |
| 反馈 | `/motion_controller/wall_alignment_status` | `std_msgs/msg/String` | 直接/自动墙面对齐状态 |

查看反馈：

```bash
ros2 topic echo /motion_controller/status
ros2 topic echo /motion_controller/control_conflict
ros2 topic echo /motion_controller/warning
```

## 状态含义

| 状态 | 含义 |
| --- | --- |
| `idle` | 没有非零请求，最终速度为零 |
| `accepted_navigation` | 接受 Nav2 请求 |
| `accepted_wall_alignment` | 接受墙面对齐请求 |
| `accepted_external` | 接受 AI 请求 |
| `active_navigation` | 正在转发 Nav2 |
| `active_wall_alignment` | 正在转发墙面对齐 |
| `active_external` | 正在转发 AI |
| `released_*` | 对应活动源归零或超时，已释放 |
| `switching_zero_hold` | 来源切换停车保护中 |
| `request_conflict_no_owner` | 空闲时多路请求同时到达，全部拒绝 |
| `conflict_stopped_active_*` | 配置为冲突停车，当前输出已归零 |
| `stop_latched_waiting_all_inputs_zero` | STOP 后等待所有输入归零 |

## 冲突反馈

默认 `stop_on_conflict=false`：已有活动源继续运行，其他请求被拒绝。日志和 `/motion_controller/warning` 会说明原因：

```text
cmd occupied by navigation; rejected: external
cmd occupied by external; rejected: wall_alignment navigation
simultaneous requests while idle; rejected: navigation external
```

设置：

```yaml
stop_on_conflict: true
```

可让冲突期间最终输出立即归零。冲突消失后，原活动源若仍在发布非零命令会自动恢复。

## STOP 服务

普通控制切换不需要服务。只有安全停止时调用：

```bash
ros2 service call /motion_controller/stop std_srvs/srv/Trigger "{}"
```

STOP 会立即归零，并等待三路输入全部变为零或超时后自动恢复仲裁。旧的 `select_navigation`、`select_wall_alignment`、`select_external` 服务仅为兼容保留，调用后不会锁定控制源。

软件 STOP 不能代替硬件急停和驱动器通信看门狗。

## XY+yaw 导航网关

AI 不需要直接调用 Nav2 Action。向以下话题发送 `Pose2D`：

```text
/motion_controller/nav_goal
```

其中 `x`、`y` 单位为 m，`theta` 是 `map` 坐标系中的 yaw，单位为 degree，范围为 `[-180, 180]`。控制器统一转换为弧度后生成 `PoseStamped` 并调用 `/navigate_to_pose`。

`direction_reverse` 只作用于 AI 导航目标：仿真默认 `1.0`，真机启动时为 `-1.0`。
例如 AI 输入 `(x, y, yaw) = (0.1, 0.2, 0)`，真机转发给 Nav2 的目标为 `(-0.1, -0.2, pi)`。
`/cmd_vel_external`、Nav2 速度和墙面对齐速度均不受影响。

命令行示例：

```bash
ros2 topic pub --once /motion_controller/nav_goal geometry_msgs/msg/Pose2D \
  "{x: 1.2, y: -0.5, theta: 90.0}"
```

查看执行状态：

```bash
ros2 topic echo /motion_controller/nav_goal_status
```

目标送出后到最终成功、失败或取消之前，motion_controller 以
`nav_goal_feedback_period`（默认 1 秒）持续发布导航心跳，即使某一秒 Nav2 没有产生新的
feedback 也会继续反馈：

```text
navigating;elapsed_s=12.000000;distance_remaining_m=1.245000
```

该接口是异步 topic，不会等待 AI 处理反馈，也不会阻塞导航执行。

取消目标仍然只调用 motion_controller：

```bash
ros2 service call /motion_controller/cancel_nav_goal std_srvs/srv/Trigger "{}"
```

同一时间只允许一个待处理或活动导航目标。Nav2 不可用、目标含 NaN/Inf 或已有目标时会拒绝并发布原因。

### 导航成功后自动墙面对齐

使用自定义消息 `motion_controller/msg/NavGoal`：

```text
float64 x
float64 y
float64 yaw
bool align_to_wall
```

示例：导航到目标后自动对墙：

```bash
ros2 topic pub --once /motion_controller/nav_goal_with_options \
  motion_controller/msg/NavGoal \
  "{x: 1.2, y: -0.5, yaw: 90.0, align_to_wall: true}"
```

`align_to_wall` 默认是 `false`。旧的 `Pose2D` 接口继续保留，并始终等价于 false。

组合任务只有在 Nav2 成功且墙面对齐发布成功后才反馈：

```text
succeeded;wall_aligned=true
```

### 直接墙面对齐

```bash
ros2 service call /motion_controller/align_wall std_srvs/srv/Trigger "{}"
ros2 topic echo /motion_controller/wall_alignment_status
```

取消：

```bash
ros2 service call /motion_controller/cancel_wall_alignment std_srvs/srv/Trigger "{}"
```

AI 不需要直接调用 `/wall_alignment/enable`。

墙面对齐根据 `/scan` 在车体坐标系内闭环计算旋转方向，因此不使用
`direction_reverse`；无论它由 AI 直接请求还是导航成功后自动触发，均保持原方向。

## 雷达数据门控

`motion_controller` 将原始 `/scan` 转发为 `/scan_gated`。Cartographer、RF2O 和 Nav2
costmap 只订阅 `/scan_gated`；墙面对齐直接订阅原始 `/scan`。因此投影机构带动雷达
抬头前，可以先暂停定位链路，再执行一次墙面对齐：

```bash
ros2 service call /motion_controller/set_sensor_gate_enabled std_srvs/srv/SetBool "{data: false}"

ros2 service call /motion_controller/align_wall std_srvs/srv/Trigger "{}"
```

雷达 C1 本身继续运行，但倾斜期间的数据不会进入定位和避障链路。机构复位并稳定后恢复：

```bash
ros2 service call /motion_controller/set_sensor_gate_enabled std_srvs/srv/SetBool "{data: true}"
```

存在导航、墙面对齐或非零底盘输出时，关闭请求会被拒绝；恢复请求始终允许。

## 启动

流程管理器会自动启动本节点。单独调试时：

```bash
cd /home/garry/car_real
source install/setup.bash
ros2 launch motion_controller motion_controller.launch.py
```

参数文件：

```text
src/motion_controller/config/motion_controller.yaml
```

第一次真机测试应架空驱动轮，验证三路自动接管、归零释放、输入超时、冲突警告和硬件急停后再落地。

## 自动接口测试

先停止正常运行的 `motion_controller`，再运行隔离测试。测试输出被重定向到专用话题，
不会发送到底盘 `/cmd_vel`，真机和仿真都不会移动：

```bash
# 仿真方向参数（direction_reverse=1）
ros2 run robot_system_test motion_controller_auto_test.py --mode sim

# 真机方向参数（direction_reverse=-1）
ros2 run robot_system_test motion_controller_auto_test.py --mode real
```

脚本自动启动独立控制器和假的 Nav2/墙对齐端点，验证雷达门控、AI 速度、输入超时、
多源冲突、目标坐标翻转、无效目标拒绝、墙面对齐转发及 STOP。任一项目失败时进程返回
非零退出码，可直接用于 CI。检测到正常 `/motion_controller` 正在运行时会拒绝测试，避免
与真实任务接口冲突。
