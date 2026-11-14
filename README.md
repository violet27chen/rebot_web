# rebot_web：智能机械臂快递物料分拣系统

基于双臂 reBot B601-DM 的快递物料分拣系统。系统在桌面上识别快递（盒、袋），由机械臂完成对应操作，**让面单朝上，再把快递向左推走**；其余快递留在桌面上等待处理。

## 分拣方式

分拣完全依靠 ACT 策略完成：在「数据采集」页录制示教、「训练」页训练 ACT，再到「真机推理」页选择检查点执行。
已移除深度相机依赖的 RGB-D 标定、YOLO + 深度定位、规则推拢与分拣控制台；相机页的深度画面预览保留。
YOLO 页仍可用于采集、标注、训练和验证检测模型。

## 运行

```bash
pip install -r requirements.txt
python start.py            # 仅本机 127.0.0.1
python start.py --lan      # 局域网（无鉴权，谨慎使用）
```

首次使用前需要：

- 自行创建 `backend/config.json`（含串口、相机等本机配置，不在仓库中）。
- 单独获取 `rebot_grasp/sdk/`（第三方 SDK，不在仓库中）。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -p "test_*.py"
```

## 目录

- `backend/`：FastAPI 后端、视觉与硬件 worker
- `frontend/`：网页界面
- `desktop/`：Electron 桌面壳
- `plugins/`：LeRobot 共享相机插件
- `vendor_patches/`：针对 B601 驱动的补丁
- `docs/`：设计与分析文档

## 当前更新：3.5.1

RGB-D 三维包裹坐标、面单顶面 / 单号条件选择独立 ACT、从实测位置慢速收工，以及隔离运动学环境安装。入口分别位于摄像头、策略评估、环境安装；使用步骤见 [RGB-D 与慢速收工说明](docs/RGBD_PARCEL_WORKFLOW_3_5_1.md)。固定俯视尚未标定时可以检测预览，运动执行保持禁用。
