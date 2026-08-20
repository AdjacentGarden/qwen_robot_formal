# reminder_schedule

创建绝对时间或相对时间提醒；不支持无法转换成具体时间的外部事件条件。

V11 权威来源：`/home/test/single_function/reminder_schedule`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/reminder_schedule
bash run.sh schedule --content 喝水 --trigger-condition 5分钟后 --dry-run
bash run.sh schedule --content 喝水 --trigger-condition 5分钟后
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
