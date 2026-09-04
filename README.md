# Qwen Audio 3.0 Realtime Flash 场景演示与常驻 Skill 测试

这是从 `/home/test/qwen_audio_3_realtime_flash_test` 创建的独立副本。它不修改、不停止，
也不在运行时导入 `/home/test/project_0727_fixed_points_home_scenes`；场景目录和已验证的
常驻 Skill 代码以快照形式复制在本项目中，拥有独立的 Socket、PID、日志和运行目录。
它直接使用：

- `qwen-audio-3.0-realtime-flash`
- 16kHz、PCM16、单声道麦克风流
- 24kHz、PCM16、单声道流式语音播放
- `smart_turn` 或 `server_vad` 自动判断用户一轮话何时结束
- 单个 WebSocket 中持续保留最多 50 轮上下文
- 断线自动重连，直到用户按 `Ctrl+C` 或进程收到 `SIGTERM`
- Qwen 原生 Function Calling 调用机器人本地 skill，工具结果回传后继续生成流式语音
- 固定场景流程编译器，兼容旧演示项目中口语化、参数省略的场景话术
- 常驻 ROS、相机、RKNN、MediaPipe 运行时，通过 Unix Socket 直接调用
- 同一会话保留最多 50 轮上下文，并将近期对话持久化供显式查询；默认不把过去的设备结果注入新会话
- 本机长期记忆支持“记住、查询、忘记”，默认拒绝保存密码、API Key 等敏感内容

## 接口状态

已验证 `sk-...` API Key 本身有效。之前提供的 `ms-2c2...` 被业务空间专属地址明确拒绝为：

```text
BadRequest.IllegalEndpoint: Workspace endpoint is invalid.
```

北京公共 Realtime 地址已经成功返回 `session.created`，因此本项目默认使用：

```text
wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash
```

不需要填写 Workspace ID。以后如果取得真实业务空间 ID，仍可通过 `--workspace` 使用专属地址。

## 配置

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test
mkdir -p runtime

# 将百炼 API Key 写入此文件，然后限制权限；不要提交到代码仓库
chmod 600 runtime/api_key

