# navigation_goto

发送导航目标点或坐标，让机器人导航到指定位置。

V11 权威来源：`/home/test/new_project_optimized_v11_navsafe/integrations/navigation_goto`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/navigation_goto
bash run.sh list
bash run.sh --point origin --dry-run
bash run.sh --point origin
bash run.sh --x 0.1 --y 0.1 --yaw 0 --dry-run
bash run.sh --x 0.1 --y 0.1 --yaw 0
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
