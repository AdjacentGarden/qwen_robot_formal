# Robot CommandSession / TaskGroup Runtime

这个目录把唤醒、麦克风、喇叭、豆包端到端实时语音决策、skill 调用、任务队列、中断栈和 history 放到同一套运行框架里。

## 核心流程

1. 唤醒模块收到 `/ros_robot_controller/wakeup` 后创建 `WakeupEvent`。
2. 如果已有 `active_task_group_id`，执行器先中断当前进程，把整个 `TaskGroup` 的 slots、steps、已完成结果和恢复上下文写入 `interrupted_stack`。
3. 每次唤醒后的语音输入都是一个新的 `CommandSession`。这个 session 调用 `AudioManager`，复用 `/home/test/self_program/wake_skill_agent.py` 同款 VAD 录音链路，也就是 `vocal_stream_llm.record_one_turn_pcm`，再用 `/home/test/self_program/self_program_asr.py` 做 ASR。
4. 语音指令由豆包端到端实时语音模型直接输出结构化 JSON；手工文字输入和模型异常恢复使用本地安全规划器，不再调用第二个大模型。
5. 规划结果生成一个或多个 `TaskGroup`。用户一次说多条指令时，一个 `CommandSession` 可以产生多个顺序 `TaskGroup`。
6. 必要 slots 不足或可选增强需要确认时，`TaskGroup` 标为 `needs_info`，追问写进这个 TaskGroup 的 `followups`，不会作为新任务单独保存。
7. 追问播报后，daemon 会直接再次打开麦克风接收补充回答，不需要用户重新说唤醒词；补充回答会通过 `answer_followup` 合并回原来的 `TaskGroup`。
8. 执行器始终只运行一个 `TaskGroup`。每个 `TaskStep` 在执行前先锁定硬件资源，例如 mic、speaker、front_camera、back_camera、base、projector_i2c。
9. 执行器实时读取 `single_function` stdout，把“计数开始”“第几个”“没有找到目标”“任务结束”等原本应该播报的文本交给 `AudioManager` 播报。
10. 一个 `TaskGroup` 整体完成后，完整写入 history。若中断栈不为空，机器人主动询问是否恢复上一个被中断任务。

## 已接入硬件

- 麦克风：`plughw:rockchipi2sdm_1`，`arecord`，16 kHz，S16_LE，单声道；实际对话录音使用 `wake_skill_agent.py` 里的 VAD 参数，自动检测人声开始和结束。
- 喇叭：`plughw:rockchiptas6424`，`aplay`，默认 24 kHz 播放 TTS PCM；唤醒应答优先播放 `/home/test/self_program/runtime/wake_reply.pcm`，普通追问/播报复用豆包实时 TTS。
- 前摄：`/dev/video22`，默认 640x640，15 fps。
- 后摄：`/dev/video31`，默认 640x640，15 fps。
- 唤醒：ROS2 topic `/ros_robot_controller/wakeup`，消息类型 `std_msgs/msg/UInt8`。
- 底盘运动：`/home/test/single_function/move_*`，底层 ROS2 `/cmd_vel`。
- 头部：`/home/test/single_function/head_control`，底层 `/step_motor_angle`。
- 导航：`/home/test/single_function/navigation_goto`，Nav2 `/navigate_to_pose`。
- 投影：`/home/test/single_function/projector_control`，I2C bus 2。
- 人脸、追踪、运动计数、环境感知和提醒都通过 `/home/test/single_function/<skill>/run.sh` 调用。
- `light_control` 和 `feeder_control` 通过米家云调用 `/home/test/single_function` 中的独立实现；`fan_control` 仍按 disabled skill 处理。

## 播报协议

`single_function` 不直接占用喇叭。每个 skill 通过 stdout 输出原本应该播报的文本；`/home/test/new_project` 统一负责过滤、TTS 和播放。这样长任务可以边执行边播报，例如运动计数中的“第一个”“第二个”和结束总结。

协议文件在：

```bash
/home/test/single_function/SPEECH_PROTOCOL.md
/home/test/single_function/speech_policy.json
```

每个 `/home/test/single_function/<skill>/skill.json` 也补充了 `speech_contract`，说明该 skill 的播报来源和事件类型。所有 `run.sh` 已加上 `PYTHONUNBUFFERED=1`，确保长任务进度可以实时被 `/home/test/new_project` 读到。

## 命令

```bash
cd /home/test/new_project
python3 -m new_project.cli self-check
python3 -m new_project.cli plan --text "我想做运动"
python3 -m new_project.cli run --text "先拍前方照片，然后提醒我五分钟后喝水"
python3 -m new_project.cli run --text "先拍前方照片，然后提醒我五分钟后喝水" --enqueue
python3 -m new_project.cli run --text "拍前方照片" --execute
python3 -m new_project.cli voice-once
python3 -m new_project.cli answer --task-group-id task_xxx --text "深蹲，需要投影" --execute
python3 -m new_project.cli daemon --execute --max-followups 3
python3 -m new_project.cli resume --execute
```

## 紧凑语音决策 JSON

`config/hardware.json` 中的 `voice_decision.compact_decision_json=true` 会让豆包只返回
紧凑的语义决策。运行时再把 `tasks/ask/intent` 扩展成原有完整 TaskGroup，因此执行器、
多任务、追问、记忆、暗示性意图和会议投影后处理保持原有接口。解析器同时兼容旧版完整
JSON；把该开关设为 `false` 即可立即恢复旧提示格式。

流式接收只在最外层 JSON 真正闭合后执行一次完整解析。若紧凑任务缺少
`actionable/authorization/negated/uncertain` 任一安全字段，任务会被拦截并进入原有语义
裁决兜底，不会直接操作硬件。

离线兼容回归：

```bash
cd /home/test/new_project
python3 compact_json_regression_check.py
```

## 语音时延诊断

每轮豆包语音决策都会生成一个 `voice_<id>` trace，并把细粒度计时写入
`runtime/events/events.jsonl`。日志包含连接、等待人声、VAD、音频尾帧、ASR、
模型首字符、JSON 首次可解析、二次语义裁决、TaskGroup 编排、技能进程、设备返回
和完成播报等阶段；只记录长度与耗时，不保存音频、密钥或完整提示词。

查看最近一轮完整时延报告：

```bash
cd /home/test/new_project
python3 timing_report.py
```

查看指定 trace：

```bash
python3 timing_report.py --trace-id voice_xxxxxxxxxxxx
```

离线验证计时汇总和 trace 关联逻辑：

```bash
python3 timing_observability_regression_check.py
```

`run` 默认只规划和保存本次 session，不实际动硬件，也不污染持久队列；加 `--enqueue` 才入队，加 `--execute` 才会执行真实 skill。
