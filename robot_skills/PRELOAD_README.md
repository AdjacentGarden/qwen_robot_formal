# V11 单功能 Skill 预加载实验

本实验只新增文件，不修改任何现有 Skill，也不修改 `Car_real_copy`。

预加载内容包括 Python 依赖、ROS 2 消息与 Action 类型、RetinaFace、FaceNet、YOLO、ReID、MediaPipe Pose，以及前后摄像头的短暂预热。摄像头和 RKNN Context 在预热后立即释放，程序不会发送运动、导航、头部、投影、语音或家电命令。

## 两终端测试

终端一：

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills
bash preload_all.sh start
```

看到 `"event": "preload_ready"` 后保持终端一运行。

终端二可以运行原来的单功能脚本，例如：

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_recognition
TIMEFORMAT='总耗时=%R秒'; time bash run.sh --camera /dev/video22
```

或者仅测试运动模型初始化，不控制底轮：

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/push_up
TIMEFORMAT='总耗时=%R秒'; time bash run.sh run --name zhangsan --camera /dev/video31 --duration 10 --dry-run
```

前台预加载器使用 `Ctrl+C` 停止。也可以使用后台方式：

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills
bash preload_all.sh daemon
bash preload_all.sh status
bash preload_all.sh stop
```

只预热一次然后退出：

```bash
bash preload_all.sh once
```

## 能加速和不能加速的部分

这个独立进程能够预热文件页、动态库、NPU 驱动、模型文件和摄像头设备，适合验证“热缓存”能够节省多少时间。

它不能让另一个 Python 进程直接复用已经创建的 RKNN Context、ROS Node、摄像头句柄、TTS WebSocket 或家电 HTTP Session。若实测仍有约 0.5～2 秒模型初始化，属于正常结果；真正消除这部分耗时需要把对应模型封装成长驻服务，并让 Skill 通过 IPC/RPC 调用。
