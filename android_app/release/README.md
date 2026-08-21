# Android APK 构建产物

现网 APK 的 assets 中包含连接 Relay 所需的 token，因此不把该二进制提交到 Git。

完整 Android 工程、网页资源和构建脚本都位于上一级目录。先将新生成的 token 写入：

```text
android_app/web/config.js
android_app/android/app/src/main/assets/public/config.js
```

再按照 `android_app/README.md` 构建。请确保机器人端
`/home/test/.config/robot_android_bridge.env` 中的 `ROBOT_BRIDGE_TOKEN` 使用同一个值。
