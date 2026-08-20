# pet_tracking

按完整语义区分宠物任务：只寻找豆豆或家里的狗使用 find_at_home；寻找并明确要求喂食使用 find_and_feed_at_home；明确要求持续跟随才使用 track。两个寻找动作都会先去书房C点再原地旋转，找到即停且不跟随。

注意：`find_at_home` 与 `find_and_feed_at_home` 是 V11 主程序的场景编排动作；单独运行底层 pet_tracking 时使用这里列出的 `find/track/stop`。

V11 权威来源：`/home/test/single_function/pet_tracking`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/pet_tracking
bash run.sh find --source /dev/video22 --pet dog --search-timeout 10 --backend dummy
bash run.sh track --source /dev/video22 --pet dog --duration 0 --backend ros2
bash run.sh stop
bash run.sh track --pet dog --dry-run
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
