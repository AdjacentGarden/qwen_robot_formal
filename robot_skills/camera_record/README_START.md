# camera_record

按 camera_name 选择前摄或后摄录制视频。

V11 权威来源：`V11 built-in executor`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/camera_record
bash run.sh --dry-run --camera front
bash run.sh --duration 3 --camera front
bash run.sh --duration 5 --output ./runtime/media/test.mp4 --camera front
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
