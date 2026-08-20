# camera_capture

按 camera_name 选择前摄或后摄拍摄一张图片。

V11 权威来源：`V11 built-in executor`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/camera_capture
bash run.sh --dry-run --camera front
bash run.sh --camera front
bash run.sh --output ./runtime/media/test.jpg --camera front
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
