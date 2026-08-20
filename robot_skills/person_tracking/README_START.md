# person_tracking

使用前摄像头先通过注册人脸确认 zhangsan（张三），再用持续 ReID 锁定并跟随同一个人。身份未确认或 ReID 歧义时禁止移动；停止时使用 stop。

V11 权威来源：`/home/test/new_project_optimized_v11_navsafe/integrations/person_tracking`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/person_tracking
bash run.sh track --name zhangsan --duration 10 --dry-run
bash run.sh find --name zhangsan --duration 10
bash run.sh track --name zhangsan --duration 30 --execute
bash run.sh stop
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
