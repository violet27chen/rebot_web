# rebot_web：智能机械臂快递物料分拣系统

基于双臂 reBot B601-DM 的快递物料分拣系统。系统在桌面上识别快递（盒、袋），由机械臂完成对应操作，**让面单朝上，再把快递向左推走**；其余快递留在桌面上等待处理。

## 分拣目标

1. 快递留在工作区，由视觉逐个识别。
2. 根据快递类型（盒 / 袋）和位置，选择对应的操作技能。
3. 操作结果必须是**面单朝上**。面单朝下时先翻面，再继续。
4. 面单朝上、单号可读后，把快递**向左推走**。

## 处理流程

```
YOLO 识别 → 深度定位 → 机械臂移到包裹上方（爪头朝下，面单可见）
          → 读取单号 → 选择 ACT 技能 → 机械臂自动完成动作 → 视觉复检
```

| 阶段 | 说明 | 代码 |
| --- | --- | --- |
| YOLO 识别 | 检测快递类别和 2D 框 | `backend/sorting/inference_worker.py` |
| 深度定位 | RGB-D 点云得到包裹三维位置、高度和范围 | `backend/sorting/geometry.py` |
| 悬停 | 单臂移到包裹上方，爪头朝下，由 IK 和路径检查保证安全 | `backend/sorting/service.py` |
| 读取单号 | 条码 / 二维码解码，清晰度判断，单号到目的地规则 | `backend/sorting/label.py` |
| ACT 动作 | 按类别选定技能，由独立控制进程限幅执行 | `runner.py`、`control_worker.py` |
| 复检 | 执行后重新检测，确认目标位置 | `runner.py` |

## 当前完成度

- 已完成：RGB-D 空间标定（含逐点残差和删除异常点）、YOLO + 深度定位、单号扫描接口、ACT 执行器及安全看门狗、遥操作 / 采集 / 评估 / 训练页面。
- 未完成：「面单朝上」的判定与翻面决策、把单号扫描接入运行流程、ACT 完成判据与失败重试。目前只能扫描单号，还不会自动判断面单朝向。

## 安全约束

- 所有电机命令只由独立的 CAN 控制进程发出，视觉和推理进程不接触电机总线。
- 电机控制模式必须实时回读确认后才允许使能，缺少反馈时拒绝运行。
- 有浏览器心跳、力矩、温度看门狗，动作跳变和关节越限会被拒绝。
- 局域网模式（`--lan`）目前没有鉴权，只在可信网络使用。

## 运行

```bash
pip install -r requirements.txt
python start.py            # 仅本机 127.0.0.1
python start.py --lan      # 局域网（无鉴权，谨慎使用）
```

首次使用前需要：

- 自行创建 `backend/config.json`（含串口、相机等本机配置，不在仓库中）。
- 单独获取 `rebot_grasp/sdk/`（第三方 SDK，不在仓库中）。
- 在「RGB-D 空间标定」页面完成相机内参、对齐、工作台和左右臂基座标定。
- 在 `sorting` 配置里登记 YOLO 模型、ACT 技能，以及单号规则 `label_rules`：`[{"pattern": "SF\\d+", "destination": "A区"}]`。

## 测试

```bash
PYTHONPATH=. python tests/test_sorting_rgbd.py
PYTHONPATH=. python tests/test_sorting_label.py
```

## 目录

- `backend/`：FastAPI 后端、视觉与硬件 worker、`sorting/` 分拣模块
- `frontend/`：网页界面
- `desktop/`：Electron 桌面壳
- `plugins/`：LeRobot 共享相机插件
- `vendor_patches/`：针对 B601 驱动的补丁
- `docs/`：设计与分析文档
