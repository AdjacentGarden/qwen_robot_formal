# back_camera_record

调用后摄像头录制一段视频。

V11 权威来源：`V11 built-in executor`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/back_camera_record
bash run.sh --dry-run
bash run.sh --duration 3
bash run.sh --duration 5 --output ./runtime/media/test.mp4
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
