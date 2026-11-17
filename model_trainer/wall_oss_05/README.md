# WALL-OSS-0.5：远程八卡训练，本机推理验证

**当前方案已按你提供的官方 Wall-X 原生部署指南调整，优先阅读 [NATIVE_WALLX.md](NATIVE_WALLX.md)，使用 native_train.py / native_export.py / native_serve.py / native_openloop.py。下文保留为另一套 LeRobot 原生格式的备用说明，依赖和检查点不能与 Wall-X 原生服务混用。**

把本文件夹完整复制给训练人员。`train_remote.py` 是训练入口，关联代码、映射和两端依赖均在此目录；数据集和模型另外传输，不打包私人配置。Ubuntu 控制机不执行训练安装/训练命令。

训练机：8 × RTX 4090 D，每卡约24GB，CPU内存约128GB。默认 FSDP2 分片、BF16、每卡 microbatch 1、累积4，有效 batch 32。显存不是一块192GB；普通 DDP 不能解决单卡装不下完整训练状态的问题。此配置尚未在远程八卡机器实跑，先检查10步的显存、梯度、保存和验证再正式训练。`nvidia-smi` 的 CUDA 13.3 是驱动能力，不要求安装同号 CUDA Toolkit。

本目录采用官方 LeRobot 的 **`wall_oss_05`** 和转换后的 **`lerobot/wall-oss-0.5`**，固定源码 commit `7910c20a75834ad3fa6a9792df4d44a614d1bba6`。模型是4B参数，0.5是版本号。上游 `X-Square-Robot/wall-x` 原始权重格式、旧 `wall_x` 策略、ACT 权重不能直接混用。

## 1. 只在远程训练机安装

```bash
conda create -n walloss05-train python=3.12 -y
conda activate walloss05-train
cd /实际位置/model_trainer/wall_oss_05
python -m pip install --upgrade pip
python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements-train.txt
python -m pip check
python -m pip freeze > environment-resolved.txt
```

依赖文件固定官方源码版本，以及相配的 Torch 2.10 / torchvision 0.25 / TorchCodec 0.10；其余依赖由 pip 解析，`environment-resolved.txt` 记录真正安装的版本。视频读取明确用PyAV。不要在旧 `(lerobot)` 控制环境直接升级。推理机之后另建 `walloss05-infer`，仅安装 requirements-inference.txt 及相同 CUDA Torch 构建；此步骤暂不由本次代码自动执行。

## 2. 数据与相机

传输**已经结束采集**的完整 LeRobot v3 数据集，包括 `meta/`、`data/`、`videos/`，不要只传视频或只传parquet。训练命令中的路径必须是训练机实际路径，例如 `/data/rebot/all_1`；本机 `/home/legion/...` 不是远程数据路径。

`camera_mapping.json` 当前按已有相机键映射：

| 数据键 | WALL 视角 | 现场要核对的实际位置 |
|---|---|---|
| observation.images.left_深度相机 | face_view | 固定俯视全局 RGB |
| observation.images.left_左 | left_wrist_view | 左侧近景 |
| observation.images.left_右 | right_wrist_view | 右侧近景 |

这些名字只是已有配置，必须核对实物；“深度相机”当前用的是RGB视频，本入口不读取深度图。不能根据名字自动认定设备具备深度能力。

保留原始14维顺序：左7、右7，每侧依次底座、肩、肘、腕俯仰、腕偏航、腕旋转、夹爪。模型内部先按本数据集 q01/q99 归一化，再补零到26维，并屏蔽补齐部分的损失；输出回到14维。**补零不会处理方向、齿比、零点或单位。** 状态是从臂电机角度，动作是方向/齿比换算前的主臂命令角度；二者不能直接相减做跟随误差保护。

训练指令来自数据集已有 `task`。旧数据若只有“快递”，不会自动获得“先拆堆叠、面单朝下翻面、盒向右袋向前”的细分语言监督。单模型完整流程示教需要覆盖四种情况、不同包裹位置、过渡动作及结束条件；面单朝上/单号核验仍须在闭环中验证。增加VLA和GPU不能补出未采集的正确动作。

## 3. 预检查 → 10步冒烟 → 正式训练

以下命令均在**训练机**运行。预检查不下载模型、不开始训练：

```bash
python train_remote.py --dataset-dir /data/rebot/all_1 --output-dir /models/wall_smoke --steps 10 --dry-run
python train_remote.py --dataset-dir /data/rebot/all_1 --output-dir /models/wall_smoke --steps 10 --check
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python train_remote.py --dataset-dir /data/rebot/all_1 --output-dir /models/wall_smoke --steps 10
```

