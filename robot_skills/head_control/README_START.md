# head_control

控制机器人头部/云台向上、向下、水平或指定角度。

V11 权威来源：`/home/test/new_project_optimized_v11_navsafe/skills/head_control`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/head_control
bash run.sh up
bash run.sh down
bash run.sh level
bash run.sh angle --angle 185
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
