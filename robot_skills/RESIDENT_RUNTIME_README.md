# V11 全 Skill 常驻运行版

项目目录：`/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills`

这是从 `/home/test/v11_single_function_skills` 独立复制出来的常驻化版本。原目录和
`/home/test/Car_real_copy` 均未修改。它不会配置开机自启动，只在手动启动后运行。

## 一键管理

```bash
# 启动并预加载
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh start

# 查看状态、模型和三个常驻进程
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh status

# 查看主服务日志
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh log 100

# 干净重启
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh restart

# 停止；会先结束任务，再释放 ROS、摄像头和 RKNN
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh stop
```

用户只需启动一次。启动脚本内部维护三个常驻所有者：Camera Broker 是唯一允许打开
`/dev/video22` 和 `/dev/video31` 的进程；主服务持有人脸、ReID、人物 YOLO、Pose、
ROS Node、DDS、ActionClient 和网络会话；宠物 YOLO 放在独立 RKNN3 工作进程。这样
既避免 RKNN3 两个上下文放在同一 Python 进程时相互阻塞，也避免多个模型进程争抢
同一摄像头。调用侧仍只有一个启动、停止和状态入口。

## 调用方式

全部 29 个 skill 的旧命令格式保持不变，只需把根目录换成常驻版。例如：

```bash
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/push_up/run.sh check
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/face_recognition/run.sh --timeout 3
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/pet_tracking/run.sh track --pet dog --duration 15 --backend ros2
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/person_tracking/run.sh check --json
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/navigation_list/run.sh
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/navigation_goto/run.sh living_room
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/move_forward/run.sh --speed 0.2 --duration 1
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/light_control/run.sh status
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/feeder_control/run.sh status
```

覆盖的入口：

- 摄像头：`camera_capture`、`camera_record`、`front_camera_capture`、
  `front_camera_record`、`back_camera_capture`、`back_camera_record`
- 视觉：`environment_perception`、`face_recognition`、`face_registration`、
  `person_tracking`、`pet_tracking`
- 运动：`push_up`、`pull_up`、`squat`
- ROS/硬件：`move_forward`、`move_backward`、`move_left`、`move_right`、
  `navigation_goto`、`navigation_list`、`head_control`
- 家电/投影：`light_control`、`fan_control`、`feeder_control`、`projector_control`
- 信息和提醒：`realtime_information`、`reminder_schedule`、`reminder_query`、
  `reminder_cancel`

每个目录中的 `run.sh` 是 Unix Socket 薄客户端，原来的入口已保存在同目录
`run_legacy.sh`，可用于对照或回退。薄客户端支持流式 stdout/stderr，运动计数进度不必
等整个任务结束才返回主程序。

## 常驻复用范围

- ROS：一个长期存活的 Context、Node、Executor、`/cmd_vel` Publisher、导航
  ActionClient 和头部控制接口，避免每次初始化 DDS 和重新发现端点。
- 视觉：RetinaFace、FaceNet、人物 YOLO、ReID 和 MediaPipe Pose 在主进程只加载
  一次；宠物 YOLO 在专用 RKNN3 进程只加载一次。
- 摄像头：Camera Broker 在启动时打开并预热设备，将原始 BGR 帧写入 `/dev/shm` 的
  四槽环形缓冲；所有模型读取共享内存，Skill 不再直接打开或释放 V4L2 设备。
- 米家：缓存鉴权对象、API 对象和 HTTP Session；远端云请求耗时仍取决于网络。
- 普通 Python skill：模块只导入一次，避免反复启动解释器和导入依赖。

## 并发和资源调度

常驻服务使用线程隔离的 stdout/stderr 路由，每个 Skill 的流式事件只会返回自己的
客户端。CLI 参数也通过显式参数传递；只有仍依赖外部旧 CLI 的提醒存储操作保留窄锁。

共享帧读取不互斥，因此人脸识别、宠物识别、拍照、录像和环境感知可以同时读取同一
Camera Broker。以下互不冲突的任务允许并发：

- 宠物 RKNN 与主 NPU 人脸/人物模型，因为二者位于不同 NPU 设备
- 前后摄像头读取、拍照、录像和检测
- 灯、喂食器、实时信息查询等独立网络请求
- 不控制底盘的人脸识别、宠物 dummy 检测和运动视觉处理

以下资源必须互斥：

- `base`：前后移动、旋转、导航、执行模式的人物或宠物追踪
- `main_npu`：人脸、人物 YOLO/ReID 和运动计数共用的主 NPU Context
- `head`：头部电机
- `projector`：投影状态修改
- `light_device`、`feeder_device`：同一台家电的并发请求
- `reminder_store`：提醒持久化存储修改

冲突任务不会破坏正在执行的任务，而会返回非零退出码和结构化结果：

```json
{
  "ok": false,
  "status": "resource_busy",
  "resource": "base",
  "owners": {"base": {"skill": "move_forward"}}
}
```

运动计数的 `query/stop`、宠物和人物追踪的 `stop`，以及投影的
`off/status/meeting_pause/meeting_resume` 是优先控制请求，可以越过长任务执行。
当前资源占用情况可以通过状态结果中的 `active_resources` 查看。

## 验证

安全回归命令：

```bash
python3 /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/validate_resident_runtime.py
```

最近结果保存在：
`/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime/validation/latest.json`

最近一次结果为 29/29 入口通过。共享摄像头连续切换实测中，前摄像头拍照约
120–142 ms，人脸识别严格执行指定的 3 秒检测窗口后总耗时约 3.16 秒，宠物检测后
立即拍照约 124–141 ms。两个独立进程同时读取前摄像头均为 20/20 帧成功，Camera
Broker 的设备重新打开次数始终保持为 1。底盘零速度
调用的首个 Twist 测量曾由旧入口约 1986 ms 降到约 163 ms。灯状态第二次约 530 ms，
喂食器状态第二次约 1742 ms；后两者主要剩余米家云端往返时间。

六个非冲突任务（宠物 dummy 追踪、人脸识别、前摄拍照、时间、灯状态、喂食器状态）
并发实测的单项耗时总和约 9.71 秒，实际墙钟时间约 3.77 秒，并行收益约 2.58 倍，
而且各客户端输出没有串台。底盘和主 NPU 冲突用例约 0.38 秒返回
`resource_busy`；宠物长任务的停止请求约 0.46 秒返回并结束任务。

`fan_control` 在原实现中就是禁用状态，验证时预期返回退出码 2，不代表常驻服务异常。
实际导航仍要求 `Car_real_copy` 的底盘、里程计和 Nav2 已在外部正常启动；常驻版只
复用客户端，不会修改或代替这些节点。

摄像头状态可在总状态的 `camera_broker` 字段查看。正常情况下前后摄像头都应为
`ready`，且 `reopens` 在连续切换 Skill 时保持为 1：

```bash
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh status
```

## 不变性和回退

复制前的校验记录：

- `/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/audit/source_v11_before.sha256`
- `/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/audit/car_real_copy_untouched.sha256`

最终复核可执行：

```bash
cd /home/test/v11_single_function_skills && sha256sum -c /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/audit/source_v11_before.sha256
cd /home/test/Car_real_copy && sha256sum -c /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/audit/car_real_copy_untouched.sha256
```

要回退只需停止常驻版，然后继续使用原目录：

```bash
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/resident_runtime.sh stop
```
