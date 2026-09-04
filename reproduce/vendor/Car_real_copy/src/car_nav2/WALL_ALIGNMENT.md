# 墙面对齐节点

`wall_alignment_node` 使用机器人停止后的最新激光扫描直接计算墙面方向，并控制小车正对墙面。节点不使用 Cartographer、RF2O、`map` 或 `odom` 提供的 yaw，因此最终对齐不受全局定位角度延迟影响。

节点订阅 C1 的原始 `/scan`，而 Cartographer、RF2O 和 Nav2 使用门控后的
`/scan_gated`。因此可以先关闭 `/scan_gated`，冻结定位链路，再进行投影前的二次
墙面对齐；关闭门控不会切断墙面对齐所需的扫描。

节点采用连续闭环控制：

```text
首次收集稳定激光并使用 RANSAC 拟合墙面
→ 持续旋转，每约 0.3 秒更新墙面误差
→ abs(error) ≥ 0.3 rad 时使用 0.6 rad/s，否则按 abs(error / goal_error)
  动态调整角速度（0.15～0.6 rad/s）
→ 误差进入 target_error（默认 1°）立即发布零速度并成功
```

任务不再采用“动—停—测量”脉冲模式。墙面拟合的多帧中位数和离散度检查用于抑制
连续运动时的激光畸变；不可靠批次不会更新控制误差。

## 安全要求

节点默认向 `/cmd_vel_wall_align` 发布速度。必须通过速度仲裁器将该话题切换到底盘，并且只能在 Nav2 导航任务结束、Nav2 已经交出底盘控制权后，允许墙面对齐节点控制小车。

不要直接把 `/cmd_vel_wall_align` 重映射到仍由 Nav2 velocity smoother 控制的底盘速度话题，否则两路速度命令会互相覆盖，可能导致旋转抖动、无法停车或运动方向异常。

推荐控制关系：

```text
/cmd_vel_nav ───────────┐
                       ├─ 速度仲裁器 → 底盘最终速度话题
/cmd_vel_wall_align ────┘
```

建议的任务顺序：

```text
Nav2 NavigateToPose 完成
→ 确认 Nav2 速度归零
→ 可选：关闭 /scan_gated，暂停定位、RF2O 和 Nav2 的雷达输入
→ 将速度控制权切换给墙面对齐节点
→ 启动墙面对齐
→ 等待 aligned=true 或失败状态
→ 收回墙面对齐节点的速度控制权
→ 开始投影
```

## 编译

```bash
cd /home/garry/car_real
colcon build --symlink-install --packages-select car_nav2
source install/setup.bash
```

## 启动节点

正常系统由 `mapping_navigation_manager`/`real_robot_nav.launch.py` 启动本节点，使用
`ai_control_sim.py` 的 `align_wall` 经 `motion_controller` 请求即可。不要在该节点已经存在时
再次运行下面的独立 launch，否则会出现重复的 `/wall_alignment` 节点和同名服务。

下面的独立启动方式仅用于没有运行完整流程时调试：

```bash
ros2 launch car_nav2 wall_alignment.launch.py
```

默认配置文件为：

```text
src/car_nav2/param/wall_alignment.yaml
```

也可以指定其他参数文件：

```bash
ros2 launch car_nav2 wall_alignment.launch.py \
  params_file:=/绝对路径/wall_alignment.yaml
```

## 启动一次墙面对齐任务

节点默认不会在启动后立即旋转。确认 Nav2 已经结束，并且速度控制权已经切换完成后，调用：

```bash
ros2 service call /wall_alignment/enable \
  std_srvs/srv/SetBool "{data: true}"
```

服务只表示“任务已接受并开始执行”，不表示墙面对齐已经成功。最终结果需要查看 `/wall_alignment/aligned` 和 `/wall_alignment/status`。

注意：独立启动的节点只发布 `/cmd_vel_wall_align`。若没有同时运行 `motion_controller`
或其他明确配置的速度仲裁器，该命令不会到达底盘 `/cmd_vel`，小车不会运动。

## 取消任务

使用以下命令取消正在执行的任务：

```bash
ros2 service call /wall_alignment/enable \
  std_srvs/srv/SetBool "{data: false}"
```

取消后，节点会在 `zero_publish_time` 指定的时间内持续发布零速度，然后停止发布速度命令。

## 状态与结果话题

查看当前状态：

