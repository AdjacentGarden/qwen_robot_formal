# Android APP 交付说明

源码目录：`/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/android_app`

本版本只适配千问端到端实时语音项目。Android 客户端和机器人桥接通过中转服务器通信；语音录音进入同一个 Qwen Realtime 会话，按钮动作通过该项目的本地控制 socket 执行，不调用旧场景项目的 ASR、DeepSeek、TTS 或运行时。

## 目录

- `web/`：Web 前端。
- `android/`：Android 原生壳与内置前端资源。
- `server/`：中转服务器后端。
- `robot_bridge/`：机器人侧状态、地图、控制、语音和视频桥接。
- `tests/`：无硬件协议测试。
- `release/`：已构建 APK。

## 机器人桥接

```bash
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/android_app/robot_bridge/robot_bridge.sh start
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/android_app/robot_bridge/robot_bridge.sh status
bash /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/android_app/robot_bridge/robot_bridge.sh stop
```

## Android 构建

```bash
cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/android_app/android
./gradlew testDebugUnitTest lintDebug assembleDebug
```

Gradle 构建目录、Python 缓存、运行日志和历史备份已清除，`release/` 中的交付 APK 保留。
