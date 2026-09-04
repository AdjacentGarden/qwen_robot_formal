# robot_system_test

真机硬件反复启停与 ROS 数据稳定性测试。每轮启动完整的
`robot_bringup/real_robot_base.launch.py`，等待全部硬件话题，采样后检查频率、断流、
无效数值和时间戳，再关闭整个进程组并冷却后进入下一轮。

## 构建

```bash
cd /home/garry/car_real
colcon build --packages-select robot_system_test
source install/setup.bash
```

## 真机运行

先停止其他占用 `/dev/ttyS0`、`/dev/ttyS8`、`/dev/ttyS9` 或 I2C-4 的程序，架空驱动轮，
然后运行：

```bash
ros2 run robot_system_test hardware_stability_test --iterations 20 \
  --output-dir /home/garry/car_real/logs/hardware_stability
```

传递真机启动参数时可重复使用 `--launch-arg`：

```bash
ros2 run robot_system_test hardware_stability_test --iterations 20 \
  --launch-arg lidar_serial_port:=/dev/ttyS8 \
  --launch-arg chassis_serial_port:=/dev/ttyS0
```

默认每轮最多等待 20 秒、稳定采样 30 秒、冷却 3 秒。阈值均在
`config/hardware_stability.yaml` 中。每次运行生成 `report.json`、`summary.csv` 和每轮完整
launch 日志；任一轮失败时程序退出码为 1。测试期间持续向 `/cmd_vel` 发布零速度，
但首次上真机仍应架空车轮并准备急停。

## 雷达硬件/软件隔离测试

完整真机底层已经启动时，保持雷达静止并连续测试至少 3 分钟：

```bash
ros2 run robot_system_test lidar_diagnostic_test.py --duration 180 \
  --output /home/test/Car_real_copy/logs/lidar_diagnostic.json
```

工具同时比较原始 `/scan` 和 `/scan_gated`，检查频率、断流、点数、scan_time、
消息几何、时间戳顺序、RPLidar 质量事件及 sensor gate 状态。结论为：

- `HARDWARE_OR_DRIVER_ACQUISITION_FAULT`：问题已发生在原始 scan/驱动层。
- `SOFTWARE_SENSOR_GATE_FAULT`：原始 scan 正常、门控输出异常。
- `LIDAR_PIPELINE_HEALTHY`：雷达和 scan gate 在测试窗口内正常。

测试只订阅数据，不启停硬件、不控制机器人运动。

如果完整链路报告采集层故障，停止完整底层，仅启动 RPLidar 驱动后绕过 gate 复测：

```bash
ros2 launch rplidar_ros rplidar_c1_launch.py
ros2 run robot_system_test lidar_diagnostic_test.py --raw-only --duration 600 \
  --output /home/test/Car_real_copy/logs/lidar_raw_only.json
```

`--raw-only` 仍然异常，故障范围已缩小到雷达本体、供电、串口链路或 RPLidar 驱动；
`--raw-only` 正常而完整链路异常，则检查 sensor gate、QoS 和 motion controller。

### 不经过 `/scan` 的 C1 原始串口测试

该测试会独占 C1 串口，不能与 `rplidar_node` 同时运行。先停止 manager、base launch 和
所有占用 `/dev/ttyS8` 的进程，然后执行：

```bash
ros2 run robot_system_test rplidar_c1_serial_diagnostic.py \
  --port /dev/ttyS8 --baudrate 460800 --duration 600 \
  --output-dir /home/test/Car_real_copy/logs
```

也可以不构建，直接运行源码：

```bash
python3 src_from_real_robot/robot_system_test/tools/rplidar_c1_serial_diagnostic.py \
  --port /dev/ttyS8 --duration 600 --output-dir logs
```

每次运行创建独立目录，包含：

- `serial_rx.bin`：从串口收到的所有原始字节，可供事后重新解析。
- `events.jsonl`：设备信息、健康状态、每圈点数/周期/角度覆盖、串口空窗和重新同步事件。
- `summary.json`：最终分类及全部统计量。
- `nodes.csv`：仅指定 `--log-nodes` 时生成，每个原始测量点一行，文件会很大。

结果 `RAW_SERIAL_OR_LIDAR_DATA_FAULT` 表示异常在 ROS `/scan` 形成以前已经存在；
`RAW_SERIAL_STREAM_HEALTHY` 表示本次原始串口和独立解析正常，此时若 ROS 驱动同时间仍报错，
重点检查 SDK 扫描模式、SDK 解析和驱动重启逻辑。标准 5 字节测量协议没有包序号和完整包校验，
因此工具会同时保留原始二进制，但无法仅凭一次正常测试排除所有极偶发单字节替换。

## 原有验收工具

本包原有工具继续保留。硬件或全系统已启动时，可运行图拓扑、发布订阅数量、频率、
延迟及积压趋势检查：

```bash
ros2 run robot_system_test robot_graph_test.py --profile hardware --duration 20
ros2 run robot_system_test robot_graph_test.py --profile integration --settle 5 --duration 30
```

运动控制器测试：

```bash
ros2 run robot_system_test motion_controller_auto_test.py --mode sim
ros2 run robot_system_test motion_controller_live_auto_test.py --mode sim
```

真机完整运动测试会让机器人运动，只有清场并准备急停后才运行：

```bash
ros2 run robot_system_test motion_controller_live_auto_test.py \
  --mode real --confirm-real-motion
```