默认10%按剧集留出离线验证，每500步或冒烟末步计算验证损失，禁用仿真/实机评估与自动上传。检查8个进程均正常、loss有限、显存、保存的预/后处理器与14维配置、导出成功，再换新输出目录正式训练：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python train_remote.py --dataset-dir /data/rebot/all_1 --output-dir /models/wall_parcel_v1 --steps 10000
```

此 LeRobot 版本的 `steps` 指每个worker消耗的microbatch循环数，**不是优化器更新次数**。累积4时10000循环约2500次优化器更新。初始学习率5e-5，不随卡数自动放大。每卡batch已为1；若仍OOM可用 `--cpu-offload`，会增加CPU内存和PCIe传输；或 `--mode lora` 减少可训练参数，再做冒烟检查。不要直接加大batch。LoRA显式适配解码器语言/动作专家线性层及动作输入输出投影，冻结视觉编码器；这是本项目候选配置，效果/显存尚未实测，不宣称与全参等效。

训练输出不覆盖已有目录，不写入原数据。加新数据微调：

```bash
python train_remote.py --dataset-dir /data/rebot/additional --base-model /models/wall_parcel_export --output-dir /models/wall_parcel_v2 --steps 4000
```

这是新优化器下的权重微调，不是断点续跑。需要恢复优化器的中断续跑，请沿用原输出、数据和拓扑，直接使用官方 `torchrun --standalone --nproc_per_node=8 -m lerobot.scripts.lerobot_train --resume=true --config_path=/models/wall_parcel_v1/checkpoints/last/pretrained_model/train_config.json --steps=新的目标循环数`。本入口的外置 `wall_parcel_v1.dataset_reference.json` 可用于恢复交付引用；原生续跑后需将它复制到对应 pretrained_model/dataset_reference.json 再导出。

## 4. 在训练机导出，复制到推理机

```bash
python export_model.py --checkpoint /models/wall_parcel_v1/checkpoints/last/pretrained_model --output-dir /models/wall_parcel_export
```

CPU导出使用训练机128GB内存；LoRA在此合并，完整模型也经过统一导出。导出包含完整权重、config、预/后处理器及归一化统计、Qwen图像/分词器资源、dataset_reference.json。复制**整个导出目录**到控制机，例如 `/home/legion/models/wall_parcel_export`。导出还需要访问训练数据元数据以及训练环境缓存的基座/分词器；先完成导出再清理训练环境。仅 adapter 文件或仅一份 safetensors 不构成推理交付。

## 5. 只在推理设备验证，不驱动硬件

在本机，纯元数据检查不需要新ML环境：

```bash
python validate_model.py --model /home/legion/models/wall_parcel_export --dataset-dir /home/legion/.cache/huggingface/lerobot/all_1
```

之后在独立 `walloss05-infer` 环境运行真实离线推理：

```bash
python validate_model.py --model /home/legion/models/wall_parcel_export --dataset-dir /home/legion/.cache/huggingface/lerobot/all_1 --infer --frames 10 --report /tmp/wall_validation.json
```

脚本读取保存的视频和实测状态，使用**训练模型保存的统计**，检查输出维度、NaN/Inf，报告首轮/后续块延迟、GPU峰值分配和第一步相对示教动作的误差（命令角度）。首轮含预热；后续延迟不含相机采集、视频解码或CAN控制，不等于端到端闭环延迟。默认只测连续10帧，不代表完整任务效果；应另外使用未参与训练的各类场景验证集。元数据检查、离线误差或loss均不能代替实机包裹成功率。

现有 Web 策略评估和 sorting_operator 的异步执行器仍是 **ACT 专用**。本次交付完成训练/导出/离线验证入口，尚未将WALL接入CAN执行；不能把新权重直接填进旧ACT运行器。下一阶段需要独立推理进程适配、动作坐标转换、推理超时/限速/力矩保护和已有同步慢速收工，再进行受控实机验证。此目录所有入口均不发送任何机械臂命令。

官方依据：[WALL-OSS-0.5](https://huggingface.co/docs/lerobot/main/en/wall_oss_05)、[固定版本FSDP与steps语义](https://github.com/huggingface/lerobot/blob/7910c20a75834ad3fa6a9792df4d44a614d1bba6/docs/source/multi_gpu_training.mdx)、[PyTorch安装](https://pytorch.org/get-started/previous-versions/)、[PEFT](https://huggingface.co/docs/lerobot/main/en/peft_training)。
