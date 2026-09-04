# 理想同学 Android App

本 App 采用独立源码、独立 Android 包名和独立安装包。

- 包名：`com.lixiang.robot.companion.fixed`
- App 名称：`理想同学`
- APK：`release/ideal-robot-qwen-2.3.0-car-real-copy-adapter.apk`
- 视觉方向：深海军蓝与青绿色的智能导航仪表风格

功能包括首页启动/停止千问机器人程序与端到端语音、宠物和运动视频、地图、实时坐标、地图点选导航、控制页四向点按微调与急停、客厅灯和投食机。家具控制保留在控制页，首页不放快捷底盘控制。问候语读取手机本地时间，并在 App 恢复到前台时刷新。

控制页的“关闭麦克风”只暂停机器人本机麦克风的现场语音输入；App 的按住说话、地图、家居和其他按键控制均保持可用。“打开麦克风”会立即恢复现场语音；如果语音主程序尚未启动，该设置会安全保存并在下次启动时生效。

机器人端唯一目标目录为 `/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test`；底盘/导航环境只读加载 `/home/test/Car_real_copy`，不会修改该目录。App 将 Manager 进程是否正常和导航当前是否可用分开显示：抬头、低头期间 sensor gate 按设计关闭时，程序仍显示“运行中”，导航能力单独标记为暂不可用；全新启动成功仍必须同时满足 Manager、定位、导航、千问与语音全部就绪。

机器人桥接使用独立实例标识与专用令牌，旧机器或复制项目中的桥接进程不能再抢占当前机器人的实时连接。

## 构建

```powershell
npm ci
npx cap sync android
cd android
.\gradlew.bat testDebugUnitTest lintDebug assembleDebug
```

## 无硬件测试

Mock 服务不会连接机器人、ROS 或真实设备：

```powershell
node tests/mock_app_server.mjs
```

浏览器打开 `http://127.0.0.1:8871/`。完成界面操作后可验证指令协议：

```powershell
node tests/test_web_contract.mjs
node tests/assert_mock_commands.mjs 8871
```

服务端测试：

```powershell
python -m pip install -r server\requirements-dev.txt
python -m pytest -q
```
