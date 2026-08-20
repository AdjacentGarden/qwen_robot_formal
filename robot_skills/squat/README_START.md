# squat

抬头后使用 config.cameras.back.device 指向的后摄像头进行深蹲计数、查询或停止。机器人约30cm高，深蹲需要拍到用户身体，因此默认后摄并要求 head_control(up)。

V11 权威来源：`/home/test/single_function/squat`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/squat
bash run.sh run --camera /dev/video31 --duration 30
bash run.sh query
bash run.sh stop
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