```bash
ros2 topic echo /wall_alignment/status
```

查看当前墙面角度误差，单位为 rad：

```bash
ros2 topic echo /wall_alignment/yaw_error
```

每批稳定测量完成后，状态话题也会直接包含弧度和角度误差，例如：

```text
measured_wall;yaw_error_rad=0.174533;yaw_error_deg=10.000000;observed_correction_rad=0.052360
```

若达到累计修正限制，失败状态会同时给出最后的墙面误差和雷达观测累计修正量：

```text
failed_correction_limit;yaw_error_rad=...;yaw_error_deg=...;observed_correction_rad=...
```

查看是否成功对齐：

```bash
ros2 topic echo /wall_alignment/aligned
```

节点检测到误差进入 `target_error` 后立即发布零速度并发布：

```text
aligned: true
```

默认要求为：

- `goal_error: 0.0872665`：最大误差约 5°。
- `target_error: 0.0174533`：优先修正到约 1°。
- `task_timeout: 4.0`：任务最长修正时间；到期时已在 5° 可接受范围内仍按成功结束。
- `max_total_correction: 0.6`：雷达实际观测到的累计修正不得超过约 34.4°。

累计修正量由相邻两批稳定墙面误差的变化量计算，而不是对下发速度进行积分。所有正向和
反向修正都会相加；真机底盘执行不足不会提前消耗额度，没有有效修正则由任务超时保护。

## 雷达水平安装角标定

`lidar_yaw_offset` 表示雷达 +X 轴相对于车体 `base` +X 轴的水平安装偏角，单位为 rad，逆时针为正，顺时针为负。

例如，雷达相对车体中心线逆时针偏转 1.5°：

```text
lidar_yaw_offset = 1.5 × π / 180 ≈ 0.02618
```

推荐标定步骤：

1. 人工将车体中心线尽可能严格地正对一面平整墙壁。
2. 保持小车完全静止，确保墙前没有人员移动。
3. 启动墙面对齐节点，但先不要把速度输出接到底盘。
4. 启动一次任务并观察 `/wall_alignment/yaw_error`。
5. 调整 `lidar_yaw_offset`，直到多帧静止测量的误差以 0 为中心。
6. 重复多次，并分别在距离墙面较近和较远的位置验证。

完成安装角标定后，再逐步把 `goal_error` 从 5° 降低到投影所需要的精度，例如 1°～2°。如果底盘最小旋转速度过大、存在机械回差或墙面不平，误差阈值设置过小可能导致任务反复修正后达到旋转上限。

## 人物和杂物过滤

节点先使用 RANSAC 从前方激光点中寻找主要直线，再使用 PCA/总最小二乘法精修墙面角度。候选墙面必须同时满足以下条件：

- 墙面距离处于 `min_wall_distance` 与 `max_wall_distance` 之间。
- 墙面宽度不小于 `min_wall_span`。
- 内点数量不小于 `min_inliers`。
- 内点比例不小于 `min_inlier_ratio`。
- 拟合残差不大于 `max_fit_rms`。
- 墙面法线角度不超过 `max_wall_normal_error`。

`min_wall_span` 是过滤人物和窄小杂物最重要的参数。默认要求拟合出的直线至少覆盖 0.8 m：

- 如果目标墙始终足够宽，但人物容易被误判，可以增大 `min_wall_span`。
- 如果墙面经常被遮挡而无法识别，可以小幅降低该值。
- 不建议为了提高识别率大幅降低，否则人物、柜子或箱子平面可能被误认为目标墙。

当人物持续遮挡墙面、扫描数据不稳定或者没有可信墙面时，小车保持停止。任务超过
`task_timeout` 后，若最后误差在 `goal_error` 内则成功，否则失败退出。

## 推荐调试顺序

1. 暂时不要将 `/cmd_vel_wall_align` 接到底盘，只观察 `yaw_error` 和 `status`。
2. 标定 `lidar_yaw_offset`。
3. 在无人、完整平墙环境验证墙面识别。
4. 加入静止人物、箱子等遮挡物，验证是否仍能识别真实墙面。
5. 接入速度仲裁器，以较大的 `goal_error` 进行低速旋转测试。
6. 根据底盘死区调整 `min_angular_speed`。
7. 确认不会过冲后，再逐步降低 `goal_error`。

完整参数说明及调大、调小的影响见：

```text
src/car_nav2/param/wall_alignment.yaml
```