# 可选：复制其他配置；默认公共地址不需要 Workspace 配置
cp config.example.env runtime/config.env
chmod 600 runtime/config.env
```

## 安全预检

预检只建立 WebSocket 并配置会话，不打开麦克风和扬声器：

```bash
bash run.sh --preflight
```

看到以下两行才表示 Key、Workspace、地域和模型权限全部正确：

```text
已连接 qwen-audio-3.0-realtime-flash。
预检成功：未打开麦克风或扬声器。
```

## 持续对话

```bash
bash run.sh
```

启动后无需唤醒词，直接说话即可。程序会持续监听、流式播放回复并保留上下文，直到在
当前终端按 `Ctrl+C`。日志写入 `runtime/realtime_chat.jsonl`，不会记录 Key 或音频。

## 场景演示

场景工具不是由模型自由拼接原子动作，而是从 `scenarios/procedure_catalog.json` 和
`scenarios/home_scene_catalog.json` 编译固定步骤、依赖关系及 `outcome_groups`。当前包括：

- 欢迎回家：首次唤醒或用户明确要求重播时，先播报“欢迎回家”，再低头播放三秒欢迎投影并恢复平视；不导航、不追踪；
- 会议开始和会议投影关闭；
- 俯卧撑、引体向上和深蹲陪练；
- 找豆豆、原地找豆豆、找到后喂食；
- 客厅开灯与休息场景。

会议投影成功启动后，主程序会根据投影控制器的 `session_active` 状态自动开启
`projection_occlusion_observer.py`。观察器通过常驻 Skill 的共享内存读取前摄像头，
每秒最多向独立的 `qwen3-omni-flash-realtime` 视觉会话提交一帧；连续两次确认人物
遮挡投影区域后，通过现有 Qwen 事件播报队列提醒“您挡住投影了，麻烦您往旁边挪一挪”。
关闭投影后自动释放摄像头和视觉连接，不会控制头部、导航或底盘。检测参数和归一化
投影区域位于 `projection_occlusion.json`，可用 `--no-projection-occlusion` 临时禁用。

“我要开会了”“来陪我运动吧”“豆豆该吃饭了”等模糊表达会进入完整场景。运动计数、
欢迎投影和会议投影等受保护原子能力不再直接暴露给模型；即使模型返回旧的原子工具名，
执行网关也会强制改走对应场景。场景步骤失败后，后续依赖步骤会跳过，清理步骤仍按
`completion` 规则运行，最终只依据真实失败阶段播报。

## 记忆能力

记忆数据只保存在本测试项目中：

```text
runtime/memory/conversation_history.jsonl
runtime/memory/long_term_memory.json
runtime/memory/command_history.jsonl
config/resident_profile.json
```

- 当前 WebSocket 会话由千问保留最多 50 轮问答；
- 默认持久保存最近 1000 条转写供 `memory_query` 查询，但启动或断线重连时不再自动注入原始对话，
  避免把过去的设备成功或失败结果误当成当前状态；
- 用户明确说“记住我喜欢喝茶”时写入长期记忆；
- `config/resident_profile.json` 保存不会频繁变化的部署事实，例如宠物名称和机器人常驻地区；
  当前已配置“豆豆是用户的宠物狗”、家庭地址“请配置家庭地址”，以及公司地址
  “北京市顺义区公司地址理想汽车研发总部”。未指定地点的本地天气、附近地点和路况默认使用家庭地址；
  这类配置不会冒充实时 GPS 定位；
- “你记得我什么”会查询长期记忆和近期对话；
- 支持查询上一条、最近两条、倒数第几条、最早一条、从最早开始的第 N 条，以及今天、昨天、前天、
  指定日期或包含某个动作关键词的历史指令和时间；
- “忘掉我喜欢喝茶这件事”只删除匹配项；
- “清空全部记忆”必须由用户明确提出，才会清空指定范围；
- 密码、API Key、身份证号和银行卡信息不会写入长期记忆。

修改固定住户信息时编辑 `config/resident_profile.json` 的 `facts` 数组；普通偏好仍应由用户明确说
“请记住……”后写入 `runtime/memory/long_term_memory.json`，不要把临时设备状态写进固定配置。

临时禁用跨会话记忆：

```bash
bash run.sh --no-persistent-memory
```

如确实需要恢复普通闲聊上下文，可以显式指定注入轮数；设备状态仍应以新的 Skill 查询结果为准：

```bash
bash run.sh --memory-history-turns 12
```

默认从本项目的 `robot_skills/config/skill_specs` 注册全部处于启用状态的本地 skill，
但采用 `dry-run`：模型可以完整测试“识别意图 → 调用本地 skill → 回传结果 → 继续语音
播报”的链路，不会实际打开硬件、移动底盘或写入业务数据。被原有规格明确标记为 disabled
的 skill 不会注册，避免模型调用一个已知不可用的入口。

当前机器人共发现 30 份 skill 规格，其中 29 个可用并已注册；`fan_control` 的原规格明确
写有旧实现已移除，因此保持禁用，不伪装成可执行能力。

逐项检查全部本地 skill 的规格、参数过滤、现有执行器接入和资源映射：

```bash
bash run.sh --catalog-test
```

此测试不需要 API Key、不联网、不打开任何硬件。云端会话是否接受完整工具目录则使用：

```bash
bash run.sh --preflight
```

无需打开麦克风、扬声器和投影硬件即可测试完整链路：

```bash
bash run.sh --tool-test-text "请开始投影"
```

测试会把模型在工具执行后的 24kHz PCM 流式语音保存为：

```text
runtime/tool_test_response.wav
```

只有明确要求真实执行本地 skill 时才增加 `--execute-skills`：

```bash
bash run.sh --execute-skills
```

该命令现在只加载（不修改）ROS 工作区：

```text
/home/test/Car_real_copy
```

并且只启动 `MappingNavigationManager`，由 Manager 统一管理底盘、雷达、里程计、定位、
Nav2、sensor gate、有限恢复和 `SAFE_STOP`。启动前会验证已安装地图，地图无效时直接拒绝，
不会回退到自动建图。只有 Manager 进入 `NAVIGATION`、sensor gate 为 `ready` 且导航链路
完整就绪后才启动持续语音。Qwen/App 的手动运动进入 `/cmd_vel_external`，导航进入
`/motion_controller/nav_goal_with_options`；不再绕过 Manager 直发底盘或 Nav2 目标。

ROS 链路就绪后还会启动本副本中的：

```text
robot_skills/resident_camera_broker.py
robot_skills/resident_pet_worker.py
robot_skills/resident_runtime_server.py
skill_host.py
```

`realtime_chat.py → skill_host.py → resident_runtime_server.py` 全程使用 Unix Socket，正常
调用不再启动 `skill_runner.py`、Bash 和新的 Skill Python 解释器。模型、ROS 节点及相机
由常驻运行时统一持有。退出 `run.sh` 时只清理本副本自己启动的常驻进程；如果检测到其他
项目的常驻 Skill 运行时，启动会直接拒绝，避免争用相机、NPU 和 ROS。

仅查看计划或状态，不启动硬件：

```bash
bash run.sh --robot-stack-plan
bash run.sh --robot-stack-status
```

默认在千问程序退出时调用 `/mapping_manager/shutdown`，由 Manager 统一关闭其拥有的链路。如底层 ROS 已由
其他受控程序启动，可跳过自动启动：

```bash
bash run.sh --execute-skills --no-auto-robot-stack
```

此时用户说“开始会议投影”，模型调用 `run_robot_scenario(scenario="meeting_projection")`；
完整场景执行结果写回 Qwen，Qwen 再自然播报成功或失败。Function Call 本身不会送入语音合成，因此不会把 JSON
或函数名读出来，也不会和本地 TTS 重复播报。

运动视觉管线产生的 `ready` 和每次 `count` 事件会在常驻 Socket 中立即流式透传。为了不让
长时间 Function Call 阻塞逐个计数，程序预连接一个只负责朗读事件的 Qwen Realtime 通道，
使用与主会话相同的音色；不会调用本地 TTS。`complete` 事件不在该通道重复朗读，最终总数、
清理状态和“喝口水”关怀仍由主会话统一播报一次。

## Android App

适配后的 App 位于 `android_app/`。首页只保留程序启动/停止和端到端语音，家具及前后左右
点控保留在控制页；宠物和运动页各自只有一套统一风格的视频播放控件。机器人桥接的
`program_start`/`program_stop` 直接控制本项目 `resident_service.sh`，App 录音通过
`runtime/app_control.sock` 送入当前 Qwen 会话。

```bash
bash android_app/robot_bridge/robot_bridge.sh start
bash android_app/robot_bridge/robot_bridge.sh status
bash android_app/robot_bridge/robot_bridge.sh stop
```

Windows 构建机已通过前端契约、生产依赖审计、Capacitor 同步、Gradle 单元测试、lint 和
APK 构建。交付包为 `android_app/release/ideal-robot-qwen-realtime-20260818.apk`。

真实执行复用 `/home/test/qwen_robot_project` 已有的参数清洗和语音结果策略，但 Skill
代码、模型缓存和运行输出均位于本副本的 `robot_skills`；ROS 工作区仍只允许
`car_real_copy_zhenghang`。若检测到场景演示或其他机器人控制进程正在运行，有副作用的调用会拒绝执行，避免
抢占底盘、摄像头、投影等资源。持续语音启动前还会以非阻塞方式锁定共享的 `mic` 和
`speaker`；任一资源已被占用时立即退出，不等待、不抢占。也可以只暴露指定 skill，进一步
缩小权限范围：

```bash
bash run.sh --enable-skill projector_control --execute-skills
```

默认使用 `speaker-safe`：机器人播放回复时暂停向云端上传麦克风数据，播放结束 0.5 秒后
自动恢复监听，可以避免扬声器声音被麦克风再次送给模型。这种模式不能在机器人说话中途
插话，但每轮结束后无需重新唤醒。

如果使用耳机，或者机器人系统已经具备可靠的声学回声消除，可以启用真正的全双工插话：

```bash
bash run.sh --echo-mode full-duplex
```

检测到用户插话后，程序会立即清空播放缓存并发送 `response.cancel`。
若回复恰好已在取消指令到达前结束，服务端可能返回“没有活动回复”；程序会把这一种精确的
取消竞态视为无害事件，不会因此关闭 Qwen、Skill 常驻执行器或机器人基础链路。其他鉴权、
参数和服务错误仍按错误处理。

## 音频设备

列出 PyAudio 设备：

```bash
bash run.sh --list-devices
```

如默认设备不正确，可明确指定：

```bash
bash run.sh --input-device-index 6 --output-device-index 6
```

设备编号可能随系统变化，应以 `--list-devices` 的实际输出为准。

## 轮次判断

默认使用更接近自然交流的语义轮次判断：

```bash
bash run.sh --turn-detection smart_turn
```

也可切换为固定静音阈值：

```bash
bash run.sh --turn-detection server_vad --silence-duration-ms 800
```

## 测试

这些测试不会打开音频设备，也不会连接云服务：

```bash
python3 -m py_compile realtime_core.py realtime_chat.py scenario_engine.py skill_host.py skill_runner.py tests/*.py
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

App 服务与地图回归使用隔离测试依赖：

```bash
cd android_app
PYTHONPATH=. python3 -m pytest -q tests/test_map_codec.py tests/test_project_adapter.py tests/test_server.py
node tests/test_voice_fitness_contract.mjs
node tests/test_web_contract.mjs
```

常驻 Skill 全目录校验不会让底轮移动；四个底盘动作固定使用零速度，导航只编译目标参数：

```bash
python3 robot_skills/validate_resident_runtime.py
```

只检查场景编译和全部 Skill 参数，不启动常驻模型或硬件：

```bash
bash run.sh --catalog-test
```

单独查看常驻执行器状态：

```bash
bash skill_host.sh status
bash robot_skills/resident_runtime.sh status
```
