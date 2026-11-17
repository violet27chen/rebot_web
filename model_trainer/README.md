# 模型训练师交付目录

WALL-OSS-0.5 当前采用官方 Wall-X 原生链路，远程八卡训练、模型导出和推理设备验证见 [wall_oss_05/NATIVE_WALLX.md](wall_oss_05/NATIVE_WALLX.md)。它使用独立环境与依赖；下文 `train.py` 继续用于 ACT，两个模型的检查点不能互换。

`train.py` 是训练主入口；`export_handoff.py` 是关联的技能入口数据导出工具。不依赖 rebot_web、不连接机械臂。复制整个文件夹即可交接代码；Python 环境仍需本目录 requirements.txt 中的 LeRobot / ACT 依赖。

```bash
python train.py --list-datasets
python train.py --dataset-dir /data/push --dry-run
python train.py --dataset-dir /data/push --steps 20000 --output-dir /models/push_v1
python train.py --dataset-dir /data/push_more --base-model /models/push_v1 --steps 5000
python train.py --resume /models/push_v1 --steps 30000
python export_handoff.py --dataset-dir /data/push --skill push --output push_entry.json
```

`--base-model` 是使用旧权重加新数据微调，建立新输出目录；`--resume` 使用原检查点及优化器状态续跑，steps 为目标总步数。不会为训练直接连接硬件。相机键、尺寸、14维状态与动作契约都必须匹配，不能猜测 left_左 与实际左相机的对应关系。

数据通过 --dataset-dir 引用采集师的完整目录，不复制数据到代码目录。DATASET_REFERENCES 可维护本机别名；它不是跨机器必然存在的路径。训练保存的 dataset_reference.json 包含数据路径、元数据哈希、动作/状态名称及模型来源。

交给执行人员的文件必须包含整个 pretrained_model 目录：config.json、model.safetensors、预/后处理器配置及其 safetensors 统计文件，另外附 dataset_reference.json 和对应的技能入口 JSON。只发 model.safetensors 无法保证归一化和相机映射正确。

`export_handoff.py` 只读每个剧集的起始实测状态，输出 skill_entries。把对应条目合并进执行人员的 sorting.json；范围表示训练中的入口分布，不能保证任意中间姿态都能接模型，也不能通过随意扩大容差绕过交接。各技能继续独立采集、独立训练。

`python verify_bundle.py` 检查代码交付完整性；dependency_versions.json 记录本次导出环境，不包含数据或模型权重。
