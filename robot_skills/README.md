# V11 全部单功能 Skill 集合

此目录由 `/home/test/new_project_optimized_v11_navsafe` 的实际 skill 注册表生成，共 **29** 个 skill。创建过程没有修改 V11、原 `/home/test/single_function` 或 `/home/test/Car_real_copy`。

## 通用启动方式

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills
bash run_skill.sh <skill名称> [参数...]
```

也可以进入任一 skill 目录后执行 `bash run.sh ...`。运行 `bash list_skills.sh` 查看清单；运行 `bash check_all.sh` 做不会触发硬件的静态检查。

> 导航、底盘与头部接口只加载 `/home/test/Car_real_copy/install/setup.bash`，不会修改 Car_real_copy 源码。带 `--dry-run` 的示例不会驱动硬件；其他示例可能打开摄像头、控制家电、头部、投影或底盘。

## 全部 Skill 与启动示例

### 1. `back_camera_capture`

调用后摄像头拍摄一张图片。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/back_camera_capture
bash run.sh --dry-run
bash run.sh
bash run.sh --output ./runtime/media/test.jpg
```

### 2. `back_camera_record`

调用后摄像头录制一段视频。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/back_camera_record
bash run.sh --dry-run
bash run.sh --duration 3
bash run.sh --duration 5 --output ./runtime/media/test.mp4
```

### 3. `camera_capture`

按 camera_name 选择前摄或后摄拍摄一张图片。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/camera_capture
bash run.sh --dry-run --camera front
bash run.sh --camera front
bash run.sh --output ./runtime/media/test.jpg --camera front
```

### 4. `camera_record`

按 camera_name 选择前摄或后摄录制视频。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/camera_record
bash run.sh --dry-run --camera front
bash run.sh --duration 3 --camera front
bash run.sh --duration 5 --output ./runtime/media/test.mp4 --camera front
```

### 5. `environment_perception`

调用前摄/后摄图像和视觉模型进行环境、投影或健身空间感知。前摄使用 config.cameras.front.device 判断投影、会议、娱乐环境；后摄使用 config.cameras.back.device 判断运动空间和用户身体是否可入镜。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/environment_perception
bash run.sh --purpose general --camera front --dry-run
bash run.sh --purpose general --camera front
bash run.sh --purpose projection --camera front
bash run.sh --purpose fitness --camera back --exercise-type push_up
```

### 6. `face_recognition`

识别当前摄像头画面中的已注册人脸。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_recognition
bash run.sh --camera /dev/video22
```

### 7. `face_registration`

为指定姓名注册人脸。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_registration
bash run.sh --name zhangsan --camera /dev/video22
```

### 8. `fan_control`

风扇控制占位 skill，当前底层标记为 disabled。

当前 V11 硬件配置明确禁用了该 skill，保留目录是为了完整列举注册表。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/fan_control
bash run.sh status  # 当前 V11 配置为 disabled，只会返回禁用状态
```

### 9. `feeder_control`

通过米家云控制宠物投食机出粮，未指定数量时默认投食10克，也可查询设备在线状态。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/feeder_control
bash run.sh status
bash run.sh feed --grams 10 --dry-run
bash run.sh feed --grams 10
```

### 10. `front_camera_capture`

调用前摄像头拍摄一张图片。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/front_camera_capture
bash run.sh --dry-run
bash run.sh
bash run.sh --output ./runtime/media/test.jpg
```

### 11. `front_camera_record`

调用前摄像头录制一段视频。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/front_camera_record
bash run.sh --dry-run
bash run.sh --duration 3
bash run.sh --duration 5 --output ./runtime/media/test.mp4
```

### 12. `head_control`

控制机器人头部/云台向上、向下、水平或指定角度。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/head_control
bash run.sh up
bash run.sh down
bash run.sh level
bash run.sh angle --angle 185
```

### 13. `light_control`

通过米家云控制普通照明。语义明确要求打开客厅灯时使用 action=on、room=living_room，场景工作流会让开灯与前往客厅A点并行；关闭和查询不触发这段导航。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/light_control
bash run.sh status
bash run.sh on --dry-run
bash run.sh on
bash run.sh off
```

### 14. `move_backward`

控制机器人短暂后退。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/move_backward
bash run.sh --duration 1 --dry-run
bash run.sh --speed 0.20 --duration 1
```

### 15. `move_forward`

控制机器人短暂前进。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/move_forward
bash run.sh --duration 1 --dry-run
bash run.sh --speed 0.20 --duration 1
```

### 16. `move_left`

