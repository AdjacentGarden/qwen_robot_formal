# 公开源码版部署要求

本仓库提供代码、Skill、场景、App源码、已收录的模型及示例图片，不提供真实API密钥、人脸数据库、注册照片、个人记忆、米家登录态或现网App凭据。原私密Release和下载入口已移除；不要使用旧链接恢复敏感数据。

1. 按主README准备同类RK3588/aarch64、Ubuntu22.04、ROS2 Humble等环境。克隆 `AdjacentGarden/carrelcopy-formal` 到 `/home/test/Car_real_copy` 并构建；不要仅凭仓库名改变代码期待的路径。
2. 按 `config.example.env`、`robot_skills/config/modelscope.env.example`、`reproduce/config_templates/` 私下设置自己的API、米家和App服务配置；运行时文件已加入忽略规则。
3. 在 `robot_skills/realtime_information/config.json` 中配置自己的地区、家庭和公司位置；公开版没有预设真实家庭地址或坐标。没有配置前不应把示例或空值当成实时定位。
4. 配置灯光/投食器设备ID；公开版使用占位符。App也需自行配置Relay地址、机器人身份及匹配令牌，之后再构建APK。
5. 人脸身份需在自己的机器人上重新注册。历史记忆从空库开始。
6. 视频、音乐、Android镜像、RKNN运行环境、App/播放器APK等未作为私密资源提供；按源码及各自许可准备或构建。不能保证任意机器克隆后不作配置即可运行。
7. `python3 reproduce/verify_snapshot.py --full` 只验证源码文件完整性，不启动硬件。安装脚本只在明确参数下复制文件；先备份目标设备。

四个投影/媒体系统脚本仍为最新源码，播放器构建后会生成摘要文件。真实机器人上的配置、凭据、资料和运行程序不因仓库脱敏而改变。

安全提醒：曾出现在Git历史中的认证令牌应视为可能泄露。即使重写历史，也不能撤回其他人的克隆、下载或外部缓存；应由部署者轮换相关令牌。不要再把密钥、私密资源压缩包、APK内嵌凭据或人脸资料推送到公开仓库。

公开仓库不自动改变第三方代码、模型、素材的许可证；请遵守各组件原有许可，未获授权的素材不要重新分发。
