# 跨进程共享运行时实测

测试日期：2026-07-22。服务通过本地 Unix Socket 保留 RetinaFace、FaceNet、YOLO、ReID、MediaPipe Pose、ROS Node 和 ActionClient。测试没有发送运动、导航或家电指令。

## 常驻客户端的单次请求

每项100次，中位数包含图像经 Unix Socket 传输的时间。

| 操作 | 客户端往返中位数 | 服务端推理中位数 |
|---|---:|---:|
| RetinaFace检测 | 60.271 ms | 59.180 ms |
| FaceNet特征 | 10.474 ms | 9.806 ms |
| YOLO | 35.977 ms | 33.665 ms |
| ReID | 11.998 ms | 10.539 ms |
| MediaPipe Pose | 79.529 ms | 77.065 ms |
| 空Ping | 0.658 ms | 0.043 ms |
| ROS Action Server状态 | 3.175 ms | 2.248 ms |

## 每次重新启动轻量客户端进程

每项10次，中位数包含新Python解释器和客户端依赖导入。

| 操作 | 完整进程耗时中位数 |
|---|---:|
| Ping | 127.826 ms |
| ROS状态 | 128.097 ms |
| 人脸检测 | 410.642 ms |
| 人脸特征 | 343.672 ms |
| YOLO | 363.988 ms |
| ReID | 340.341 ms |
| Pose | 409.928 ms |
| 一个进程依次调用五种模型 | 567.277 ms |

对照原独立进程的全部模型初始化约5376 ms，一个轻量客户端依次调用五种模型约567 ms，约快9.5倍。若客户端本身也常驻，模型请求会进一步下降到10～80 ms。

真实前摄像头帧也已验证通过：人脸流水线62.452 ms、YOLO 35.351 ms、Pose 78.455 ms。测试画面没有检测到人脸、人体或姿态，但请求、推理和返回链路均成功。

结果文件：

- `runtime/shared_runtime/benchmark_100.json`
- `runtime/shared_runtime/client_process_benchmark.json`
- `runtime/shared_runtime/camera_test_front.json`
