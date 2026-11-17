# 当前采用：官方 Wall-X 原生链路

根据你提供的 `workspace/README.md` 和 `workspace/rtx5090/DEPLOY.md`，训练、保存和推理统一使用 **Wall-X 1.1.0 原生源码**。这是本项目当前推荐入口。此前 `train_remote.py / export_model.py / validate_model.py / requirements-train.txt` 对应另一套 LeRobot 原生 `wall_oss_05` 格式，保留备用，**不要与本流程混装或混用权重**。

固定源码版本 `ce2a1ae31d9ee1028b97ca40e3f66034cf8c0e5f`，与本机 `/home/legion/wall-x` 核对一致。训练在远端8×4090D、128GB内存服务器；本机5090 Laptop 24GB负责服务与验证。文档中桌面5090 32GB的0.2秒参考结果不是本机实测。下面命令未在远程服务器执行；本次未改本机环境、安装编译依赖或启动模型/机械臂。

| 文件 | 执行位置 | 用途 |
|---|---|---|
| native_train.py | 训练服务器 | 数据/基座预检查、生成配置、官方统计计算、八卡FSDP训练 |
| native_train_entry.py | torchrun 自动调用 | 沿用原生训练器，保证到步数上限后全rank保存末次模型与交付引用 |
| native_export.py | 训练服务器 | 导出完整原生权重、配置、统计与分词器文件 |
| native_serve.py | 推理设备 | 原生模型服务＋严格双臂关节协议，无机械臂控制器 |
| native_openloop.py | 推理设备 | 沿用官方开环评估，适配关节输入、相机键、关节轴标签 |
| native_common.py / contract.py / camera_mapping.json | 两端随入口一起带走 | 数据及交付契约 |

## 双臂数据契约

本项目采集的每个7维向量是**6个臂关节＋夹爪**，不是 XYZ＋RPY＋夹爪。

```yaml
task:
  dof_config:
    follow_left_arm_joint_pos: 7
    follow_right_arm_joint_pos: 7
    action_padding: 12
  ar_dof_config:  # 同上，保持顺序
    follow_left_arm_joint_pos: 7
    follow_right_arm_joint_pos: 7
    action_padding: 12
  agent_pos_config:
    follow_left_arm_joint_pos: 7
    follow_right_arm_joint_pos: 7
    action_padding: 12
```

关节顺序每侧固定：shoulder_pan、shoulder_lift、elbow_flex、wrist_flex、wrist_yaw、wrist_roll、gripper。训练器保留14维原数据，补12维到26维，补齐部分不参与动作损失。本机服务也把补齐部分的dof_mask设为0；缺失关节或相机直接报错，不补零角度、不补黑图。

状态单位是原始从臂电机角度；动作单位是从臂方向/夹爪齿比转换前的示教命令角度，和现有采集数据一致。推理返回的动作不能直接视为电机实测角度，也不自动做FK、IK、零点校准或安全运动规划。

三相机使用 camera_mapping.json：`left_深度相机 → face_view`、`left_左 → left_wrist_view`、`left_右 → right_wrist_view`；核对实物位置后可编辑映射。这里只读取RGB，不因相机名称而假定具备深度。

## 1. 更新训练代码包

在本机上传新的 `wall_x_native_remote_training.zip`。远端解压到 `/home/trainer`，形成 `/home/trainer/wall_oss_05/`；旧代码仍保留，但之后使用 **native_*.py**。完整数据集另放 `/home/trainer/all_1`，包含meta、parquet和三路视频。服务器路径不能填成本机路径。

## 2. 只在训练服务器建立原生环境

