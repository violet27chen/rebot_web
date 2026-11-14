# 当前项目重新分析（2026-11-14）

实际入口 `/home/legion/rebot_web` 指向 `/home/legion/rebot_source_20261113/rebot_web`；不是原回收站路径。本目录没有 Git 历史，近期变更判断基于当前源码、现有配置及已安装 editable 插件，不能等同 Git diff。当前版本 3.4.0+20261114.1。配置可能包含 API 密钥，本报告不复制配置值。

## 运行结构

Electron 桌面壳 → FastAPI 服务 → 常驻父工作台 + 功能 iframe。父工作台负责所有页面共享的相机/深度、官方 URDF 场景、命令输入及收工入口。Web 与摄像头由独立视觉进程通过 RPC 共享；LeRobot 相机插件通过本机无损流取帧。遥操作、采集、评估、训练及显式零点/收工各自作为任务进程运行。真实硬件驱动还依赖目录外的 editable B601 插件与 motorbridge，不能只检查本项目。

当前修改和验证边界见 [3.4 说明](GLOBAL_CONTROLS_3_4.md)，深度只读分析见 [深度相机分析](DEPTH_CAMERA_ANALYSIS_20261114.md)。

## 与比赛目标仍有差距

- 没有完整实现 SCAN_WORKSPACE → SKILL_PUSH → 盒/袋翻面 → 扫码 → 最终分流的业务状态机；协作模板目前只是计时与说明。
- 日志的进度心跳不等于控制链路的断网停机看门狗。夹爪力矩比例与温度保护不等于覆盖全臂接触状态的力矩看门狗。
- LeRobot 作业虽然与 Web 分进程，但其内部 CAN 控制与策略/观测没有普遍拆成独立控制进程和 IPC；B601-DM 的现有遥操作分支仍没有所要求的重力前馈。历史抓取控制器有前馈，不代表全平台已具备。
- 独立技能的数据目录、任务字符串和训练入口存在，但还没有强制阻止跨技能混采混训。
- 当前双臂基座共同坐标未配置；显示的并排布局不能当作已测量真实距离。
- 桌面层新增 Windows 本机控制声明，但视觉 RPC、反馈和设备释放仍有 Linux 专用实现；完整 Windows 控制支持尚未完成。这也与最初 Ubuntu 唯一控制底座的约束不同。
- `main.py` 仍集中大量路由，历史硬件脚本和视觉项目并存；多处 Python/SDK 路径硬编码。
- `/api/config` 返回完整配置；当前默认回环监听，但开放局域网时现有 API 缺少控制权限隔离。
- 策略相机名回退匹配中整数 0 的处理存在边界错误，稳定设备路径也不能仅靠 camN 数字规则匹配。

这些是后续范围，不能因本次 UI 与启动故障修正就宣称完成全自主比赛系统。

## 业务代码逐文件职责索引

索引列出本体、前端、插件、桌面和测试文件；第三方 SDK、二进制库、模型和生成物另作目录级说明，不宣称已逐行审计所有第三方文件。

