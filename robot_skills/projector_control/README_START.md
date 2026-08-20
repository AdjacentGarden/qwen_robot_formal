# projector_control

控制投影仪开关、运动视频和会议图片循环。会议开始时先打开光源再执行启动脚本；meeting_pause 停止循环并保留当前画面，meeting_resume 恢复循环。

V11 权威来源：`/home/test/new_project_optimized_v11_navsafe/integrations/projector_control`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/projector_control
bash run.sh status
bash run.sh meeting_presentation_on --dry-run
bash run.sh meeting_presentation_on
bash run.sh meeting_pause
bash run.sh meeting_resume
bash run.sh fitness_video_on
bash run.sh off
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
