# environment_perception

调用前摄/后摄图像和视觉模型进行环境、投影或健身空间感知。前摄使用 config.cameras.front.device 判断投影、会议、娱乐环境；后摄使用 config.cameras.back.device 判断运动空间和用户身体是否可入镜。

V11 权威来源：`/home/test/single_function/environment_perception`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/environment_perception
bash run.sh --purpose general --camera front --dry-run
bash run.sh --purpose general --camera front
bash run.sh --purpose projection --camera front
bash run.sh --purpose fitness --camera back --exercise-type push_up
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
