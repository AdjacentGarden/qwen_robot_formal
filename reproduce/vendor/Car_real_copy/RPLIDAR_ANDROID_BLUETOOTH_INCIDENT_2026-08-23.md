# RPLIDAR 串口异常事故报告与 Android 蓝牙 HAL 修复记录

## 1. 结论

本次 RPLIDAR C1 运行约 20 分钟后异常的根本原因，不是电池、电源线、CPU 负载或 ModemManager，而是 Android 容器的蓝牙 HAL 与雷达错误地共用了同一个物理串口 `/dev/ttyS8`。

雷达驱动以 **460800 baud** 使用 `/dev/ttyS8`。Android 容器的 `/vendor/etc/bluetooth/bt_vendor.conf` 同时把蓝牙控制器端口配置为 `/dev/ttyS8`。当 Android 蓝牙 HAL `vendor.bluetooth-1-0` 崩溃并由 Android init 重新拉起时，供应商蓝牙库把该串口 termios 改成了 **115200 baud**。termios 是串口设备的共享状态，因此即使雷达进程 PID 没有变化，雷达正在使用的串口也立即变成错误波特率，随后产生大量 framing error 和伪造的高频 `/scan` 消息。

本次修复只修改 Android 容器的蓝牙 HAL 配置；**未修改雷达驱动、雷达 launch、雷达目标波特率或 `/dev/ttyS8` 的设备定义**。

## 2. 影响与现象

- manager 启动后雷达通常正常工作一段时间，约 20 分钟后突然异常。
- 正常时 `/scan` 约 10 Hz；故障后跳到几十至数百 Hz，数据不是有效雷达帧。
- 串口 framing error 持续高速增长，典型区间约占接收字符的 48%～49%。
- 故障时整机 CPU 可达到 98%～100%。这是错误数据洪泛及其下游处理造成的放大结果，不是改变波特率的起因。
- 使用外接电源而非电池时仍可复现，因此电池电压不是原因。

## 3. 关键证据

时间均为设备 UTC；北京时间需加 8 小时。

### 3.1 2026-08-23 09:18 的完整复现

- `09:18:26.892`：仍为正常状态，`/scan` 约 10.041 Hz，串口 framing error 没有新增。
- `09:18:28.808`：`/scan` 突然升至 38.755 Hz。
- `09:18:29.816`：`/scan` 升至 162.323 Hz。
- `09:18:30.460`：监控首次捕获新 framing error；直接读取正在使用的串口得到 `input_baud=115200`、`output_baud=115200`。
- 整个切换过程中 `rplidar_node` PID 始终为 `65270`，说明不是雷达进程用新参数重启，而是另一个进程修改了共享串口状态。
- `09:18:36.778`：一次采样新增 `rx=45554`、`fe=21961`，framing error 比例约 48.2%。
- `09:18:39.918`：CPU 98.2%，但波特率切换和扫描频率异常已经先发生。
- 同一故障窗口捕获到 Android `vendor.bluetooth-1-0` 异常退出/重新启动。第一次故障（约 `07:48:24`）也存在相同时间关联。

### 3.2 Android 配置和二进制证据

故障前 Android 容器配置为：

```ini
# UART device port where Bluetooth controller is attached
UartPort = /dev/ttyS8
```

Android 服务为：

```text
/vendor/bin/hw/android.hardware.bluetooth@1.0-service
```

供应商库 `/vendor/lib64/libbt-vendor.so` 中可检索到日志字符串：

```text
bt vendor lib: set UART baud 115200
```

这三项与故障时实测的 `/dev/ttyS8 = 115200` 完全吻合。

### 3.3 排除项

