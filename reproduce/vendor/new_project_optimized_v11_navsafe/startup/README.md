# V8 开机启动

`robot-stack-v8.service` 由 systemd 在开机时调用。服务首先等待本次启动完成真实 NTP 同步，并确认墙钟连续稳定 3 秒；时间门通过后，监督器才严格按以下就绪顺序启动：

1. `real_robot_base.launch.py`
2. `real_robot_odometry.launch.py`
3. `real_robot_nav.launch.py use_rviz:=false`
4. V8 daemon
5. `/home/test/mpv_emotion.sh`

每一步必须同时满足“进程仍存活”和自己的就绪检查，才允许进入下一步。任何一步失败都会停止本次由监督器启动的进程，后续步骤不会启动。锁文件防止重复监督器。

时间同步门使用 `/run/systemd/timesync/synchronized` 和 `timedatectl` 双重确认，等待和超时全部基于单调时钟，因此不会被 NTP 对系统墙钟的校正干扰。如果 300 秒内没有完成同步，启动会失败且由 systemd 稍后重试；ROS、TF、激光和里程计不会在未同步时间上提前启动。

无硬件测试：

```bash
cd /home/test/new_project_optimized_v11_navsafe
python3 startup/test_startup_orchestration.py
python3 startup/test_time_sync_gate.py
```

日志位于 `/home/test/new_project_optimized_v11_navsafe/runtime/startup/`，时间门的最后状态记录在 `time_sync_gate.json`。
