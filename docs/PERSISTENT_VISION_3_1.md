# v3.1.0 · build 20261113.1

## 常驻工作区

所有功能页均加载在同一个工作区外壳中。导航只更换功能区 iframe，摄像头连接和官方 URDF/STL 模型不销毁。遥操作、采集、视觉抓取页面均能看到三路 RGB、Astra 原生深度及左右从臂模型。手机端使用底部常驻监视区，画面支持横向滚动。

双臂同向并排显示，左、右各使用既有从臂页面的 ARM3D 和同一个官方模型，不伪造两臂基座的实测间距。关节使用控制进程实测反馈，通过本机 Unix datagram + WebSocket 推送；掉线、过期、未连接均明确显示。模型更新不依赖 HTTP 定时轮询，不做平滑或插值。传感器采样、网络和浏览器渲染仍有物理延迟。

页面显示 v3.1.0。`/api/version` 和 `X-Rebot-Version` 返回构建号；全部 HTML 禁止缓存，JS/CSS 使用统一的版本查询参数。

## 全平台唯一视觉采集进程

```
RGB 视频设备 + Astra OpenNI 深度模块
                  ↓ 仅此进程打开硬件
          rebot-vision（独立 Python 进程）
             ├─ 每路 RGB 一个采集线程：MJPG / 最新帧缓存
             └─ OpenNI 深度线程：原生 uint16 毫米 + 显示色彩图
                  ↓ 本机 Unix socket RPC（有认证，0700 目录）
              Web 服务：共享读帧和流转发
               ├─ 常驻监视 + 功能页镜像画布
               ├─ YOLO / 抓取 / 抓拍共用帧
               └─ LeRobot rebot_shared 插件（loopback 无损 BGR 流）
```

`backend/vision_process.py` 提供前端兼容的相机 lease，`vision_broker.py` 是唯一采集进程入口，`camera_manager.py` 与 `depth_manager.py` 仅在该进程内执行物理采集。Web 不直接打开 VideoCapture；采集、遥操作、评估不再独占设备。`/api/cameras/status` 和 `/api/depth/status` 的 `capture_pid` 可核对属于同一个进程。

旧 Orbbec RGB 抓拍端点也转发共享 RGB；相机扫描改用元数据发现，不再启动抢占硬件的 CLI。相机设置页面的画布从常驻画面镜像，避免额外三条 MJPEG 流触及浏览器连接上限。

三台 RGB 默认 640×480 / 30 FPS，先设置 MJPG 再读首帧。遥操作和录制显式传入 30 FPS（可按相机配置下调），避免 LeRobot 默认 60 FPS 覆盖 30 FPS 相机。原生桌面 Rerun 显示关闭，网页负责显示。配置保留离线的合法 `/dev/v4l/by-id/…` / `by-path/…`，不把掉线设备当成非法路径，也不把解析失败变成相机 0。

真实硬件识别为 Astra（2bc5:060f 深度，2bc5:050f RGB），使用 OpenNI 2.3。深度不是 RGB 伪造图，提供原生 uint16 PNG、毫米读数和色彩图；目前**未与 RGB 对齐**，不能直接将 RGB 像素当作深度像素使用。当前深度为监视与点值接口，LeRobot 训练图像仍为已配置的 RGB，未擅自改变已有数据集特征。

SDK 来源：https://www.orbbec.com/developers/openni-sdk/ ，运行库版本 2.3.0.86-beta6，下载地址见 `vendor/openni2/README.rebot.md`。

## 一键断开所有设备

顶部按钮先暂停重连，然后按实际设备文件（ttyUSB/ttyACM、已配置串口、视频节点和 Orbbec USB 节点）查找占用者，**直接 SIGKILL 所有命中的设备占用进程，包括 Web 外进程**，停止 Web 任务、观察器、从臂控制守护进程和统一视觉进程，最后复查实际占用。只处理占用目标设备的进程，不按进程名泛杀 Python。

操作不调用回零。暂停状态写入配置，刷新页面、切页及重启服务不会自动重新占用设备；点击“恢复监视”恢复相机，重新连接设备后才能启动控制。

结果对话框列出终止的 PID、进程名称、设备和剩余占用；不展示可能含密钥的完整命令行。PID 重用校验及 pidfd 避免误杀复用的 PID。Web 自身及其父进程不能被按钮杀死，正常通过关闭资源释放；若驱动阻塞仍占用，则如实报告。对于系统权限不允许终止的其他用户/root 进程，报告权限不足，不伪造“已释放”。外部系统服务若自动拉起占用者，也会在复查中报告。

## 视觉抓取

页面按工作设备、画面选点、桌面标定、目标识别、路径与执行组织。支持稳定相机路径、左/右臂选择、共享实时画面、冻结选点、YOLO/AI 目标选择、设置自动保存及任务日志。

标定按相机 + 机械臂独立保存。共线点、重复点、非有限数值、桌面高度差和区域外选点被拒绝；新增标定点使旧矩阵失效。FK/IK 纯计算不打开串口，使用当前实测关节角。执行使用服务端一次性预演 ID，120 秒失效；设备、标定、抓取高度、机械臂实际位置变化均要求重新预演。抓取点和预抓取点都通过 IK 后才可执行。

执行纳入全局任务管理，支持全局停止。旧版引用不存在的 `drivers.robot.grasp_driver.GraspDriver` 已修复，使用现有官方 `RebotArmEndPose` 轨迹控制器、MIT 重力前馈、独立 100 Hz 控制进程、夹爪力矩限幅及温度检查。启动目标来自实测角度，不默认回零；结束不调用 SDK 的 homing `end()`。夹爪张开目标沿用现有从臂页面实测的 -3.05 rad，默认力矩上限 0.3 Nm。

该页当前是桌面点抓取流程，不代替比赛中的双臂抵盒翻面、固定袋子翻面和分拣技能。验证过程中不自动执行抓取或遥操作，不声称完成真实抓取效果验收。

## 安装与验证

在实际 LeRobot 环境执行：

```bash
/home/legion/miniconda3/envs/lerobot/bin/python -m pip install -e /home/legion/rebot_web/plugins/lerobot_camera_rebot_shared --no-deps --no-build-isolation
```

双臂 gripper limit 补丁位于 `vendor_patches/bi_gripper_limits.patch`，已应用到当前 editable B601 插件：配置 dataclass 接受左右 `force_pos_torque_ration`，并传给两个实际子臂。

后端回归：`python -m unittest discover -s tests -v`。浏览器回归：`node tests/browser/persistent-workspace.cjs`（所有运动 API 拦截，不移动真机）。`tests/vision_native_smoke.py` 是显式的摄像头读取测试，验证深度毫米格式、共享 PID 及重启恢复，不连接机械臂。