- **电源/电池**：故障在外接电源下复现；波特率被软件明确改为 115200，不符合欠压导致的随机通信错误模式。
- **CPU 负载**：切换前 CPU 已较高但雷达稳定 10 Hz、无新增 framing error；波特率改变后才出现伪扫描洪泛和 98% CPU。CPU 是后果/放大因素。
- **ModemManager**：已开启 debug 并持续记录；两个故障窗口都没有发现它探测或打开 `/dev/ttyS8` 的事件。
- **Linux 主机 bluetoothd**：主机没有发现 Bluetooth controller，`bluetoothctl list` 为空；与 Android 容器内的 HAL 是两个独立组件。
- **simple_bugreportd**：它在 Android 服务崩溃后收集错误报告，是崩溃的后续动作，不是串口波特率改变者。

## 4. 根因链路

```text
Android 蓝牙 HAL 异常退出
  -> Android init/HIDL 尝试重新拉起 vendor.bluetooth-1-0
  -> bt_vendor.conf 指向 /dev/ttyS8
  -> libbt-vendor.so 打开雷达串口并设置 115200
  -> /dev/ttyS8 的共享 termios 从 460800 变为 115200
  -> rplidar_node 按错误波特率解释数据
  -> UART framing error + 无效高频 /scan
  -> 下游 ROS 节点处理数据洪泛，CPU 升至 98%～100%
```

## 5. 实施的修改

修改时间：2026-08-23 09:35～09:36 UTC（北京时间 17:35～17:36）。

Android Docker 容器：`android_0`。

### 5.1 禁止蓝牙 HAL 使用雷达串口

文件：`/vendor/etc/bluetooth/bt_vendor.conf`

修改前：

```ini
UartPort = /dev/ttyS8
```

修改后：

```ini
# Disabled on this robot: /dev/ttyS8 is reserved for the RPLIDAR C1.
UartPort = /dev/ttyBT_DISABLED
```

使用不存在的设备名，而不是猜测另一个真实串口，避免把问题转移到电机、GNSS 或其他硬件串口。

### 5.2 禁止 Android init/HIDL 自动启动蓝牙 HAL

文件：`/vendor/etc/init/android.hardware.bluetooth@1.0-service.rc`

执行了两项修改：

1. 增加 `disabled`，使服务不随 `class hal` 自动启动。
2. 删除 `interface android.hardware.bluetooth@1.0::IBluetoothHci default`，防止客户端通过 HIDL 接口请求再次自动拉起服务。

修改后的完整服务定义：

```rc
# Disabled on this robot because /dev/ttyS8 is reserved for the RPLIDAR C1.
# The missing HIDL interface declaration also prevents interface-triggered starts.
service vendor.bluetooth-1-0 /vendor/bin/hw/android.hardware.bluetooth@1.0-service
    class hal
    disabled
    capabilities BLOCK_SUSPEND NET_ADMIN SYS_NICE
    user bluetooth
    group bluetooth
    task_profiles HighPerformance
```

### 5.3 未修改的内容

- `Car_real_copy/src/driver/rplidar_ros` 的所有雷达源码。
- `real_robot_base.launch.py` 和 `rplidar_c1_launch.py`。
- 雷达的 460800 baud 参数。
- Linux 主机的 `bluetoothd`。
- ModemManager 配置。

## 6. 备份、校验和回滚

原文件备份目录：

```text
/home/test/Car_real_copy/backups/android_bluetooth_hal_20260823_0922Z/
```

原文件 SHA-256：

```text
d46c3a637dcfc732c201fd23ccb4db8a83f5f5a7c0777c1a146c73b7f1ce995f  android.hardware.bluetooth@1.0-service.rc.original
c36265367d2311883b207c5bd9093670bccfd81f7b73ec68222d2549786472a5  bt_vendor.conf.original
```

修改后 SHA-256：

```text
2092abd4133ddda9e0e517a00b610737b1c89558454ca108fc34bd647a618265  android.hardware.bluetooth@1.0-service.rc
d03a6cfef23b036a86c9c26ecdc62b5dc0b06459be13946602c0d3a219dc7fba  bt_vendor.conf
```

如以后确认安装了真实蓝牙控制器，并为其分配了**独立且正确的 UART**，应修改为该 UART，而不是直接恢复 `/dev/ttyS8`。若必须完整回滚本次修改，可执行：

