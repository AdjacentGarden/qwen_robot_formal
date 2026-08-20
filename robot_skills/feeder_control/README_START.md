# feeder_control

通过米家云控制宠物投食机出粮，未指定数量时默认投食10克，也可查询设备在线状态。

V11 权威来源：`/home/test/single_function/feeder_control`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/feeder_control
bash run.sh status
bash run.sh feed --grams 10 --dry-run
bash run.sh feed --grams 10
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
