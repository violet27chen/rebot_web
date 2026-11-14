# 多相机与控制台界面修复（2026-11-13）

本次以实际运行的 `/home/legion/rebot_web` 为准修改，并把相机、界面改动同步到工作区 `/home/legion/.local/share/Trash/files/rebot_web`。两份后端原有功能不同，未用其中一份覆盖另一份。

## 相机故障与修复

现场配置为 `/dev/video2`、`/dev/video4`、`/dev/video6`。三个外部 USB 相机共用 480M USB2 总线。旧实现未在读帧前设置 FOURCC 和 FPS，内核出现 `Not enough bandwidth for new device state` / `Not enough bandwidth for altsetting 6`。显式配置 MJPG 后第三路可以正常读帧。

同时修复了以下独立问题：

- 用列表长度猜测新相机编号，可能选择已被占用的设备。
- 将路径强转数字，导致 `/dev/videoN` 或稳定路径错误回退到相机 0。
- 每次预览、抓拍或新开页面都重新打开相机，引起设备争用。
- 同步打开相机阻塞异步推流接口，失败后页面只能看到等待首帧。
- 编辑列表后增删相机丢失草稿；部分表单静默保存且吞掉失败。
- 失败占位图的 `load` 事件清除原本的错误提示。

`backend/camera_manager.py` 统一管理采集资源：一个物理设备一个采集线程，数字编号和稳定路径统一到同一个真实设备，JPEG 与帧供多个消费者复用。最后一个预览关闭后自动释放设备。设置 MJPG、尺寸与帧率后才读取第一帧；帧等待有超时，失败保留设备级错误信息。

相机预览线程只负责视觉采集。它不承担 CAN 控制，也不替代比赛控制进程或看门狗。

启动 Web 管理的遥操作、录制、评估、异步推理客户端及 SDK 相机扫描前，会释放预览设备；这些任务运行期间禁止网页重新抢占相机。任务结束后用户可以重新启动预览。外部程序仍然可能独占相机，界面会报告错误。

接口：

| 接口 | 行为 |
| --- | --- |
| `GET /api/cameras/devices` | 非流式 V4L2 能力查询，排除元数据节点，返回稳定路径与 USB 总线信息 |
| `GET /api/cameras/status` | 查询采集实例、消费者数量、帧计数、实际协商参数与错误 |
| `GET /api/camera/stream?device=…` | 共享 MJPEG 预览，兼容旧 `index=N` 参数 |
| `GET /api/camera/snapshot?device=…` | 从同一采集实例获取 JPEG，兼容旧参数 |
| `POST /api/config` | 相机名称、物理设备去重及尺寸 / FPS / FOURCC 校验，校验失败不保存 |

当前 UVC 接口提供彩色图像。Orbbec SDK 的权限安装、抓图入口保留在高级诊断中；本次没有验证深度流。

## 界面约定

- 沿用现有蓝色主题、图标库和明暗主题变量。
- 导航按工作台、设备、数据与训练、运行、工具分组；手机使用折叠导航。
- 页面内只放当前任务的操作。串口、权限、校准统一从设备页进入。
- 全局停止固定在顶部。任务日志按需打开，不再固定占用一列。
- 普通反馈使用可关闭、不会抢焦点的通知。错误也显示在对应区域。
- 确认框默认聚焦取消，Enter 只激活焦点所在按钮；Escape 关闭，Tab 保持在确认框或日志对话区内。
- 配置修改先保留草稿，明确点击保存才写入；相机页面关闭前提示未保存更改。保存期间禁止再次编辑。
- 相机卡片分别提供预览、暂停、抓拍下载、设置与移除；失败只影响对应卡片。
- 视觉画面使用 `object-fit: contain` 保留完整视野。网页传输上限为 15 FPS，设备采集 FPS 使用配置值。
- 首页面向工作流程，主机诊断信息折叠显示。
- 首页连接入口等待连接模式返回，再跳转到对应的单臂 / 双臂页面。
- 从臂控制页进入时不自动连接硬件；用户点击“连接并读取状态”后才开始状态轮询，收工后停止轮询。修复收工回调访问不存在的 `armInfo` 节点。

## 验证

新增 `tests/test_camera_manager.py`，覆盖三路并发、多个页面与抓拍共享采集、稳定路径别名、空闲释放与重连、任务独占交接、读帧错误、首帧超时、非法设备及重复配置。

```bash
/home/legion/miniconda3/envs/lerobot/bin/python -m unittest discover -s tests -p 'test_camera_manager.py' -v
```

工作区完整回归测试为 36 项：LeRobot 环境通过 32 项，4 项 SDK 测试在 rebotarm 环境单独通过。

浏览器验证使用用户确认的本机 Playwright + Chrome，仅操作相机和界面，没有发起机械臂动作。保存测试会核对前后相机配置完全一致。

正式服务真机验证结果：三路相机同时协商为 640×480 / 30 FPS / MJPG；两个浏览器页面共享三台设备，连续 45 秒帧计数持续增长，抓拍正常。暂停全部后设备释放，再次连接成功。新增与移除草稿不静默保存，确认框默认取消，保存前后原始三路配置完全一致。桌面和 390px 手机页面检查均无横向溢出与 JavaScript 错误；从臂页停留不会发起硬件请求。

截图与本机复查脚本在 [ui-review](ui-review/)。脚本需要 Node.js 与 Playwright；当前临时依赖位于 `/tmp/rebot-ui-qa/node_modules`：

```bash
NODE_PATH=/tmp/rebot-ui-qa/node_modules node docs/ui-review/verify-local.js
```

对比截图：[相机修改前](ui-review/camera-before.png)、[桌面修改后](ui-review/camera-after-desktop.png)、[手机修改后](ui-review/camera-after-mobile.png)、[首页修改后](ui-review/home-after-desktop.png)。

正式服务为用户级 `rebot-web.service`，访问 `http://127.0.0.1:8000/p/camera`。服务重启前确认没有任务和机械臂连接。
