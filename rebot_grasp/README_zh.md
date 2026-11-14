# 🦾 reBot Arm B601-DM 视觉抓取演示（Jetson 版）

<p align="center">
  <img src="https://raw.githubusercontent.com/Seeed-Projects/reBot-DevArm/main/media/v1.0.png" alt="reBot Arm B601" width="600">
</p>

<p align="center">
    <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
    <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/Platform-Jetson%20(ARM64)-orange.svg" alt="Platform: Jetson">
    <img src="https://img.shields.io/badge/Camera-Orbbec%20Gemini%202-green.svg" alt="Camera: Orbbec Gemini 2">
    <img src="https://img.shields.io/badge/Detection-GraspNet%2BYOLO-yellow.svg" alt="Detection: GraspNet + YOLO">
</p>

<p align="center">
  <strong>RGB-D 感知 · 目标检测 · 手眼标定 · GraspNet 6-DoF 抓取姿态 · 机械臂控制</strong>
</p>

<p align="center">
  <a href="./README.md"><strong>English Guide</strong></a>
  &nbsp;|&nbsp;
  <a href="#readme-zh"><strong>中文文档</strong></a>
</p>

---

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- 中文目录                                                   -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div id="readme-zh"></div>

## 目录

> **快速跳转：** [1. 项目概述](#1-项目概述--zh) · [2. 硬件要求](#2-硬件要求--zh) · [3. 系统架构](#3-系统架构--zh) · [4. 环境配置](#4-环境配置--zh) · [5. 环境验证四步法](#5-环境验证四步法--zh) · [6. 手眼标定](#6-手眼标定--zh) · [7. YOLO / TensorRT 模型导出](#7-yolo--tensorrt-模型导出--zh) · [8. 运行 Web 演示](#8-运行-web-演示--zh) · [9. Web 界面指南](#9-web-界面指南--zh) · [10. 脚本说明](#10-脚本说明--zh) · [11. 配置参数参考](#11-配置参数参考--zh) · [12. 命令行抓取](#12-命令行抓取--zh) · [13. 常见问题排查](#13-常见问题排查--zh) · [14. 参考链接](#14-参考链接--zh)

---

<div id="1-overview--zh"></div>

## 1. 项目概述 <span style="font-size:0.6em">[<a href="./README.md#1-overview--en">English</a>]</span>

本项目在 **NVIDIA Jetson** 上为 **reBot Arm B601-DM** 实现了一套完整的视觉抓取流水线，融合多模态感知、深度学习抓取姿态估计与真实机械臂控制。

### 流水线流程

```
Orbbec Gemini 2 RGB-D 相机
        ↓
  YOLO 实例分割（目标筛选）
        ↓
  GraspNet 6-DoF 抓取姿态估计
        ↓
  手眼标定（眼在手上，TSAI 算法）
        ↓
  坐标变换：相机坐标系 → 机器人基坐标系
        ↓
  机械臂 IK 轨迹规划 + 夹爪力控
        ↓
  底座旋转 + 物体放置
```

### 核心特性

| 特性 | 说明 |
|------|------|
| **双抓取估计** | GraspNet（6-DoF，预训练模型）+ 普通抓取（基于深度，高频率） |
| **眼在手上标定** | TSAI 算法，全自动采集标定数据 |
| **YOLO 目标筛选** | 对场景中任意目标进行分割，或按类别名指定 |
| **Web 界面** | 实时 MJPEG 视频流、目标类别选择、抓取预览、外参补偿调节 |
| **底座电机调试** | 通过 Web 面板直接控制 joint1 |
| **多相机支持** | Orbbec Gemini 2、RealSense D435i、RealSense D405 |
| **外参补偿** | 夹爪 / 相机 / 基座的 XYZ + RPY 偏移量在线调节 |

### 默认配置

| 项目 | 默认值 |
|------|--------|
| conda 环境 | `graspnet` |
| 相机 | Orbbec Gemini 2（`pyorbbecsdk`） |
| 目标检测 | YOLO `yolo11n-seg.engine` |
| 抓取估计 | GraspNet `checkpoint-rs.tar` |
| 手眼标定 | `config/calibration/orbbec_gemini2/hand_eye.npz` |
| 主程序入口 | `scripts/grasp_web.py` |

---

<div id="2-hardware-requirements--zh"></div>

## 2. 硬件要求 <span style="font-size:0.6em">[<a href="./README.md#2-hardware-requirements--en">English</a>]</span>

| 组件 | 型号 / 说明 |
|------|-------------|
| 机械臂 | reBot Arm B601-DM（大连茂森 DAMIAO 电机版） |
| 深度相机 | Orbbec Gemini 2 |
| 连接线 | USB2CAN 适配器（机械臂 CAN 总线）；USB 3.0（相机） |
| 主机 | Jetson（Ubuntu 22.04，Python 3.10，ARM64） |
| ArUco 标定板 | 4x4，ID=0，边长 0.1 m（用于手眼标定） |

### 接线说明

```bash
# 1. 将 Gemini 2 通过 USB 3.0 连接到 Jetson
# 2. 将 USB2CAN 适配器连接到机械臂 CAN 总线，插入 Jetson USB 口
# 3. 设置设备权限（首次使用需要）
sudo chmod a+rw /dev/bus/usb/*/*
sudo chmod 666 /dev/ttyUSB0   # 根据实际情况调整端口号
```

---

<div id="3-system-architecture--zh"></div>

## 3. 系统架构 <span style="font-size:0.6em">[<a href="./README.md#3-system-architecture--en">English</a>]</span>

```
┌──────────────────────────────────────────────────────────────┐
│                 grasp_web.py（Web 界面）                       │
│  实时 MJPEG 视频流 · 目标类别下拉框 · 真实抓取按钮               │
│              底座电机调试面板（joint1）                         │
└───────────────────────┬──────────────────────────────────────┘
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
┌──────────────────┐        ┌─────────────────────────┐
│  YOLO 实例分割    │        │  GraspNet 6-DoF         │
│  目标筛选         │        │  抓取姿态估计            │
│  （TensorRT）    │        │  + 普通抓取（Ordinary）   │
└────────┬─────────┘        └────────────┬────────────┘
         │                               │
         └───────────────┬───────────────┘
                         ▼
              ┌──────────────────┐
              │  手眼变换矩阵    │ ← hand_eye.npz（标定结果）
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐
              │  机械臂 IK 轨迹  │
              │  + 夹爪状态机    │
              └──────────────────┘
```

---

<div id="4-environment-setup--zh"></div>

## 4. 环境配置 <span style="font-size:0.6em">[<a href="./README.md#4-environment-setup--en">English</a>]</span>

> **建议顺序：** 按顺序完成每一步，尤其是第 5 节的环境验证四步法，不要跳过。

### 4.1 克隆代码仓库

```bash
git clone https://github.com/Seeed-Projects/reBot-DevArm-Grasp.git rebot_grasp
cd rebot_grasp
```

### 4.1.5 JetPack 兼容性矩阵

> 下面的步骤同时覆盖 **JetPack 6.x（Jetson Orin）** 和 **JetPack 7.x（Jetson Thor / L4T 38.x 上的 Orin）**。请按你的板子选对列。

| 组件 | JetPack 6.x (Orin) | JetPack 7.0 / 7.2 (Thor) |
|---|---|---|
| L4T (Linux for Tegra) | 36.x | **38.2** |
| Linux 内核 | 5.15 | **6.8.12** |
| Ubuntu 基础 | 22.04 | **24.04 LTS** |
| Python (conda) | 3.10 | **3.12** |
| CUDA Toolkit | 12.6 | **13.0** |
| cuDNN | 9.3 | **9.12** |
| TensorRT | 10.3 | **10.13** |
| GPU 架构 (`-gencode`) | `sm_87` (Orin) | **`sm_100` / `sm_120`** (Thor / Blackwell) |
| gcc / g++ | 11.x | **13.2** (SBSA) |
| aarch64 ABI | aarch64 | **arm64-sbsa** |
| PyTorch 轮包来源 | `developer.download.nvidia.com/...` | **`pypi.jetson-ai-lab.io`** (JP7) |
| cuDSS 运行时 | 不需要 | **必需**（见 §4.3.5） |
| `CUDA_HOME` 路径 | `/usr/local/cuda-12.6` | **`/usr/local/cuda-13.0`** |
| `graspnet-baseline` CUDA 算子 | 用 `MAX_JOBS=1` 重编 | **用专用脚本重编**（见 §4.6.4） |

> **先查一下你的 JetPack 版本：**
> ```bash
> cat /etc/nv_tegra_release           # 快速查询
> # 在 JP7 上也可以用
> dpkg -l nvidia-jetpack 2>/dev/null | tail -1
> ```
> **JP7** 的首行形如 `# R38 (release), REVISION: 2.1, ...`。

### 4.2 创建 conda 环境

> **JP6 (Orin)**：用 `python=3.10`。**JP7 (Thor)**：用 `python=3.12`（Ubuntu 24.04 自带 Python 3.12，JP7 的 PyTorch 轮包也是基于 3.12 编译的）。

```bash
# JetPack 6.x (Orin)
conda create -n graspnet python=3.10 -y

# JetPack 7.0/7.2 (Thor) — Ubuntu 24.04 自带 Python 3.12
conda create -n graspnet python=3.12 -y

conda activate graspnet
```

### 4.3 安装 NVIDIA PyTorch（Jetson 专用）

> **重要：** 在 Jetson 上，**不要**安装 PyPI 上的通用 `torch`。必须安装与 JetPack 版本匹配的 NVIDIA 轮包：

```bash
# 查询 JetPack 版本
dpkg -l | grep jetpack
# 或
cat /etc/nv_tegra_release
```

参考：https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/index.html

```bash
# 示例（JetPack 6.0 + CUDA 12.6，Python 3.10 aarch64）
pip install torch-2.6.0 torchvision-0.21.0 -f https://developer.download.nvidia.com/compute/pytorchicrob/links.html
```

验证安装：

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 4.3.5 JetPack 7 / Thor 专用步骤（仅 JP7，JP6 跳过）

JP7 / Thor（arm64-sbsa）有 **两个 JP6 不需要的额外运行时依赖**，且轮包索引也不同。**用 JP6 的请完全跳过本节。**

**(a) 从 `pypi.jetson-ai-lab.io` 安装 JP7 PyTorch 轮包**（`developer.download.nvidia.com` 索引不托管 JP7 SBSA 轮包）：

```bash
# 选择与 CUDA 13.x 和 Python 3.12 匹配的 torch 版本
pip install --extra-index-url https://pypi.jetson-ai-lab.io/jp7/cu13 torch torchvision
# 如果该索引上暂时只有 cu126 轮包，回退到：
pip install --extra-index-url https://pypi.jetson-ai-lab.io/jp7/cu126 torch torchvision
```

安装后务必重新验证——JP7 轮包的修订号会不时变化。

**(b) 安装 `libcudss` + CUDA 12 cuBLAS / cuBLASLt**（JP7 轮包中 `libtorch_cuda.so` 的硬性运行时依赖；系统的 CUDA 13.0 工具包**不带** `libcudss.so.0`，而 `.so.13` 的 cuBLAS **无法满足** cuDSS 需要的 `.so.12` 符号版本）：

```bash
# 1) 下载 cuDSS 可重分发包（CUDA 12，SBSA / aarch64）到临时目录
mkdir -p ~/cudss && cd ~/cudss
wget https://developer.download.nvidia.com/compute/cudss/redist/cudss/linux-sbsa/libcudss-linux-sbsa-0.7.0.7-archive.tar.xz
tar xf libcudss-linux-sbsa-0.7.0.7-archive.tar.xz
cd libcudss-linux-sbsa-0.7.0.7-archive
sudo cp -a lib/* /usr/local/cuda-13.0/lib64/
sudo ldconfig

# 2) 为 cuDSS 的 NEEDED 项提供 CUDA 12 的 cuBLAS / cuBLASLt
#    （降级符号链接链：cuDSS 要 .so.12，系统只有 .so.13）
cd /usr/local/cuda-13.0/lib64
sudo ln -sf libcublas.so.13  libcublas.so.12
sudo ln -sf libcublasLt.so.13 libcublasLt.so.12
sudo ldconfig
```

> **为什么这样可以：** 通过符号链接解析到的 `libcublas.so.12` 是常规文件——加载器会看到两个带版本号的符号，优先取 `.so.12`（因为 cuDSS 按名字请求）。`.so.13` 的 ABI 不会被消费。

**(c) 快速冒烟测试：**

```bash
python -c "
import torch
print('torch:', torch.__version__, 'cuda:', torch.cuda.is_available())
x = torch.zeros(2, device='cuda')
print('alloc OK:', x.shape, x.device)
"
```

如果 `cuda.is_available()` 返回 `False`，且用 `LD_DEBUG=libs` 运行时看到 `libcudss.so.0: cannot open shared object file`，请重做步骤 (b) 并再次 `ldconfig`。如果看到 `libcublas.so.12: version 'libcublas.so.12' not found`，说明你的符号链接不对——见 §13 Q7。

> **JP7 相关参考**：[NVIDIA JetPack 7.0 / Thor Components](https://developer.ridgerun.com/wiki/index.php/NVIDIA_Jetson_AGX_Thor/JetPack_7.0/Getting_Started/Components)、[Jetson-ai-lab 轮包](https://pypi.jetson-ai-lab.io/)。

### 4.4 安装非 PyTorch 依赖

```bash
pip install -r requirements-graspnet-jetson.txt
```

### 4.5 配置 CUDA 环境变量

> **JP6 (Orin)**：用 `CUDA_HOME=/usr/local/cuda-12.6`。**JP7 (Thor)**：用 `CUDA_HOME=/usr/local/cuda-13.0`。路径必须正确——`nvcc` 通过该目录的符号链接暴露，`torch.utils.cpp_extension` 也通过 `CUDA_HOME` 找到它。

```bash
# JetPack 6.x (Orin)
export CUDA_HOME=/usr/local/cuda-12.6

# JetPack 7.0/7.2 (Thor)
export CUDA_HOME=/usr/local/cuda-13.0

export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$LD_LIBRARY_PATH"
```

> 将以下内容添加到 `~/.bashrc`，避免每次打开终端都重新设置：
> ```bash
> # JP6：
> echo 'export CUDA_HOME=/usr/local/cuda-12.6' >> ~/.bashrc
> # JP7：
> # echo 'export CUDA_HOME=/usr/local/cuda-13.0' >> ~/.bashrc
> echo 'export PATH="$CUDA_HOME/bin:$PATH"' >> ~/.bashrc
> echo 'export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$LD_LIBRARY_PATH"' >> ~/.bashrc
> source ~/.bashrc
> ```

### 4.6 准备 SDK 子模块

#### 4.6.1 reBotArm SDK

```bash
git clone https://github.com/vectorBH6/reBotArm_control_py.git sdk/reBotArm_control_py
cd sdk/reBotArm_control_py
pip install -e .
cd ../..
```

#### 4.6.2 GraspNet Baseline

```bash
git clone https://github.com/graspnet/graspnet-baseline.git sdk/graspnet-baseline
```

下载 GraspNet 预训练权重（必须，约 1.6 GB）：

```bash
mkdir -p sdk/graspnet-baseline/checkpoints
# 从 https://graspnet.net/download 下载 checkpoint-rs.tar
# 放置到 sdk/graspnet-baseline/checkpoints/
```

#### 4.6.3 GraspNet API

```bash
git clone https://github.com/graspnet/graspnetAPI.git sdk/graspnetAPI
cd sdk/graspnetAPI
pip install -e .
cd ../..
```

#### 4.6.4 编译 GraspNet CUDA 算子（关键步骤！）

> 出现 `No module named pointnet2._ext` / `No module named knn_pytorch.knn_pytorch` 几乎都是因为跳过了这一步。**在 JP7 上必须重新编译——JP6 的预编译 `.so` 文件链接的是错误的 Python 标签（`cp310`）和错误的 cuDNN / cuBLAS ABI，在 `cpython-312` 环境下 import 必然失败。**

仓库里带了一个专用编译脚本，同时支持 JP6 和 JP7：

```bash
conda activate graspnet
cd ~/Documents/rebot_grasp-jetson    # 仓库根目录
bash scripts/install_graspnet_cuda_ops.sh
# 或者在 JetPack / torch 升级后重新构建：
bash scripts/install_graspnet_cuda_ops.sh --force
```

脚本做了什么：

1. 自动检测当前 Python（JP6 是 `3.10`，JP7 是 `3.12`），并从 `$CUDA_HOME` 或 torch 轮包中找到 `nvcc`。
2. 修改两个 `setup.py`（`pointnet2` 和 `knn`），向 `extra_ldflags` 注入 `-L<torch>/lib` 和 `-Wl,-rpath,<torch>/lib`。没有这一步，编译出的 `.so` 在运行时就找不到 `libc10.so` / `libtorch_cpu.so`（这是全新环境上最常见的编译失败原因）。
3. 跑 `python setup.py build_ext --inplace` 并把生成的 `.so` 拷到包根目录。
4. 装一个 `conda activate.d` 钩子，在每次激活时把 torch lib 目录加到 `LD_LIBRARY_PATH`——双保险，以防某条编译路径仍然嵌入了错误的 RPATH。
5. 跑一个自检脚本，import `pointnet2._ext` 和 `knn_pytorch.knn_pytorch`。重启用 `--check` 即可单独验证。

> **JP7 专属提示：** 如果报 `fatal error: Python.h: No such file or directory`，先装对应的 dev 包：
> ```bash
> # JP6 / Python 3.10
> sudo apt-get install -y python3.10-dev
> # JP7 / Python 3.12
> sudo apt-get install -y python3.12-dev
> ```
> 如果报 `unsupported GNU version`（JP7 上的 gcc 13 对某些老头文件太新），脚本运行前先 `export CC=gcc-12 CXX=g++-12`，然后用 `--force` 重跑。

#### 4.6.4.b 手动回退（不用脚本）

如果因为某些原因跑不了脚本（比如 CI 没有 `apt` 权限），JP6 和 JP7 都可以用同样两条手动命令：

```bash
cd sdk/graspnet-baseline/pointnet2
MAX_JOBS=1 python setup.py install

cd ../knn
FORCE_CUDA=1 MAX_JOBS=1 python setup.py install
```

### 4.7 安装 Orbbec Python SDK

```bash
# 安装编译依赖
sudo apt-get install -y cmake build-essential libusb-1.0-0-dev

# 获取 pyorbbecsdk
cd sdk
git clone https://github.com/orbbec/pyorbbecsdk.git
cd pyorbbecsdk
pip install -e .

# 如果预编译版本不兼容，从源码编译：
# bash ../../scripts/install_pyorbbecsdk.sh --from-source
```

### 4.8 配置 Orbbec udev 规则（必须）

```bash
cd /home/seeed/Downloads/rebot_grasp/sdk/pyorbbecsdk/scripts/env_setup
sudo ./install_udev_rules.sh
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### 4.9 下载 YOLO 模型权重

```bash
# 创建 models 目录
mkdir -p models

# 下载 YOLO11n-seg 模型
wget https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-seg.pt -O models/yolo11n-seg.pt
```

### 4.10 导出 YOLO TensorRT 模型（必须在目标 Jetson 上执行）

> TensorRT `.engine` 文件与 Jetson 设备、GPU、CUDA、TensorRT 版本高度耦合。**不要**从其他机器拷贝 engine，必须在目标 Jetson 上重新导出：

```bash
yolo export model=models/yolo11n-seg.pt format=engine imgsz=640 half=True device=0 workspace=4
```

输出文件：`models/yolo11n-seg.engine`

如果 FP16 导出失败，回退到 FP32：

```bash
yolo export model=models/yolo11n-seg.pt format=engine imgsz=640 device=0 workspace=4
```

---

<div id="5-four-step-environment-verification--zh"></div>

## 5. 环境验证四步法 <span style="font-size:0.6em">[<a href="./README.md#5-four-step-environment-verification--en">English</a>]</span>

> **每次部署完成后必须执行。** 按顺序完成四步，再进行标定或运行演示。

### 步骤一：Orbbec RGB-D 视频流

```bash
conda activate graspnet
cd /home/seeed/Downloads/rebot_grasp

# 纯文本检查
python scripts/verify_pyorbbec_stream.py

# 带预览窗口
python scripts/verify_pyorbbec_stream.py --preview --seconds 10
```

**预期结果：** 显示 RGB 和 Depth 分辨率 + 帧率，无报错。

### 步骤二：机械臂连接

```bash
# 只读检查（安全，不会移动机械臂）
python scripts/verify_rebot_arm_motion.py --read-only

# 小角度 jog 测试（确保机械臂运动路径畅通！）
python scripts/verify_rebot_arm_motion.py --deg 5
```

**预期结果：** 返回 `[OK]` 或正常的位姿数据。

### 步骤三：手眼标定文件

```bash
python scripts/verify_handeye_calibration.py
```

**预期结果：**

```
[OK] hand-eye calibration looks usable
```

> 如果提示 `No such file`，说明手眼标定尚未完成。请继续阅读第 6 节。

### 步骤四：GraspNet 依赖栈

```bash
python scripts/verify_graspnet_stack.py
```

**预期结果：**

```
[OK] GraspNet stack is ready
```

> 如果出现 `No module named pointnet2._ext`，请重新执行 [4.6.4 节](#464-编译-graspnet-cuda-算子关键步骤)。

---

<div id="6-hand-eye-calibration--zh"></div>

## 6. 手眼标定 <span style="font-size:0.6em">[<a href="./README.md#6-hand-eye-calibration--en">English</a>]</span>

本系统采用**眼在手上（Eye-in-Hand）**标定方式：相机安装在机械臂末端，随末端一起运动；ArUco 标定板固定在工作台上。

### 6.1 何时需要重新标定

以下情况发生时，需要重新标定：

- 相机安装位置发生变化（支架移动或重新安装）
- 夹爪几何参数变化（换了夹爪或夹爪位置移动）
- ArUco 标定板尺寸更换
- 工作台布局大幅改变
- 抓取精度明显下降

### 6.2 标定板设置

使用 **ArUco DICT_4X4_50，ID=0**，边长 **0.1 m** 的标定板（边长必须与 `config/default.yaml` 中的 `aruco.marker_length_m` 一致）。

打印标定板并平贴于工作台上，或使用实体标定靶。确保标定板在机械臂各观测角度下都能清晰可见。

### 6.3 自动采集模式（推荐）

```bash
python scripts/collect_handeye_eih.py
```

机械臂自动遍历 50 个预设姿态，到位后若识别到 ArUco 标记则自动采集。最少需要 **5 个采样点**；建议采集 **15 个以上**以获得稳定结果。

| 按键 | 操作 |
|------|------|
| `c` 或 `q` | 结束采集并计算标定结果 |
| `Ctrl+C` | 中途结束也会触发计算 |

### 6.4 手动采集模式

```bash
python scripts/collect_handeye_eih.py --manual
```

机械臂进入重力补偿模式，手动推动机械臂到任意角度后按采集：

| 按键 | 操作 |
|------|------|
| `Enter` | 采集当前姿态 |
| `pos` | 打印当前末端位姿 |
| `c` / `q` | 结束并计算 |

### 6.5 标定结果输出

结果自动保存到：

```
config/calibration/orbbec_gemini2/hand_eye.npz
config/calibration/orbbec_gemini2/intrinsics.npz
```

### 6.6 验证标定结果

```bash
python scripts/verify_handeye_calibration.py
```

---

<div id="7-yolo--tensorrt-model-export--zh"></div>

## 7. YOLO / TensorRT 模型导出 <span style="font-size:0.6em">[<a href="./README.md#7-yolo--tensorrt-model-export--en">English</a>]</span>

### 7.1 从 .pt 导出 ONNX（中间格式）

```bash
yolo export model=models/yolo11n-seg.pt format=onnx imgsz=640 opset=12 simplify=True
```

### 7.2 直接从 .pt 导出 TensorRT Engine（推荐）

```bash
yolo export model=models/yolo11n-seg.pt format=engine imgsz=640 half=True device=0 workspace=4
```

### 7.3 从 ONNX 转换为 TensorRT Engine

```bash
trtexec \
  --onnx=models/yolo11n-seg.onnx \
  --saveEngine=models/yolo11n-seg.engine \
  --fp16 \
  --workspace=4096
```

### 7.4 验证导出的 Engine

```bash
python scripts/verify_graspnet_stack.py --engine models/yolo11n-seg.engine
python scripts/object_detection.py
```

### 7.5 切换不同模型

修改 `config/default.yaml`：

```yaml
yolo:
  model_name: "yolo11n-seg.engine"  # 替换为你的 engine 文件名
  device: "auto"
```

或在命令行指定：

```bash
python scripts/grasp_web.py --yolo-model yolo11n-seg.engine
```

> **注意：** `.engine` 文件与设备高度绑定，不可跨机器使用。如果 JetPack / TensorRT / CUDA 版本发生变化，需要在目标 Jetson 上重新导出。

---

<div id="8-running-the-web-demo--zh"></div>

## 8. 运行 Web 演示 <span style="font-size:0.6em">[<a href="./README.md#8-running-the-web-demo--en">English</a>]</span>

### 8.1 预览模式（不连接机械臂）

```bash
conda activate graspnet
cd /home/seeed/Downloads/rebot_grasp
python scripts/grasp_web.py --host 0.0.0.0 --port 8000
```

在浏览器中打开：

```
http://<jetson_ip>:8000
```

此模式下可以：
- 查看实时 RGB-D 视频流
- 查看 YOLO 检测框叠加效果
- 预览 GraspNet 抓取点
- 调节外参补偿参数（夹爪、相机、基座）
- 调试底座电机

### 8.2 启用真实机械臂执行

```bash
python scripts/grasp_web.py --host 0.0.0.0 --port 8000 --enable-robot
```

在 Web 界面中点击**真实抓取**按钮，触发完整抓取流程。

### 8.3 禁用抓取后动作（调试用）

每次抓取后的底座旋转和放置动作会干扰连续抓取调试，可禁用：

```bash
python scripts/grasp_web.py --host 0.0.0.0 --port 8000 --enable-robot --no-place-after-grasp
```

### 8.4 全场景 GraspNet（不使用 YOLO 筛选）

```bash
python scripts/grasp_web.py --host 0.0.0.0 --port 8000 --no-yolo
```

### 8.5 常用命令行参数

| 参数 | 说明 |
|------|------|
| `--host 0.0.0.0 --port 8000` | 绑定所有网络接口 |
| `--enable-robot` | 允许真实机械臂运动 |
| `--no-yolo` | 跳过 YOLO，启用全场景 GraspNet |
| `--camera-type orbbec_gemini2` | 强制指定相机类型 |
| `--target-class cup` | 自动选择该类别 |
| `--no-place-after-grasp` | 跳过底座旋转和放置 |
| `--no-auto-graspnet` | 禁用自动 GraspNet 更新 |
| `--graspnet-interval 2.0` | GraspNet 更新间隔（秒） |

---

<div id="9-web-ui-guide--zh"></div>

## 9. Web 界面指南 <span style="font-size:0.6em">[<a href="./README.md#9-web-ui-guide--en">English</a>]</span>

### 界面布局

```
┌──────────────────────────────────────────────────────────────┐
│ [reBot Grasp Web] [EN/中文] 目标:[▼] [设置] [刷新] [真实抓取]   │
│ 模式提示...                                                  │
├─────────────────────────────────┬────────────────────────────┤
│                                 │  ┌─ 外参补偿 ──────────────┐ │
│                                 │  │ 夹爪前/左/上(m)        │ │
│     实时 MJPEG 相机画面           │  │ [___] [___] [___]     │ │
│     + YOLO 检测框                 │  │ 夹爪 RPY(°)          │ │
│     + GraspNet 抓取点             │  │ [___] [___] [___]     │ │
│                                 │  │ 相机 XYZ(m)           │ │
│                                 │  │ [___] [___] [___]     │ │
│                                 │  │ 相机 RPY(°)           │ │
│                                 │  │ [___] [___] [___]     │ │
│                                 │  │ 基座 XYZ(m)           │ │
│                                 │  │ [___] [___] [___]     │ │
│                                 │  │ 基座 RPY(°)           │ │
│                                 │  │ [___] [___] [___]     │ │
│                                 │  │ [设置补偿]             │ │
│                                 │  └─────────────────────────┘ │
│                                 │  ┌─ 底座电机调试 ───────────┐ │
│                                 │  │ joint1 jog(°): [___]   │ │
│                                 │  │ 运动时长:    [___]     │ │
│                                 │  │ 安全边距:    [___]     │ │
│                                 │  │ [-30°] [执行] [+30°]   │ │
│                                 │  └─────────────────────────┘ │
│                                 │  状态栏...                  │
│                                 │  {状态 JSON}                │
└─────────────────────────────────┴────────────────────────────┘
```

### 外参补偿字段说明

界面中有三类补偿参数：

| 类别 | 字段 | 作用 |
|------|------|------|
| **夹爪前/左/上** | forward, lateral, vertical（米） | 修正最终抓取 TCP 位置 |
| **夹爪 RPY** | roll, pitch, yaw（度） | TCP 局部旋转补偿 |
| **相机 XYZ/RPY** | 6 自由度（米 / 度） | 修正相机外参 |
| **基座 XYZ/RPY** | 6 自由度（米 / 度） | 修正基座外参（**不会旋转底座电机**） |

### 底座电机调试面板

底座电机调试面板**直接 jog `joint1`**，返回 JSON 格式结果：

```json
{
  "before_deg": 12.5,
  "after_deg": -17.5,
  "limit_deg": -30.0,
  "safe_limit_deg": -25.0
}
```

此面板可用于：
- 测试底座电机响应
- 寻找安全的关节限位
- 调试 `base_delta_deg` 和 `base_safety_margin_deg` 参数

---

<div id="10-script-reference--zh"></div>

## 10. 脚本说明 <span style="font-size:0.6em">[<a href="./README.md#10-script-reference--en">English</a>]</span>

| 脚本 | 说明 |
|------|------|
| `verify_pyorbbec_stream.py` | Orbbec RGB-D 视频流检查（纯文本 / 预览） |
| `verify_rebot_arm_motion.py` | 机械臂连接状态和 jog 检查 |
| `verify_handeye_calibration.py` | 手眼标定文件完整性检查 |
| `verify_graspnet_stack.py` | GraspNet / CUDA / YOLO Engine 可用性检查 |
| `collect_handeye_eih.py` | 眼在手上手眼标定数据采集（自动 / 手动模式） |
| `graspnet_camera_demo.py` | 相机端 GraspNet 预览（独立 Open3D 窗口） |
| `object_detection.py` | 纯 YOLO 检测演示 |
| `grasp_web.py` | **主入口：** Web 界面、目标选择、真实抓取、底座 jog |
| `grasp.py` | 命令行抓取流水线（非 Web，OpenCV 窗口） |
| `install_pyorbbecsdk.sh` | pyorbbecsdk 安装辅助脚本 |

---

<div id="11-configuration-reference--zh"></div>

## 11. 配置参数参考 <span style="font-size:0.6em">[<a href="./README.md#11-configuration-reference--en">English</a>]</span>

主配置文件：`config/default.yaml`

### 11.1 相机配置

```yaml
camera:
  type: orbbec_gemini2
  color_width: 1280
  color_height: 720
  depth_width: 1280
  depth_height: 720
  fps: 30
```

### 11.2 YOLO 配置

```yaml
yolo:
  model_name: "yolo11n-seg.engine"  # 替换为你的 engine 文件名
  device: "auto"                    # 自动选择 GPU
  use_world: false                   # 设为 true 以启用开放词汇检测（需要 .pt 模型）
  custom_classes: []                # 开放词汇检测自定义类别
```

### 11.3 抓取偏移量配置

```yaml
grasp_pipeline:
  grasp:
    pregrasp_offset_m: 0.080         # 预抓取高度偏移（米）
    grasp_forward_offset_m: 0.000    # 沿接近方向移动抓取点
    camera_x_offset_m: 0.000         # 相机外参补偿 X
    camera_y_offset_m: 0.000         # 相机外参补偿 Y
    camera_z_offset_m: 0.000         # 相机外参补偿 Z
    camera_roll_offset_deg: 0.0      # 相机外参补偿 roll
    camera_pitch_offset_deg: 0.0     # 相机外参补偿 pitch
    camera_yaw_offset_deg: 0.0       # 相机外参补偿 yaw
    base_x_offset_m: 0.000           # 基座外参补偿 X
    base_yaw_offset_deg: 0.0        # 基座外参补偿 yaw
```

### 11.4 放置配置

```yaml
grasp_pipeline:
  place:
    enabled: true
    base_joint: joint1
    base_delta_deg: 90.0             # 底座旋转角度（负数 = 负方向）
    base_direction: auto             # auto / positive / negative
    base_rotate_duration: 2.5        # 旋转时长（秒）
    base_safety_margin_deg: 5.0      # 关节限位安全边距（度）
    return_home: true                # 放置完成后是否回零位
```

### 11.5 ArUco 标定配置

```yaml
calibration:
  aruco:
    marker_length_m: 0.1
    dict_id: 0
    target_marker_id: 0
  hand_eye_method: TSAI
```

---

<div id="12-cli-grasping--zh"></div>

## 12. 命令行抓取 <span style="font-size:0.6em">[<a href="./README.md#12-cli-grasping--en">English</a>]</span>

适用于无界面或自动化调用的场景。

### 12.1 模拟运行（不移动机械臂）

```bash
# 全场景抓取
python scripts/grasp.py --dry-run --camera-type orbbec_gemini2 --no-yolo

# 指定目标类别
python scripts/grasp.py --dry-run --camera-type orbbec_gemini2 --target-class cup
```

### 12.2 真实执行

```bash
python scripts/grasp.py --camera-type orbbec_gemini2 --target-class cup
```

### 12.3 禁用抓取后放置

```bash
python scripts/grasp.py --camera-type orbbec_gemini2 --target-class cup --no-place-after-grasp
```

### 12.4 自定义底座旋转

```bash
python scripts/grasp.py \
  --camera-type orbbec_gemini2 \
  --target-class cup \
  --place-base-delta-deg -30 \
  --place-base-rotate-duration 2.5
```

---

<div id="13-troubleshooting--zh"></div>

## 13. 常见问题排查 <span style="font-size:0.6em">[<a href="./README.md#13-troubleshooting--en">English</a>]</span>

### Q1: `pyorbbecsdk import failed`

```bash
bash scripts/install_pyorbbecsdk.sh
# 如果仍然失败，从源码编译：
bash scripts/install_pyorbbecsdk.sh --from-source
```

### Q2: 相机画面全黑

```bash
# 检查 udev 规则
ls -la /dev/bus/usb/*/*
# 重新加载 udev
sudo udevadm control --reload-rules && sudo udevadm trigger
# 检查 USB 3.0 连接
```

### Q3: `nvbufsurftransform: Could not get EGL display connection`

这是 Jetson 无桌面显示时的正常现象。纯文本检查（不加 `--preview`）不受影响。

### Q4: `torch.cuda.is_available()` 返回 False

```bash
# 检查 PyTorch 版本
python -c "import torch; print(torch.__version__)"
# 重新安装与 JetPack 版本匹配的轮包
```

### Q5: `No module named pointnet2._ext`

重新编译 CUDA 算子。仓库里的专用脚本同时支持 JP6 和 JP7（包括 `libc10.so` 的 rpath 修复）：

```bash
bash scripts/install_graspnet_cuda_ops.sh --force
```

或者手动：

```bash
cd sdk/graspnet-baseline/pointnet2
MAX_JOBS=1 python setup.py install

cd ../knn
FORCE_CUDA=1 MAX_JOBS=1 python setup.py install
```

### Q5b（仅 JP7）：`libcudss.so.0: cannot open shared object file`

你跳过了 §4.3.5 (b)。重做 cuDSS 安装 + 符号链接步骤：

```bash
cd /usr/local/cuda-13.0/lib64
ls -l libcudss* libcublas*     # 确认 cuDSS .so 在这里，并且 .so.12 符号链接存在
sudo ldconfig
python -c "import torch; print(torch.cuda.is_available())"
```

如果 `ldconfig -p | grep libcudss` 没有结果，说明文件没拷到对的目录。重做上面 §4.3.5 (b) 的安装步骤。

### Q5c（仅 JP7）：`version 'libcublas.so.12' not found`（cuDSS 要求）

`.so.13 → .so.12` 符号链接存在但加载器仍然解析不到。两种常见原因：

```bash
# 1) 符号链接坏了 — 重建
cd /usr/local/cuda-13.0/lib64
sudo ln -sf libcublas.so.13  libcublas.so.12
sudo ln -sf libcublasLt.so.13 libcublasLt.so.12
sudo ldconfig

# 2) libtorch_cuda.so 是用 CUDA 12 编译的，而不是 13
python -c "import torch, os; print(os.path.join(os.path.dirname(torch.__file__), 'lib'))"
# 确保 LD_LIBRARY_PATH 没有指向某个缺少匹配 .so.13 的 CUDA 12 安装。
# 只用 /usr/local/cuda-13.0/lib64。
```

### Q5d（仅 JP7）：`nvcc fatal: Value 'sm_100' is not defined for option 'gpu-architecture'`

系统的 nvcc 太旧，不认识 Thor / Blackwell 架构。JP7 自带的 CUDA 13.0 是认识 `sm_100` / `sm_120` 的，但如果 `which nvcc` 返回的是 `/usr/local/cuda-12.x/...` 路径，说明 `CUDA_HOME` 不对：

```bash
which nvcc
# 应该输出 /usr/local/cuda-13.0/bin/nvcc
unset CUDA_VISIBLE_DEVICES   # 只是清理一下，但容易忘
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
hash -r
nvcc --version
```

### Q6: YOLO Engine 加载失败

常见原因：

- Engine 是从其他机器拷贝过来的
- JetPack / TensorRT / CUDA 版本发生了变化
- Ultralytics 版本发生变化

解决方法：在目标 Jetson 上重新导出：

```bash
yolo export model=models/yolo11n-seg.pt format=engine imgsz=640 half=True device=0 workspace=4
python scripts/verify_graspnet_stack.py --engine models/yolo11n-seg.engine
```

### Q7: ArUco 标定板检测不到

- 确认标定板是 `DICT_4X4_50`，ID 为 `0`
- 确认 `marker_length_m` 与实际黑色方块边长一致
- 标定板必须平整、完整，光照稳定
- 确认 `config/calibration/orbbec_gemini2/intrinsics.npz` 存在

### Q8: 底座 jog 没有效果

先检查：joint1 电机使能状态、编码器反馈、限位设置、低级控制模式。

### Q9: 抓取偏移量调节

如果夹爪始终抓偏，可以参考以下调节方向：

| 现象 | 调节参数 |
|------|---------|
| 夹爪深度不对 | `grasp_forward_offset_m` |
| 夹爪左右偏移 | `grasp_lateral_offset_m` 或 `camera_x_offset_m` |
| 夹爪高度偏差 | `grasp_vertical_offset_m` 或 `camera_z_offset_m` |
| 旋转角度偏差 | `grasp_roll_offset_deg` / `grasp_pitch_offset_deg` / `grasp_yaw_offset_deg` |
| 相机安装误差 | `camera_x/y/z_offset_m` 和 `camera_r/p/y_offset_deg` |

建议先通过 Web 界面在线调节，确认数值后再写入 `config/default.yaml` 永久保存。

---

<div id="14-references--zh"></div>

## 14. 参考链接 <span style="font-size:0.6em">[<a href="./README.md#14-references--en">English</a>]</span>

| 资源 | 链接 |
|------|------|
| Seeed Wiki | https://wiki.seeedstudio.com/rebot_arm_b601_dm_grasping_demo/ |
| reBot 机械臂 SDK | https://github.com/vectorBH6/reBotArm_control_py |
| Orbbec SDK v2 | https://github.com/orbbec/OrbbecSDK_v2 |
| pyorbbecsdk | https://github.com/orbbec/pyorbbecsdk |
| GraspNet Baseline | https://github.com/graspnet/graspnet-baseline |
| GraspNet API | https://github.com/graspnet/graspnetAPI |
| NVIDIA Jetson PyTorch | https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/index.html |

---

<p align="center">
  <strong>如果这个项目对你有帮助，欢迎 star！</strong>
</p>
