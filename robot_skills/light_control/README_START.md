# light_control

通过米家云控制普通照明。语义明确要求打开客厅灯时使用 action=on、room=living_room，场景工作流会让开灯与前往客厅A点并行；关闭和查询不触发这段导航。

V11 权威来源：`/home/test/single_function/light_control`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/light_control
bash run.sh status
bash run.sh on --dry-run
bash run.sh on
bash run.sh off
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
