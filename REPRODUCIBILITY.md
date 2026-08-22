# Qwen 家庭机器人固定场景项目复现说明

本目录记录 2026-08-23 机器人当前正式演示版本的运行依赖。仓库根目录是
`/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test` 的脱敏工作树快照，
其中包含实时千问语音、场景引擎、Skill、RKNN 模型和完整 Android App 源码。

## 目标环境

- Ubuntu 22.04，aarch64（RK3588）
- Python 3.10.12
- ROS 2 Humble
- 正式 ROS 工作空间：`/home/test/car_real_copy_zhenghang`
- 正式项目路径：`/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test`

## 仓库结构

- 根目录：正式 Qwen 项目源码和本地模型
- `android_app/`：网页端、Android 工程、机器人桥、服务端和构建脚本
- `reproduce/vendor/`：当前运行所需但原本散落在其他项目中的源码快照
- `reproduce/home_test_root/`：投影所需的 `/home/test` 根目录脚本
- `reproduce/system_files/`：当前安装在 `/usr/local/libexec`、`/usr/local/sbin` 和 systemd 中的辅助文件
- `reproduce/config_templates/`：需要在目标机器人上补齐的私密配置模板
- `reproduce/dependencies.lock.json`：外部仓库 commit、平台版本和关键文件校验值
- `reproduce/SHA256SUMS`：源码、模型、音频和 App 资源的完整文件级校验清单
- `reproduce/install_robot_snapshot.sh`：把快照恢复到项目原本期望的绝对路径
- `reproduce/verify_snapshot.py`：只读检查目录、脚本、模型和 App 是否完整

## 没有提交的内容

以下内容故意不进入 Git：

- 千问/ModelScope API Key
- 米家 `auth.json` 登录态
- App bridge token、App 内 Relay token、外部 Relay 凭据
- 白墙服务 API Key
- 人脸数据库、注册照片、识别历史
- 对话日志、运行时视频、PID、Unix socket 和缓存

这些内容不是通用源码。部署后按模板重新填写凭据，并重新进行人脸注册。

## 安装顺序

1. 克隆并构建导航仓库：

   ```bash
   git clone git@github.com:AdjacentGarden/car_real_copy_zhenghang.git /home/test/car_real_copy_zhenghang
   git -C /home/test/car_real_copy_zhenghang checkout f6f4edd6270f62eeda843885a0236aca12a0d7c5f
   cd /home/test/car_real_copy_zhenghang
   bash build_restored_car.sh
   ```

2. 将本仓库放到正式项目路径，然后恢复配套快照：

   ```bash
   git clone git@github.com:AdjacentGarden/qwen_robot.git /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test
   cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test
   bash reproduce/install_robot_snapshot.sh --user-files
   ```

3. 按 `reproduce/config_templates/` 填写私密配置。不要把填好的配置提交回 Git。

   如需恢复投影播放器、投影准备脚本和 App bridge 的系统级文件，可在审核快照后执行：

   ```bash
   cd /home/test/qwen_audio_3_realtime_flash_scenarios_resident_test
   sudo bash reproduce/install_robot_snapshot.sh --system-files
   ```

   该命令只复制文件并刷新 systemd 配置，不会启动服务、ROS、Skill 或任何硬件。

4. 进行只读验证：

   ```bash
   python3 reproduce/verify_snapshot.py
   python3 reproduce/verify_snapshot.py --full
   ```

5. App 源码位于 `android_app/`。网页界面、Android assets、机器人桥和服务端均已保存，
   可按照 `android_app/README.md` 重新构建。构建前必须在以下文件填入新生成的 App token：

   ```text
   android_app/web/config.js
   android_app/android/app/src/main/assets/public/config.js
   ```

   这两个文件在仓库中使用 `replace_me`，不会泄露现网凭据。APK 二进制会把 token
   固化在 assets 中，因此没有把现网 APK 上传到 Git；这不影响界面和功能源码的复现。

## 私密配置

至少需要配置：

- `robot_skills/config/modelscope.env`
- `/home/test/Mijia/auth.json`（仅灯光和投食器需要）
- `/home/test/.config/robot_android_bridge.env`，其 token 必须与重新构建 App 的 token 一致
- `/home/test/.config/white_wall_pucoding.env`（仅白墙相关服务需要）

项目启动入口、停止命令和 App 部署说明分别见仓库根目录 `README.md` 与
`android_app/README.md`。安装脚本默认只复制文件，不启动 ROS、项目或硬件。
