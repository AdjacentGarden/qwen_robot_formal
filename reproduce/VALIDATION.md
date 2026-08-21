# 快照验证记录（2026-08-21）

所有验证均在 `/tmp/qwen_robot_publish_20260821` 脱敏发布副本上进行，没有启动项目、
ROS、底盘、头部、摄像头、投影或其他硬件。

## 已通过

- Bash 入口语法：`run.sh`、`robot_stack.sh`、`resident_service.sh`、`skill_host.sh`、
  `robot_skills/resident_runtime.sh`、安装脚本全部通过 `bash -n`。
- Python 源码语法：发布快照内 Python 文件全部通过 `py_compile`。
- 文件完整性：`verify_snapshot.py --full` 校验 1,602 个文件通过（添加本报告和修正
  测试后需以最终 `SHA256SUMS` 数量为准）。
- Qwen 主项目：排除旧 IMU 外部接口契约测试后，176 项通过。
- App Python：28 项通过，包括服务 API、认证、地图编码、项目适配和机器人桥。
- App Web/Android 静态契约：2 项 Node 测试通过，包括语音输入、Android 麦克风权限、
  统一任务调度、运动视频元数据和页面结构。
- 敏感信息扫描：没有提交 API Key、Relay token、米家登录态、人脸数据库、识别照片、
  对话日志或现网 APK。

## 已知的旧测试契约

完整 `tests/` 运行结果为 176 通过、5 失败。5 个失败都来自
`tests/test_imu_monotonic_timestamp.py`：该测试要求
`car_real_copy_zhenghang` 的 IMU publisher 导出
`strictly_monotonic_timestamp_ns`，但当前已锁定的导航仓库 commit
`0a83831ebae3b0c5c52c05d6e652430d830a8ebf` 没有这个辅助函数。

这是 Qwen 仓库中遗留测试与当前外部导航仓库之间的接口不一致；本次没有为了让测试
变绿而修改正式导航代码，也没有修改 `/home/test/Car_real_copy`。当前正式运行效果按锁定的
`car_real_copy_zhenghang` commit 复现。

## 环境问题及处理

- 系统 `pytest 6.2.5` 会误加载新版 `anyio` 插件，验证时使用隔离的 pytest 9 依赖。
- 原 `android_app/requirements-dev.txt` 错误引用不存在的顶层 `requirements.txt`，快照中已
  改为 `-r server/requirements.txt`；这是测试/构建依赖修正，不改变 App 运行逻辑。
- 机器人没有 Node.js，Node 契约测试在本地 Node 26.7.0 上读取同一发布快照执行。
