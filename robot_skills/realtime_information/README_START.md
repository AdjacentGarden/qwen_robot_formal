# realtime_information

通过实时数据源查询当前时间、天气、机器人当前粗略位置、附近地点和交通状态。回答必须来自本次查询结果，不能凭模型常识猜测实时信息。

V11 权威来源：`/home/test/single_function/realtime_information`

## 启动

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/realtime_information
bash run.sh --action current_time
bash run.sh --action weather --location 北京市顺义区
bash run.sh --action location
bash run.sh --action nearby --query 美食 --location 北京市顺义区
bash run.sh --action traffic --location 北京市顺义区
```

执行真实硬件动作前，请确认对应摄像头、ROS/Nav2 或家电服务已准备好。