```bash
conda create -n wallx-train python=3.10 -y
conda activate wallx-train

git clone https://github.com/X-Square-Robot/wall-x.git /home/trainer/wall-x-native
git -C /home/trainer/wall-x-native checkout ce2a1ae31d9ee1028b97ca40e3f66034cf8c0e5f

python -m pip install --upgrade pip
python -m pip install torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r /home/trainer/wall-x-native/requirements.txt
python -m pip install -r /home/trainer/wall_oss_05/requirements-native-data.txt
python -m pip install 'dmuon @ git+https://github.com/X-Square-Robot/dmuon.git'
python -m pip install --no-deps lerobot==0.4.4

conda install -p "${CONDA_PREFIX}" -y -c conda-forge cuda-nvcc=12.8 cuda-cudart-dev=12.8
export CUDA_HOME="${CONDA_PREFIX}"
export PATH="${CUDA_HOME}/bin:${PATH}"
export CPATH="${CUDA_HOME}/targets/x86_64-linux/include:${CPATH:-}"

# 训练机是4090D，不要照抄5090推理机的 FLASH_ATTN_CUDA_ARCHS=120。
# 默认编译目标包含sm80等目标；Wall-X算子按训练机可见GPU编译。
unset FLASH_ATTN_CUDA_ARCHS
MAX_JOBS=4 python -m pip install flash-attn==2.8.3 --no-build-isolation
cd /home/trainer/wall-x-native
MAX_JOBS=4 python -m pip install --no-build-isolation --no-deps -e .
python -m pip freeze > /home/trainer/wallx-train-resolved.txt
```

LeRobot 0.4.4 的依赖声明中Torch/torchvision/TorchCodec上界早于上游Wall-X推荐的2.10组合，huggingface-hub上界也早于Transformers5所需版本，所以按原生指南用 `--no-deps` 安装LeRobot。此组合不是LeRobot0.4.4全栈依赖一致的通用环境，`pip check` 会报告这些声明冲突；不要让resolver自动把Torch降级。新增数据依赖覆盖本流程的导入需求，最终仍须以环境导入、三相机数据读取、GPU前后向及保存冒烟验证为准。本次没有实际安装验证该远程环境。

## 3. 在训练服务器下载原始基座与processor

使用 **X-Square-Robot/wall-oss-0.5**，不是此前LeRobot转换仓库：

```bash
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('X-Square-Robot/wall-oss-0.5', local_dir='/home/trainer/models/wall-oss-0.5')
snapshot_download('Qwen/Qwen2.5-VL-3B-Instruct',
                  local_dir='/home/trainer/models/Qwen2.5-VL-3B-Instruct',
                  allow_patterns=['*.json', '*.txt', '*.model', '*.tiktoken'],
                  ignore_patterns=['model.safetensors.index.json'])
PY
```

Qwen目录用于processor/分词器，不下载第二份VLM权重。基座原始权重约17GB；完整训练检查点还带优化器与分片状态，磁盘需要按多个检查点预留，不只按单份权重大小计算。

## 4. 预检查与10次更新冒烟

```bash
cd /home/trainer/wall_oss_05
python native_train.py \
  --wallx-dir /home/trainer/wall-x-native \
  --dataset-dir /home/trainer/all_1 \
  --base-model /home/trainer/models/wall-oss-0.5 \
  --processor-dir /home/trainer/models/Qwen2.5-VL-3B-Instruct \
  --output-dir /home/trainer/runs/wall_native_smoke \
  --steps 10 --dry-run
```

将 `--dry-run` 换成 `--check` 核对环境，不下载/训练。两者均不创建输出目录。检查通过后删掉这个选项，开始八卡训练：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python native_train.py \
  --wallx-dir /home/trainer/wall-x-native \
  --dataset-dir /home/trainer/all_1 \
  --base-model /home/trainer/models/wall-oss-0.5 \
  --processor-dir /home/trainer/models/Qwen2.5-VL-3B-Instruct \
  --output-dir /home/trainer/runs/wall_native_smoke \
  --steps 10
```

入口先调用官方 `compute_norm_stats.py` 计算本数据统计，再用官方FSDP训练器。默认每卡batch1、累积4、BF16、full_shard和梯度检查点，实际内存仍须冒烟测量；OOM时可尝试 `--cpu-offload`。有效batch32。**这里的steps是原生Wall-X优化器更新次数**，不同于备用LeRobot新版本的microbatch循环语义。

按剧集90%/10%训练验证；统计脚本按整个数据集计算分位数，因此严格独立的最终验收还需要另一份完全不参与统计和训练的数据。任务指令沿用原数据task，不会自动为“快递”补写四场景动作、语言标签或面单朝上证据。原生迭代到更新上限会跳过epoch末保存，native_train_entry.py只在成功返回后补上一次全rank保存，不在异常时伪造已完成模型。

检查loss、显存、验证和最后检查点后，正式训练使用新输出目录及 `--steps 2000` 起步。默认每1000次更新保存，并最终保存；不自动反复保存每个epoch的重复全量文件。可以用 `--save-every` 调整。追加数据时，`--base-model` 指向导出的原生模型目录，重新生成统计和新的输出；不是恢复旧优化器。

## 5. 在训练机导出

从 `/home/trainer/runs/wall_native_smoke/checkpoints/` 选择刚完成的目录（名称是 `epoch_step`，例如 `0_10`，实际epoch以日志为准）：

```bash
python native_export.py \
  --checkpoint /home/trainer/runs/wall_native_smoke/checkpoints/0_10 \
  --output-dir /home/trainer/exports/wall_parcel_native