控制机器人原地左转。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/move_left
bash run.sh --duration 1 --dry-run
bash run.sh --angular-speed 0.40 --duration 1
```

### 17. `move_right`

控制机器人原地右转。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/move_right
bash run.sh --duration 1 --dry-run
bash run.sh --angular-speed 0.40 --duration 1
```

### 18. `navigation_goto`

发送导航目标点或坐标，让机器人导航到指定位置。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/navigation_goto
bash run.sh list
bash run.sh --point origin --dry-run
bash run.sh --point origin
bash run.sh --x 0.1 --y 0.1 --yaw 0 --dry-run
bash run.sh --x 0.1 --y 0.1 --yaw 0
```

### 19. `navigation_list`

查询可用导航点位列表。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/navigation_list
bash run.sh list
```

### 20. `person_tracking`

使用前摄像头先通过注册人脸确认 zhangsan（张三），再用持续 ReID 锁定并跟随同一个人。身份未确认或 ReID 歧义时禁止移动；停止时使用 stop。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/person_tracking
bash run.sh track --name zhangsan --duration 10 --dry-run
bash run.sh find --name zhangsan --duration 10
bash run.sh track --name zhangsan --duration 30 --execute
bash run.sh stop
```

### 21. `pet_tracking`

按完整语义区分宠物任务：只寻找豆豆或家里的狗使用 find_at_home；寻找并明确要求喂食使用 find_and_feed_at_home；明确要求持续跟随才使用 track。两个寻找动作都会先去书房C点再原地旋转，找到即停且不跟随。

注意：`find_at_home` 与 `find_and_feed_at_home` 是 V11 主程序的场景编排动作；单独运行底层 pet_tracking 时使用这里列出的 `find/track/stop`。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/pet_tracking
bash run.sh find --source /dev/video22 --pet dog --search-timeout 10 --backend dummy
bash run.sh track --source /dev/video22 --pet dog --duration 0 --backend ros2
bash run.sh stop
bash run.sh track --pet dog --dry-run
```

### 22. `projector_control`

控制投影仪开关、运动视频和会议图片循环。会议开始时先打开光源再执行启动脚本；meeting_pause 停止循环并保留当前画面，meeting_resume 恢复循环。

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

### 23. `pull_up`

抬头后使用 config.cameras.back.device 指向的后摄像头进行引体向上计数、查询或停止。机器人约30cm高，引体向上需要拍到用户身体，因此默认后摄并要求 head_control(up)。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/pull_up
bash run.sh run --camera /dev/video31 --duration 30
bash run.sh query
bash run.sh stop
```

### 24. `push_up`

抬头后使用后摄像头进行俯卧撑计数。先用 /home/test/single_function 的人脸数据库确认 zhangsan，再以高频自适应 ReID 持续跟踪，并使用机器人侧面低机位专用的水平准备门控和双证据动作状态机计数。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/push_up
bash run.sh run --name zhangsan --camera /dev/video31 --duration 30 --dry-run
bash run.sh run --name zhangsan --camera /dev/video31 --duration 30
bash run.sh query
bash run.sh stop
```

### 25. `realtime_information`

通过实时数据源查询当前时间、天气、机器人当前粗略位置、附近地点和交通状态。回答必须来自本次查询结果，不能凭模型常识猜测实时信息。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/realtime_information
bash run.sh --action current_time
bash run.sh --action weather --location 北京市顺义区
bash run.sh --action location
bash run.sh --action nearby --query 美食 --location 北京市顺义区
bash run.sh --action traffic --location 北京市顺义区
```

### 26. `reminder_cancel`

取消已创建的提醒。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/reminder_cancel
bash run.sh cancel --query 喝水 --dry-run
bash run.sh cancel --query 喝水
```

### 27. `reminder_query`

查询当前提醒列表。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/reminder_query
bash run.sh query --dry-run
bash run.sh query
```

### 28. `reminder_schedule`

创建绝对时间或相对时间提醒；不支持无法转换成具体时间的外部事件条件。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/reminder_schedule
bash run.sh schedule --content 喝水 --trigger-condition 5分钟后 --dry-run
bash run.sh schedule --content 喝水 --trigger-condition 5分钟后
```

### 29. `squat`

抬头后使用 config.cameras.back.device 指向的后摄像头进行深蹲计数、查询或停止。机器人约30cm高，深蹲需要拍到用户身体，因此默认后摄并要求 head_control(up)。

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/squat
bash run.sh run --camera /dev/video31 --duration 30
bash run.sh query
bash run.sh stop
```
