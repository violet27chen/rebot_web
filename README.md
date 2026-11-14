# rebot_web：智能机械臂快递物料分拣系统

基于双臂 reBot B601-DM 的快递物料分拣系统。系统在桌面上识别快递（盒、袋），由机械臂完成对应操作，**让面单朝上，再把快递向左推走**；其余快递留在桌面上等待处理。

## 分拣目标

1. 快递留在工作区，由视觉逐个识别。
2. 根据快递类型（盒 / 袋）和位置，选择对应的操作技能。
3. 操作结果必须是**面单朝上**。面单朝下时先翻面，再继续。
4. 面单朝上、单号可读后，把快递**向左推走**。

## 处理流程

```
YOLO 识别 → 深度定位 → 机械臂移到包裹上方（爪头朝下，面单可见）→ 读取单号
   ├─ 读到单号（面单朝上）→ 向左推走 → 复检位移
   └─ 读不到 → 先重拍一次 → 仍读不到则判定面单朝下 → ACT 翻面技能 → 回到悬停重新扫描
```

| 阶段 | 说明 | 代码 |
| --- | --- | --- |
| YOLO 识别 | 检测快递类别和 2D 框 | `inference_worker.py` |
| 深度定位 | RGB-D 点云得到包裹三维位置、高度和范围 | `geometry.py` |
| 悬停 | 单臂（默认取离包裹近的一臂）移到包裹上方；接近姿态必须让爪头朝下，IK 解还要回算朝向 | `service.py`、`pushleft.py`、`kinematics_worker.py` |
| 面单朝向 | 条码 / 二维码解码 + 清晰度判断：读到 = 面单朝上；失焦 = 重拍；清晰无码 = 朝下 | `label.py`、`policy.py` |
| 翻面 | 按类别选 `role: "flip"` 的 ACT 技能，先校验路径再回到技能起始姿态，限幅执行 | `runner.py`、`skills.py` |
| 向左推走 | 一臂侧面围挡、另一臂从右侧接触后沿“左”方向推；复检沿左位移或已离开工作区 | `pushleft.py`、`cycle.py` |
| 流程编排 | 纯逻辑状态机，所有设备动作经注入接口，可脱离硬件测试 | `cycle.py` |

**安全规则**：读不到清晰单号时**绝不推走**；翻面次数、重拍次数、推拢次数都有上限，超限即停止并报错。

## 当前完成度

- 软件已实现并有单元测试（38 个用例）：RGB-D 标定（含逐点残差）、YOLO + 深度定位、单号扫描、面单朝向决策、爪头朝下校验、翻面与向左推走的流程编排与几何、ACT 执行器及看门狗。
- **尚未在真实硬件上验证**：整套流程（悬停、翻面、向左推）没有接真机运行过。首次上机请低速、清空桌面、手放急停，并先用 `inspect` 预演确认姿态与路径。
- 已知局限：面单朝向目前靠“能否读到码”判断，面单被遮挡、反光或没有条码时会被当作朝下而翻面（有次数上限）；翻面 ACT 技能需要你自己采集数据训练。

## 配置（`sorting` 段）

```json
{
  "label_rules": [{"pattern": "SF\\d+", "destination": "A区"}],
  "label_camera": "/dev/v4l/by-id/...",
  "cycle": {
    "arm": "auto",
    "approach_rpy": {"left": [3.14159, 0, 0], "right": [3.14159, 0, 0]},
    "tool_axis": [0, 0, 1],
    "max_tilt_rad": 0.35,
    "left_direction": [-1, 0, 0],
    "left_push_m": 0.12,
    "min_left_push_m": 0.08,
    "max_flips": 2
  },
  "skills": [
    {"name": "flip_box", "class_name": "box", "role": "flip", "arm": "left", "...": "ACT 检查点、相机映射、起始姿态等"}
  ]
}
```

- `left_direction` 是工作台坐标系里的水平方向，你需要按自己的坐标定义指明哪边是“左”。
- `approach_rpy` 与 `tool_axis` 必须使工具轴朝下，否则预演直接拒绝；数值要按你的末端坐标实际标定。

## 使用流程

1. 在「RGB-D 空间标定」页完成相机内参、对齐、工作台和左右臂基座标定。
2. 登记 YOLO 模型、翻面 ACT 技能、`cycle` 和 `label_rules`。
3. 调用 `POST /api/sorting/plan`，参数 `action: "cycle"`，得到整套预演。
4. `POST /api/sorting/execute`（`confirm: true`）按预演执行；单步可用 `action: "inspect"` 或 `"push_left"` 预演。

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
PYTHONPATH=. python tests/test_sorting_cycle.py
```

## 目录

- `backend/`：FastAPI 后端、视觉与硬件 worker、`sorting/` 分拣模块
- `frontend/`：网页界面
- `desktop/`：Electron 桌面壳
- `plugins/`：LeRobot 共享相机插件
- `vendor_patches/`：针对 B601 驱动的补丁
- `docs/`：设计与分析文档
