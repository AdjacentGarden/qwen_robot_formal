# push_up

抬头后使用后摄像头进行俯卧撑计数。先用 /home/test/single_function 的人脸数据库确认 zhangsan，再以高频自适应 ReID 持续跟踪，并使用机器人侧面低机位专用的水平准备门控和双证据动作状态机计数。

V11 权威来源：`/home/test/new_project_optimized_v11_navsafe/integrations/push_up`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/push_up
bash run.sh run --name zhangsan --camera /dev/video31 --duration 30 --dry-run
bash run.sh run --name zhangsan --camera /dev/video31 --duration 30
bash run.sh query
bash run.sh stop
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