```bash
ANDROID_INIT_PID=$(sudo docker inspect -f '{{.State.Pid}}' android_0)
sudo cp /home/test/Car_real_copy/backups/android_bluetooth_hal_20260823_0922Z/bt_vendor.conf.original \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/bluetooth/bt_vendor.conf
sudo cp /home/test/Car_real_copy/backups/android_bluetooth_hal_20260823_0922Z/android.hardware.bluetooth@1.0-service.rc.original \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/init/android.hardware.bluetooth@1.0-service.rc
sudo chown root:root \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/bluetooth/bt_vendor.conf \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/init/android.hardware.bluetooth@1.0-service.rc
sudo chmod 0644 \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/bluetooth/bt_vendor.conf \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/init/android.hardware.bluetooth@1.0-service.rc
sudo chcon u:object_r:vendor_configs_file:s0 \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/bluetooth/bt_vendor.conf \
  /proc/$ANDROID_INIT_PID/root/vendor/etc/init/android.hardware.bluetooth@1.0-service.rc
sudo docker restart android_0
```

注意：完整回滚会重新制造 `/dev/ttyS8` 冲突，除非同时修正蓝牙使用的真实 UART，否则不建议回滚。

## 7. 修复后的验证

重启 `android_0` 后已完成以下检查：

- 容器以新 Android init PID 正常启动。
- 两个修改后的 vendor 文件在容器重启后仍保持，证明修改已进入容器持久可写层。
- 文件仍为 `root:root`、`0644`，SELinux 标签仍为 `u:object_r:vendor_configs_file:s0`。
- `android.hardware.bluetooth@1.0-service` 进程不存在。
- `getprop init.svc.vendor.bluetooth-1-0` 为空，服务未启动。
- `/dev/ttyS8` 没有 Android 蓝牙 HAL 打开者。
- 通过系统已有的 `/ai_control_sim/manager_restart` 服务以 `init=false` 重启 manager 后，新 `rplidar_node` PID 为 `21313`，`/dev/ttyS8` 的唯一打开者是该雷达进程。
- 对新雷达文件描述符执行只读 `TCGETS2` 检查，结果为 `input_baud=460800`、`output_baud=460800`。
- 重启后 `/scan` 恢复并稳定在约 9.9～10.0 Hz。一次 10 秒复核中 UART `rx` 从 `212403789` 增至 `212658709`（新增 `254920`），而 framing error 保持为历史累计值 `14374832`（增量 `0`）。
- 故障数据洪泛停止后系统 load 从约 38 持续下降，恢复时约为 10，进一步证明高负载是故障后果。

ROS manager 重启后的最终稳定性验证应至少持续超过以往约 20 分钟的典型复现周期。建议持续运行现有监控，验收条件为：

- `/dev/ttyS8` 始终为 460800。
- framing error 增量保持为 0。
- `/scan` 稳定在约 10 Hz。
- Android 蓝牙 HAL 始终没有进程，且不打开 `/dev/ttyS8`。
- 运行超过 30～60 分钟无再次异常。

## 8. 监控与日志位置

监控程序：

```text
/home/test/Car_real_copy/src/robot_system_test/tools/rplidar_runtime_monitor.py
```

本次关键日志：

```text
/home/test/Car_real_copy/logs/rplidar_runtime_monitor/rplidar_runtime_20260823_085448_+0000.jsonl
```

该监控记录 UART 计数、端口打开/关闭事件、串口拥有者、ROS `/scan` 频率、CPU/负载/温度、manager/rplidar PID、ModemManager 日志，并只在检测到串口错误后读取波特率。

## 9. 功能影响

- Android 容器内的 Bluetooth HAL 被禁用，Android 蓝牙功能不可用。
- 机器人当前未检测到蓝牙控制器，`Car_real_copy` 也没有实际依赖 Android 蓝牙，因此此影响符合当前硬件配置。
- ROS 中出现的 `bt_navigator` 是 Nav2 的 Behavior Tree（行为树）导航器，不是 Bluetooth，不受本次修改影响。