```

导出保留完整 `model.safetensors`、`config.json/config.yml`、本数据 `norm_stats.json`、分词器全部相关文件（包括merges.txt）、dataset_reference.json。代码检查关节布局、三相机和统计，`norm_key`固定本项目 **rebot_parcel**。若使用原生分片保存，先在训练机用官方 `merge_sharded_weights.py` 合并，再把**同次检查点**的配置、统计和引用一并补齐；只合并权重不构成交付。当前默认full策略避免额外合并步骤。

把整个导出目录复制到本机，例如 `/home/legion/models/wall_parcel_native`。

## 6. 本机仅做推理和开环验证

本机使用独立 `wallx` 环境，按你提供的RTX5090指南安装原生依赖、CUDA12.8编译工具链和flash-attn。本次没有自动执行安装；不要升级现有CAN/ACT控制环境。上游 `run_server.sh` 默认ex001末端位姿并启用序列化，本项目改用下列包装入口：

```bash
python native_serve.py \
  --wallx-dir /home/legion/wall-x \
  --checkpoint /home/legion/models/wall_parcel_native --dry-run

python native_serve.py \
  --wallx-dir /home/legion/wall-x \
  --checkpoint /home/legion/models/wall_parcel_native
```

默认只监听127.0.0.1:44660，基础路径先验证；之后同命令加 `--accelerated` 启用CUDA Graph和实验引擎。不会把加速开关当作已验证延迟。禁用fast preprocessing和模型外平滑，方便比较原始输出。服务只加载模型、不打开串口、不发送机械臂命令。迁移到本机时临时重定向模型/processor路径，不改交付文件或训练溯源。

请求使用以下关节字段，**不要发送follow*_pos**：

```python
{
    'state': {'follow1_joints': [左7个实测电机角度], 'follow2_joints': [右7个实测电机角度]},
    'views': {'camera_front': '顶部RGB的base64 JPEG',
              'camera_left': '左RGB的base64 JPEG', 'camera_right': '右RGB的base64 JPEG'},
    'instruction': '与训练任务对应的指令',
}
```

返回 `predict_action` 是32步、26维的原始动作块，前14维是已反归一化的示教命令角度，尾12维舍弃；不能把末端坐标序列当成此结果。任何动作执行还必须经过独立控制进程的方向/齿比换算、限位、限速、力矩、心跳和同步慢速收工。

另开终端（可用现有LeRobot数据环境，它只作为WebSocket客户端读离线数据，不加载Wall-X模型）：

```bash
python native_openloop.py \
  --wallx-dir /home/legion/wall-x \
  --uri ws://127.0.0.1:44660 \
  --dataset-root /home/legion/.cache/huggingface/lerobot/all_1 \
  --train-config /home/legion/models/wall_parcel_native/config.yml \
  --episode-indices 0 --max-inferences 5 --save-dir /tmp/wall_parcel_openloop
```

包装沿用官方评估流程，修正其“14维默认当XYZ/RPY”的输入假设，使用真实三路画面、关节名称和command deg轴标题；不把缺失相机补成黑图。图和误差只说明开环拟合，不能证明实机能完成拆堆叠、翻面、盒袋分流和面单朝上核验。接入现有Web/独立CAN闭环仍是后续工作，当前ACT执行器不能直接填入此模型。

原始依据：[官方训练指南](https://github.com/X-Square-Robot/wall-x/blob/ce2a1ae31d9ee1028b97ca40e3f66034cf8c0e5f/workspace/README.md)、[RTX5090部署](https://github.com/X-Square-Robot/wall-x/blob/ce2a1ae31d9ee1028b97ca40e3f66034cf8c0e5f/workspace/rtx5090/DEPLOY.md)。本项目的关节适配、最终保存及开环包装是新增代码，尚未用原生大模型完成远程训练或本机端到端推理实测。
