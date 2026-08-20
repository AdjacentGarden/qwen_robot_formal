# fan_control

风扇控制占位 skill，当前底层标记为 disabled。

当前 V11 硬件配置明确禁用了该 skill，保留目录是为了完整列举注册表。

V11 权威来源：`/home/test/single_function/fan_control`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/fan_control
bash run.sh status  # 当前 V11 配置为 disabled，只会返回禁用状态
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