| 文件 | 行数 | 职责 / 检查边界 |
| --- | ---: | --- |
| `backend/arm_daemon.py` | 287 | 历史常驻手动控制工具；页面已移除 |
| `backend/arm_move_test.py` | 65 | 会在模块顶层连接硬件的手动脚本，不能当作普通测试导入 |
| `backend/arm_worker.py` | 160 | 历史单次手动动作工具；不是全局收工入口 |
| `backend/camera_manager.py` | 260 | RGB 设备标识、共享采集、格式与引用计数 |
| `backend/config.json` | — | 本机配置与密钥；只分析结构，不复制内容 |
| `backend/depth_manager.py` | 87 | 深度线程、原始 PNG 与显示 JPEG、有效性与时间戳 |
| `backend/device_release.py` | 113 | 枚举占用设备的真实进程并释放 |
| `backend/dual_probe.py` | 184 | 插件注册、Draccus 类型探测 |
| `backend/feedback_observer.py` | 46 | 空闲时只请求 CAN 反馈 |
| `backend/grasp_runtime.py` | 90 | 历史官方轨迹控制扩展与限力夹爪 |
| `backend/grasp_worker.py` | 106 | 历史 FK/IK/抓取子进程；页面已移除 |
| `backend/gripper_open.py` | 47 | 手动夹爪工具 |
| `backend/gripper_open_force.py` | 45 | 手动限力开爪工具 |
| `backend/gripper_test.py` | 41 | 会在模块顶层驱动夹爪的手动脚本 |
| `backend/grounding_dino_worker.py` | 43 | 独立本地视觉检测进程 |
| `backend/main.py` | 5979 | FastAPI 路由、配置、任务与日志、数据集和训练分发（当前仍偏集中） |
| `backend/monitor_geometry.py` | 26 | 共同坐标与实测基座中心距校验 |
| `backend/motor_mode.py` | 23 | 丢失 ACK 后的受限模式回读校验 |
| `backend/openni_depth.py` | 118 | OpenNI C ABI、深度模式与毫米帧读取 |
| `backend/orbbec_snapshot.py` | 45 | 历史独立 Orbbec 快照工具，不应另开设备与共享视觉争用 |
| `backend/raw_frames.py` | 10 | 无损 RBFR 图像帧打包 |
| `backend/safe_record.py` | 75 | 录制保持姿态的收尾包装 |
| `backend/shutdown_worker.py` | 171 | 独立收工、分阶段回零、确认失能 |
| `backend/telemetry.py` | 100 | 关节反馈收集、订阅推送与空闲观察器 |
| `backend/telemetry_client.py` | 25 | 控制进程发送实测反馈 |
| `backend/trainers/__init__.py` | 10 | 模块包入口 / 支持文件 |
| `backend/trainers/opencv_trainer.py` | 194 | OpenCV 特征与分类器训练进程 |
| `backend/trainers/yolo_trainer.py` | 200 | YOLO 训练进程 |
| `backend/vision_broker.py` | 56 | 视觉进程内 RPC 服务 |
| `backend/vision_process.py` | 122 | 独立视觉进程客户端与进程生命周期 |
| `backend/zero_adjust_worker.py` | 96 | 独立读角度、释放、设零和参数持久化 |
| `desktop/renderer/launcher.css` | 2 | 响应式界面样式 |
| `desktop/renderer/launcher.html` | 9 | 对应功能页面 / 父壳模板 |
| `desktop/renderer/launcher.js` | 23 | 连接设置界面 |
| `desktop/src/backend.cjs` | 38 | 桌面连接/启动控制后端 |
| `desktop/src/main.cjs` | 62 | Electron 窗口、菜单和加载 |
| `desktop/src/preload.cjs` | 10 | 受限 IPC 桥 |
| `desktop/src/settings.cjs` | 13 | 连接偏好持久化与校验 |
| `desktop/test/settings.test.cjs` | 7 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `frontend/console.html` | 13 | 对应功能页面 / 父壳模板 |
| `frontend/index.html` | 144 | 对应功能页面 / 父壳模板 |
| `frontend/pages/camera.html` | 56 | 对应功能页面 / 父壳模板 |
| `frontend/pages/connect.html` | 75 | 对应功能页面 / 父壳模板 |
| `frontend/pages/dataset.html` | 124 | 对应功能页面 / 父壳模板 |
| `frontend/pages/dual.html` | 1 | 对应功能页面 / 父壳模板 |
| `frontend/pages/eval.html` | 51 | 对应功能页面 / 父壳模板 |
| `frontend/pages/home.html` | 67 | 对应功能页面 / 父壳模板 |
| `frontend/pages/logs.html` | 44 | 对应功能页面 / 父壳模板 |
| `frontend/pages/setup.html` | 79 | 对应功能页面 / 父壳模板 |
| `frontend/pages/teleop.html` | 59 | 对应功能页面 / 父壳模板 |
| `frontend/pages/train.html` | 169 | 对应功能页面 / 父壳模板 |
| `frontend/pages/viewer.html` | 49 | 对应功能页面 / 父壳模板 |
| `frontend/pages/yolo.html` | 169 | 对应功能页面 / 父壳模板 |
| `frontend/static/css/style.css` | 1136 | 响应式界面样式 |
| `frontend/static/js/STLLoader.js` | 371 | 第三方模型显示库 |
| `frontend/static/js/URDFLoader.js` | 1426 | 第三方模型显示库 |
| `frontend/static/js/arm3d.js` | 164 | 官方 URDF 加载、单双臂共同场景与实测角度更新 |
| `frontend/static/js/autosave.js` | 198 | 差量保存、写队列、草稿、配置修订同步 |
| `frontend/static/js/camera.js` | 254 | 相机列表、配置、预览和诊断 |
| `frontend/static/js/collection-plan.js` | 98 | 协作说明与计时；没有驱动比赛状态机 |
| `frontend/static/js/common.js` | 2303 | 共享壳、API、状态、日志与确认弹层 |
| `frontend/static/js/connect.js` | 179 | 设备模式、端口、插件、连接及校准 |
| `frontend/static/js/dataset-features.js` | 17 | 数据动作维数与相机特征检查 |
| `frontend/static/js/dataset.js` | 295 | 独立采集配置与数据管理 |
| `frontend/static/js/eval.js` | 110 | 策略评估设置与启动 |
| `frontend/static/js/home.js` | 111 | 平台摘要 |
| `frontend/static/js/job-console.js` | 93 | 全局输入提示与二次确认后的收工入口 |
| `frontend/static/js/logs.js` | 86 | 任务日志与训练曲线 |
| `frontend/static/js/monitor.js` | 146 | 常驻 RGB/深度预览、单场景模型、放大与布局 |
| `frontend/static/js/setup.js` | 163 | 环境安装与选择 |
| `frontend/static/js/teleop.js` | 108 | 遥操作与异步推理入口 |
| `frontend/static/js/three.min.js` | 6 | 第三方模型显示库 |
| `frontend/static/js/train.js` | 609 | ACT / YOLO / OpenCV 训练设置 |
| `frontend/static/js/viewer.js` | 224 | 多相机剧集回放、同步、放大与轨迹图 |
| `frontend/static/js/wizard.js` | 304 | 首次环境与设备向导 |
| `frontend/static/js/workspace.js` | 78 | 父工作台、页面导航、保存确认与常驻监视 |
| `frontend/static/js/yolo.js` | 916 | 视觉数据、标注与预标注 |
| `frontend/static/js/zero-adjust.js` | 51 | 设备页零点调整与操作结果 |
| `plugins/lerobot_camera_rebot_shared/lerobot_camera_rebot_shared/__init__.py` | 147 | LeRobot 共享相机协议 / 实测反馈挂钩 / 插件包元数据 |
| `plugins/lerobot_camera_rebot_shared/lerobot_camera_rebot_shared/feedback.py` | 22 | LeRobot 共享相机协议 / 实测反馈挂钩 / 插件包元数据 |
| `plugins/lerobot_camera_rebot_shared/pyproject.toml` | 12 | LeRobot 共享相机协议 / 实测反馈挂钩 / 插件包元数据 |
| `tests/browser/annotation-queue.cjs` | 4 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/browser/autosave.cjs` | 27 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/browser/functional-pages.cjs` | 7 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/browser/global-controls.cjs` | 150 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/browser/persistent-workspace.cjs` | 37 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/camera_consumers_smoke.py` | 23 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_camera_manager.py` | 134 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_device_release.py` | 95 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_global_controls.py` | 230 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_grasp_workflow.py` | 64 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_viewer_multicamera.py` | 62 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/test_workflow_config.py` | 120 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |
| `tests/vision_native_smoke.py` | 35 | 自动检查；硬件 smoke 脚本不在本次实际运行范围 |

## 其他目录与文件

- `start.py`、`start_windows.bat`：平台启动与依赖引导。`requirements.txt`：直接 Python 依赖；LeRobot、硬件插件和训练依赖由对应安装流程补充。`VERSION`：发布版本。
- `desktop/package.json`、`package-lock.json`、`build-icon.cjs`、`build/`、`README.md`：桌面打包元数据与图标。当前目录没有新编译的 deb/exe，已有桌面客户端使用服务端新界面。
- `frontend/static/arm_urdf/`：官方 URDF、重复存放的 URDF/CSV、8 个 STL 零部件。单双臂场景沿用这些文件，没有替换自制模型。`frontend/static/img/`：品牌和离线相机资源。
- `vendor/openni2/`：已安装 Orbbec OpenNI 运行库与配置；二进制不做源码审计。`vendor_patches/`：双臂夹爪参数及 B601 启动模式回读补丁。
- `rebot_grasp/drivers/`：相机抽象、RealSense / Orbbec 驱动和旧机器人驱动。`calibration/`：ArUco 与手眼标定。`utils/`：短轴/轮廓/深度抓取估计、坐标变换和模型加载。
- `rebot_grasp/scripts/`：另一个抓取 Web、GraspNet、短轴抓取、标定采集、API 客户端和硬件核查脚本。它们有独立相机/控制入口，不代表已经接入当前比赛工作流；未经用户启动不运行。
- `rebot_grasp/sdk/reBotArm_control_py`：官方控制、运动学、URDF 和示例；`graspnetAPI` / `graspnet-baseline`：点云抓取与 CUDA 扩展；`pyorbbecsdk`：另一套 Orbbec SDK 包装与示例。按依赖边界分析，不混用设备所有权。
- `outputs/`、`yolo_datasets/`、`rebot_grasp/models/`、`Log/`：训练结果、视觉数据、权重及运行日志，属于数据/生成物。
- `docs/`：各次变更说明与历史截图；旧说明不应优先于当前源码和设备信息。
- `.tmp_plugin*`、`.tmp_orbbec_test.py`：历史实验；不是稳定运行入口。`_wiki_cn.html`：缓存参考资料。`.vscode/`、`.workbuddy/`：编辑器配置。

## 下一阶段顺序

先把 Ubuntu 上双臂启动/断开、显式零点与收工流程稳定下来；完成共同坐标及 RGB-D 标定条件分析；然后实现可检查的工作区状态与规则推拢；再分别接入盒、袋翻面技能和扫码状态机。控制进程隔离、重力前馈与真正的安全看门狗应作为启用自主动作前的独立工程任务。

## 附：历史视觉项目逐文件索引

下列文件按源码入口与说明核对；硬件核查脚本未运行。

| 文件 | 职责说明 |
| --- | --- |
| `rebot_grasp/scripts/collect_handeye_eih.py` | Eye-in-Hand 手眼标定 — 数据采集与计算（Gemini2 + reBotArm） |
| `rebot_grasp/scripts/grasp.py` | grasp.py - 基于 GraspNet 的机械臂视觉夹取主程序 |
| `rebot_grasp/scripts/grasp_api_client.py` | grasp_api_client.py |
| `rebot_grasp/scripts/grasp_web.py` | Local web UI for YOLO target selection and GraspNet grasp-point preview. |
| `rebot_grasp/scripts/graspnet_camera_demo.py` | Live RGB-D GraspNet demo for rebot_grasp cameras. |
| `rebot_grasp/scripts/main.py` | main.py — 基于短轴估计的机械臂夹取主程序 |
| `rebot_grasp/scripts/object_detection.py` | 统一版 3D 视觉目标检测管线 (支持 D435i / Gemini 2 双相机接入) |
| `rebot_grasp/scripts/ordinary_grasp_pipeline.py` | 简化版夹取姿态测试脚本 |
| `rebot_grasp/scripts/verify_graspnet_stack.py` | Verify the local runtime needed by the rebot_grasp GraspNet demos. |
| `rebot_grasp/scripts/verify_handeye_calibration.py` | Print and sanity-check a saved hand-eye calibration file. |
| `rebot_grasp/scripts/verify_pyorbbec_stream.py` | Verify that pyorbbecsdk can open an Orbbec camera and stream RGB-D frames. |
| `rebot_grasp/scripts/verify_rebot_arm_motion.py` | Verify reBot arm connection with a small joint6 jog and return. |
| `rebot_grasp/drivers/__init__.py` | 包入口 / 历史辅助脚本 |
| `rebot_grasp/drivers/camera/__init__.py` | 包入口 / 历史辅助脚本 |
| `rebot_grasp/drivers/camera/base.py` | 相机驱动基类。 |
| `rebot_grasp/drivers/camera/orbbec_gemini2.py` | Orbbec Gemini 2 相机驱动。 |
| `rebot_grasp/drivers/camera/realsense.py` | Intel RealSense 相机驱动（D435i / D405 等）。 |
| `rebot_grasp/drivers/robot/__init__.py` | 包入口 / 历史辅助脚本 |
| `rebot_grasp/drivers/robot/rebot_arm.py` | cameraws.drivers.robot.rebot_arm |
| `rebot_grasp/utils/__init__.py` | Utility helpers for cameraws. |
| `rebot_grasp/utils/ordinary_grasp.py` | Utilities for simple OBB/min-area-rect grasp estimation. |
| `rebot_grasp/utils/transforms.py` | 坐标变换工具函数。 |
| `rebot_grasp/utils/yolo_runtime.py` | YOLO runtime helpers for PyTorch and TensorRT model files. |
| `rebot_grasp/calibration/__init__.py` | 包入口 / 历史辅助脚本 |
| `rebot_grasp/calibration/aruco_pose.py` | ArUco 标记检测与位姿估计。 |
| `rebot_grasp/calibration/hand_eye.py` | 手眼标定 — 基于 OpenCV calibrateHandEye。 |
| `rebot_grasp/unused/collect_intrinsic_frames.py` | 内参标定 - 步骤1：采集棋盘格图像 |
| `rebot_grasp/unused/gen_aruco.py` | 生成 ArUco 标记图片并保存为 PNG，用于手眼标定。 |
| `rebot_grasp/unused/get_camera_intrinsics.py` | 直接读取深度相机硬件出厂内参并保存 (支持 RealSense & Orbbec) |
| `rebot_grasp/unused/run_intrinsic_calib.py` | 内参标定 - 步骤2：从已保存图像计算内参 |
| `rebot_grasp/unused/snapshot.py` | 快照式静态点云生成与可视化测试管线 (Snapshot Point Cloud Pipeline) |
| `rebot_grasp/unused/test_gemini2_distortion.py` | Gemini 2 畸变参数检验脚本 (已加入动态格式解码) |
| `rebot_grasp/unused/view_gemini2.py` | Orbbec Gemini 2 专属 3D 视觉目标检测管线 (已适配外部 YAML 与 Models 目录) |
| `rebot_grasp/unused/yolo_d435i_3d.py` | Intel RealSense D435i 专属 3D 视觉目标检测管线 (已修复 YOLOE-26 识别与沙盒路径) |
| `rebot_grasp/unused/yolo_gemini2_3d.py` | Orbbec Gemini 2 专属 3D 视觉目标检测管线 |
