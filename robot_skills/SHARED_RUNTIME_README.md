# V11 跨进程共享运行时原型

这个原型通过 Unix Socket 复用常驻进程中的 RetinaFace、FaceNet、YOLO、ReID、MediaPipe Pose、ROS Node、`/cmd_vel` Publisher 和 NavigateToPose ActionClient。它不修改现有 Skill。底盘接口强制限制为线速度不超过 0.25 m/s、角速度不超过 0.60 rad/s、单次不超过 5 秒，并在结束或异常时重复发送停车命令。

启动：

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills
bash shared_runtime.sh daemon
bash shared_runtime.sh status
```

默认使用 `system_default`，与直接执行 `ros2 launch robot_bringup ...` 的终端一致。如果底盘、里程计和Nav2整套都显式使用CycloneDDS，则启动共享服务时也必须统一：

```bash
V8_DDS_TRANSPORT=cyclone bash shared_runtime.sh daemon
```

测试复用推理：

```bash
python3 shared_runtime_client.py benchmark --operation all --runs 50
python3 shared_runtime_client.py camera-test --device /dev/video22
python3 shared_runtime_client.py ros-ready
```

底盘小范围测试（必须先启动 `real_robot_base.launch.py`）：

```bash
python3 shared_runtime_client.py chassis forward --speed 0.08 --duration 0.5
python3 shared_runtime_client.py chassis backward --speed 0.08 --duration 0.5
python3 shared_runtime_client.py chassis left --angular-speed 0.20 --duration 0.5
python3 shared_runtime_client.py chassis right --angular-speed 0.20 --duration 0.5
python3 shared_runtime_client.py chassis stop
```

导航测试（必须依次启动底盘、里程计和 Nav2）：

```bash
python3 shared_runtime_client.py ros-ready
python3 shared_runtime_client.py nav origin --timeout 120
python3 shared_runtime_client.py nav living_room_entry_a --timeout 120
```

另开终端取消正在执行的导航：

```bash
python3 shared_runtime_client.py nav-cancel
```

停止：

```bash
bash shared_runtime.sh stop
```

原来的 `run.sh` 不会自动获得加速。要正式接入，需要为对应 Skill 增加客户端适配层，把原来直接调用模型的代码替换为向本服务提交帧并读取结果。
