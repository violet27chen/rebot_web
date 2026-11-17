# 第四位：单个 ACT 的快递分拣演示入口

3.5.16 将执行长度明确命名为 `n_action_steps`：`"inference": {"n_action_steps": 16, "flow_matching_steps": 0}`。模型原始 `chunk_size` 不变，每块执行长度上限为该值。0沿用检查点；旧的 `inference.chunk_size` 仍可读取，但与新字段同时出现时以 `n_action_steps` 为准。子进程重新加载检查点时也会应用执行长度设置。

3.5.15 增加 Action Chunk 和 SmolVLA Flow Matching 的推理设置。`sorting.json` 中可设置 `"inference": {"chunk_size": 0, "flow_matching_steps": 0}`；0沿用检查点，显式值分别限制在0–1000、0–50。执行步数不超过模型原有动作块/执行长度，Flow Matching 正数只适用于 SmolVLA。执行器持续使用独立进程 Async 预取，`--check-async` 使用相同参数且不打开硬件。设置采样次数不会改变模型权重或训练算法，降低次数可能影响动作质量。

3.5.14 的异步执行器新增 SmolVLA 支持：可将 complete 的模型路径换成经过验证的14维、同三相机键 SmolVLA 检查点；仍需整个权重和预/后处理器目录以及VLM配置/分词器缓存。随包默认权重继续是已有 ACT，没有复制用户新训练的 SmolVLA。新增依赖见 requirements.txt。执行器根据模型类型加载，不会把 SmolVLA 作为ACT解析；动作限位、反馈领先限制及失跟停滞保护保持启用。

主入口 `run_sorting.py`，直接把整个文件夹交给第四位。默认 execution_mode=complete，使用当前已有的 tese1 / step 020000 模型连续执行完整流程，不要求先训练多套技能，也不会为了切换视觉状态自动回零。训练数据中学到的“收回再伸出”仍可能由模型自己输出，异步推理不会自动删除这些示教动作。

当前 ACT 权重、预/后处理器配置和两份归一化统计已复制到 models/current_act，默认配置使用相对路径。模型约198MB；只发Python入口不发这些关联文件会无法启动。来源及权重哈希见 model_reference.json。训练数据没有复制进源码包。

```bash
python run_sorting.py --dry-run
python run_sorting.py --simulate
python run_sorting.py --check-async --device cuda --seconds 5
cp sorting.example.json sorting.json
# 完成现场参数后：
python run_sorting.py --config sorting.json --check
python run_sorting.py --config sorting.json --execute
```

执行台 http://127.0.0.1:8767，远程访问可加 --host 0.0.0.0。Ubuntu 是唯一硬件控制主机。--execute 后仍要在网页二次确认“开始分拣”，才连接/使能从臂；仅 --dry-run / --simulate / --check 不打开任何设备。

`--check-async` 使用随包的真实检查点、预/后处理器、独立推理进程和动作缓冲，输出延迟与等待帧统计；它只用内存里的测试画面和虚拟反馈，不连接相机或机械臂，也不需要先配置力矩阈值/Pinocchio。可加 --device cpu 检查CPU路径，但CPU的速度不能代表CUDA。此命令验证异步推理链路，不验证包裹操作成功率。

3.5.13 修正独立启动时的ACT配置注册；将物理限位校验、每帧限速、实际待发送命令的跟随偏差校验按顺序执行。远期绝对目标与当前位置相差超过30°，不再在限速前直接退出；所有关节对实际待发送命令仍保留30°跟随误差保护。故障原因会保留在执行台状态中。

## 单模型演示与外部视觉的分工

三路RGB和14维实测状态持续进入同一个 ACT，由它学习处理堆叠、翻面、盒袋和正常推走。能否处理四种情况取决于已有训练数据和闭环效果，复制代码不会额外赋予模型未学到的技能。

没有独立包裹YOLO模型时也可使用 complete 模式。此时顶部扫码只显示识别结果，画面显示 supervision=barcode_only，分拣数量为“未自动确认”；不能把扫不到单号等同于面单朝下，也不能自动证明每个出料包裹已朝上。演示应由操作员结合实时画面监督。

后续填入自训练 yolo_model 与类别映射后，视觉进程会把条码/单号与包裹框关联；未验证单号的目标接近出料区会请求保持，只有连续看到已验证目标进入出料区域才计数。视觉检测存在延迟和遮挡，软件停止不构成包裹一定不会惯性滑出工作区的物理保证。

`--simulate` 演示四种场景的辅助规则选择，不运行模型、不接设备。complete模式不会按这些规则换模型；独立技能模式 execution_mode=skills 保留为后续升级，届时需配置各自独立技能权重。

## 现场必须填写

- 左右从臂端口、三路相机键及顶部相机。默认 left_左、left_深度相机、left_右 与当前检查点一致；名称不等于实物位置，需核对 source。推荐稳定设备路径。
- 14个经过现场核对的 torque_limits_nm，以及 hardware_validated=true。没有猜测默认力矩值。控制采用官方URDF重力前馈和限幅的总PD力矩，与采集时POS_VEL不同，增益/负载跟随必须由硬件负责人验证。
- 相机中的工作区和出料区。示例图像右侧/下侧必须对应物理右侧/前方；未标定图像坐标不会被当成机器人XYZ。
- skill_entries.complete 已从训练数据 all_1 各集第一帧实测角度导出。它表示训练入口分布，不代表任意姿态可接入。更换模型后由训练师重新导出；不要扩大容差绕过检查。

模型、相机、依赖在连接从臂前检查和预热。主臂不参与自主执行，只有CAN子进程打开两个从臂串口；相机采集、ACT、扫码/可选YOLO分别运行。共享内存传递画面和14维状态。浏览器失联、过期观测、过载、异常目标触发保持；网页“收工归零并失能”或正常退出请求经实测反馈的双臂同步慢速归零。归零失败不伪报成功。保持状态不会自动更改零点。

## 关联文件与依赖

- rebot_sort_support：相机、异步ACT、动作缓冲、安全限幅、慢速归零、扫码。
- assets：直接复制的官方DM URDF、网格和硬件YAML，没有自行重画机械臂。
- models/current_act：当前模型运行所需文件；不包含训练数据、原Web配置或密钥。
- requirements.txt、dependency_versions.json、bundle_manifest.json、verify_bundle.py：环境与交付校验。

先在执行人员自己的环境安装 requirements.txt。当前 lerobot 环境未安装 pin，需要在部署环境补齐；本次没有改变正在运行采集/评估的Python环境。示例里的 depth.enabled=false 保留备用，RGB设备名不代表已有深度图。

本次已做无硬件的配置、四场景、输出确认/遮挡、限幅、真实CPU ACT推理和官方URDF重力计算验证；没有用这个新入口驱动机械臂。`python verify_bundle.py` 校验随包文件是否完整，实际启动前需完成上述现场参数。
