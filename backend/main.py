# -*- coding: utf-8 -*-
"""
跨品牌机械臂「一键操控与训练」平台 —— FastAPI 后端（真机版 / 无模拟数据）
======================================================================
Leader  : 幻尔 Star Arm 102-LD  -> rebot_arm_102_leader
Follower: Seeed B601-DM        -> seeed_b601_dm_follower (--robot.can_adapter=damiao)

命令来源: https://wiki.seeedstudio.com/cn/rebot_arm_b601_dm_lerobot/
说明    : 官方文档只提供 lerobot-* CLI，没有 get_action / send_action 的 Python API，
          因此硬件操作一律用 subprocess.Popen 执行官方命令，不臆造 Python 接口。

本版本约定：
  * 没有任何 Mock / 假数据。串口、数据集、检查点、摄像头、Loss 全部来自真实系统。
  * 任何一步连不上 / 缺依赖 / 缺权限，都返回统一错误信封 {ok:false,error:"..."}，
    由前端优雅提示，不静默伪造成功。
  * 训练引擎可插拔：LeRobot / YOLO / OpenCV，选择后分发到对应真实训练代码。

启动: python start.py            (会先提示 sudo 密码授权串口，再启动网页服务)
      python start.py --host 0.0.0.0
"""
from __future__ import annotations

import asyncio
import base64
import glob
import io
import json
import math
import locale
import os
import platform
import re
import tempfile
import urllib.error
import urllib.request
import shlex
import shutil
import signal
import socket
import select
import subprocess
import sys
import threading
import time
import zipfile
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from backend.monitor_geometry import resolve_monitor_geometry
from backend.device_release import device_paths, kill_device_owners
from backend.vision_process import VisionProcess
from backend.telemetry import Telemetry
from backend.training import training_mode, prepare_finetune, read_policy
from backend.training_metrics import numeric_step, parse_metric
from backend.model_evaluation import prepare_evaluation
from backend.raw_frames import frame_packet
from backend.camera_manager import CameraManager, CameraError, source_key, list_devices, validate_configs

from fastapi import Body, FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

# ==================================================================
# 0. 常量
# ==================================================================
IS_WINDOWS = platform.system() == "Windows"
ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
PAGES_DIR = FRONTEND_DIR / "pages"
STATIC_DIR = FRONTEND_DIR / "static"
TRAINERS_DIR = BACKEND_DIR / "trainers"
CONFIG_FILE = BACKEND_DIR / "config.json"
LEROBOT_HOME = Path(os.environ.get(
    "LEROBOT_HOME", str(Path.home() / ".cache" / "huggingface" / "lerobot")))
OUTPUTS_DIR = ROOT / "outputs"

FOLLOWER_TYPE = "seeed_b601_dm_follower"   # Seeed B601-DM
LEADER_TYPE = "rebot_arm_102_leader"       # 幻尔 Star Arm 102-LD
CAN_ADAPTER = "damiao"

LEROBOT_CMD_PREFIX: list[str] = []

PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOST = "pypi.tuna.tsinghua.edu.cn"
REPO_URL = "https://v4.gh-proxy.org/https://github.com/Seeed-Projects/lerobot.git"
# Miniforge 安装包与 conda 可执行文件名在 Windows / Linux 上完全不同，按平台取
CONDA_URL_LINUX = ("https://v4.gh-proxy.org/https://github.com/conda-forge/miniforge/"
                   "releases/latest/download/Miniforge3-Linux-x86_64.sh")
CONDA_URL_WINDOWS = ("https://v4.gh-proxy.org/https://github.com/conda-forge/miniforge/"
                     "releases/latest/download/Miniforge3-Windows-x86_64.exe")
CONDA_URL = CONDA_URL_WINDOWS if IS_WINDOWS else CONDA_URL_LINUX
REPO_DIR = ROOT / "lerobot"
CONDA_BASE = Path.home() / "miniforge3"
# Windows 用 COMx，且默认留空由用户在页面扫描后选择；Linux 保持文档里的 /dev 端口
DEFAULT_FOLLOWER_PORT = "" if IS_WINDOWS else "/dev/ttyACM0"
DEFAULT_LEADER_PORT = "" if IS_WINDOWS else "/dev/ttyUSB0"

# Windows 内置命令（TIMEOUT.EXE 等）输出的是本地 ANSI 编码（cp936），
# git / pip 输出的是 UTF-8；子进程输出统一走逐行编码回退，避免日志乱码。
LOCAL_ENCODING = locale.getpreferredencoding(False) or "utf-8"
SUBPROC_KW_ENCODING = LOCAL_ENCODING if IS_WINDOWS else "utf-8"
# 下载脚本的临时路径必须跨平台，Windows 上没有 /tmp
MINIFORGE_SCRIPT = Path(tempfile.gettempdir()) / (
    "Miniforge3-Windows-x86_64.exe" if IS_WINDOWS else "miniforge3.sh")
# 单条安装步骤的最长执行时间（秒），用于 Python 侧超时兜底
STEP_TIMEOUT = 900
# git clone 单独用更短的超时：卡住时尽快给出明确失败，而不是让用户干等
CLONE_TIMEOUT = 300
# gh-proxy 不通时自动尝试的官方源（仍是文档里的同一个仓库）
REPO_FALLBACK = "https://github.com/Seeed-Projects/lerobot.git"
# 后台任务进度：每 2s 推一次 state，每 10s 打一条心跳日志（无输出也不会像死机）
PROGRESS_BEAT_SEC = 2
HEARTBEAT_LOG_SEC = 10

HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8000"))

# 需要真实硬件串口连接的操作（未连接则优雅拒绝）
HW_KINDS = {"teleoperate", "record", "eval", "replay", "robot-client",
            "calibrate-leader", "calibrate-follower",
            "calibrate-left-leader", "calibrate-left-follower",
            "calibrate-right-leader", "calibrate-right-follower", "grasp-execute", "home-and-disable", "zero-adjust", "sorting-run"}

# 允许访问的页面白名单
PAGE_NAMES = {"index", "home", "connect", "camera", "teleop", "dataset",
              "train", "logs", "setup", "yolo", "viewer", "eval",
              "dual"}


# ==================================================================
# 1. 统一错误与响应信封
# ==================================================================
class ApiError(Exception):
    """所有业务错误的唯一出口，前端据此优雅提示。"""

    def __init__(self, detail: str, code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def ok(**kw: Any) -> dict:
    return {"ok": True, **kw}


def _brief(exc: BaseException) -> str:
    """把底层异常压成一句人能看懂的话，避免把 traceback 甩给用户。"""
    msg = str(exc).strip() or exc.__class__.__name__
    return msg if len(msg) < 400 else msg[:400] + " …"


def _req_path(request: Any) -> str:
    """异常处理路径本身不能再抛异常。"""
    try:
        return str(getattr(getattr(request, "url", None), "path", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ==================================================================
# 2. 配置
# ==================================================================
DEFAULT_CONFIG: dict[str, Any] = {
    "environment": {"active_env": "", "env_dir": "", "python_version": "3.12",
                    "env_location": "", "lerobot_python": "", "lerobot_bin_dir": "",
                    "lerobot_env_dir": ""},
    "setup": {"repo_url": REPO_URL, "pip_index": PIP_INDEX,
              "conda_url": CONDA_URL, "with_ffmpeg": False},
    "follower": {"type": FOLLOWER_TYPE, "port": DEFAULT_FOLLOWER_PORT,
                 "id": "follower1", "can_adapter": CAN_ADAPTER,
                 "gripper_torque_ratio": 0.12},
    "leader": {"type": LEADER_TYPE, "port": DEFAULT_LEADER_PORT, "id": LEADER_TYPE},
    # 双臂：默认关闭，关闭时所有命令与原来完全一致（单 leader + 单 follower）。
    # 打开后使用「复合机器人」：左右从臂/主臂作为同一 robot/teleop 配置的子字段，
    # 由单个 lerobot 进程在同一个控制循环里同步驱动，这是双臂协作能被采进同一数据集的前提。
    # robot_type / teleop_type 必须由设备插件注册，页面会真实探测当前 conda 环境里的类型；
    # 留空时后端拒绝生成必然失败的命令，而不是假装支持。
    "dual_arm": {
        "enabled": False,
        "robot_type": "",
        "teleop_type": "",
        "left_prefix": "left_arm",
        "right_prefix": "right_arm",
        "coordination": "sync",
        "left": {"follower_type": FOLLOWER_TYPE, "follower_port": "",
                 "follower_id": "left_follower", "can_adapter": CAN_ADAPTER,
                 "gripper_torque_ratio": 0.12,
                 "leader_type": LEADER_TYPE, "leader_port": "",
                 "leader_id": "left_leader"},
        "right": {"follower_type": FOLLOWER_TYPE, "follower_port": "",
                  "follower_id": "right_follower", "can_adapter": CAN_ADAPTER,
                  "gripper_torque_ratio": 0.12,
                  "leader_type": LEADER_TYPE, "leader_port": "",
                  "leader_id": "right_leader"},
    },
    "cameras": [
        {"name": "front", "type": "opencv", "index_or_path": 0, "width": 640,
         "height": 480, "fps": 30, "fourcc": "MJPG", "preview": True},
        {"name": "side", "type": "opencv", "index_or_path": 2, "width": 640,
         "height": 480, "fps": 30, "fourcc": "MJPG", "preview": False},
    ],
    "dataset": {"repo_id": "seeed_rebot_b601_dm/test", "num_episodes": 5, "auto_home": True,
                "single_task": "Grab the black cube", "push_to_hub": False,
                "episode_time_s": 30, "reset_time_s": 30, "resume": False,
                "display_data": True},
    "hf": {"endpoint": "", "auto": True},
    "train": {"backend": "lerobot",
              "repo_id": "seeed_rebot_b601_dm/test", "dataset_dir": "",
              "policy_type": "act", "base_model": "", "training_mode": "", "learning_rate": 0,
              "output_dir": "outputs/train/act_rebot_test", "job_name": "act_rebot_test",
              "device": "cuda", "steps": 300000, "batch_size": 0, "wandb": False,
              "push_to_hub": False, "video_backend": "", "resume_config": ""},
    "model_eval": {"base_model": "", "repo_id": "", "dataset_dir": "", "episodes": "",
                   "max_samples": 200, "batch_size": 4, "device": "cpu", "video_backend": "pyav"},
    "yolo": {"data_yaml": "", "dataset_dir": "", "model": "yolov8n.pt",
             "camera_index": 0,
             "epochs": 50, "imgsz": 640, "batch": 8, "device": "cpu",
             "output_dir": "outputs/train/yolo"},
    "opencv": {"dataset_dir": "", "classifier": "svm", "epochs": 20,
               "img_size": 64, "output_dir": "outputs/train/opencv"},
    "eval": {"policy_path": "outputs/train/act_rebot_test/checkpoints/last/pretrained_model",
             "repo_id": "seeed/eval_test123",
             "single_task": "Put lego brick into the transparent box",
             "num_episodes": 1, "episode_time_s": 30, "reset_time_s": 10,
             "auto_home": True},
    "viz": {"repo_id": "seeed_rebot_b601_dm/test", "episode_index": 0,
            "display_compressed_images": False},
    "replay": {"repo_id": "seeed_rebot_b601_dm/test", "episode": 0},
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "api_key": "",
        "prompt": "你是机器人视觉助手。只允许使用这些类别：{类别}。找出图中所有目标物体（不同类别、同类别的多个都要），只输出 JSON：{\"objects\": [{\"class\": \"类别名\", \"bbox\": [x1, y1, x2, y2]}, ...]}，bbox 使用 0~1 归一化坐标（左上角、右下角）。不要输出任何解释。",
    },
    "monitor_layout": {"mode":"coordinates", "frame_id":"workspace", "unit":"mm",
                       "left_base_mm":None, "right_base_mm":None, "base_center_distance_mm":None},
    "grasp": {
        "points": [],
        "H": None,
        "table_z": 0.0,
        "z_offset": 0.03,
        "rpy": [0.0, 0.7, 0.0],
        "model": "",
        "camera_index": 0,
    },
    "async_inference": {
        "server_address": "127.0.0.1:8080",
        "actions_per_chunk": 50, "chunk_size_threshold": 0.5, "fixed_update_fps": 30,
        "model_path": "outputs/train/act_rebot_test/checkpoints/last/pretrained_model"},
    "teleop": {"display_data": True},
    "camera_backend": "opencv",
}

# 文档命令中心按运行平台给出「当前系统里真的能执行」的命令：
# Windows 上没有 /dev、sudo、bash，把这些命令摆出来只会让用户点了必然失败。
_INSTALL_COMMANDS_COMMON: list[dict[str, str]] = [
    {"group": "conda", "label": "创建 lerobot 虚拟环境",
     "cmd": "conda create -y -n lerobot python=3.12"},
    {"group": "仓库", "label": "克隆 Seeed 验证版 LeRobot（gh-proxy）",
     "cmd": f"git clone {REPO_URL} lerobot"},
    {"group": "安装", "label": "安装 LeRobot 主项目（清华源）",
     "cmd": f"pip install -e ./lerobot -i {PIP_INDEX}"},
    {"group": "安装", "label": "安装幻尔主臂 + Seeed 从臂插件（清华源）",
     "cmd": f"pip install lerobot-teleoperator-rebot-arm-102 lerobot-robot-seeed-b601 motorbridge -i {PIP_INDEX}"},
    {"group": "安装", "label": "安装 Web / 相机依赖（清华源）",
     "cmd": f"pip install fastapi uvicorn pyserial opencv-python numpy websockets python-multipart PyYAML zxing-cpp ultralytics -i {PIP_INDEX}"},
    {"group": "安装", "label": "安装 YOLO 训练依赖（清华源）",
     "cmd": f"pip install ultralytics -i {PIP_INDEX}"},
    {"group": "安装", "label": "安装 ffmpeg（文档要求 7.X）",
     "cmd": "conda install -y ffmpeg -c conda-forge"},
    {"group": "相机", "label": "切换 DepthCameraSupport 分支",
     "cmd": "git checkout DepthCameraSupport && git pull origin DepthCameraSupport"},
    {"group": "相机", "label": "RealSense 依赖", "cmd": 'pip install -e ".[realsense]"'},
    {"group": "相机", "label": "Orbbec 依赖", "cmd": 'pip install -e ".[orbbec]"'},
]

LINUX_INSTALL_COMMANDS: list[dict[str, str]] = [
    {"group": "conda", "label": "下载并安装 Miniforge（gh-proxy）",
     "cmd": f"curl -fL --connect-timeout 10 -o /tmp/miniforge.sh '{CONDA_URL_LINUX}' && bash /tmp/miniforge.sh -b -p $HOME/miniforge3"},
    *_INSTALL_COMMANDS_COMMON,
    {"group": "权限", "label": "串口权限（启动网页时已提示输入密码执行）",
     "cmd": "sudo chmod 666 /dev/ttyUSB* /dev/ttyACM*"},
    {"group": "权限", "label": "USB 相机权限", "cmd": "sudo chmod a+rw /dev/bus/usb/*/*"},
    {"group": "Hub", "label": "登录 Hugging Face",
     "cmd": "huggingface-cli login --token ${HUGGINGFACE_TOKEN} --add-to-git-credential"},
]

WINDOWS_INSTALL_COMMANDS: list[dict[str, str]] = [
    {"group": "conda", "label": "下载 Miniforge Windows 安装包（gh-proxy，静默安装请用「一键安装 Miniforge」）",
     "cmd": f'curl -fL --connect-timeout 10 -o "%TEMP%\\Miniforge3.exe" "{CONDA_URL_WINDOWS}"'},
    *_INSTALL_COMMANDS_COMMON,
    {"group": "Hub", "label": "登录 Hugging Face",
     "cmd": "huggingface-cli login --token %HUGGINGFACE_TOKEN% --add-to-git-credential"},
]

INSTALL_COMMANDS = WINDOWS_INSTALL_COMMANDS if IS_WINDOWS else LINUX_INSTALL_COMMANDS


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _posix_leftover(value: Any) -> bool:
    """判断是不是 Ubuntu 真机残留的绝对路径（Windows 盘符路径不算）。"""
    v = str(value or "").strip()
    if not v or re.match(r"^[A-Za-z]:[\\/]", v):
        return False
    return v.startswith(("/", "\\")) or "/bin/python" in v.replace("\\", "/")


def _normalize_platform_config(cfg: dict) -> dict:
    """把 Ubuntu 真机迁移到 Windows 后 config.json 里的 Linux 专属值清理掉。

    只改内存中的配置（不静默写盘）：/dev 串口、Linux 版 conda 环境路径、
    Linux 安装包 URL、Linux 数据集路径都换成 Windows 可用的等效值（多数留空，
    由用户在页面重新选择）。页面下次保存配置时自然会把新值落盘。
    """
    if not IS_WINDOWS:
        return cfg
    for side in ("leader", "follower"):
        part = cfg.get(side)
        if isinstance(part, dict) and str(part.get("port") or "").startswith("/dev/"):
            part["port"] = ""
    da = cfg.get("dual_arm")
    if isinstance(da, dict):
        for side in ("left", "right"):
            part = da.get(side)
            if not isinstance(part, dict):
                continue
            for key in ("follower_port", "leader_port"):
                if str(part.get(key) or "").startswith("/dev/"):
                    part[key] = ""
    ec = cfg.get("environment")
    if isinstance(ec, dict):
        for key in ("env_dir", "lerobot_python", "lerobot_bin_dir", "lerobot_env_dir"):
            if _posix_leftover(ec.get(key)):
                ec[key] = ""
    st = cfg.get("setup")
    if isinstance(st, dict) and "Miniforge3-Linux" in str(st.get("conda_url") or ""):
        st["conda_url"] = CONDA_URL_WINDOWS
    tr = cfg.get("train")
    if isinstance(tr, dict) and _posix_leftover(tr.get("dataset_dir")):
        tr["dataset_dir"] = ""
    return cfg


DUAL_SIDES = ("left", "right")
DUAL_PORT_KEYS = ("left_follower", "left_leader", "right_follower", "right_leader")
# 协作编排模板：只决定任务描述与阶段提示，不伪造任何硬件动作或采集数据
COORDINATION_TASKS: dict[str, dict[str, Any]] = {
    "sync": {"label": "双臂同步", "task": "双臂同步协同操作",
             "desc": "两臂在同一控制循环里同时动作，适合同步抓取/放置",
             "phases": [{"name": "准备", "seconds": 3, "left": "回到初始位姿", "right": "回到初始位姿", "sync": True},
                        {"name": "同步接近", "seconds": 6, "left": "接近目标左侧", "right": "接近目标右侧", "sync": True},
                        {"name": "协同夹持", "seconds": 6, "left": "闭合并保持", "right": "闭合并保持", "sync": True},
                        {"name": "同步搬运", "seconds": 8, "left": "沿同一轨迹移动", "right": "沿同一轨迹移动", "sync": True},
                        {"name": "复位", "seconds": 4, "left": "回零卸力", "right": "回零卸力", "sync": True}]},
    "handover": {"label": "双手交接", "task": "左臂抓取并交接给右臂",
                 "desc": "左臂抓取 → 中间交接 → 右臂放置，交接点必须两臂同时到位",
                 "phases": [{"name": "准备", "seconds": 3, "left": "回到初始位姿", "right": "回到初始位姿", "sync": True},
                            {"name": "左臂抓取", "seconds": 7, "left": "接近并抓取物体", "right": "在交接位等待", "sync": False},
                            {"name": "中间交接", "seconds": 6, "left": "送到交接点并张开", "right": "对准交接点并夹持", "sync": True},
                            {"name": "右臂放置", "seconds": 7, "left": "撤离让位", "right": "搬运并放置", "sync": False},
                            {"name": "复位", "seconds": 4, "left": "回零卸力", "right": "回零卸力", "sync": True}]},
    "transport": {"label": "协同搬运", "task": "双臂协同托举长形物体",
                  "desc": "两臂分别托住物体两端，保持同步速度移动",
                  "phases": [{"name": "准备", "seconds": 3, "left": "回到初始位姿", "right": "回到初始位姿", "sync": True},
                             {"name": "对准两端", "seconds": 6, "left": "对准左端", "right": "对准右端", "sync": True},
                             {"name": "同时托举", "seconds": 5, "left": "托举左端", "right": "托举右端", "sync": True},
                             {"name": "同步平移", "seconds": 10, "left": "匀速平移", "right": "匀速平移", "sync": True},
                             {"name": "复位", "seconds": 4, "left": "回零卸力", "right": "回零卸力", "sync": True}]},
    "mirror": {"label": "镜像对称", "task": "双臂镜像对称操作",
               "desc": "两臂做左右对称动作，用于对称装配与折叠类任务",
               "phases": [{"name": "准备", "seconds": 3, "left": "回到初始位姿", "right": "回到初始位姿", "sync": True},
                          {"name": "镜像展开", "seconds": 7, "left": "向左外侧展开", "right": "向右外侧展开", "sync": True},
                          {"name": "镜像合拢", "seconds": 7, "left": "向中心合拢", "right": "向中心合拢", "sync": True},
                          {"name": "同步抬升", "seconds": 6, "left": "抬升", "right": "抬升", "sync": True},
                          {"name": "复位", "seconds": 4, "left": "回零卸力", "right": "回零卸力", "sync": True}]},
}


def dual_enabled(cfg: dict) -> bool:
    return bool((cfg.get("dual_arm") or {}).get("enabled"))


def dual_side(cfg: dict, side: str) -> dict:
    """取单侧（left/right）双臂配置，缺字段用默认值补齐。"""
    d = cfg.get("dual_arm") or {}
    return _deep_merge(DEFAULT_CONFIG["dual_arm"][side], d.get(side) or {})


def dual_ports(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for side in DUAL_SIDES:
        a = dual_side(cfg, side)
        out[f"{side}_follower"] = str(a.get("follower_port") or "").strip()
        out[f"{side}_leader"] = str(a.get("leader_port") or "").strip()
    return out


def _relocate_env_dir(name: str) -> Optional[Path]:
    """按环境名把 Ubuntu 上保存的虚拟环境重新定位到本机真实 conda 环境。

    只读 conda 自己的 environments.txt 与 conda-meta 目录，找不到返回 None，
    绝不臆造一个不存在的环境目录。
    """
    if not name:
        return None
    home = Path.home()
    cands = [home / ".conda" / "envs" / name,
             home / "miniforge3" / "envs" / name,
             home / "miniconda3" / "envs" / name,
             home / "anaconda3" / "envs" / name]
    known = home / ".conda" / "environments.txt"
    if known.exists():
        try:
            for line in known.read_text("utf-8", errors="replace").splitlines():
                p = Path(line.strip())
                if p.name == name:
                    cands.insert(0, p)
        except OSError:
            pass
    for p in cands:
        try:
            if (p / "conda-meta").is_dir():
                return p
        except OSError:
            continue
    return None


CONFIG_LOCK = threading.RLock()


def load_config() -> dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            return _normalize_platform_config(_deep_merge(
                DEFAULT_CONFIG, json.loads(CONFIG_FILE.read_text("utf-8"))))
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[config] 读取失败，已回退默认配置：{_brief(exc)}", "warn")
    return _normalize_platform_config(json.loads(json.dumps(DEFAULT_CONFIG)))


def save_config(patch: Optional[dict]) -> dict[str, Any]:
    # Serialize read/merge/replace: independent autosaves must not lose each other.
    with CONFIG_LOCK:
        previous = load_config()
        cfg = _deep_merge(previous, patch or {})
        if cfg == previous and CONFIG_FILE.exists():
            return cfg
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".config-", dir=str(CONFIG_FILE.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(cfg, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, CONFIG_FILE)
        except OSError as exc:
            raise ApiError(f"配置写入失败：{_brief(exc)}", 500)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        telemetry=globals().get('TELEMETRY')
        if telemetry is not None and any(k in (patch or {}) for k in ('dual_arm','follower')):
            telemetry.configure(monitor_ports(cfg))
        hub = globals().get("HUB")
        if hub:
            hub.publish({"type": "config", "revision": CONFIG_FILE.stat().st_mtime_ns,
                         "sections": list((patch or {}).keys())})
        return cfg


# ==================================================================
# 3. 日志总线（子线程 -> asyncio -> WebSocket，线程安全）
# ==================================================================
class LogHub:
    def __init__(self, maxlen: int = 3000) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self.history: deque[dict] = deque(maxlen=maxlen)
        self.loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _safe_put(q: asyncio.Queue, ev: dict) -> None:
        try:
            q.put_nowait(ev)
        except Exception:  # noqa: BLE001
            pass

    def publish(self, event: dict) -> None:
        event.setdefault("ts", round(time.time(), 3))
        with self._lock:
            self.history.append(event)
            subs = list(self._subs)
        loop = self.loop
        for q in subs:
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._safe_put, q, event)
                    continue
                except RuntimeError:
                    pass
            self._safe_put(q, event)

    def log(self, line: str, level: str = "info") -> None:
        lvl = "error" if re.search(r"error|traceback|failed|no such|not found|permission denied",
                                   str(line), re.I) else level
        self.publish({"type": "log", "level": lvl, "line": str(line).rstrip()})

    def state(self, **kw: Any) -> None:
        self.publish({"type": "state", **kw})

    def loss(self, step: int, value: float, **metadata) -> None:
        if math.isfinite(value):
            self.publish({"type": "loss", "step": step, "value": round(value, 6), **metadata})

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def snapshot(self, n: int = 300) -> list[dict]:
        with self._lock:
            return list(self.history)[-n:]


HUB = LogHub()


# ==================================================================
# 4. 全局状态：急停 Flag / 虚拟环境 / 串口连接
# ==================================================================
@dataclass
class Job:
    name: str
    kind: str
    command: list[str]
    display: str
    proc: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    started_at: float = field(default_factory=time.time)
    job_id: str = field(default_factory=lambda: os.urandom(12).hex())
    prompt_id: int = 0
    stopping: bool = False
    result: Optional[dict] = None
    finish_lock: threading.Lock = field(default_factory=threading.Lock)
    finished: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    stdin_data: str | None = None
    # 子进程 stdin 保持打开的管道：lerobot-calibrate 等命令会用 input() 等回车，
    # 必须能由网页 /api/job/input 写入，否则会立刻 EOFError。
    stdin_live: bool = False
    awaiting_input: bool = False
    awaiting_prompt: str = ""
    input_lock: threading.Lock = field(default_factory=threading.Lock)
    # 最近若干行真实输出，用于任务失败后给出针对性提示
    tail: deque = field(default_factory=lambda: deque(maxlen=60))
    # 命令链进度（全部来自真实子进程，不是模拟值）
    step_index: int = 0
    step_total: int = 0
    step_label: str = ""
    step_started_at: float = field(default_factory=time.time)
    step_limit: int = STEP_TIMEOUT
    last_line: str = ""
    last_line_at: float = field(default_factory=time.time)
    progress_stop: threading.Event = field(default_factory=threading.Event)


class EnvState:
    def __init__(self) -> None:
        self.active_env = ""
        self.env_dir: Optional[Path] = None
        self.connected = False
        self.leader_port = ""
        self.follower_port = ""
        # 双臂：保存真实打开过的 4 路串口；进程重启后必须重新点「连接」
        self.dual_connected = False
        self.dual_ports: dict[str, str] = {}
        # 上一次确认过的 LeRobot 运行时（真实探测结果，重启后先按它复用，再异步复核）
        self.lerobot_runtime: dict = {}

    def load(self) -> None:
        cfg = load_config()
        ec = cfg["environment"]
        self.active_env = ec.get("active_env", "")
        d = ec.get("env_dir", "")
        self.env_dir = Path(d) if d else None
        if IS_WINDOWS and (self.env_dir is None
                           or not (self.env_dir / "conda-meta").is_dir()):
            # 从 Ubuntu 迁移过来的环境路径在本机不存在：按名字重定位到真实 conda 环境
            relocated = _relocate_env_dir(self.active_env)
            if relocated:
                HUB.log(f"[env] 已把虚拟环境 {self.active_env} 重定位到本机真实目录："
                        f"{relocated}", "ok")
            self.env_dir = relocated
        self.leader_port = cfg["leader"]["port"]
        self.follower_port = cfg["follower"]["port"]
        self.dual_ports = dual_ports(cfg)
        py = str(ec.get("lerobot_python") or "").strip()
        self.lerobot_runtime = {
            "python": py,
            "bin_dir": str(ec.get("lerobot_bin_dir") or ""),
            "env_dir": str(ec.get("lerobot_env_dir") or ""),
            "installed": bool(py),
            "presumed": bool(py),   # 仅从配置恢复，尚未用真实探测复核
        }
        # 进程重启后必须重新点「连接」，不恢复连接标记
        self.connected = False


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.job: Optional[Job] = None
        self.step_counter = 0
        self.backend_cache: dict[str, tuple[float, dict]] = {}
        self.lerobot_cache: dict[str, Any] = {}
        self.lerobot_lock = threading.RLock()

    def running(self) -> bool:
        with self.lock:
            return bool(self.job and not self.job.finished.is_set())


STATE = AppState()
ENV = EnvState()
ENV.load()

# 网页可视化：Rerun 的 web 查看器（录制时在浏览器里看相机画面）。
# 不使用 rr.spawn 的本地窗口，而是让录制进程 connect_grpc 到这个常驻服务。
RERUN_GRPC_PORT = int(os.environ.get("RERUN_GRPC_PORT", "9876"))
RERUN_WEB_PORT = int(os.environ.get("RERUN_WEB_PORT", "9090"))
VIZ_WEB: dict[str, Any] = {"proc": None}


# ==================================================================
# 5. conda 环境（真实读取，不存在就明确报错）
# ==================================================================
def _norm_path(p: Any) -> str:
    """路径比较统一大小写与分隔符，Windows 上尤其必要。"""
    try:
        return os.path.normcase(os.path.normpath(str(p)))
    except (OSError, ValueError):
        return str(p)


def find_conda() -> str:
    """找到真实存在的 conda 可执行文件；Windows 上是 .exe/.bat 而不是 bin/conda。"""
    home = Path.home()
    candidates = [os.environ.get("CONDA_EXE", ""), shutil.which("conda") or ""]
    if IS_WINDOWS:
        candidates += [
            str(home / "miniforge3" / "Scripts" / "conda.exe"),
            str(home / "miniforge3" / "condabin" / "conda.bat"),
            str(home / "miniconda3" / "Scripts" / "conda.exe"),
            str(home / "miniconda3" / "condabin" / "conda.bat"),
            str(home / "anaconda3" / "Scripts" / "conda.exe"),
            str(home / "anaconda3" / "condabin" / "conda.bat"),
            os.environ.get("CONDA_BAT", ""),
            shutil.which("conda.exe") or "",
            shutil.which("conda.bat") or "",
            r"C:\ProgramData\miniforge3\Scripts\conda.exe",
            r"C:\ProgramData\miniforge3\condabin\conda.bat",
            r"C:\ProgramData\Miniconda3\Scripts\conda.exe",
            r"C:\ProgramData\Anaconda3\Scripts\conda.exe",
        ]
    else:
        candidates += [
            str(home / "miniforge3" / "bin" / "conda"),
            str(home / "miniconda3" / "bin" / "conda"),
            str(home / "anaconda3" / "bin" / "conda"),
            "/opt/conda/bin/conda",
        ]
    for c in candidates:
        try:
            if c and Path(c).exists():
                return c
        except OSError:
            continue
    return ""


def conda_cmd(*args: str, conda: str = "") -> list[str]:
    """构造 conda 的 argv：Windows 的 conda.bat 必须经 cmd.exe 才能被 CreateProcess 启动。"""
    fallback = (CONDA_BASE / "Scripts" / "conda.exe" if IS_WINDOWS
                else CONDA_BASE / "bin" / "conda")
    exe = conda or find_conda() or str(fallback)
    if IS_WINDOWS and str(exe).lower().endswith((".bat", ".cmd")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(exe), *args]
    return [str(exe), *args]


def _dir_writable(p: Path) -> bool:
    """目录不存在时检查最近的存在祖先；真实可写性仍由 conda 创建时裁决。"""
    cur = p
    for _ in range(16):
        try:
            if cur.exists():
                return os.access(str(cur), os.W_OK)
        except OSError:
            return False
        if cur.parent == cur:
            return False
        cur = cur.parent
    return False


def _conda_envs_dirs(conda: str) -> list[str]:
    """读取 conda 真实配置的 envs_dirs，不用猜测的默认路径。"""
    if not conda:
        return []
    try:
        r = subprocess.run(conda_cmd("info", "--json", conda=conda), capture_output=True,
                           text=True, encoding=SUBPROC_KW_ENCODING, errors="replace", timeout=20)
        if r.returncode != 0:
            HUB.log(f"[env] conda info 读取失败：{(r.stderr or '').strip()[:200]}", "warn")
            return []
        return [str(d) for d in (json.loads(r.stdout).get("envs_dirs") or []) if str(d).strip()]
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[env] conda info 读取失败：{_brief(exc)}", "warn")
        return []


def conda_envs() -> list[dict]:
    """真实环境列表；额外合并 conda environments.txt 与自定义位置创建的 prefix 环境。"""
    paths: list[str] = []
    conda = find_conda()
    if conda:
        try:
            r = subprocess.run(conda_cmd("env", "list", "--json", conda=conda),
                               capture_output=True, text=True,
                               encoding=SUBPROC_KW_ENCODING, errors="replace", timeout=20)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip() or f"exit {r.returncode}")
            paths += [str(p) for p in json.loads(r.stdout).get("envs", [])]
        except Exception as exc:  # noqa: BLE001
            HUB.log(f"[env] 读取 conda 环境失败：{_brief(exc)}", "warn")
    known = Path.home() / ".conda" / "environments.txt"
    if known.exists():
        try:
            paths += [ln.strip() for ln in known.read_text("utf-8", errors="replace").splitlines()
                       if ln.strip()]
        except OSError:
            pass
    try:
        ec = load_config()["environment"]
        if ec.get("env_dir"):
            paths.append(str(ec["env_dir"]))
        if ec.get("env_location") and ec.get("active_env"):
            paths.append(str(Path(str(ec["env_location"])) / str(ec["active_env"])))
    except Exception:  # noqa: BLE001
        pass
    out: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        try:
            pp = Path(p).expanduser()
            # 只认真实存在的 conda 环境目录，避免把普通目录当成环境
            if not (pp / "conda-meta").is_dir():
                continue
            key = _norm_path(pp)
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": pp.name or "base", "path": str(pp)})
        except OSError:
            continue
    return out


def conda_env_locations() -> dict:
    """创建位置的真实候选：conda envs_dirs + 常见安装目录 + 已有环境所在目录 + 上次选择。"""
    cfg = load_config()["environment"]
    conda = find_conda()
    envs_dirs = _conda_envs_dirs(conda)
    default = envs_dirs[0] if envs_dirs else str(CONDA_BASE / "envs")
    candidates: list[tuple[str, str, str]] = []
    for i, d in enumerate(envs_dirs):
        candidates.append((d, "conda 默认 envs 目录" if i == 0 else f"conda envs_dirs #{i + 1}",
                           "conda"))
    candidates += [
        (str(CONDA_BASE / "envs"), "Miniforge / envs", "常见安装"),
        (str(Path.home() / "miniconda3" / "envs"), "Miniconda / envs", "常见安装"),
        (str(Path.home() / "anaconda3" / "envs"), "Anaconda / envs", "常见安装"),
        (str(Path.home() / ".conda" / "envs"), "用户级 conda 环境目录", "用户目录"),
        (str(Path.home() / "conda_envs"), "用户主目录 conda_envs", "用户目录"),
        (str(ROOT / "envs"), "项目目录 envs（便于随项目迁移）", "项目目录"),
    ]
    base_roots = {_norm_path(Path(conda).parent.parent)} if conda else set()
    for e in conda_envs():
        ep = Path(e["path"])
        # base 环境的父目录是 conda 安装目录，不适合作为新环境的存放位置
        if _norm_path(ep) in base_roots or ep.parent == ep:
            continue
        candidates.append((str(ep.parent), f"已有环境 {e['name']} 所在目录", "已有环境"))
    saved = str(cfg.get("env_location") or "").strip()
    if saved:
        candidates.insert(0, (saved, "上次使用的创建位置", "已保存"))
    locations: list[dict] = []
    seen: set[str] = set()
    for raw, label, source in candidates:
        try:
            p = Path(raw).expanduser()
            key = _norm_path(p)
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        locations.append({
            "path": str(p), "label": label, "source": source,
            "exists": p.is_dir(), "writable": _dir_writable(p),
            "default": key == _norm_path(default),
        })
    locations.sort(key=lambda x: (not x["default"], x["source"] != "conda",
                                  not x["writable"], x["path"]))
    return {
        "locations": locations, "default": default, "saved": saved,
        "conda_found": bool(conda), "conda_path": conda,
        "home": str(Path.home()), "project_root": str(ROOT),
        "platform": platform.system(),
    }


def resolve_env_target(location: str, name: str) -> Optional[Path]:
    """返回 conda -p 的完整环境目录；None 表示交给 conda 用 -n 放到默认 envs 目录。"""
    txt = (location or "").strip()
    if not txt or txt.lower() in ("default", "__default__", "@default"):
        return None
    if "\x00" in txt:
        raise ApiError("创建位置包含非法字符", 400)
    try:
        parent = Path(txt).expanduser()
    except (OSError, ValueError) as exc:
        raise ApiError(f"创建位置无效：{_brief(exc)}", 400)
    if not parent.is_absolute():
        raise ApiError(f"创建位置必须是绝对路径（例如 /data/conda_envs 或 "
                       f"D:\\conda\\envs）：{parent}", 400)
    try:
        parent = parent.resolve()
    except OSError as exc:
        raise ApiError(f"创建位置无法解析：{_brief(exc)}", 400)
    if parent.parent == parent:
        raise ApiError("不能把文件系统根目录作为创建位置，请选择或输入一个具体目录", 400)
    target = parent / name
    if target.exists():
        try:
            occupied = any(target.iterdir())
        except OSError as exc:
            raise ApiError(f"无法检查目标目录 {target}：{_brief(exc)}", 403)
        if occupied:
            raise ApiError(f"目标环境目录已存在且非空：{target}。"
                           f"请换一个环境名，或选择其它创建位置", 409)
    if not _dir_writable(parent):
        raise ApiError(f"创建位置不可写：{parent}。请改选可写目录，或先在服务器上"
                       f"用 chown/chmod 授予当前用户写权限", 403)
    return target


def select_env(target: str) -> dict:
    """支持按环境名或完整路径选择，避免自定义位置下同名环境混淆。"""
    envs = conda_envs()
    t = (target or "").strip()
    hit = next((e for e in envs
                if _norm_path(e["path"]) == _norm_path(t) or e["name"] == t), None)
    if not hit:
        raise ApiError(f"未找到 conda 环境「{t}」。请先在启动向导中创建，"
                       f"或确认 Miniforge/conda 已安装 ({find_conda() or '未检测到 conda'})", 404)
    ENV.active_env = hit["name"]
    ENV.env_dir = Path(hit["path"])
    save_config({"environment": {"active_env": hit["name"], "env_dir": hit["path"]}})
    # 换环境后必须重新探测 LeRobot，不能沿用上一个环境的安装状态
    STATE.lerobot_cache.clear()
    STATE.backend_cache.clear()
    ENV.lerobot_runtime = {}
    HUB.log(f"[env] 已选择虚拟环境：{hit['name']} -> {ENV.env_dir}", "ok")
    HUB.state(env_ready=True, active_env=hit["name"])
    return env_status()


def env_status() -> dict:
    cfg = load_config()
    envs = conda_envs()
    lerobot = detect_lerobot()
    active_dir = _norm_path(ENV.env_dir) if ENV.env_dir else ""
    return {
        "conda_found": bool(find_conda()),
        "conda_path": find_conda(),
        "envs": [{**e, "active": (_norm_path(e["path"]) == active_dir)
                  if active_dir else e["name"] == ENV.active_env} for e in envs],
        "active": ENV.active_env,
        "env_dir": str(ENV.env_dir or ""),
        "env_location": cfg["environment"].get("env_location", ""),
        "lerobot_installed": bool(lerobot["installed"]),
        "lerobot": lerobot,
        # 系统里已装好 LeRobot 时，不必再建 conda 环境即可直接使用
        "ready": bool(ENV.active_env) or bool(lerobot["installed"]),
        "connected": ENV.connected,
        "leader_port": ENV.leader_port,
        "follower_port": ENV.follower_port,
        "dual_connected": ENV.dual_connected,
        "dual_arm": _dual_status(cfg),
        "setup": cfg["setup"],
        "repo_dir": str(REPO_DIR),
        "platform": platform.system(),
        "serial": serial_permission_report(),
    }


# ==================================================================
# 5b. LeRobot 安装探测：装好了就直接复用，不重复 clone / 重装
# ==================================================================
LEROBOT_PLUGIN_DISTS = [
    "lerobot-teleoperator-rebot-arm-102", "lerobot-robot-seeed-b601", "motorbridge",
]
LEROBOT_WEB_DISTS = ["fastapi", "uvicorn", "pyserial", "opencv-python", "numpy", "websockets", "python-multipart", "PyYAML", "zxing-cpp", "ultralytics"]

# -I 忽略 cwd / PYTHONPATH，避免把项目里同名 lerobot/ 源码目录误判成已安装；
# 只查真实 site-packages 与发行包元数据，不真正 import（避免导入 torch 拖慢探测）。
_PROBE_PY = r'''
import json, sys, sysconfig
from importlib import metadata, util

out = {"python": sys.executable, "version": "", "module": "", "scripts": "", "packages": {}}
try:
    spec = util.find_spec("lerobot")
    if spec is not None:
        loc = spec.submodule_search_locations
        out["module"] = spec.origin or (list(loc)[0] if loc else "")
except Exception as exc:
    out["error"] = str(exc)[:160]
try:
    out["version"] = metadata.version("lerobot")
except Exception:
    pass
try:
    out["scripts"] = sysconfig.get_path("scripts") or ""
except Exception:
    pass
for n in ["lerobot", "lerobot-teleoperator-rebot-arm-102", "lerobot-robot-seeed-b601",
          "motorbridge", "fastapi", "uvicorn", "pyserial", "opencv-python", "numpy", "websockets", "python-multipart", "PyYAML", "zxing-cpp", "ultralytics"]:
    try:
        out["packages"][n] = metadata.version(n)
    except Exception:
        out["packages"][n] = ""
out["has_module"] = bool(out["module"])
print(json.dumps(out, ensure_ascii=False))
'''


def _probe_python(py: str) -> dict:
    try:
        r = subprocess.run([py, "-I", "-c", _PROBE_PY], capture_output=True, text=True,
                           encoding=SUBPROC_KW_ENCODING, errors="replace",
                           timeout=15, cwd=str(tempfile.gettempdir()))
        if r.returncode != 0:
            last = (r.stderr or "").strip().splitlines()
            return {"python": py, "has_module": False, "packages": {},
                    "error": last[-1] if last else f"exit {r.returncode}"}
        lines = (r.stdout or "").strip().splitlines()
        return json.loads(lines[-1]) if lines else {"python": py, "has_module": False,
                                                  "packages": {}, "error": "探测无输出"}
    except subprocess.TimeoutExpired:
        return {"python": py, "has_module": False, "packages": {}, "error": "探测超时（15 秒）"}
    except Exception as exc:  # noqa: BLE001
        return {"python": py, "has_module": False, "packages": {}, "error": _brief(exc)}


def _python_rels() -> tuple[str, ...]:
    return ("bin/python", "bin/python3") if not IS_WINDOWS else \
           ("python.exe", "Scripts/python.exe")


def lerobot_candidates() -> list[tuple[str, str, str]]:
    """(python 路径, 来源, 环境目录)：全部来自真实 conda 环境 / 配置 / PATH。"""
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(py: str, source: str, env_dir: str = "") -> None:
        try:
            if not str(py or "").strip():   # Path("") 会变成 "."，绝不能当成 Python 可执行文件
                return
            p = str(Path(py).expanduser())
            if not p or not Path(p).exists():
                return
            key = _norm_path(p)
            if key in seen:
                return
            seen.add(key)
            items.append((p, source, env_dir))
        except (OSError, ValueError):
            return

    ec = load_config()["environment"]
    add(str(ec.get("lerobot_python") or ""), "上次使用的 LeRobot 环境",
        str(ec.get("lerobot_env_dir") or ""))
    if ENV.env_dir:
        for rel in _python_rels():
            add(str(ENV.env_dir / rel), "当前选择的 conda 环境", str(ENV.env_dir))
    for e in conda_envs()[:8]:
        for rel in _python_rels():
            add(str(Path(e["path"]) / rel), "conda 环境 " + e["name"], e["path"])
    add(sys.executable, "运行网页服务的 Python")
    for n in ("python3", "python"):
        w = shutil.which(n)
        if w:
            add(w, "PATH 中的 " + n)
    exe = shutil.which("lerobot-train")
    if exe:
        d = Path(exe).parent
        for n in ("python", "python3", "python.exe"):
            add(str(d / n), "PATH 中 lerobot-train 所在环境",
                str(d.parent) if (d.parent / "conda-meta").is_dir() else "")
    return items[:12]


def detect_lerobot(force: bool = False) -> dict:
    """并行探测真实 Python 环境，找到第一个装了 lerobot 的作为运行时（并发安全）。"""
    with STATE.lerobot_lock:
        return _detect_lerobot_locked(force)


def _detect_lerobot_locked(force: bool) -> dict:
    now = time.time()
    cached = STATE.lerobot_cache
    if not force and cached and now - float(cached.get("ts", 0)) < 25:
        return cached["result"]

    items = lerobot_candidates()
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as pool:
        futures = [(py, src, env, pool.submit(_probe_python, py)) for py, src, env in items]
        probes = []
        for py, src, env, fut in futures:
            try:
                p = fut.result(timeout=18)
            except Exception as exc:  # noqa: BLE001
                p = {"python": py, "has_module": False, "packages": {}, "error": _brief(exc)}
            probes.append((src, env, p))

    checked = [{"python": p.get("python", ""), "source": src, "env_dir": env,
                "has_module": bool(p.get("has_module")), "version": p.get("version", ""),
                "error": p.get("error", "")} for src, env, p in probes]
    hit = next(((src, env, p) for src, env, p in probes if p.get("has_module")), None)

    if hit:
        src, env, p = hit
        packages = p.get("packages") or {}
        missing = [d for d in LEROBOT_PLUGIN_DISTS + LEROBOT_WEB_DISTS if not packages.get(d)]
        cli = ""
        for d in (p.get("scripts", ""), str(Path(p.get("python", "")).parent)):
            if not d:
                continue
            for n in ("lerobot-train", "lerobot-train.exe"):
                cand = Path(d) / n
                if cand.exists():
                    cli = str(cand)
                    break
            if cli:
                break
        if not cli:
            cli = shutil.which("lerobot-train") or ""
        result = {
            "installed": True, "source": src, "python": p.get("python", ""),
            "env_dir": env, "bin_dir": p.get("scripts", ""),
            "version": p.get("version") or "版本元数据不可用",
            "module": p.get("module", ""), "cli": cli,
            "missing": missing, "packages": packages,
            "checked": checked, "detected_at": now,
        }
        ENV.lerobot_runtime = dict(result)
    else:
        result = {
            "installed": False, "source": "", "python": "", "env_dir": "",
            "bin_dir": "", "version": "", "module": "", "cli": "",
            "missing": LEROBOT_PLUGIN_DISTS + LEROBOT_WEB_DISTS,
            "packages": {}, "checked": checked, "detected_at": now,
            "note": "已检测 " + str(len(checked)) + " 个 Python 环境，均未找到 lerobot 模块",
        }
        ENV.lerobot_runtime = {}
    STATE.lerobot_cache = {"ts": now, "result": result}
    return result


def lerobot_reusable(info: dict, force: bool = False) -> bool:
    """是否可以直接复用：用户明确选了另一个 conda 环境时，仍需把 LeRobot 装进所选环境。"""
    if not info.get("installed") or force:
        return False
    if ENV.env_dir:
        return bool(info.get("env_dir")) and \
            _norm_path(info["env_dir"]) == _norm_path(ENV.env_dir)
    return True


def use_lerobot(info: Optional[dict] = None) -> dict:
    """把探测到的已安装 LeRobot 设为当前运行时；conda 环境顺带设为已选环境。"""
    info = info or detect_lerobot(force=True)
    if not info["installed"]:
        raise ApiError("未检测到已安装的 LeRobot。请点「一键安装 LeRobot」，"
                       "或确认安装它的 Python 环境能被后端访问（conda 环境需含 conda-meta）", 404)
    patch: dict[str, Any] = {"environment": {
        "lerobot_python": info["python"], "lerobot_bin_dir": info["bin_dir"],
        "lerobot_env_dir": info["env_dir"]}}
    hit = None
    if info["env_dir"] and Path(info["env_dir"]).exists():
        hit = next((e for e in conda_envs()
                    if _norm_path(e["path"]) == _norm_path(info["env_dir"])), None)
    if hit:
        ENV.active_env = hit["name"]
        ENV.env_dir = Path(hit["path"])
        patch["environment"]["active_env"] = hit["name"]
        patch["environment"]["env_dir"] = hit["path"]
    else:
        # 装在非 conda 的 Python（系统 / venv）里：清掉旧的所选环境，
        # 否则 _py() 会优先用那个环境、把命令发到没装 LeRobot 的解释器。
        ENV.active_env = ""
        ENV.env_dir = None
        patch["environment"]["active_env"] = ""
        patch["environment"]["env_dir"] = ""
    save_config(patch)
    STATE.backend_cache.clear()
    HUB.log(f"[lerobot] 直接复用已安装环境：{info['source']} -> {info['python']}", "ok")
    HUB.state(env_ready=True, active_env=ENV.active_env or "LeRobot 已安装")
    return ok(**env_status())


def _bin(name: str) -> str:
    """所选 conda 环境 > 已安装 LeRobot 的运行时 > PATH / 当前解释器。"""
    if ENV.env_dir:
        try:
            # Windows conda 环境：CLI 在 Scripts/*.exe，python 在环境根目录 python.exe
            cands = ((ENV.env_dir / "Scripts" / (name + ".exe"),
                      ENV.env_dir / (name + ".exe"),
                      ENV.env_dir / name)
                     if IS_WINDOWS else (ENV.env_dir / "bin" / name,))
            for cand in cands:
                if cand.exists():
                    return str(cand)
        except OSError:
            pass
    rt = ENV.lerobot_runtime or {}
    if rt.get("installed"):
        if name == "lerobot-train" and rt.get("cli"):
            return str(rt["cli"])
        d = str(rt.get("bin_dir") or "")
        if d:
            for n in ((name + ".exe", name) if IS_WINDOWS else (name,)):
                cand = Path(d) / n
                if cand.exists():
                    return str(cand)
    if name in ("python", "python3"):
        return str(rt.get("python") or sys.executable) if rt.get("installed") else sys.executable
    return name


def _py() -> list[str]:
    return LEROBOT_CMD_PREFIX + [_bin("python")]


def require_env() -> None:
    rt = ENV.lerobot_runtime or {}
    if rt.get("installed") and rt.get("python") and Path(rt["python"]).exists():
        return  # LeRobot 已装好，直接复用，不必再创建 conda 环境
    if not ENV.active_env:
        raise ApiError("尚未选择虚拟环境，也未检测到已安装的 LeRobot。请先在启动向导中"
                       "选择/创建 conda 环境，或点「直接使用已安装的 LeRobot」", 409)
    if ENV.env_dir and not ENV.env_dir.exists():
        raise ApiError(f"所选虚拟环境目录不存在：{ENV.env_dir}。请重新选择环境", 409)


# ==================================================================
# 6. 串口权限（真实检查 + sudo 密码授权，绝不静默假装成功）
# ==================================================================
def _serial_devices() -> list[str]:
    if IS_WINDOWS:
        return []
    return sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))


def _no_new_privs() -> bool:
    """NoNewPrivs=1 时 sudo 无法提权（沙箱/容器启动的服务常见），且无法在运行时撤销。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
    except OSError:
        pass
    return False


def serial_permission_report() -> dict:
    """Linux 查 /dev 节点权限；Windows 查真实 COM 端口（无需 chmod）。

    返回 requires_chmod 供前端决定要不要显示 sudo 密码框与授权按钮，
    all_writable 表示「串口这一前置条件是否已满足」。
    """
    if IS_WINDOWS:
        try:
            ports = scan_ports()
            note = ("Windows 无需 sudo/chmod 授权；若端口打不开，请在设备管理器确认 "
                    "USB 转串口驱动与 COM 编号，并关闭其它占用该端口的程序"
                    if ports else
                    "未检测到 COM 串口：请确认主从臂已接入、USB 转串口驱动已安装"
                    "（设备管理器 → 端口 COM 和 LPT）")
        except ApiError as exc:
            ports = []
            note = "串口扫描失败：" + str(exc.detail)
        items = [{"device": p["device"], "writable": True, "mode": "COM",
                  "description": p.get("description", "")} for p in ports]
        return {"devices": items,
                "all_writable": True, "sudo_available": False, "no_new_privs": False,
                "requires_chmod": False, "platform": "Windows", "note": note}

    devices = _serial_devices()
    items = []
    for d in devices:
        try:
            writable = os.access(d, os.R_OK | os.W_OK)
            mode = oct(os.stat(d).st_mode & 0o777)[2:]
            items.append({"device": d, "writable": writable, "mode": mode})
        except OSError as exc:
            items.append({"device": d, "writable": False, "mode": "", "error": _brief(exc)})
    nnp = _no_new_privs()
    if not devices:
        note = "未发现 /dev/ttyUSB* /dev/ttyACM* 设备，请确认主从臂已通过 USB 连接"
    elif nnp:
        note = ("当前服务进程带 no_new_privs 限制，sudo 无法提权；串口权限若已就绪可直接使用，"
                "否则请从桌面终端重新启动 start.py 后再授权")
    else:
        note = ""
    return {"devices": items,
            "all_writable": bool(items) and all(x["writable"] for x in items),
            "sudo_available": shutil.which("sudo") is not None,
            "no_new_privs": nnp,
            "requires_chmod": True, "platform": platform.system(),
            "note": note}


def run_chmod(password: str = "", usb: bool = False) -> dict:
    """真实执行 sudo chmod。密码只通过 stdin 传给 sudo，绝不写入日志。"""
    if IS_WINDOWS:
        # Windows 没有 /dev 节点与 chmod：直接返回真实 COM 列表，让按钮变成"刷新状态"
        rep = serial_permission_report()
        return {"skipped": True, "usb": usb,
                "reason": "Windows 使用 COM 端口与驱动，无需 sudo/chmod 授权",
                "devices": [d["device"] for d in rep["devices"]],
                "mode": "", "report": rep}
    if shutil.which("sudo") is None:
        raise ApiError("系统未安装 sudo，无法自动授权。请手动以管理员身份修改串口权限", 500)
    devices = sorted(glob.glob("/dev/bus/usb/*/*")) if usb else _serial_devices()
    if not devices:
        what = "USB 设备节点 /dev/bus/usb/*/*" if usb else "串口 /dev/ttyUSB* /dev/ttyACM*"
        raise ApiError(f"未发现{what}，请确认设备已连接后重试", 404)
    mode = "a+rw" if usb else "666"
    if password:
        cmd = ["sudo", "-S", "-p", "", "chmod", mode, *devices]
        stdin_data: Optional[str] = password + "\n"
    else:
        cmd = ["sudo", "-n", "chmod", mode, *devices]
        stdin_data = None
    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True,
                           encoding=SUBPROC_KW_ENCODING, errors="replace", timeout=25)
    except subprocess.TimeoutExpired:
        raise ApiError("sudo 授权超时（25 秒），请检查系统状态后重试", 504)
    except OSError as exc:
        raise ApiError(f"执行 sudo 失败：{_brief(exc)}", 500)
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        low = err.lower()
        if "no new privileges" in low or "no_new_privs" in low:
            raise ApiError(
                "当前服务进程带 no_new_privs 限制，sudo 无法提权（通常是被沙箱/容器启动）。"
                "请从桌面终端重新运行 start.py 后再授权；若「串口权限」已显示 666/可读写，"
                "可直接点「连接」，无需授权。", 409)
        if ("password" in low or "a password is required" in low or "incorrect" in low
                or "密码" in err):
            raise ApiError("需要 sudo 密码（或密码不正确）。请在页面输入当前用户密码后重试", 401)
        raise ApiError(f"授权失败：{err or 'sudo 返回非零退出码 ' + str(r.returncode)}", 400)
    bad = [d for d in devices if not os.access(d, os.R_OK | os.W_OK)]
    if bad:
        raise ApiError("命令已执行，但以下设备仍不可读写：" + ", ".join(bad), 500)
    HUB.log(f"[权限] chmod {mode} 已生效：{', '.join(devices)}", "ok")
    return {"devices": devices, "mode": mode, "report": serial_permission_report()}


def check_permissions_on_startup() -> None:
    """启动时只做检查与告警；Linux 的真正授权由 start.py 提示输入密码完成。"""
    if IS_WINDOWS:
        rep = serial_permission_report()
        names = [d["device"] for d in rep["devices"]]
        HUB.log("[启动] Windows 串口无需 sudo/chmod；" +
                (f"检测到 COM 端口：{', '.join(names)}" if names else "暂未检测到 COM 端口") +
                "；设备管理器可查看 COM 编号", "ok" if names else "warn")
        if not names:
            HUB.log("[启动] 若已接入主从臂，请确认 USB 转串口驱动已安装；"
                    "Windows 本机控制需要设备管理器中出现对应 COM 端口", "warn")
        return
    rep = serial_permission_report()
    if rep.get("no_new_privs"):
        HUB.log("[启动] 当前进程带 no_new_privs 限制，需要 sudo 的操作会失败；"
                "请从桌面终端重启 start.py（串口已是 666 时不影响连接）", "warn")
    if not rep["devices"]:
        HUB.log("[启动] 未发现 /dev/ttyUSB* /dev/ttyACM* 设备；接入机械臂后可在「串口与连接」页面授权",
                "warn")
    elif rep["all_writable"]:
        HUB.log("[启动] 串口权限已就绪：" +
                ", ".join(x["device"] for x in rep["devices"]), "ok")
    else:
        bad = [x["device"] for x in rep["devices"] if not x["writable"]]
        HUB.log("[启动] 以下串口尚无读写权限，请在页面输入 sudo 密码授权：" + ", ".join(bad), "warn")


# ==================================================================
# 7. 子进程工具
# ==================================================================
def _decode_stream_line(raw: Any) -> str:
    """子进程输出逐行解码：git/pip 是 UTF-8，Windows 内置命令是本地 ANSI(cp936)。"""
    if isinstance(raw, str):
        return raw
    encodings = ("utf-8", SUBPROC_KW_ENCODING, "gbk")
    for enc in dict.fromkeys(e for e in encodings if e):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _popen(cmd: list[str], stdin_data: Optional[str] = None,
           env_extra: Optional[dict] = None) -> subprocess.Popen:
    # 使用二进制流，由 _decode_stream_line 逐行猜测编码；避免 Windows 中文输出乱码
    env = os.environ.copy()
    # systemd / 桌面会话启动时 PATH 往往不含 conda 环境，导致 lerobot 的辅助可执行文件
    # （rerun、ffmpeg 等）找不到。把当前 LeRobot 环境的 bin 与 ~/.local/bin 前置。
    extra_bins = [str(ENV.lerobot_runtime.get("bin_dir") or ""),
                  str(Path.home() / ".local" / "bin")]
    bins = [p for p in extra_bins if p and os.path.isdir(p)]
    if bins:
        env["PATH"] = os.pathsep.join(bins + [env.get("PATH", "")])
    # 禁止 git 弹出凭据输入框把命令挂死；pip/python 也关闭输出缓冲
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["PYTHONUNBUFFERED"] = "1"
    # stdin 一律用管道：lerobot-calibrate / lerobot-teleoperate 在未校准时会
    # input("Press ENTER when ready...")。给 DEVNULL 会立刻 EOFError，校准永远失败。
    # 不用管道时由 /api/job/input 写入（见 write_job_input）；给 sudo 密码时写完即关闭。
    kw: dict[str, Any] = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        bufsize=0, cwd=str(ROOT), env=env)
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    if stdin_data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_data.encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError) as exc:
            HUB.log(f"[proc] 写入 sudo 密码失败：{_brief(exc)}", "warn")
    return proc


def _terminate(proc: Optional[subprocess.Popen], graceful: bool = False) -> None:
    """停止子进程。graceful=True 时先发 SIGINT（相当于 Ctrl+C），
    让 lerobot-teleoperate/record 走自己的清理流程（从臂回零、卸力）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=5)
        elif graceful:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            try:
                proc.wait(timeout=1200)
            except subprocess.TimeoutExpired:
                HUB.log("[stop] 慢速收工超过 20 分钟，终止异常进程", "warn")
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[stop] 终止进程异常：{_brief(exc)}", "warn")


# 终端在等用户回答的特征串。lerobot-calibrate / lerobot-teleoperate 用 input()
# 打印 "Press ENTER when ready..."，需要网页把这一行识别出来并提示用户确认。
PROMPT_RE = re.compile(
    r"(press\s+(?:enter|return|any\s+key)|type\s+'?c'?[^\n]*press|"
    r"输入[^\n]*(?:回车|[：:])|按[^\n]*回车|\[(?:y/n|yes/no|n/y)\]|(?:password|密码)\s*[：:])",
    re.I)

# 已知失败模式 -> 可执行的下一步。只在退出码非 0 时匹配，不影响正常输出。
FAIL_HINTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"gripper zero-detect[^\n]*(?:outside joint limits|no feedback|failed)", re.I),
     "夹爪零点或反馈校验未通过。请在「设备连接与校准」的零点调整中读取实测角度；"
     "只有物理零位错误时，才在托住机械臂、闭合夹爪后确认重新设零。"),
    (re.compile(r"ensure_mode failed: register 10|控制模式无法通过寄存器 10", re.I),
     "控制模式寄存器 10 通信确认失败，不等同于零点错误。请检查对应关节的电源、CAN 接线、"
     "反馈 ID 与串口占用；模式必须实时回读正确后才能使能。"),
    (re.compile(r"EOF when reading a line|EOFError", re.I),
     "子进程在等待终端输入时收到了 EOF。请刷新页面后重试，并在出现提示时点进度条下方的「回车确认」"
     "向终端发送回车。"),
    (re.compile(r"device or resource busy|failed to open|permission denied", re.I),
     "串口被占用或没有权限。请关闭其他占用该串口的程序，并在「串口与校准」页重新授权后再试。"),
)


def _note_prompt(job: Job, line: str) -> None:
    """终端提示回车时把任务标记为「等待输入」，前端据此高亮确认按钮。"""
    if job.finished.is_set() or not job.stdin_live or not PROMPT_RE.search(line):
        return
    with STATE.lock:
        first = not job.awaiting_input
        if first:
            job.prompt_id += 1
        job.awaiting_input = True
        job.awaiting_prompt = line.strip()[-200:]
    if first:
        if job.kind in ("calibrate-leader", "calibrate-follower"):
            who = "从臂" if job.kind == "calibrate-follower" else "主臂"
            extra = ("；从臂还需用手把夹爪完全捏到底并保持住（张开约 8° 会导致后续校验失败）"
                     if job.kind == "calibrate-follower" else "")
            HUB.log(f"[输入] {who}校准等待确认：若提示 type 'c'，输入 c 再点「发送」= 重新置零，"
                    f"直接「回车确认」= 沿用已有校准文件；若提示 Press ENTER when ready，"
                    f"把 {who} 摆到零位{extra}，再点「回车确认」。", "warn")
        else:
            HUB.log("[输入] 终端正在等待确认：把机械臂摆到零位后，点进度条下方的「回车确认」；"
                    "若要回答 c 等字符，先在输入框填写再点「发送」。", "warn")
    publish_progress(job, "prompt")


def _emit_job_line(job: Job, line: str) -> None:
    """记录一行真实输出 + 检测交互提示 + 推送日志，三处顺序保持一致。"""
    _record_line(job, line)
    _note_prompt(job, line)
    _emit_line(line, job.kind, job)


def _fmt_elapsed(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _job_progress(job: Job) -> dict[str, Any]:
    """把真实子进程的步骤 / 耗时 / 最近输出打包成前端可直接渲染的进度对象。"""
    with STATE.lock:
        now = time.time()
        return {
            "running": not job.finished.is_set(),
            "job": job.name,
            "job_id": job.job_id,
            "prompt_id": job.prompt_id,
            "stopping": job.stopping,
            "kind": job.kind,
            "step": job.step_index,
            "step_total": job.step_total,
            "step_label": job.step_label or ("" if job.step_total else job.name),
            "elapsed": round(now - job.started_at, 1),
            "step_elapsed": round(now - job.step_started_at, 1),
            "step_limit": job.step_limit,
            "last_line": job.last_line[-180:],
            "silent_for": round(now - job.last_line_at, 1),
            "awaiting_input": job.awaiting_input,
            "prompt": job.awaiting_prompt[-180:],
            "exit_code": job.exit_code,
        }


def publish_progress(job: Job, action: str = "progress") -> None:
    HUB.state(**_job_progress(job), action=action)


def _progress_beats(job: Job, stop: threading.Event) -> None:
    """独立心跳：即使 git/pip 长时间不输出，也持续推进度并打心跳日志。"""
    last_log = 0.0
    while not stop.wait(PROGRESS_BEAT_SEC):
        if job.finished.is_set():
            break
        p = _job_progress(job)
        publish_progress(job)
        now = time.time()
        if now - last_log >= HEARTBEAT_LOG_SEC:
            last_log = now
            step = f"[{p['step']}/{p['step_total']}] " if p["step_total"] else ""
            silent = int(p["silent_for"])
            HUB.log(
                f"[进度] {step}{p['step_label'] or p['job']} · 已用时 {_fmt_elapsed(p['elapsed'])}"
                + (f" · {silent}s 无新输出（下载中常见，可点急停终止）" if silent > 12 else "")
                + f" · 最近输出：{(p['last_line'] or '命令尚未输出')[:90]}",
                "warn" if silent > 30 else "info")


def _record_line(job: Job, line: str) -> None:
    with STATE.lock:
        if job.kind == 'zero-adjust' and line.startswith(('REBOT_ZERO_RESULT ','REBOT_ZERO_ERROR ')):
            try:job.result=json.loads(line.split(' ',1)[1])
            except ValueError:pass
        if job.kind == 'model-eval' and line.startswith('REBOT_MODEL_EVAL_RESULT '):
            try:job.result=json.loads(line.split(' ',1)[1])
            except ValueError:pass
        job.last_line = str(line)[-180:]
        job.last_line_at = time.time()
        job.tail.append(str(line)[-300:])


def _emit_line(line: str, kind: str, job: Optional[Job] = None) -> None:
    # 训练脚本输出的结构化指标：{"type":"metric","message":...,"step":...,"loss":...}
    def emit_metric(value, raw_step):
        explicit = raw_step is not None
        step = numeric_step(raw_step) if explicit else STATE.step_counter + 1
        if not math.isfinite(value):
            return
        STATE.step_counter = max(STATE.step_counter, step)
        unit = ('epoch' if job and job.kind == 'train' and '--epochs' in job.command else 'step') if explicit else 'metric'
        HUB.loss(step, value, step_unit=unit, kind=kind, job_id=job.job_id if job else None)
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and obj.get("type") in ("metric", "error"):
            if obj.get("message"):
                HUB.log(str(obj["message"]), "error" if obj["type"] == "error" else "info")
            if obj.get("loss") is not None:
                try:
                    emit_metric(float(obj['loss']), obj.get('step'))
                except (TypeError, ValueError):
                    pass
            return
    HUB.log(line)
    if kind.startswith("train") or kind in ("eval", "shell"):
        try:
            metric = parse_metric(line)
            if metric:
                emit_metric(metric['value'], metric['step'])
        except (ValueError, TypeError, OverflowError):
            pass


_WIN_PEEK_NAMED_PIPE: Optional[tuple] = None


def _win_pipe_available(fd: int) -> int:
    """Windows 管道当前可读字节数：select 在 Windows 只支持 socket，不支持管道。

    -1 表示管道已关闭/句柄失效，0 表示暂时没有数据。调用方定时轮询即可实现
    与 Linux select 一致的非阻塞读取，避免日志线程被阻塞式 read() 永久卡住。
    """
    global _WIN_PEEK_NAMED_PIPE
    if _WIN_PEEK_NAMED_PIPE is None:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = k32.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD)]
        peek.restype = wintypes.BOOL
        _WIN_PEEK_NAMED_PIPE = (peek, msvcrt, ctypes, wintypes)
    peek, msvcrt, ctypes, wintypes = _WIN_PEEK_NAMED_PIPE
    try:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    except (OSError, ValueError):
        return -1
    avail = wintypes.DWORD(0)
    if not peek(handle, None, 0, None, ctypes.byref(avail), None):
        return -1
    return int(avail.value)


def _pump_stream(job: Job, proc: subprocess.Popen, throttle: bool = True) -> None:
    """按 \\n 或 \\r 分行读取合并后的 stdout/stderr。
    git --progress 与 pip 用 \\r 原地刷新百分比，旧版 readline 会一直等换行，
    看起来像「点了没反应」；这里把 \\r 也当行结束符，进度就能实时回显。"""
    stream = proc.stdout
    if stream is None:
        return
    buf = b""
    last_progress_log = 0.0
    import select as _select
    fd = stream.fileno()
    can_select = os.name != "nt"
    use_peek = os.name == "nt"     # Windows：PeekNamedPipe 轮询，不再用阻塞式 read
    exited_at = None
    try:
        while True:
            # Only the pump owns/reads/closes this pipe. A child inheriting stdout
            # must not keep a completed main process alive indefinitely.
            if job.finished.is_set():
                break
            if proc.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                elif time.monotonic() - exited_at > 1.2:
                    break
            if can_select:
                try:
                    ready, _, _ = _select.select([fd], [], [], 0.4)
                except (OSError, ValueError):
                    break
                if ready:
                    chunk = os.read(fd, 4096)
                else:
                    chunk = b""
            elif use_peek:
                n = _win_pipe_available(fd)
                if n < 0:                       # 管道已关闭
                    break
                if n:
                    chunk = stream.read(min(n, 4096))
                else:
                    chunk = b""
                    time.sleep(0.15)
            else:
                chunk = stream.read(4096)
            if not chunk:
                if can_select or use_peek:
                    # 主进程已退出但孙进程还占着管道时，永远等不到 EOF：
                    # 退出后 1.2 秒没有新输出就强制收尾，避免任务永远"运行中"。
                    if job.proc is not None and job.proc.poll() is not None:
                        if exited_at is None:
                            exited_at = time.monotonic()
                        elif time.monotonic() - exited_at > 1.2:
                            break
                    continue
                break
            buf += chunk
            while True:
                cand = [x for x in (buf.find(b"\n"), buf.find(b"\r")) if x >= 0]
                if not cand:
                    break
                i = min(cand)
                raw, buf = buf[:i], buf[i + 1:]
                line = _decode_stream_line(raw).strip("\r\n")
                if not line.strip().strip("\x1b").strip():
                    continue
                _record_line(job, line)
                now = time.time()
                is_progress = bool(re.search(r"\d+%|objects:|Receiving|Resolving|remote:", line, re.I))
                if throttle and is_progress and now - last_progress_log < 1.0:
                    continue
                if is_progress:
                    last_progress_log = now
                _note_prompt(job, line)
                _emit_line(line, job.kind, job)
            # input("Press ENTER when ready...") 的提示不带换行，会一直卡在 buf 里，
            # 用户看不到「该按回车了」。识别到交互提示就立即推出，并清空 buf 防重复。
            if buf and b"\n" not in buf and b"\r" not in buf:
                partial = _decode_stream_line(buf)
                if PROMPT_RE.search(partial):
                    buf = b""
                    _emit_job_line(job, partial.strip("\r\n"))
            if len(buf) > 65536:  # 极端情况：超长无换行输出也要出得来
                line = _decode_stream_line(buf).strip("\r\n")
                buf = b""
                if line.strip():
                    _record_line(job, line)
                    HUB.log(line)
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[proc] 读取输出异常：{_brief(exc)}", "warn")
    if buf.strip():
        line = _decode_stream_line(buf).strip("\r\n")
        if line.strip():
            _emit_job_line(job, line)


def _log_failure_hints(job: Job) -> None:
    """任务失败后按真实输出给出针对性下一步，而不是让用户自己猜 traceback。"""
    if job.exit_code in (0, None):
        return
    blob = "\n".join(job.tail)
    for rx, hint in FAIL_HINTS:
        if rx.search(blob):
            HUB.log("[提示] " + hint, "warn")


def _finish_job_once(job: Job, exit_code: int) -> bool:
    """Publish one terminal state, even if the pump and exit watcher race."""
    with job.finish_lock:
        if job.finished.is_set():
            return False
        sorting=globals().get('SORTING')
        if job.kind=='sorting-run' and sorting and sorting.control and sorting.control.alive():
            try:sorting.control.call('hold',timeout=2)
            except Exception as exc:HUB.log('[分拣] 保持请求失败：'+_brief(exc),'warn')
        job.exit_code = exit_code
        job.stdin_live = False
        job.awaiting_input = False
        job.awaiting_prompt = ""
        job.progress_stop.set()
        job.finished.set()
        HUB.log(f"[{job.name}] 结束，退出码 {job.exit_code}",
                "ok" if job.exit_code == 0 else "warn")
        _log_failure_hints(job)
        if STATE.job is job:
            HUB.state(**_job_progress(job), action="finish", refresh_env=True)
        return True


def _watch_job_exit(job: Job) -> None:
    """Fallback finalizer; never close another thread's output descriptor."""
    while not job.finished.is_set():
        proc = job.proc
        if proc is not None and proc.poll() is not None:
            time.sleep(1.5)
            if job.finished.is_set():
                return
            _finish_job_once(job, proc.returncode)
            return
        time.sleep(0.5)


def _pump_stdout(job: Job) -> None:
    exit_code = 0
    try:
        if job.proc is not None:
            _pump_stream(job, job.proc)
            exit_code = job.proc.wait()
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        HUB.log(f"[{job.name}] 读取输出异常：{_brief(exc)}", "error")
    finally:
        try:
            if job.proc is not None and job.proc.stdin is not None:
                job.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            if job.proc is not None and job.proc.stdout is not None:
                job.proc.stdout.close()
        except (OSError, ValueError):
            pass
        _finish_job_once(job, exit_code)


# ==================================================================
# 8. 命令构造（严格照抄 Seeed 文档）
# ==================================================================
def _lerobot(name: str) -> list[str]:
    if name == 'lerobot-train':
        return _py() + ['-u', str(TRAINERS_DIR / 'lerobot_train.py')]
    return LEROBOT_CMD_PREFIX + [_bin(name)]


SAFE_RECORD = ROOT / "backend" / "safe_record.py"


def _safe_record() -> list[str]:
    """用包装脚本跑 lerobot-record：结束保持姿态，不回零下压。"""
    return _py() + [str(SAFE_RECORD)]


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def cameras_arg(cameras: list[dict]) -> str:
    parts=[]
    for camera in cameras or []:
        source=camera.get('index_or_path',0)
        try:source_key(source)
        except CameraError as exc:raise ApiError(str(exc),400)
        fields=['type: rebot_shared','source: '+json.dumps(str(source)),
                f"width: {camera.get('width',640)}",f"height: {camera.get('height',480)}",f"fps: {camera.get('fps',30)}"]
        parts.append(json.dumps(str(camera.get('name') or 'cam'),ensure_ascii=False)+': { '+', '.join(fields)+' }')
    return '{ '+', '.join(parts)+' }' if parts else ''


def eval_policy_cameras(cfg: dict, policy_path: str) -> list[dict]:
    """Select and name the configured camera inputs expected by a local policy.

    Policies trained with a camera key such as ``cam2`` must receive that same key
    during real-robot evaluation. When no exact camera name exists, a ``camN`` key
    can be resolved to the configured numeric /dev/videoN source. Other cameras are
    omitted because a policy trained on one view must not receive unrelated views.
    """
    path = Path(str(policy_path or "")).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    config_path = path if path.name == "config.json" else path / "config.json"
    if not config_path.is_file():
        return list(cfg.get("cameras") or [])
    try:
        policy_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApiError(f"无法读取策略相机配置：{config_path}（{exc}）", 400) from exc

    expected = [
        key.removeprefix("observation.images.")
        for key, feature in (policy_cfg.get("input_features") or {}).items()
        if key.startswith("observation.images.") and
        (feature or {}).get("type") == "VISUAL"
    ]
    if not expected:
        return list(cfg.get("cameras") or [])

    configured = list(cfg.get("cameras") or [])
    selected: list[dict] = []
    used: set[int] = set()
    for name in expected:
        # B601's compound robot attaches shared cameras to the left sub-arm,
        # then prefixes every observation with left_ (including right views).
        bi_prefix = 'left_' if dual_enabled(cfg) and str((cfg.get('dual_arm') or {}).get('robot_type')) == 'bi_seeed_b601_dm_follower' else ''
        source_name = name[len(bi_prefix):] if bi_prefix and name.startswith(bi_prefix) else name
        matches = [(i, camera) for i, camera in enumerate(configured)
                   if i not in used and str(camera.get("name") or "") == source_name]
        if not matches and source_name == name and name.startswith(('left_', 'right_')):
            # A single-arm evaluation may still consume a compound checkpoint.
            alias = name.split('_', 1)[1]
            matches = [(i, camera) for i, camera in enumerate(configured)
                       if i not in used and str(camera.get('name') or '') == alias]
        if not matches:
            number = re.fullmatch(r"cam(\d+)", source_name, re.IGNORECASE)
            if number:
                wanted = number.group(1)

                def source_number(camera: dict) -> Optional[str]:
                    source = str(camera.get("index_or_path", "")).strip()
                    if source.isdigit():
                        return str(int(source))
                    match = re.fullmatch(r"/dev/video(\d+)", source)
                    return str(int(match.group(1))) if match else None

                matches = [(i, camera) for i, camera in enumerate(configured)
                           if i not in used and source_number(camera) == wanted]
        if len(matches) != 1:
            configured_names = [
                f"{camera.get('name') or '(未命名)'} [{camera.get('index_or_path', '')}]"
                for camera in configured
            ]
            raise ApiError(
                f"检查点需要相机键 {name}，但无法从当前相机配置唯一匹配。"
                f"当前配置：{', '.join(configured_names) or '无'}。"
                "请在摄像头页将对应相机设为同名，或确认 camN 对应的视频设备编号。", 400)
        index, camera = matches[0]
        used.add(index)
        if bi_prefix and not name.startswith(bi_prefix):
            raise ApiError(f'检查点相机键 {name} 与当前 B601 双臂前缀规则不一致，请使用匹配的机器人模式或数据集重新训练', 400)
        selected.append({**camera, "name": source_name})
    return selected


def dual_validate(cfg: dict, with_teleop: bool = True) -> None:
    """双臂命令的硬性前置校验：类型与端口必须由用户/设备插件真实提供。

    绝不拼一条必然失败的命令再让子进程去报错；也绝不假装单臂类型能驱动两台臂。
    """
    d = cfg.get("dual_arm") or {}
    rt = str(d.get("robot_type") or "").strip()
    tt = str(d.get("teleop_type") or "").strip()
    hint = ("请到「设备连接与校准」页面点「检测双臂类型」从当前 conda 环境里选择已注册的复合类型，"
            "或手动填写类型名；若插件只提供单臂类型，需要在设备插件里注册双臂复合配置后再使用。")
    if not rt:
        raise ApiError("双臂模式未设置复合机器人类型（robot_type）。" + hint, 400)
    if with_teleop and not tt:
        raise ApiError("双臂遥操作/采集需要复合主臂类型（teleop_type）。" + hint, 400)
    if rt == 'seeedstudiob601dm' or (with_teleop and tt == 'seeedstudiob601dm'):
        raise ApiError('seeedstudiob601dm 不是已注册的双臂设备类型。请点击“检测双臂类型”，'
                       '再应用 bi_seeed_b601_dm_follower / bi_rebot_arm_102_leader。', 400)
    ports = dual_ports(cfg)
    need = DUAL_PORT_KEYS if with_teleop else ("left_follower", "right_follower")
    missing = [k for k in need if not ports.get(k)]
    if missing:
        raise ApiError("双臂模式缺少串口：" + "、".join(missing) +
                       "。请到「设备连接与校准」页面扫描串口并完成四路连接", 400)
    seen: dict[str, str] = {}
    for key in need:
        v = ports[key]
        if v in seen:
            raise ApiError(f"串口 {v} 被 {seen[v]} 与 {key} 重复使用；每路串口只能给一条臂", 400)
        seen[v] = key


def dual_robot_args(cfg: dict, with_cameras: bool = False, with_teleop: bool = True,
                    display: Optional[bool] = None) -> list[str]:
    """生成双臂复合机器人的 CLI 参数。

    LeRobot 的 draccus 用 `--robot.left_arm.port=...` 这种点号路径注入子配置，
    左右子臂由同一个进程驱动，天然保证 observation/action 在同一帧对齐。
    子字段名（left_arm / right_arm）可配置，以兼容不同插件的命名。
    """
    dual_validate(cfg, with_teleop)
    d = cfg.get("dual_arm") or {}
    args = [f"--robot.type={str(d['robot_type']).strip()}"]
    prefixes = {"left": str(d.get("left_prefix") or "left_arm").strip() or "left_arm",
                "right": str(d.get("right_prefix") or "right_arm").strip() or "right_arm"}
    for side in DUAL_SIDES:
        a, p = dual_side(cfg, side), f"--robot.{prefixes[side]}"
        args += [f"{p}.type={a['follower_type']}", f"{p}.port={a['follower_port']}",
                 f"{p}.id={a['follower_id']}"]
        if a.get("can_adapter"):
            args.append(f"{p}.can_adapter={a['can_adapter']}")
        ratio = a.get("gripper_torque_ratio")
        if ratio in (None, ""):
            ratio = (cfg.get("follower") or {}).get("gripper_torque_ratio")
        if ratio not in (None, ""):
            try:
                args.append(f"{p}.force_pos_torque_ration={float(ratio)}")
            except (TypeError, ValueError):
                pass
    if with_cameras:
        cam = cameras_arg(cfg.get("cameras", []))
        if cam:
            args.append(f"--robot.cameras={cam}")
    if with_teleop:
        args.append(f"--teleop.type={str(d['teleop_type']).strip()}")
        for side in DUAL_SIDES:
            a, p = dual_side(cfg, side), f"--teleop.{prefixes[side]}"
            args += [f"{p}.type={a['leader_type']}", f"{p}.port={a['leader_port']}",
                     f"{p}.id={a['leader_id']}"]
    if display is not None:
        args.append("--display_data=false")
    return args


def dual_calibrate_args(cfg: dict, side: str, arm: str) -> list[str]:
    """单侧单臂校准：复合机器人校准文件按各子臂 id 分别保存，所以用单臂配置跑 calibrate。

    录制时复合配置会以相同 id 读取这些校准文件。
    """
    if side not in DUAL_SIDES or arm not in ("leader", "follower"):
        raise ApiError(f"未知校准目标：{side}/{arm}", 400)
    a = dual_side(cfg, side)
    port = str(a.get(f"{arm}_port") or "").strip()
    if not port:
        raise ApiError(f"{side}/{arm} 尚未选择串口，请到「设备连接与校准」页面完成连接", 400)
    prefix = "--teleop" if arm == "leader" else "--robot"
    typ, ident = a[f"{arm}_type"], a[f"{arm}_id"]
    args = [f"{prefix}.type={typ}", f"{prefix}.port={port}", f"{prefix}.id={ident}"]
    if arm == "follower" and a.get("can_adapter"):
        args.append(f"{prefix}.can_adapter={a['can_adapter']}")
    return args


def robot_base_args(cfg: dict, with_cameras: bool = False, with_teleop: bool = True,
                    display: Optional[bool] = None) -> list[str]:
    if dual_enabled(cfg):
        return dual_robot_args(cfg, with_cameras, with_teleop, display)
    f, l = cfg["follower"], cfg["leader"]
    args = [f"--robot.type={f['type']}", f"--robot.port={f['port']}", f"--robot.id={f['id']}"]
    if f.get("can_adapter"):
        args.append(f"--robot.can_adapter={f['can_adapter']}")
    ratio = f.get("gripper_torque_ratio")
    if ratio not in (None, ""):
        try:
            args.append(f"--robot.force_pos_torque_ration={float(ratio)}")
        except (TypeError, ValueError):
            pass
    if with_cameras:
        cam = cameras_arg(cfg.get("cameras", []))
        if cam:
            args.append(f"--robot.cameras={cam}")
    if with_teleop:
        args += [f"--teleop.type={l['type']}", f"--teleop.port={l['port']}",
                 f"--teleop.id={l['id']}"]
    if display is not None:
        args.append("--display_data=false")
    return args


def build_command(kind: str, cfg: dict, extra: Optional[dict] = None) -> list[str]:
    e = extra or {}
    ds, tr, ev, vz, rp, ai = (cfg["dataset"], cfg["train"], cfg["eval"],
                              cfg["viz"], cfg["replay"], cfg["async_inference"])
    if kind in ("chmod-ports", "chmod-usb"):
        if IS_WINDOWS:
            raise ApiError("Windows 无需 sudo/chmod 串口授权；请在设备管理器确认 "
                           "COM 编号与 USB 转串口驱动，并关闭占用该端口的程序", 400)
        if kind == "chmod-ports":
            files = _serial_devices() or ["/dev/ttyUSB*", "/dev/ttyACM*"]
            return ["sudo", "-n", "chmod", "666", *files]
        files = sorted(glob.glob("/dev/bus/usb/*/*")) or ["/dev/bus/usb/*/*"]
        return ["sudo", "-n", "chmod", "a+rw", *files]
    if kind == "check-env":
        return _py() + ["-c", "import torch; print('torch', torch.__version__); "
                        "print('cuda_available', torch.cuda.is_available())"]
    if kind in ("calibrate-leader", "calibrate-follower",
                "calibrate-left-leader", "calibrate-left-follower",
                "calibrate-right-leader", "calibrate-right-follower"):
        if dual_enabled(cfg):
            if kind == "calibrate-leader":
                side, arm = "left", "leader"       # 旧页面按钮在双臂下作用于左主臂
            elif kind == "calibrate-follower":
                side, arm = "left", "follower"
            else:
                _, side, arm = kind.split("-")
            return _lerobot("lerobot-calibrate") + dual_calibrate_args(cfg, side, arm)
        if kind not in ("calibrate-leader", "calibrate-follower"):
            raise ApiError("当前是单臂模式，没有左右臂可分别校准；请先在「设备连接与校准」页开启双臂", 400)
        if kind == "calibrate-leader":
            return _lerobot("lerobot-calibrate") + [
                f"--teleop.type={cfg['leader']['type']}", f"--teleop.port={cfg['leader']['port']}",
                f"--teleop.id={cfg['leader']['id']}"]
        return _lerobot("lerobot-calibrate") + [
            f"--robot.type={cfg['follower']['type']}", f"--robot.port={cfg['follower']['port']}",
            f"--robot.id={cfg['follower']['id']}",
            f"--robot.can_adapter={cfg['follower']['can_adapter']}"]
    if kind == "teleoperate":
        rate = min([int((cfg.get('teleop') or {}).get('fps') or 30)] +
                   [int(c.get('fps') or 30) for c in cfg.get('cameras',[])])
        return _lerobot("lerobot-teleoperate") + [f"--fps={rate}"] + robot_base_args(
            cfg, with_cameras=True, display=bool(cfg["teleop"].get("display_data", True)))
    if kind == "find-cameras":
        return _lerobot("lerobot-find-cameras") + [
            str(e.get("backend") or cfg.get("camera_backend") or "opencv")]
    if kind == "record":
        record_base = _lerobot("lerobot-record") if bool(ds.get("auto_home", True)) \
            else _safe_record()
        cmd = record_base + robot_base_args(
            cfg, with_cameras=True, display=bool(ds.get("display_data", True)))
        cmd += [f"--dataset.fps={min([int(ds.get('fps') or 30)]+[int(c.get('fps') or 30) for c in cfg.get('cameras',[])])}", f"--dataset.repo_id={ds['repo_id']}",
                f"--dataset.num_episodes={ds['num_episodes']}",
                f"--dataset.single_task={ds['single_task']}",
                f"--dataset.push_to_hub={_fmt(ds.get('push_to_hub', False))}",
                f"--dataset.episode_time_s={ds['episode_time_s']}",
                f"--dataset.reset_time_s={ds['reset_time_s']}"]
        if ds.get("resume"):
            cmd.append("--resume=true")
        # 网页可视化已启动时，让录制进程连到常驻的 Rerun 服务，而不是弹本地窗口
        viz_proc = VIZ_WEB.get("proc")
        if ds.get("display_data", True) and viz_proc is not None and viz_proc.poll() is None:
            cmd += ["--display_ip=127.0.0.1", f"--display_port={RERUN_GRPC_PORT}"]
        return cmd
    if kind == "dataset-viz":
        return _lerobot("lerobot-dataset-viz") + [
            f"--repo-id={e.get('repo_id') or vz['repo_id']}",
            f"--episode-index={e.get('episode_index', vz['episode_index'])}",
            f"--display-compressed-images={_fmt(vz.get('display_compressed_images', False))}"]
    if kind == "replay":
        return _lerobot("lerobot-replay") + robot_base_args(
            cfg, with_teleop=False) + [f"--dataset.repo_id={e.get('repo_id') or rp['repo_id']}",
                                       f"--dataset.episode={e.get('episode', rp['episode'])}"]
    if kind == "train":
        try:
            steps = float(tr['steps'])
            batch = float(tr.get('batch_size') or 0)
            if not math.isfinite(steps) or not steps.is_integer() or steps <= 0:
                raise ValueError('训练步数必须为正整数')
            if not math.isfinite(batch) or not batch.is_integer() or batch < 0:
                raise ValueError('批大小必须为非负整数，0 表示默认')
        except (ValueError, TypeError) as exc:
            raise ApiError(f'训练参数无效：{exc}', 400) from exc
        ds_dir = str(tr.get("dataset_dir") or "").strip()
        if ds_dir:
            d = Path(ds_dir).expanduser()
            repo_id = d.name
            cmd = _lerobot("lerobot-train") + [
                f"--dataset.repo_id={repo_id}", f"--dataset.root={d}"]
        else:
            repo_id = str(tr["repo_id"])
            cmd = _lerobot("lerobot-train") + [f"--dataset.repo_id={repo_id}"]
        if tr.get("video_backend"):
            cmd.append(f"--dataset.video_backend={tr['video_backend']}")
        try:
            mode = training_mode(tr)
            base_model = prepare_finetune(tr, ROOT, LEROBOT_HOME)[0] if mode == 'finetune' else ''
        except ValueError as exc:
            raise ApiError(str(exc), 400) from exc
        if base_model:
            cmd.append(f"--policy.path={base_model}")
            # ACT keeps its saved architecture and camera schema. Only VLA
            # supports adapting the pretrained base to different dataset inputs.
            feats = _dataset_policy_features(repo_id, ds_dir or None) if tr['policy_type'] == 'smolvla' else None
            if feats:
                cmd.append("--policy.input_features=" + json.dumps(
                    feats["input"], ensure_ascii=False, separators=(",", ":")))
                cmd.append("--policy.output_features=" + json.dumps(
                    feats["output"], ensure_ascii=False, separators=(",", ":")))
        else:
            cmd.append(f"--policy.type={tr['policy_type']}")
        cmd += [f"--output_dir={tr['output_dir']}",
                f"--job_name={tr['job_name']}", f"--policy.device={tr['device']}",
                f"--wandb.enable={_fmt(tr.get('wandb', False))}",
                f"--policy.push_to_hub={_fmt(tr.get('push_to_hub', False))}",
                f"--steps={int(steps)}"]
        if batch > 0:
            cmd.append(f"--batch_size={int(batch)}")
        try:
            lr = float(tr.get('learning_rate') or 0)
        except (ValueError, TypeError) as exc:
            raise ApiError('学习率必须为有效的非负数，0 表示沿用模型默认值', 400) from exc
        if not math.isfinite(lr) or lr < 0:
            raise ApiError('学习率必须为有效的非负数，0 表示沿用模型默认值', 400)
        if lr and tr['policy_type'] == 'act':
            cmd += [f'--policy.optimizer_lr={lr}', f'--policy.optimizer_lr_backbone={lr}']
        elif lr:
            raise ApiError('自定义微调学习率目前仅支持 ACT；其他策略请填 0', 400)
        return cmd
    if kind == "train-resume":
        return _lerobot("lerobot-train") + [
            f"--config_path={e.get('config_path') or tr.get('resume_config')}", "--resume=true"]
    if kind == "eval":
        # 必须显式给剧集数/时长，否则 LeRobot 用默认 50 集 × 60s + 60s 复位，
        # 评估看起来"永远不结束"。
        record_base = _lerobot("lerobot-record") if bool(ev.get("auto_home", True)) \
            else _safe_record()
        eval_cfg = dict(cfg)
        eval_cfg["cameras"] = eval_policy_cameras(cfg, str(ev.get("policy_path") or ""))
        return record_base + robot_base_args(
            eval_cfg, with_cameras=True, with_teleop=False, display=False) + [
                f"--dataset.repo_id={ev['repo_id']}",
                f"--dataset.single_task={ev['single_task']}",
                f"--dataset.num_episodes={int(ev.get('num_episodes') or 1)}",
                f"--dataset.episode_time_s={ev.get('episode_time_s') or 30}",
                f"--dataset.reset_time_s={ev.get('reset_time_s') or 10}",
                "--dataset.push_to_hub=false",
                f"--policy.path={ev['policy_path']}"]
    if kind == "policy-server":
        return _py() + ["-m", "lerobot.async_inference.policy_server",
                        f"--model_path={ai['model_path']}",
                        f"--server_address={ai['server_address']}"]
    if kind == "robot-client":
        return _py() + ["-m", "lerobot.async_inference.robot_client"] + robot_base_args(
            cfg, with_cameras=True, with_teleop=False) + [
                f"--server_address={ai['server_address']}",
                f"--actions_per_chunk={ai['actions_per_chunk']}",
                f"--chunk_size_threshold={ai['chunk_size_threshold']}",
                f"--fixed_update_fps={ai['fixed_update_fps']}",
                "--visualize_action_queue=false"]
    raise ApiError(f"未知操作类型：{kind}", 400)


def display_command(cmd: list[str]) -> str:
    try:
        parts = [str(x) for x in cmd]
        # shlex.join 是 POSIX 引号规则，Windows 路径与 /D=... 会被错误转义
        return subprocess.list2cmdline(parts) if IS_WINDOWS else shlex.join(parts)
    except Exception:  # noqa: BLE001
        return " ".join(str(x) for x in cmd)


# ==================================================================
# 9. 安装命令链（conda / 清华源 / gh-proxy，真实执行）
# ==================================================================
def pip_flags(cfg: dict) -> list[str]:
    idx = cfg["setup"].get("pip_index") or PIP_INDEX
    flags = ["-i", idx, "--trusted-host", PIP_TRUSTED_HOST,
             "--timeout", "15", "--retries", "2"]
    if IS_WINDOWS and idx == PIP_INDEX:
        # 本机 Windows 上清华 simple 索引偶发返回空版本列表；真机 Ubuntu 仍严格只用清华源
        flags += ["--extra-index-url", "https://pypi.org/simple", "--trusted-host", "pypi.org"]
    return flags


def _repo_cloned() -> bool:
    """只有真正检出源码才算克隆成功，半截目录不能当成「仓库已存在」。"""
    if not REPO_DIR.exists():
        return False
    has_checkout = (REPO_DIR / "pyproject.toml").exists() or (REPO_DIR / "src").is_dir()
    return has_checkout and ((REPO_DIR / ".git").exists() or (REPO_DIR / "pyproject.toml").exists())


def _probe_repo(url: str, timeout: int = 12) -> tuple[bool, str]:
    """克隆前快速探测仓库是否可达，避免 git clone 静默挂住几分钟。"""
    cmd = ["git", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=10",
           "ls-remote", url, "HEAD"]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                            env=env, cwd=str(ROOT))
    except FileNotFoundError:
        return False, "找不到 git，请先安装 git 并加入 PATH"
    except subprocess.TimeoutExpired:
        return False, f"连接超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return False, _brief(exc)
    if r.returncode == 0:
        return True, ""
    out = _decode_stream_line((r.stderr or r.stdout or b"").strip())
    tail = [x.strip() for x in out.splitlines() if x.strip()]
    return False, (tail[-1][:160] if tail else f"退出码 {r.returncode}")


def _choose_repo_url(url: str) -> str:
    ok1, err1 = _probe_repo(url)
    if ok1:
        HUB.log(f"[setup] 仓库连通性检查通过：{url}", "ok")
        return url
    HUB.log(f"[setup] 配置的仓库地址不可达：{url} · {err1}", "warn")
    if url == REPO_FALLBACK:
        raise ApiError(f"仓库连通性检查失败（{err1}），已阻止启动安装以免卡住；"
                       "请在「安装源」换成可访问的仓库地址或配置网络代理", 502)
    HUB.log(f"[setup] 自动改试官方源：{REPO_FALLBACK}", "warn")
    ok2, err2 = _probe_repo(REPO_FALLBACK)
    if ok2:
        HUB.log("[setup] 官方源可用，本次克隆自动改用官方源", "ok")
        return REPO_FALLBACK
    raise ApiError("仓库连通性检查失败，已阻止启动安装以免卡住。\n"
                   f"gh-proxy：{err1}\n官方源：{err2}\n"
                   "请在「安装源」换成可访问的仓库地址，或配置好网络代理后重试", 502)


def setup_steps(which: str, cfg: dict, extra: Optional[dict] = None) -> list[tuple[str, list[str]]]:
    e = extra or {}
    st = cfg["setup"]
    py = _bin("python")
    steps: list[tuple[str, list[str]]] = []
    if which == "install-conda":
        url = e.get("conda_url") or st.get("conda_url") or CONDA_URL
        if IS_WINDOWS and str(url).split("?")[0].lower().endswith(".sh"):
            HUB.log("[setup] 安装源里仍是 Linux .sh 安装包，Windows 自动改用 Miniforge .exe", "warn")
            url = CONDA_URL_WINDOWS
        tmp = str(MINIFORGE_SCRIPT)
        if IS_WINDOWS:
            # NSIS 安装包的 /D= 必须放在最后且不能带引号，路径含空格无法自动安装
            if " " in str(CONDA_BASE):
                raise ApiError(
                    f"用户目录含空格（{CONDA_BASE}），Miniforge 静默安装无法使用 /D 参数。"
                    "请手动双击安装 Miniforge，或把安装目录改成无空格路径后重试", 400)
            steps.append(("下载 Miniforge Windows 安装包（gh-proxy）",
                          ["curl", "-fL", "--connect-timeout", "10", "--max-time", "900",
                           "-o", tmp, url]))
            steps.append((f"静默安装 Miniforge 到 {CONDA_BASE}",
                          [tmp, "/InstallationType=JustMe", "/RegisterPython=0",
                           "/AddToPath=0", "/S", "/D=" + str(CONDA_BASE)]))
            verify = ("import os,sys; "
                      "p=os.path.join(os.path.expanduser('~'),'miniforge3','Scripts','conda.exe'); "
                      "print('conda.exe:',p,'exists',os.path.isfile(p)); "
                      "sys.exit(0 if os.path.isfile(p) else 1)")
            steps.append(("验证 conda.exe 已生成", [sys.executable, "-c", verify]))
        else:
            steps.append(("下载 Miniforge（gh-proxy）",
                          ["curl", "-fL", "--connect-timeout", "10", "--max-time", "900",
                           "-o", tmp, url]))
            steps.append(("安装 Miniforge 到 ~/miniforge3",
                          ["bash", tmp, "-b", "-p", str(CONDA_BASE)]))
            steps.append(("初始化 conda", [str(CONDA_BASE / "bin" / "conda"), "init", "bash"]))
    elif which == "create-env":
        name = e.get("name") or "lerobot"
        ver = e.get("python_version") or cfg["environment"].get("python_version") or "3.12"
        conda = find_conda()
        location = str(e.get("location") or cfg["environment"].get("env_location") or "").strip()
        prefix = resolve_env_target(location, name)
        if prefix is None:
            steps.append((f"创建虚拟环境 {name} (python={ver}，conda 默认 envs 目录)",
                          conda_cmd("create", "-y", "-n", name, f"python={ver}", conda=conda)))
        else:
            steps.append((f"创建虚拟环境 {name} -> {prefix} (python={ver})",
                          conda_cmd("create", "-y", "-p", str(prefix), f"python={ver}",
                                    conda=conda)))
    elif which == "install-lerobot":
        url = e.get("repo_url") or st.get("repo_url") or REPO_URL
        flags = pip_flags(cfg)
        py = _py()[-1]   # 装到「所选环境 / 已复用的 LeRobot 运行时」，不装错解释器
        info = e.get("_detected") or detect_lerobot()
        force = bool(e.get("force"))
        if IS_WINDOWS:
            HUB.log("[setup] 当前为 Windows：依赖安装在本机环境中执行；机械臂通过 Windows COM 端口由本机控制",
                    "info")
        if lerobot_reusable(info, force):
            # 已经装好 LeRobot：直接复用，不重复 clone / pip install -e，只补真正缺的依赖
            HUB.log(f"[setup] 检测到已安装 LeRobot（{info['source']} / {info['python']}），"
                    f"直接复用，跳过 git clone 与 pip install -e", "ok")
            missing = list(info.get("missing") or [])
            if missing:
                steps.append(("仅补齐缺失依赖：" + ", ".join(missing),
                              [py, "-m", "pip", "install", *missing, *flags]))
            else:
                HUB.log("[setup] 插件与 Web 依赖齐全，无需任何安装步骤", "ok")
        else:
            if force and info["installed"]:
                HUB.log("[setup] 用户选择强制重新安装，按完整安装链执行", "warn")
            if not _repo_cloned():
                # 清掉上次卡住/失败留下的半截目录，避免重试时误判「仓库已存在」
                _cleanup_failed_clone()
                # 先快速探活：源不通就直接给出可操作的错误，不启动会挂住的 clone
                url = _choose_repo_url(url)
                # --progress 强制 git 在非终端下也输出百分比；\r 刷新的进度行由 _pump_stream 实时回显
                clone = ["git", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=30",
                         "clone", "--progress", url, str(REPO_DIR)]
                if not IS_WINDOWS:
                    # Linux 额外用 GNU timeout 兜底；Windows 上 `timeout` 会被 TIMEOUT.EXE 接管
                    clone = ["timeout", str(CLONE_TIMEOUT)] + clone
                steps.append(("克隆 LeRobot（gh-proxy 加速）", clone))
                HUB.log(f"[setup] 克隆已配置实时进度与 {CLONE_TIMEOUT}s 超时兜底"
                        + ("；当前为 Windows，使用 git 内置低速超时" if IS_WINDOWS else ""), "info")
            else:
                HUB.log(f"[setup] 仓库已存在且源码已检出，跳过克隆：{REPO_DIR}", "warn")
            steps.append(("pip install -e ./lerobot（清华源）",
                          [py, "-m", "pip", "install", "-e", str(REPO_DIR), *flags]))
            steps.append(("安装幻尔主臂 + Seeed 从臂插件（清华源）",
                          [py, "-m", "pip", "install", "lerobot-teleoperator-rebot-arm-102",
                           "lerobot-robot-seeed-b601", "motorbridge", *flags]))
            steps.append(("安装 Web / 相机依赖（清华源）",
                          [py, "-m", "pip", "install", "fastapi", "uvicorn", "pyserial",
                           "opencv-python", "numpy", "websockets", "python-multipart", "PyYAML", "zxing-cpp", "ultralytics", *flags]))
        if bool(e.get("with_ffmpeg", st.get("with_ffmpeg", False))):
            steps.append(("安装 ffmpeg（conda-forge）",
                          conda_cmd("install", "-y", "ffmpeg", "-c", "conda-forge",
                                    conda=find_conda())))
    return steps


# ==================================================================
# 10. 训练引擎注册表（可插拔，不写死一个）
# ==================================================================
TRAIN_BACKENDS: dict[str, dict] = {
    "lerobot": {
        "label": "LeRobot · 模仿学习策略",
        "engine": "官方 lerobot-train CLI（ACT / Diffusion / SmolVLA）",
        "desc": "从头训练或加载已有策略微调，产出 pretrained_model 检查点；ACT 微调会检查动作维度和相机配置。",
        "needs": ["lerobot-train"], "deps": [],
        "params": ["repo_id", "dataset_dir", "policy_type", "base_model", "training_mode", "learning_rate", "steps", "batch_size", "device",
                   "output_dir", "job_name", "wandb", "video_backend"],
    },
    "yolo": {
        "label": "YOLO · 目标检测训练",
        "engine": "Ultralytics YOLO（Python API）",
        "desc": "训练视觉检测模型识别抓取目标，需提供 data.yaml 或按 YOLO 结构组织的数据集目录。",
        "needs": ["ultralytics"], "deps": ["ultralytics"],
        "params": ["data_yaml", "dataset_dir", "model", "epochs", "imgsz",
                   "batch", "device", "output_dir"],
    },
    "opencv": {
        "label": "OpenCV · 经典视觉训练",
        "engine": "OpenCV HOG/HSV 特征 + SVM/KNN",
        "desc": "用 OpenCV 提取图像特征并训练分类器，适合轻量分类任务；数据集按类别分子目录存放图片。",
        "needs": ["cv2", "numpy"], "deps": ["opencv-python", "numpy"],
        "params": ["dataset_dir", "classifier", "epochs", "img_size", "output_dir"],
    },
}


def _module_available(mod: str) -> tuple[bool, str]:
    py = _bin("python")
    try:
        r = subprocess.run([py, "-c", f"import {mod}"], capture_output=True,
                           text=True, encoding=SUBPROC_KW_ENCODING, errors="replace",
                           timeout=20, cwd=str(ROOT))
        if r.returncode == 0:
            return True, ""
        last = (r.stderr or "").strip().splitlines()
        return False, last[-1] if last else f"import {mod} 失败"
    except subprocess.TimeoutExpired:
        return False, "依赖检测超时（20 秒）"
    except Exception as exc:  # noqa: BLE001
        return False, _brief(exc)


def backend_availability() -> list[dict]:
    now = time.time()
    cached = STATE.backend_cache.get("all")
    if cached and now - cached[0] < 30:
        return cached[1]
    out: list[dict] = []
    for key, meta in TRAIN_BACKENDS.items():
        problems: list[str] = []
        if key == "lerobot":
            exe = _bin("lerobot-train")
            found = Path(exe).exists() or (shutil.which(exe) is not None)
            if not found:
                problems.append("未找到 lerobot-train。若系统已装 LeRobot，请到「环境安装」页"
                                "点「直接使用已安装的 LeRobot」；否则先执行一键安装")
        else:
            for mod in meta["needs"]:
                avail, hint = _module_available(mod)
                if not avail:
                    problems.append(f"缺少 {mod}：{hint}")
        install = f"pip install {' '.join(meta['deps'])} -i {PIP_INDEX}" if meta["deps"] else ""
        out.append({"key": key, "label": meta["label"], "engine": meta["engine"],
                    "desc": meta["desc"], "params": meta["params"],
                    "available": not problems, "problems": problems,
                    "install_hint": install})
    STATE.backend_cache["all"] = (now, out)
    return out


def _dataset_policy_features(repo_id: str, dataset_dir: Optional[str] = None) -> Optional[dict]:
    """从数据集 meta/info.json 生成 policy 的 input/output features 覆盖。

    预训练 VLA（如 SmolVLA）的 config 里写死了相机名 camera1/2/3 和 6 维状态；
    本地数据集一般是 front/side + 7 维（含夹爪）。不覆盖就会报
    "Feature mismatch between dataset/environment and policy config"。
    """
    root = Path(dataset_dir).expanduser() if dataset_dir else (LEROBOT_HOME / str(repo_id))
    info = root / "meta" / "info.json"
    if not info.is_file():
        return None
    try:
        j = json.loads(info.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    feats = j.get("features") or {}
    inputs = {}
    for name, f in feats.items():
        if name == "action":
            continue
        if name == "observation.state":
            inputs[name] = {"type": "STATE", "shape": list(f.get("shape") or [])}
        elif name.startswith("observation.images."):
            # VLA 内部统一缩放到 256；数据集原图分辨率无关
            inputs[name] = {"type": "VISUAL", "shape": [3, 256, 256]}
    action = feats.get("action") or {}
    shape = list(action.get("shape") or [])
    if not inputs or not shape:
        return None
    return {"input": inputs, "output": {"action": {"type": "ACTION", "shape": shape}}}


def train_command(backend: str, cfg: dict, params: dict) -> list[str]:
    """按用户选择的引擎，分发到对应的真实训练代码。"""
    py = _bin("python")
    if backend == "lerobot":
        merged = _deep_merge(cfg, {"train": {k: params[k] for k in
                                             TRAIN_BACKENDS["lerobot"]["params"]
                                             if k in params}})
        return build_command("train", merged)
    if backend == "yolo":
        script = TRAINERS_DIR / "yolo_trainer.py"
        if not script.exists():
            raise ApiError(f"训练脚本不存在：{script}", 500)
        data = str(params.get("data_yaml") or "").strip()
        auto = str(params.get("dataset_dir") or "").strip()
        if not data and not auto:
            raise ApiError("YOLO 训练需要填写 data.yaml 路径，或填写数据集目录自动生成标注文件", 400)
        return [py, "-u", str(script),
                "--data", data, "--auto-yaml", auto,
                "--model", str(params.get("model") or "yolov8n.pt"),
                "--epochs", str(params.get("epochs") or 50),
                "--imgsz", str(params.get("imgsz") or 640),
                "--batch", str(params.get("batch") or 8),
                "--device", str(params.get("device") or "cpu"),
                "--output", str(params.get("output_dir") or "outputs/train/yolo")]
    if backend == "opencv":
        script = TRAINERS_DIR / "opencv_trainer.py"
        if not script.exists():
            raise ApiError(f"训练脚本不存在：{script}", 500)
        ds = str(params.get("dataset_dir") or "").strip()
        if not ds:
            raise ApiError("OpenCV 训练需要填写图像数据集目录（每个类别一个子目录）", 400)
        return [py, "-u", str(script),
                "--dataset", ds,
                "--output", str(params.get("output_dir") or "outputs/train/opencv"),
                "--epochs", str(params.get("epochs") or 20),
                "--classifier", str(params.get("classifier") or "svm"),
                "--img-size", str(params.get("img_size") or 64)]
    raise ApiError(f"未知训练引擎：{backend}，可选：{', '.join(TRAIN_BACKENDS)}", 400)


# ==================================================================
# 11. 任务启动器（真实子进程 / 命令链）
# ==================================================================
def _guard(kind: str) -> None:
    if kind in HW_KINDS:
        if dual_enabled(load_config()):
            if not ENV.dual_connected:
                raise ApiError("当前是双臂模式，尚未连接双臂。请到「设备连接与校准」页面完成"
                               "左从臂/左主臂/右从臂/右主臂四路串口连接", 409)
        elif not ENV.connected:
            raise ApiError("尚未连接主从臂。请到「串口与连接」页面分别选择主臂、从臂串口并点击连接", 409)
    require_env()


def _spawn(kind: str, name: str, cmd: list[str], display: Optional[str] = None,
           stdin_data: Optional[str] = None, env_extra: Optional[dict] = None) -> dict:
    """统一创建子进程 + 日志泵线程，任何失败都回滚状态并优雅报错。"""
    with STATE.lock:
        if STATE.running():
            raise ApiError(f"已有任务在运行（{STATE.job.name if STATE.job else '?'}），请先急停或等待结束", 409)
        if kind == "find-cameras":
            try:
                CAMERAS.close_all()
            except CameraError as exc:
                raise ApiError(str(exc), 409)
        if kind in HW_KINDS:
            sorting=globals().get('SORTING')
            if kind!='sorting-run' and sorting and sorting.control and sorting.control.alive():
                raise ApiError('自动操作 CAN 进程正在保持姿态，请先使用顶部收工归零并失能或断开全部设备',409)
            TELEMETRY.stop_observers()
            daemon = globals().get('_ARM_PROC')
            if daemon is not None and daemon.poll() is None:
                raise ApiError('从臂控制仍占用串口，请先点击顶部“断开全部设备”，再重新连接并启动任务',409)
        shown = display or display_command(cmd)
        STATE.stop_event.clear()
        STATE.step_counter = 0
        HUB.log("$ " + shown, "cmd")
        HUB.state(running=True, job=name, command=shown, kind=kind)
        if kind in CAMERA_JOB_KINDS and kind != 'find-cameras':
            env_extra = dict(env_extra or {})
            env_extra['REBOT_CAMERA_BROKER_URL'] = os.environ.get('REBOT_CAMERA_BROKER_URL', f'http://127.0.0.1:{PORT}')
            env_extra.update(TELEMETRY.environment(monitor_ports(load_config())))
            env_extra['PYTHONPATH'] = str(BACKEND_DIR)+os.pathsep+os.environ.get('PYTHONPATH','')
        job = Job(name=name, kind=kind, command=cmd, display=shown, stdin_data=stdin_data)
        job.step_label = name
        try:
            job.proc = _popen(cmd, stdin_data, env_extra)
        except FileNotFoundError:
            STATE.job = None
            STATE.stop_event.set()
            HUB.state(running=False, job=None)
            raise ApiError(f"找不到可执行程序：{cmd[0]}。请确认已选对 conda 环境并安装 LeRobot", 500)
        except Exception as exc:  # noqa: BLE001
            STATE.job = None
            STATE.stop_event.set()
            HUB.state(running=False, job=None)
            raise ApiError(f"命令启动失败：{_brief(exc)}", 500)
        STATE.job = job
        job.stdin_live = bool(job.proc.stdin is not None and stdin_data is None)
        t = threading.Thread(target=_pump_stdout, args=(job,), daemon=True,
                             name=f"pump-{kind}")
        job.thread = t
        t.start()
        threading.Thread(target=_watch_job_exit, args=(job,), daemon=True,
                         name=f"watch-{kind}").start()
        threading.Thread(target=_progress_beats, args=(job, job.progress_stop), daemon=True,
                         name=f"beat-{kind}").start()
        return ok(name=name, kind=kind, job_id=job.job_id, command=shown)


def _policy_job_env(kind: str, cfg: dict) -> Optional[dict]:
    """评估/推理加载本地检查点时，SmolVLA 需要 VLM 基座。

    VLM 已在本地缓存就强制离线（避免联网 HEAD 卡半天）；
    没缓存则用国内镜像下载。"""
    if kind == "eval":
        path = str(((cfg.get("eval") or {}).get("policy_path") or "")).strip()
    elif kind in ("policy-server", "robot-client"):
        path = str(((cfg.get("async_inference") or {}).get("model_path") or "")).strip()
    else:
        return None
    if not path:
        return None
    local = Path(path).exists() if path.startswith("/") else (ROOT / path).exists()
    vlm = "HuggingFaceTB/SmolVLM2-500M-Video-Instruct"
    if local and _hf_model_cached(vlm)[0]:
        return {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if not local:
        return None
    return {"HF_ENDPOINT": "https://hf-mirror.com"}


def start_job(kind: str, name: str, cfg: dict, extra: Optional[dict] = None) -> dict:
    with STATE.lock:
        _guard(kind)
        cmd = build_command(kind, cfg, extra)
        env_extra = _policy_job_env(kind, cfg)
    return _spawn(kind, name, cmd, env_extra=env_extra)


def start_train(backend: str, params: dict) -> dict:
    require_env()
    params = dict(params)
    if STATE.running():
        raise ApiError('已有任务运行中，请等待当前任务结束后再开始训练', 409)
    if backend == "lerobot" and not str(params.get("job_name") or "").strip():
        policy = re.sub(r"[^A-Za-z0-9_-]+", "_", str(params.get("policy_type") or "model")).strip("_")
        params["job_name"] = f"{policy or 'model'}_{time.strftime('%Y%m%d_%H%M%S')}"
    if backend == "lerobot":
        candidate = _deep_merge(load_config()['train'], params)
        try:
            mode = training_mode(candidate)
            if mode == 'finetune':
                params['base_model'] = prepare_finetune(candidate, ROOT, LEROBOT_HOME)[0]
                output = Path(str(candidate.get('output_dir') or f'outputs/train/{params["job_name"]}')).expanduser()
                if not output.is_absolute():
                    output = ROOT / output
                if output.exists():
                    # A new optimizer/run must never write into an old run.
                    output = output.with_name(output.name + '_finetune_' + str(time.time_ns()))
                params['output_dir'] = str(output)
            params['training_mode'] = mode
        except ValueError as exc:
            raise ApiError(str(exc), 400) from exc
        patch = {"train": dict(params, backend=backend)}
    else:
        patch = {"train": {"backend": backend}, backend: params}
    # Validate the whole command before persisting a new run configuration.
    cmd = train_command(backend, _deep_merge(load_config(), patch), params)
    cfg = save_config(patch)
    # 双臂训练前真实读取数据集 meta：动作维度不匹配是最常见的失败原因
    if backend == "lerobot" and dual_enabled(cfg):
        try:
            f = _dual_dataset_features(str(cfg["train"].get("repo_id") or ""),
                                       str(cfg["train"].get("dataset_dir") or ""))
            if f.get("found"):
                HUB.log(f"[dual] 训练数据集 {f['repo_id']}：state_dim={f['state_dim']}, "
                        f"action_dim={f['action_dim']}, 相机={','.join(f['cameras']) or '无'}。{f['note']}",
                        "ok" if f["dual_likely"] else "warn")
                if not f["dual_likely"]:
                    HUB.log("[dual] 该数据集可能不是双臂采集；用单臂数据训练双臂策略会维度不匹配，"
                            "请在「模型训练」页核对数据集特征", "warn")
            else:
                HUB.log(f"[dual] 数据集元信息未就绪：{f.get('note', '')}", "warn")
        except ApiError as exc:
            HUB.log(f"[dual] 数据集校验跳过：{exc.detail}", "warn")
    env_extra = None
    base = str(params.get("base_model") or "").strip() if params.get('training_mode') == 'finetune' else ''
    if backend == "lerobot" and base:
        cached = Path(base).is_dir()
        if not cached:
            cached, _ = _hf_model_cached(base)
        if cached:
            # 本地已经有模型：强制离线，绝不重复拉取
            env_extra = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
            HUB.log(f"[hf] 本地已缓存 {base}，训练直接复用，不联网下载", "ok")
        else:
            hf = load_config().get("hf") or {}
            endpoint = str(hf.get("endpoint") or "").strip()
            if not endpoint and bool(hf.get("auto", True)):
                # 官方可用就不用镜像；官方不可用但镜像可用才切镜像
                if not _hf_probe("https://huggingface.co/lerobot/smolvla_base/resolve/main/config.json")                         and _hf_probe("https://hf-mirror.com/lerobot/smolvla_base/resolve/main/config.json"):
                    endpoint = "https://hf-mirror.com"
            if endpoint:
                env_extra = {"HF_ENDPOINT": endpoint.rstrip("/")}
                HUB.log(f"[hf] 本地没有 {base}，使用下载源 {endpoint}", "warn")
    result = _spawn("train", f"模型训练 · {TRAIN_BACKENDS[backend]['label']}", cmd,
                    env_extra=env_extra)
    if backend == "lerobot":
        result["job_name"] = params["job_name"]
        result['training_mode'] = params['training_mode']
        result['output_dir'] = cfg['train']['output_dir']
        result['base_model'] = base
    return result


def _cleanup_failed_clone() -> None:
    """克隆失败 / 被急停后清掉只建了一半的目录，否则重试会被误判成「仓库已存在」。
    只清理项目根目录下名为 lerobot 且未检出源码的目录，绝不动用户自己的源码。"""
    try:
        if not REPO_DIR.exists() or _repo_cloned():
            return
        if REPO_DIR.parent != ROOT or REPO_DIR.name != "lerobot":
            return
        shutil.rmtree(REPO_DIR)
        HUB.log(f"[setup] 已清理未完成的克隆目录：{REPO_DIR}", "warn")
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[setup] 清理未完成克隆目录失败：{_brief(exc)}", "warn")


def chain_runner(job: Job, steps: list[tuple[str, list[str]]]) -> None:
    """顺序执行安装链：逐行日志 + 步骤进度 + 心跳 + 超时兜底 + 随时急停。"""
    total = len(steps)
    try:
        for idx, (label, cmd) in enumerate(steps, 1):
            if STATE.stop_event.is_set():
                break
            is_clone = "克隆" in label or any(str(x) == "clone" for x in cmd)
            limit = CLONE_TIMEOUT if is_clone else STEP_TIMEOUT
            with STATE.lock:
                job.step_index = idx
                job.step_total = total
                job.step_label = label
                job.step_started_at = time.time()
                job.step_limit = limit
                job.last_line = ""
                job.last_line_at = time.time()
                job.awaiting_input = False
                job.awaiting_prompt = ""
            HUB.log(f"[setup] [{idx}/{total}] {label} · 开始（超时上限 {limit}s）", "cmd")
            HUB.log("$ " + display_command(cmd), "cmd")
            publish_progress(job, "step-start")
            try:
                proc = _popen(cmd, job.stdin_data)
            except Exception as exc:  # noqa: BLE001
                HUB.log(f"[setup] [{idx}/{total}] 启动失败：{_brief(exc)}", "error")
                job.exit_code = 1
                break
            job.proc = proc
            job.stdin_live = bool(proc.stdin is not None and job.stdin_data is None)
            pump = threading.Thread(target=_pump_stream, args=(job, proc), daemon=True,
                                    name=f"pump-step-{idx}")
            pump.start()
            deadline = time.time() + limit
            code: int | None = None
            while True:
                if STATE.stop_event.is_set():
                    HUB.log(f"[setup] [{idx}/{total}] 收到急停，正在终止子进程", "warn")
                    _terminate(proc)
                    code = 130
                    break
                rc = proc.poll()
                if rc is not None:
                    code = rc
                    break
                if time.time() > deadline:
                    HUB.log(f"[setup] [{idx}/{total}] 超过 {limit}s 仍未结束，已自动终止。"
                            "常见原因：仓库源 / pip 源不通或代理异常，请在「安装源」更换地址后重试", "error")
                    _terminate(proc)
                    code = 124
                    break
                time.sleep(0.25)
            pump.join(timeout=3)
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            job.stdin_live = False
            job.awaiting_input = False
            job.proc = None
            if code != 0:
                HUB.log(f"[setup] [{idx}/{total}] 「{label}」失败（退出码 {code}），已停止后续步骤", "error")
                job.exit_code = code
                break
            HUB.log(f"[setup] [{idx}/{total}] 「{label}」完成", "ok")
        else:
            job.exit_code = 0
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[setup] 异常：{_brief(exc)}", "error")
        job.exit_code = 1
    finally:
        if job.exit_code is None:
            job.exit_code = 130 if STATE.stop_event.is_set() else 0
        if job.exit_code not in (0, None):
            _cleanup_failed_clone()
        if job.exit_code == 0:
            HUB.log(f"=== {job.name} 全部完成（{total} 步）===", "ok")
        else:
            HUB.log(f"=== {job.name} 未全部完成，退出码 {job.exit_code} ===", "warn")
        job.progress_stop.set()
        job.finished.set()
        HUB.state(**_job_progress(job), action="finish", refresh_env=True)


def start_chain(kind: str, name: str, steps: list[tuple[str, list[str]]], cfg: dict,
                stdin_data: Optional[str] = None) -> dict:
    if not steps:
        raise ApiError("没有可执行的步骤", 400)
    with STATE.lock:
        if STATE.running():
            raise ApiError("已有任务在运行，请先停止", 409)
        STATE.stop_event.clear()
        display = "\n".join(display_command(c) for _, c in steps)
        job = Job(name=name, kind=kind, command=steps[0][1], display=display,
                  stdin_data=stdin_data, step_total=len(steps), step_label=steps[0][0])
        STATE.job = job
        HUB.log(f"=== {name}：共 {len(steps)} 步 ===", "ok")
        HUB.state(**_job_progress(job), command=display, action="start")
        t = threading.Thread(target=chain_runner, args=(job, steps), daemon=True,
                             name=f"chain-{kind}")
        job.thread = t
        t.start()
        threading.Thread(target=_progress_beats, args=(job, job.progress_stop), daemon=True,
                         name=f"beat-{kind}").start()
        return ok(name=name, kind=kind, steps=len(steps),
                  commands=[display_command(c) for _, c in steps])


def stop_all(force: bool = False) -> dict:
    # Request graceful cleanup once, keep status/camera requests responsive while
    # both arms complete their slow homing. A second click must not send SIGINT
    # into an already running home and cut the trajectory short.
    with STATE.lock:
        STATE.stop_event.set();job=STATE.job
        if not job or job.finished.is_set():return ok(stopped=None,stopping=False)
        if job.stopping and not force:return ok(stopped=job.name,stopping=True)
        job.stopping=True;stopped=job.name
        if not force:
            def cleanup():
                _terminate(job.proc,graceful=True)
                if job.proc and job.proc.poll() is not None:_finish_job_once(job,job.proc.returncode)
            threading.Thread(target=cleanup,daemon=True,name='slow-stop-'+job.kind).start()
            HUB.log('[停止] 已请求结束动作；如任务需要归零，将从实测当前位置慢速收工，请等待进度完成','warn')
            publish_progress(job,'stopping')
            return ok(stopped=stopped,stopping=True)
    # Explicit forced release remains immediate and never starts a home motion.
    _terminate(job.proc,graceful=False)
    if job.proc and job.proc.poll() is not None:_finish_job_once(job,job.proc.returncode)
    HUB.log('[急停] 已终止：'+stopped,'warn')
    return ok(stopped=stopped,stopping=False)


def write_job_input(text: str, job_id: Optional[str] = None, prompt_id: Optional[int] = None) -> dict:
    """把一行输入写进当前子进程的 stdin（校准 / 遥操作在等回车时使用）。

    只写管道，不经过 shell，也不回显内容（可能含密码等敏感信息）。
    """
    with STATE.lock:
        job = STATE.job
        if job is None or job.finished.is_set():
            raise ApiError("当前没有正在运行的任务，无法发送输入", 409)
        if job_id is not None and job.job_id != job_id:
            raise ApiError("原任务已结束，输入未发送到新的任务", 409)
        if prompt_id is not None and (not job.awaiting_input or job.prompt_id != prompt_id):
            raise ApiError("此输入提示已处理或已过期，请查看当前提示", 409)
        if not job.stdin_live or job.proc is None or job.proc.stdin is None:
            raise ApiError("当前任务的终端输入已关闭（例如已在等待 sudo 密码），无法发送", 409)
        stdin = job.proc.stdin
        data = text.encode("utf-8")
        with job.input_lock:
            try:
                stdin.write(data)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ApiError(f"发送输入失败：{_brief(exc)}", 409)
        job.awaiting_input = False
        job.awaiting_prompt = ""
        job.last_line_at = time.time()
    HUB.log("[输入] 已向终端发送 " + ("回车确认" if data.strip() == b"" else "1 行输入（内容不记录）"),
            "ok")
    publish_progress(job, "input")
    return ok(sent=len(data))


# ==================================================================
# 12. 真实文件系统 / 串口 / 相机辅助
# ==================================================================
def _safe_dataset_path(repo_id: str) -> Optional[Path]:
    rid = str(repo_id or "").strip().strip("/")
    if not rid or rid.startswith(".") or ".." in rid.split("/") or Path(rid).is_absolute():
        return None
    target = (LEROBOT_HOME / rid).resolve()
    try:
        target.relative_to(LEROBOT_HOME.resolve())
    except ValueError:
        return None
    return target


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _dataset_episodes(d: Path) -> int:
    try:
        return int(json.loads((d / "meta" / "info.json").read_text("utf-8"))
                   .get("total_episodes", 0))
    except Exception:  # noqa: BLE001
        return 0


def list_datasets() -> list[dict]:
    """列出真实数据集。

    repo_id 可以是 a/b（LeRobot 存到 <root>/a/b），所以顶层目录没有
    meta/info.json 时要向下一层继续找；否则会把父目录当成 0 集数据集，
    回放/可视化用这个父目录还会去 Hub 下载而卡住。
    """
    items: list[dict] = []
    if not LEROBOT_HOME.exists():
        return items
    try:
        candidates: list[tuple[str, Path]] = []
        for d in sorted(LEROBOT_HOME.iterdir()):
            if not d.is_dir() or d.name == "calibration":
                continue
            if (d / "meta" / "info.json").is_file():
                candidates.append((d.name, d))
                continue
            try:
                for c in sorted(d.iterdir()):
                    if c.is_dir() and (c / "meta" / "info.json").is_file():
                        candidates.append((f"{d.name}/{c.name}", c))
            except OSError:
                continue
        for repo_id, path in candidates:
            items.append({"repo_id": repo_id, "path": str(path),
                          "size_mb": round(_dir_size(path) / 1048576, 2),
                          "episodes": _dataset_episodes(path)})
    except OSError as exc:
        HUB.log(f"[dataset] 扫描失败：{_brief(exc)}", "warn")
    return items


def list_checkpoints() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    def _add(rel_path: str, cfg_path: str = ""):
        rel = rel_path.replace("\\", "/")
        if rel not in seen:
            seen.add(rel)
            item = {"path": rel, "config": cfg_path.replace("\\", "/") if cfg_path else ""}
            if (ROOT / rel / 'config.json').is_file():
                try:
                    policy = read_policy(ROOT / rel)
                    item.update(policy_type=policy['type'], can_finetune=(ROOT / rel / 'model.safetensors').is_file())
                except ValueError:
                    item.update(can_finetune=False)
            out.append(item)
    try:
        if OUTPUTS_DIR.exists():
            for d in sorted(OUTPUTS_DIR.rglob("pretrained_model")):
                if d.is_dir():
                    cfg = d / "train_config.json"
                    _add(str(d.relative_to(ROOT)), str(cfg.relative_to(ROOT)) if cfg.exists() else "")
            for p in sorted(OUTPUTS_DIR.rglob("*.xml")):
                _add(str(p.relative_to(ROOT)))
            # Trained best.pt first, then other .pt under outputs/
            pts = sorted(OUTPUTS_DIR.rglob("*.pt"), key=lambda x: (0 if x.name == "best.pt" else 1, str(x)))
            for p in pts:
                _add(str(p.relative_to(ROOT)))
        # Ultralytics 旧版本/旧路径会把权重写到 runs/detect/...，也一起列出来
        runs = ROOT / "runs"
        if runs.is_dir():
            for p in sorted(runs.rglob("*.pt"), key=lambda x: (0 if x.name == "best.pt" else 1, str(x))):
                _add(str(p.relative_to(ROOT)))
        # 项目 models/ 目录与根目录下的本地 .pt 权重
        models_dir = ROOT / "models"
        if models_dir.is_dir():
            for p in sorted(models_dir.glob("*.pt")):
                _add(str(p.relative_to(ROOT)))
        for p in sorted(ROOT.glob("*.pt")):
            _add(str(p.relative_to(ROOT)))
    except OSError as exc:
        HUB.log(f"[checkpoint] 扫描失败：{_brief(exc)}", "warn")
    return out


def scan_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        raise ApiError("缺少 pyserial，无法扫描串口。请在「环境安装」或所选 conda 环境执行："
                       f"pip install pyserial -i {PIP_INDEX}", 503)
    try:
        ports = list(list_ports.comports())
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"串口扫描失败：{_brief(exc)}", 500)
    out = []
    for p in ports:
        writable = True
        if not IS_WINDOWS:
            try:
                writable = os.access(p.device, os.R_OK | os.W_OK)
            except OSError:
                writable = False
        out.append({"device": p.device, "description": p.description or "",
                    "hwid": p.hwid or "", "writable": writable})
    return out


def test_port(port: str) -> None:
    """真实尝试打开串口，连不上就抛出可读错误，绝不伪造连接成功。"""
    try:
        import serial  # type: ignore
    except ImportError:
        raise ApiError("缺少 pyserial，无法验证串口连接", 503)
    try:
        s = serial.Serial(port, timeout=0.3, write_timeout=0.3)
        s.close()
    except Exception as exc:  # noqa: BLE001
        hint = ("请确认 COM 编号正确、USB 转串口驱动已安装（设备管理器），且端口未被其它程序占用"
                if IS_WINDOWS else
                "请确认设备已连接、未被其它程序占用，并已完成 sudo chmod 666 授权")
        raise ApiError(f"无法打开串口 {port}：{_brief(exc)}。{hint}", 400)


TELEMETRY = Telemetry(ROOT)
VISION = VisionProcess(ROOT)
DEPTH = VISION.depth
DEVICES_RELEASED = bool(load_config().get('device_session',{}).get('paused',False))
VISION.paused = DEVICES_RELEASED
CAMERAS = VISION.rgb


def monitor_ports(cfg):
    if dual_enabled(cfg):
        ports = dual_ports(cfg)
        return {'left':ports.get('left_follower',''),'right':ports.get('right_follower','')}
    return {'left':str((cfg.get('follower') or {}).get('port') or '')}


def observer_supervisor():
    while not TELEMETRY.stop.wait(.5):
        try:
            with STATE.lock:
                TELEMETRY.configure(monitor_ports(load_config()))
                owns_ports=STATE.running() and getattr(STATE.job,'kind',None) in HW_KINDS
                if owns_ports or DEVICES_RELEASED:
                    TELEMETRY.stop_observers()
                else:
                    TELEMETRY.observe_idle(monitor_ports(load_config()), bool(ENV.connected or ENV.dual_connected))
        except Exception as exc:
            HUB.log(f'[monitor] 状态观察器：{_brief(exc)}','warn')

CAMERA_JOB_KINDS = {"teleoperate", "record", "eval", "robot-client", "find-cameras", "sorting-run"}


def camera_probe(index=0) -> Any:
    """Acquire a shared capture lease, preserving the existing read/release API."""
    try:
        if DEVICES_RELEASED:
            raise ApiError("设备已全部断开，请先恢复监视或重新连接设备",409)
        key = source_key(index)
        with STATE.lock:
            config = {}
            for c in load_config().get("cameras", []):
                if source_key(c.get("index_or_path", 0)) == key:
                    config = c
                    break
            if STATE.running() and STATE.job.kind == "find-cameras":
                raise ApiError("SDK 扫描正在使用相机，请稍后重试", 409)
            return CAMERAS.acquire(index, config)
    except CameraError as exc:
        raise ApiError(str(exc), 503)


# ==================================================================
# 13. FastAPI（统一异常处理 + 所有路由）
# ==================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    HUB.loop = asyncio.get_running_loop()
    TELEMETRY.start(HUB.loop)
    threading.Thread(target=observer_supervisor,daemon=True,name='observer-supervisor').start()
    HUB.log(f"后端就绪 | {platform.system()} | 真机模式（无模拟数据）", "ok")
    check_permissions_on_startup()
    if ENV.active_env:
        HUB.log(f"[env] 当前虚拟环境：{ENV.active_env}", "ok")
    else:
        HUB.log("[env] 尚未选择虚拟环境，请在启动向导中选择或创建", "warn")
    HUB.log(f"数据集目录：{LEROBOT_HOME}")

    def _background_lerobot_detect() -> None:
        try:
            info = detect_lerobot(force=True)
            if info["installed"]:
                HUB.log(f"[lerobot] 检测到已安装，可直接复用：{info['source']} / "
                        f"{info['version']} / {info['python']}", "ok")
            else:
                HUB.log("[lerobot] 未检测到已安装的 LeRobot，可在「环境安装」页一键安装", "warn")
        except Exception as exc:  # noqa: BLE001
            HUB.log(f"[lerobot] 安装探测失败：{_brief(exc)}", "warn")

    # 放到后台线程，避免启动时并行探测十几个 Python 环境阻塞网页服务
    threading.Thread(target=_background_lerobot_detect, daemon=True,
                      name="lerobot-detect").start()
    yield
    if globals().get('SORTING'):SORTING.close()
    TELEMETRY.close()
    VISION.stop()
    try:
        CAMERAS.close_all(exclusive=True)
    except CameraError as exc:
        HUB.log(f"[camera] 关闭预览：{exc}", "warn")
    STATE.stop_event.set()
    if STATE.job and STATE.job.proc:
        _terminate(STATE.job.proc)


APP_VERSION='3.5.4'
APP_BUILD='20261114.20'
app = FastAPI(title="机械臂一键操控与训练平台", version=APP_VERSION, lifespan=lifespan)

class BuildHeaders:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        async def versioned(message):
            if message['type']=='http.response.start':
                headers=list(message.get('headers',[]));headers.append((b'x-rebot-version',(APP_VERSION+'+'+APP_BUILD).encode()))
                path=scope.get('path','')
                if path=='/' or path.startswith('/p/'):
                    headers=[h for h in headers if h[0].lower()!=b'cache-control'];headers.append((b'cache-control',b'no-store, max-age=0'))
                message=dict(message,headers=headers)
            await send(message)
        await self.app(scope,receive,versioned)

app.add_middleware(BuildHeaders)

@app.get('/api/version')
def api_version():
    return {'version':APP_VERSION,'build':APP_BUILD,'vision_architecture':'single-capture-process','platform':platform.system(),'control_host':socket.gethostname()}


@app.exception_handler(CameraError)
async def camera_error_handler(request: Request, exc: CameraError):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)



class ClientAuditMiddleware:
    """把「哪台访问设备点了什么」写进真实日志。

    用纯 ASGI 包装而非 BaseHTTPMiddleware，避免影响摄像头 MJPEG 流式响应；
    只记录非 GET 的 /api 动作，不修改任何请求或响应。
    """

    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            method = scope.get("method", "")
            path = scope.get("path", "")
            if method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
                headers = {}
                for key, value in scope.get("headers") or []:
                    headers[key.decode("latin-1").lower()] = value.decode("latin-1")
                alias = unquote((headers.get("x-arm-client") or "").strip())[:40]
                client = scope.get("client")
                ip = client[0] if client else "?"
                who = alias or ip
                HUB.log(f"[访问] {who} · {method} {path}", "info")
        await self.wrapped(scope, receive, send)


app.add_middleware(ClientAuditMiddleware)


@app.exception_handler(ApiError)
async def _on_api_error(request: Request, exc: ApiError):
    HUB.log(f"[请求失败] {_req_path(request)}: {exc.detail}", "error")
    return JSONResponse(status_code=exc.code, content={"ok": False, "error": exc.detail})


@app.exception_handler(RequestValidationError)
async def _on_validation(request: Request, exc: RequestValidationError):
    detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg', '')}"
                       for e in exc.errors()[:3])
    return JSONResponse(status_code=422,
                        content={"ok": False, "error": f"请求参数不正确：{detail}"})


@app.exception_handler(StarletteHTTPException)
async def _on_http(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"ok": False, "error": str(exc.detail)})


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    traceback.print_exc()
    HUB.log(f"[未捕获异常] {_req_path(request)}: {_brief(exc)}", "error")
    return JSONResponse(status_code=500,
                        content={"ok": False,
                                 "error": f"服务器内部错误：{_brief(exc)}。详情见后端终端与日志页面"})


# ---- 静态页面 ----
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index(request: Request):
    if request.headers.get('sec-fetch-dest') != 'iframe' and request.query_params.get('view') != 'workspace':
        return FileResponse(FRONTEND_DIR/'console.html')
    f = FRONTEND_DIR / "index.html"
    if not f.exists():
        raise ApiError("缺少 frontend/index.html，前端文件不完整", 500)
    return FileResponse(f)


@app.get("/p/{name}")
def page(name: str, request: Request):
    if name in {"arm", "grasp"}:
        return RedirectResponse("/p/home", status_code=307)
    if name == "dual":
        return RedirectResponse("/p/connect", status_code=307)
    if name not in PAGE_NAMES:
        raise ApiError("页面不存在", 404)
    if request.headers.get('sec-fetch-dest') != 'iframe' and request.query_params.get('view') != 'workspace':
        return FileResponse(FRONTEND_DIR/'console.html')
    f = PAGES_DIR / f"{name}.html"
    if not f.exists():
        raise ApiError(f"缺少页面文件：{f.name}", 500)
    return FileResponse(f)


# ---- 状态与配置 ----
def _dual_status(cfg: dict) -> dict:
    """双臂模式摘要：给总览、右栏、各页面判断当前是单臂还是双臂。"""
    d = cfg.get("dual_arm") or {}
    return {"enabled": bool(d.get("enabled")),
            "connected": ENV.dual_connected,
            "robot_type": str(d.get("robot_type") or ""),
            "teleop_type": str(d.get("teleop_type") or ""),
            "coordination": str(d.get("coordination") or "sync"),
            "ports": dual_ports(cfg),
            "active_ports": dict(ENV.dual_ports),
            "sides": {"left": dual_side(cfg, "left"), "right": dual_side(cfg, "right")}}


@app.get("/api/status")
def api_status():
    try:
        rt = ENV.lerobot_runtime or {}
        with STATE.lock:
            job = STATE.job
            running = bool(job and not job.finished.is_set())
            p = _job_progress(job) if job else {}
            info = {"name": job.name, "kind": job.kind, "command": job.display,
                    "elapsed": p["elapsed"], "exit_code": job.exit_code,
                    "step": p["step"], "step_total": p["step_total"],
                    "step_label": p["step_label"], "step_elapsed": p["step_elapsed"],
                    "step_limit": p["step_limit"], "last_line": p["last_line"],
                    "silent_for": p["silent_for"],
                    "awaiting_input": p["awaiting_input"], "prompt": p["prompt"],
                    "job_id": p["job_id"], "prompt_id": p["prompt_id"], "stopping":job.stopping,
                    } if job else None
            snapshot_at=time.time()
        return {"status_ts":snapshot_at,"platform": platform.system(), "is_windows": IS_WINDOWS,
                "running": running,
                "job": info if running else None,
                "progress": info if running else None, "lerobot_home": str(LEROBOT_HOME),
                "outputs_dir": str(OUTPUTS_DIR), "leader_type": LEADER_TYPE,
                "follower_type": FOLLOWER_TYPE, "can_adapter": CAN_ADAPTER,
                "env_ready": bool(ENV.active_env) or bool(rt.get("installed")),
                "active_env": ENV.active_env,
                "lerobot_installed": bool(rt.get("installed")),
                "lerobot_source": str(rt.get("source") or ""),
                "connected": ENV.connected, "leader_port": ENV.leader_port,
                "follower_port": ENV.follower_port,
                "dual_connected": ENV.dual_connected,
                "dual_arm": _dual_status(load_config()),
                "serial": serial_permission_report()}
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"读取状态失败：{_brief(exc)}", 500)


def _lan_ips() -> list[str]:
    """列出本机可用于局域网访问的 IPv4（真实网卡地址，不是模拟值）。"""
    ips: list[str] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            ip = str(item[4][0])
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        # UDP connect 不发包，只用来取本机出口地址；断网时也能拿到网卡地址
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("223.5.5.5", 80))
        ip = str(s.getsockname()[0])
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return ips


def _ua_brief(ua: str, sec_platform: str) -> tuple[str, str]:
    """从真实 User-Agent / Sec-CH-Platform 读出浏览器与访问端系统。"""
    low = (ua or "").lower()
    if "edg/" in low:
        browser = "Edge"
    elif "opr/" in low or "opera" in low:
        browser = "Opera"
    elif "chrome/" in low or "crios" in low:
        browser = "Chrome"
    elif "firefox/" in low or "fxios" in low:
        browser = "Firefox"
    elif "safari/" in low:
        browser = "Safari"
    else:
        browser = "浏览器"
    p = (sec_platform or "").strip('"')
    if not p:
        if "windows" in low:
            p = "Windows"
        elif "android" in low:
            p = "Android"
        elif "iphone" in low or "ipad" in low:
            p = "iOS"
        elif "mac os" in low or "macintosh" in low:
            p = "macOS"
        elif "linux" in low:
            p = "Linux"
        else:
            p = "未知系统"
    return browser, p


@app.get("/api/runtime")
def api_runtime(request: Request):
    """明确区分「服务端真机」与「当前打开网页的那台设备」。

    串口、摄像头、conda 环境、数据集、训练进程都只存在于运行 python start.py
    的那台机器上；浏览器没有权限读取访问端本机硬件，所以所有访问设备看到的是
    同一份服务端状态。本接口把两端信息都如实返回，避免误读状态来源。
    """
    try:
        ua = request.headers.get("user-agent", "")
        browser, os_name = _ua_brief(ua, request.headers.get("sec-ch-platform", ""))
        peer = request.client.host if request.client else ""
        fwd = request.headers.get("x-forwarded-for", "")
        client_ip = fwd.split(",")[0].strip() if fwd else peer
        # 浏览器把中文设备名 URL 编码后放在头里（HTTP 头本身只允许 ASCII）
        alias = unquote((request.headers.get("x-arm-client") or "").strip())[:40]
        srv = request.scope.get("server") or ("", "")
        lan = _lan_ips()
        local = client_ip in ("127.0.0.1", "::1", "localhost") or client_ip in lan
        return {
            "server": {
                "hostname": socket.gethostname(),
                "platform": platform.system(),
                "platform_detail": platform.platform(),
                "python": sys.version.split()[0],
                "pid": os.getpid(),
                "root": str(ROOT),
                "lan_ips": lan,
                "port": srv[1] if len(srv) > 1 else None,
                "listen_host": str(srv[0]) if srv else "",
            },
            "client": {
                "ip": client_ip, "peer": peer, "browser": browser, "os": os_name,
                "alias": alias, "user_agent": ua[:300], "local": local,
                "language": request.headers.get("accept-language", "")[:80],
            },
            "note": "硬件与运行环境状态来自服务端真机；界面偏好只保存在当前浏览器。",
        }
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"读取运行位置失败：{_brief(exc)}", 500)


@app.get("/api/config")
def api_get_config():
    return load_config()


@app.get("/api/config/revision")
def api_config_revision():
    return {"revision": CONFIG_FILE.stat().st_mtime_ns if CONFIG_FILE.exists() else 0}


@app.post("/api/config")
def api_save_config(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    with STATE.lock:
        old = load_config()
        merged = _deep_merge(old, e)
        if 'monitor_layout' in e:
            try:resolve_monitor_geometry(merged.get('monitor_layout'))
            except ValueError as exc:raise ApiError(str(exc),400)
        devices_changed = any(old.get(k) != merged.get(k) for k in ('dual_arm', 'leader', 'follower'))
        sorting=globals().get('SORTING')
        if sorting and STATE.running() and getattr(STATE.job,'kind',None)=='sorting-run' and any(k in e for k in ('sorting','cameras')):
            raise ApiError('自动操作期间不能修改定位、技能或相机配置，请先停止任务',409)
        if devices_changed and sorting and sorting.control and sorting.control.alive():
            raise ApiError('请先收工归零并失能，再修改设备配置',409)
        if devices_changed and STATE.running():
            raise ApiError("任务运行期间不能修改设备配置，请先停止任务", 409)
        try:
            if "cameras" in e:
                validate_configs(e["cameras"])
                if STATE.running() and STATE.job.kind in CAMERA_JOB_KINDS:
                    raise ApiError("相机使用中，修改已保留；任务结束后将自动重试", 409)
                def profiles(cameras):
                    return {source_key(c['index_or_path']): (c.get('width',640), c.get('height',480),
                            c.get('fps',30), c.get('fourcc') or 'MJPG')
                            for c in cameras}
                before, after = profiles(old.get('cameras', [])), profiles(e['cameras'])
                CAMERAS.close_devices({k for k in before.keys() | after.keys() if before.get(k) != after.get(k)})
            config = save_config(e)
        except CameraError as exc:
            raise ApiError(str(exc), 400)
        if devices_changed:
            ENV.connected = ENV.dual_connected = False
            ENV.dual_ports = {}
            HUB.state(connected=False, dual_connected=False)
        return ok(config=config)


@app.get("/api/ports")
def api_ports():
    return {"ports": scan_ports()}


@app.get("/api/install-commands")
def api_install_commands():
    return {"commands": INSTALL_COMMANDS, "platform": platform.system(),
            "is_windows": IS_WINDOWS,
            "note": ("Windows：已自动隐藏 sudo/chmod 等 Linux 专属命令，串口使用 COM 端口"
                     if IS_WINDOWS else "Ubuntu 真机命令（含串口 sudo 授权）")}


@app.post("/api/stop")
def api_stop(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    return stop_all(force=bool(e.get("force", False)))


@app.post("/api/job/input")
def api_job_input(payload: Optional[dict] = Body(default=None)):
    """向正在运行的子进程写入一行输入：校准/遥操作在等回车时由网页按钮调用。"""
    e = payload or {}
    key = str(e.get("key") or "").strip().lower()
    if key and key not in ("enter", "c"):
        raise ApiError("key 只支持 enter（回车）或 c（强制重新校准）", 400)
    text = str(e.get("text") or "")
    if key == "enter":
        text = ""
    elif key == "c":
        text = "c"
    if "\x00" in text:
        raise ApiError("输入不能包含 NUL 字符", 400)
    if not text.endswith("\n"):
        text += "\n"
    if len(text) > 4096:
        raise ApiError("单次输入过长（上限 4096 字符）", 400)
    prompt_id = e.get("prompt_id")
    if prompt_id is not None and (isinstance(prompt_id, bool) or not isinstance(prompt_id, int)):
        raise ApiError("prompt_id 必须是整数", 400)
    return write_job_input(text, job_id=e.get("job_id"), prompt_id=prompt_id)


# ---- 启动向导：conda 环境 ----
@app.get("/api/env/status")
def api_env_status():
    return env_status()


@app.get("/api/env/locations")
def api_env_locations():
    """创建位置候选：全部来自后端真实文件系统与 conda info，无模拟数据。"""
    return conda_env_locations()


@app.post("/api/env/select")
def api_env_select(payload: Optional[dict] = Body(default=None)):
    target = str((payload or {}).get("env") or "").strip()
    if not target:
        raise ApiError("请选择环境（环境名或完整路径）", 400)
    return ok(**select_env(target))


@app.post("/api/env/create")
def api_env_create(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    name = str(e.get("name") or "lerobot").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ApiError("环境名只能包含字母、数字、点、下划线和连字符", 400)
    if not find_conda():
        raise ApiError("未检测到 conda。请先点击「一键安装 Miniforge/conda」", 409)
    location = str(e.get("location") or "").strip()
    target = resolve_env_target(location, name)   # 位置错误在启动后台任务前就优雅返回
    saved_location = "" if target is None else str(Path(location).expanduser())
    cfg = save_config({"environment": {
        "python_version": e.get("python_version") or "3.12",
        "env_location": saved_location,
    }})
    return start_chain("env-create", f"创建环境 {name}",
                       setup_steps("create-env", cfg,
                                   {"name": name, "location": location}), cfg)


@app.post("/api/setup/config")
def api_setup_config(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    cfg = save_config({"setup": {k: e[k] for k in
                                 ("repo_url", "pip_index", "conda_url", "with_ffmpeg")
                                 if k in e}})
    return ok(setup=cfg["setup"])


@app.post("/api/setup/install-conda")
def api_install_conda(payload: Optional[dict] = Body(default=None)):
    cfg = load_config()
    return start_chain("setup-conda", "一键安装 Miniforge/conda",
                       setup_steps("install-conda", cfg, payload), cfg)


@app.get("/api/lerobot/detect")
def api_lerobot_detect(force: int = 0):
    """真实探测系统里是否已安装 LeRobot（并行检查 conda 环境 / 当前 Python / PATH）。"""
    return ok(lerobot=detect_lerobot(force=bool(force)))


@app.post("/api/lerobot/use")
def api_lerobot_use():
    """检测到已安装时直接复用：门禁放行、命令改用该环境的 Python 与 CLI。"""
    return use_lerobot()


@app.post("/api/setup/install-lerobot")
def api_install_lerobot(payload: Optional[dict] = Body(default=None)):
    require_env()
    e = dict(payload or {})
    force = bool(e.get("force"))
    cfg = save_config({"setup": {k: e[k] for k in
                                 ("repo_url", "pip_index", "with_ffmpeg")
                                 if k in e}})
    info = detect_lerobot(force=force)
    steps = setup_steps("install-lerobot", cfg, {**e, "_detected": info})
    if lerobot_reusable(info, force) and not steps:
        # 已安装且依赖齐全：不启动任何安装任务，直接复用
        use_lerobot(info)
        HUB.state(refresh_env=True)
        return ok(already_installed=True, reused=info,
                  skipped=["git clone", "pip install -e ./lerobot", "插件与 Web 依赖"],
                  message="检测到已安装 LeRobot 且依赖齐全，直接复用，无需重新安装")
    if lerobot_reusable(info, force):
        HUB.log("[setup] 已装 LeRobot：仅补齐缺失依赖，不重复安装主程序", "ok")
        use_lerobot(info)   # 同时把命令运行时切到这个已安装的环境
    return start_chain("setup-lerobot", "一键安装 LeRobot（清华源 + gh-proxy）", steps, cfg)


# ---- 串口权限与连接 ----
@app.get("/api/system/permissions")
def api_permissions():
    return serial_permission_report()


@app.post("/api/system/chmod")
def api_chmod(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    return ok(**run_chmod(str(e.get("password") or ""), bool(e.get("usb", False))))


@app.post("/api/system/chmod-ports")
def api_chmod_ports():
    return start_job("chmod-ports", "串口权限（免密）", load_config())


@app.post("/api/system/chmod-usb")
def api_chmod_usb():
    return start_job("chmod-usb", "USB 相机权限（免密）", load_config())


@app.post("/api/system/check-env")
def api_check_env():
    require_env()
    return start_job("check-env", "环境检查", load_config())


@app.post("/api/system/clear-calibration")
def api_clear_calibration(payload: Optional[dict] = Body(default=None)):
    which = str((payload or {}).get("target") or "both")
    if which not in ("robots", "teleoperators", "both"):
        raise ApiError("target 只能是 robots / teleoperators / both", 400)
    base = LEROBOT_HOME / "calibration"
    dirs = ([base / "robots"] if which in ("robots", "both") else []) + \
           ([base / "teleoperators"] if which in ("teleoperators", "both") else [])
    removed = []
    for d in dirs:
        try:
            if d.exists():
                shutil.rmtree(d)
                removed.append(str(d))
        except OSError as exc:
            raise ApiError(f"清理 {d} 失败：{_brief(exc)}", 500)
    HUB.log(f"校准缓存清理完成，共 {len(removed)} 个目录", "ok")
    return ok(removed=removed)


def _check_ports_exist(wanted: dict[str, str]) -> None:
    """真实校验串口：都在扫描结果里、Linux 有读写权限、逐个真打开一次。"""
    known = {p["device"] for p in scan_ports()}
    for key, port in wanted.items():
        if port not in known:
            raise ApiError(f"{key} 的串口 {port} 不在当前扫描结果中，请重新扫描后再连接", 400)
    if not IS_WINDOWS:
        for key, port in wanted.items():
            if not Path(port).exists():
                raise ApiError(f"{key} 的串口设备已消失：{port}，请重新插拔并扫描", 400)
            if not os.access(port, os.R_OK | os.W_OK):
                raise ApiError(f"{key} 的串口 {port} 没有读写权限。请在「串口与校准」页面完成授权", 403)
    for key, port in wanted.items():
        test_port(port)
        HUB.log(f"[connect] {key} -> {port} 打开成功", "ok")


def _connect_dual(e: dict) -> dict:
    """双臂连接：四路串口逐个真实打开，绝不只凭下拉框选择就标记已连接。"""
    cfg = load_config()
    saved = dual_ports(cfg)
    ports: dict[str, str] = {}
    for key in DUAL_PORT_KEYS:
        v = str(e.get(key) or "").strip() or saved.get(key, "")
        ports[key] = v
    missing = [k for k, v in ports.items() if not v]
    if missing:
        raise ApiError("双臂连接需要四个串口：" + "、".join(missing), 400)
    seen: dict[str, str] = {}
    for key, port in ports.items():
        if port in seen:
            raise ApiError(f"串口 {port} 同时分配给了 {seen[port]} 和 {key}；四路必须互不相同", 400)
        seen[port] = key
    _check_ports_exist(ports)
    patch = {"dual_arm": {
        "enabled": True,
        "left": {"follower_port": ports["left_follower"], "leader_port": ports["left_leader"]},
        "right": {"follower_port": ports["right_follower"], "leader_port": ports["right_leader"]}}}
    save_config(patch)
    ENV.dual_ports, ENV.dual_connected, ENV.connected = ports, True, False
    for key in DUAL_PORT_KEYS:
        HUB.log(f"[dual] {key} 已连接 -> {ports[key]}", "ok")
    HUB.state(connected=False, dual_connected=True, dual_ports=ports)
    return ok(connected=True, dual=True, ports=ports)


@app.post("/api/connect")
def api_connect(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    dual_request = bool(e.get("dual")) or any(k in e for k in DUAL_PORT_KEYS)
    if dual_request:
        result = _connect_dual(e)
        api_devices_resume()
        return result
    if dual_enabled(load_config()):
        raise ApiError("当前是双臂模式，请到「设备连接与校准」页面完成四路串口连接；"
                       "若要回到单臂，请在该页关闭双臂模式", 400)
    lp = str(e.get("leader_port") or "").strip()
    fp = str(e.get("follower_port") or "").strip()
    if not lp or not fp:
        raise ApiError("必须分别选择主臂和从臂的串口", 400)
    if lp == fp:
        raise ApiError("主臂和从臂不能使用同一个串口", 400)
    _check_ports_exist({"leader": lp, "follower": fp})
    save_config({"leader": {"port": lp}, "follower": {"port": fp}})
    ENV.leader_port, ENV.follower_port, ENV.connected = lp, fp, True
    ENV.dual_connected = False
    api_devices_resume()
    HUB.log(f"[connect] 主臂 幻尔 Star Arm 102-LD -> {lp}", "ok")
    HUB.log(f"[connect] 从臂 Seeed B601-DM -> {fp}（CAN: {CAN_ADAPTER}）", "ok")
    HUB.state(connected=True, leader_port=lp, follower_port=fp, dual_connected=False)
    return ok(connected=True, leader_port=lp, follower_port=fp)


@app.post("/api/disconnect")
def api_disconnect():
    ENV.connected = False
    ENV.dual_connected = False
    HUB.state(connected=False, dual_connected=False)
    HUB.log("[connect] 已断开机械臂连接（单臂 / 双臂标记均已清除）", "warn")
    return ok(connected=False)


# ---- 双臂协作 ----
DUAL_PREVIEW_KINDS = ("teleoperate", "record", "eval", "replay", "robot-client",
                      "train", "calibrate-left-leader", "calibrate-left-follower",
                      "calibrate-right-leader", "calibrate-right-follower")


def dual_types_probe() -> dict:
    """在所选 Python 环境里真实执行探测脚本，返回该环境注册过的类型。"""
    script = ROOT / "backend" / "dual_probe.py"
    if not script.is_file():
        raise ApiError(f"探测脚本缺失：{script}", 500)
    cmd = _py() + [str(script)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding=SUBPROC_KW_ENCODING, errors="replace",
                           timeout=40, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        raise ApiError("探测 LeRobot 类型超时（40s），请确认所选环境可正常启动 Python", 504)
    except OSError as exc:
        raise ApiError(f"无法启动探测脚本：{_brief(exc)}", 500)
    line = next((x for x in (r.stdout or "").splitlines()
                 if x.strip().startswith("{")), "")
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        data = {"error": (r.stderr or r.stdout or "探测脚本没有输出 JSON").strip()[:400],
                "robots": [], "teleops": [], "dual_robots": [], "dual_teleops": []}
    data["python"] = _bin("python")
    data["command"] = display_command(cmd)
    data["returncode"] = r.returncode
    return data


def _dual_dataset_features(repo_id: str = "", dataset_dir: str = "") -> dict:
    """真实读取数据集 meta/info.json，报告 state / action 维度与双臂特征名。"""
    if dataset_dir.strip():
        root = Path(dataset_dir.strip()).expanduser()
    else:
        root = _safe_dataset_path(repo_id)
        if root is None:
            raise ApiError("数据集 repo_id 不合法", 400)
    info = root / "meta" / "info.json"
    if not info.is_file():
        return {"found": False, "root": str(root),
                "note": "没有找到 meta/info.json；请确认已完成采集，或填写数据集目录"}
    try:
        j = json.loads(info.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"found": False, "root": str(root), "note": f"info.json 读取失败：{_brief(exc)}"}
    feats = j.get("features") or {}
    rows = []
    for k, f in feats.items():
        rows.append({"key": k, "shape": list((f or {}).get("shape") or []),
                     "dtype": str((f or {}).get("dtype") or "")})
    state = list((feats.get("observation.state") or {}).get("shape") or [])
    action = list((feats.get("action") or {}).get("shape") or [])
    state_dim = state[0] if state else 0
    action_dim = action[0] if action else 0
    cameras = [k.split("images.", 1)[1] for k in feats
               if k.startswith("observation.images.")]
    arm_keys = [k for k in feats
                if any(h in k.lower() for h in ("left", "right", "_arm", "arm_"))]
    if not action_dim:
        hint = "info.json 里没有 action 特征，训练前请确认数据集完整"
    elif arm_keys and action_dim >= 12:
        hint = "包含左右臂特征名且动作维度 >= 12，符合双臂数据集特征（依据 meta 推断，非硬件确认）"
    elif action_dim >= 12:
        hint = "动作维度 >= 12，可能是两臂拼接，但没有左右臂特征名；请人工核对采集时的机器人配置"
    else:
        hint = "动作维度较小，可能是单臂数据集；用单臂数据训练双臂策略会维度不匹配"
    return {"found": True, "root": str(root), "repo_id": str(repo_id or root.name),
            "total_episodes": j.get("total_episodes"), "total_frames": j.get("total_frames"),
            "fps": j.get("fps"), "features": rows, "state_dim": state_dim,
            "action_dim": action_dim, "cameras": cameras, "arm_keys": arm_keys,
            "dual_likely": bool(arm_keys and action_dim >= 12), "note": hint}


@app.get("/api/dual/types")
def api_dual_types():
    return dual_types_probe()


@app.get("/api/commands/preview")
@app.get("/api/dual/preview")
def api_dual_preview(kind: str = "record"):
    if kind not in DUAL_PREVIEW_KINDS:
        raise ApiError(f"不支持预览的操作：{kind}", 400)
    cfg = load_config()
    try:
        cmd = build_command(kind, cfg)
        shown = display_command(cmd)
        error = ""
    except ApiError as exc:
        shown, error = "", exc.detail
    return {"kind": kind, "enabled": dual_enabled(cfg), "command": shown,
            "error": error, "dual_arm": _dual_status(cfg)}


@app.get("/api/datasets/features")
@app.get("/api/dual/features")
def api_dual_features(repo_id: str = "", dataset_dir: str = ""):
    if not repo_id.strip() and not dataset_dir.strip():
        raise ApiError("请提供数据集 repo_id 或数据集目录", 400)
    return _dual_dataset_features(repo_id, dataset_dir)


@app.post("/api/dual/mode")
def api_dual_mode(payload: Optional[dict] = Body(default=None)):
    enabled = (payload or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError("enabled 必须是布尔值", 400)
    with STATE.lock:
        if dual_enabled(load_config()) == enabled:
            return ok(enabled=enabled, dual_arm=_dual_status(load_config()))
        if STATE.running():
            raise ApiError("当前有任务运行，请先停止任务再切换机器人模式", 409)
        sorting=globals().get('SORTING')
        if sorting and sorting.control and sorting.control.alive():
            raise ApiError('请先收工归零并失能，再切换模式',409)
        cfg = save_config({"dual_arm": {"enabled": enabled}})
    # 硬件组合变了，旧的连接标记必须失效，不能沿用单臂连接去跑双臂命令
    ENV.connected = False
    ENV.dual_connected = False
    HUB.state(connected=False, dual_connected=False)
    HUB.log(f"[dual] 已切换到{'双臂' if enabled else '单臂'}模式，连接状态已重置，"
            "请重新连接对应串口", "warn")
    return ok(enabled=enabled, dual_arm=_dual_status(cfg))


@app.post("/api/dual/calibrate")
def api_dual_calibrate(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    side = str(e.get("side") or "").strip()
    arm = str(e.get("arm") or "").strip()
    if side not in DUAL_SIDES or arm not in ("leader", "follower"):
        raise ApiError("side 只能是 left / right，arm 只能是 leader / follower", 400)
    if not dual_enabled(load_config()):
        raise ApiError("请先开启双臂模式", 400)
    label = ("左" if side == "left" else "右") + ("主臂" if arm == "leader" else "从臂")
    return start_job(f"calibrate-{side}-{arm}", f"校准{label}", load_config())


@app.get("/api/collection/plan")
@app.get("/api/dual/plan")
def api_dual_plan_get(coordination: str = ""):
    cfg = load_config()
    key = str(coordination or (cfg["dual_arm"].get("coordination") or "sync")).strip()
    tpl = COORDINATION_TASKS.get(key)
    if not tpl:
        raise ApiError(f"未知协作模式：{key}；可选 {', '.join(COORDINATION_TASKS)}", 400)
    phases = tpl["phases"]
    return {"coordination": key, "label": tpl["label"], "task": tpl["task"],
            "desc": tpl["desc"], "phases": phases,
            "total_seconds": sum(int(p["seconds"]) for p in phases),
            "templates": [{"key": k, "label": v["label"], "desc": v["desc"],
                           "task": v["task"], "total_seconds": sum(
                               int(p["seconds"]) for p in v["phases"])}
                          for k, v in COORDINATION_TASKS.items()]}


@app.post("/api/collection/plan")
@app.post("/api/dual/plan")
def api_dual_plan_save(payload: Optional[dict] = Body(default=None)):
    """把协作编排落盘为真实 JSON 文件，供采集时按阶段执行/复盘；不产生模拟数据。"""
    e = payload or {}
    key = str(e.get("coordination") or "sync").strip()
    tpl = COORDINATION_TASKS.get(key)
    if not tpl:
        raise ApiError(f"未知协作模式：{key}", 400)
    cfg = save_config({"dual_arm": {"coordination": key}})
    if bool(e.get("apply_task")):
        cfg = save_config({"dataset": {"single_task": tpl["task"]}})
    plan = {"coordination": key, "label": tpl["label"], "task": tpl["task"],
            "desc": tpl["desc"], "phases": tpl["phases"],
            "total_seconds": sum(int(p["seconds"]) for p in tpl["phases"]),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "双臂同步（单个复合机器人控制循环）",
            "note": "阶段时长用于操作节奏与录制计时，不代替真机控制指令；"
                    "两臂实际动作由遥操作设备与策略产生。"}
    out_dir = OUTPUTS_DIR / "dual_plans"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"plan_{time.strftime('%Y%m%d_%H%M%S')}_{key}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
    except OSError as exc:
        raise ApiError(f"协作计划写入失败：{_brief(exc)}", 500)
    HUB.log(f"[dual] 协作编排已保存：{path}（{tpl['label']}，共 {len(tpl['phases'])} 阶段）", "ok")
    return ok(plan=plan, path=str(path), dataset_task=cfg["dataset"]["single_task"])


# ---- 机械臂 ----
@app.post("/api/calibrate/leader")
def api_calibrate_leader():
    return start_job("calibrate-leader", "校准主臂（幻尔）", load_config())


@app.post("/api/calibrate/follower")
def api_calibrate_follower():
    return start_job("calibrate-follower", "校准从臂（B601-DM）", load_config())


@app.post("/api/teleop/start")
def api_teleop_start():
    return start_job("teleoperate", "遥操作", load_config())


# ---- 摄像头（真实 MJPEG 画面） ----
@app.post('/api/cameras/scan')
def api_cameras_scan(payload: Optional[dict]=Body(default=None)):
    result=VISION.discover()
    HUB.log(f"[camera] 统一视觉进程发现 {len(result['devices'])} 路视频设备；深度模块 {'已连接' if DEPTH.status()['available'] else '未连接'}",'ok')
    return ok(**result,note='设备扫描完成，现有画面继续运行')


@app.get("/api/cameras/devices")
def api_camera_devices():
    return VISION.discover()


@app.get("/api/cameras/status")
def api_camera_status():
    return CAMERAS.status()


@app.get('/api/monitor/layout')
def api_monitor_layout():
    settings=load_config().get('monitor_layout') or {}
    try:geometry=resolve_monitor_geometry(settings)
    except ValueError as exc:raise ApiError(str(exc),400)
    return {'settings':settings,'geometry':geometry}


@app.get('/api/monitor/status')
def api_monitor_status():
    TELEMETRY.configure(monitor_ports(load_config()))
    return {**TELEMETRY.snapshot(), 'released':DEVICES_RELEASED,
            'cameras':CAMERAS.status(), 'depth':DEPTH.status()}


@app.websocket('/ws/monitor')
async def ws_monitor(ws: WebSocket):
    await ws.accept(); TELEMETRY.configure(monitor_ports(load_config())); queue = TELEMETRY.subscribe()
    try:
        await ws.send_json({**TELEMETRY.snapshot(),'released':DEVICES_RELEASED})
        last_health = 0
        while True:
            try: packet = await asyncio.wait_for(queue.get(), timeout=2)
            except asyncio.TimeoutError: packet = None
            if packet is not None:await ws.send_json(packet)
            if time.monotonic()-last_health >= 2:
                last_health=time.monotonic()
                await ws.send_json(await asyncio.to_thread(lambda:{'type':'health','time':time.time(),'depth':DEPTH.status(),'cameras':CAMERAS.status(),'released':DEVICES_RELEASED,'bindings':TELEMETRY.snapshot()['bindings'],'binding_generation':TELEMETRY.snapshot()['binding_generation']}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        TELEMETRY.subscribers.discard(queue)


@app.post('/api/devices/disconnect-all')
def api_disconnect_all():
    global DEVICES_RELEASED, _ARM_PROC
    DEVICES_RELEASED = True; TELEMETRY.paused = True; VISION.paused = True
    save_config({'device_session':{'paused':True}})
    TELEMETRY.publish({'type':'device-session','paused':True})
    targets = device_paths(load_config())
    initial_report = kill_device_owners(targets,timeout=.1)
    stop_all(force=True); TELEMETRY.stop_observers()
    sorting=globals().get('SORTING')
    if sorting and sorting.control:
        sorting.control.close();sorting.control=None;sorting.session=None
    # Do not use the old /arm/close command: it homes the arm before disconnecting.
    daemon = _ARM_PROC
    if daemon is not None and daemon.poll() is None:
        try: daemon.kill()
        except ProcessLookupError: pass
    with _ARM_LOCK:
        _ARM_PROC = None
    VISION.stop()
    DEPTH.close()
    try: CAMERAS.close_all()
    except CameraError as exc: HUB.log(str(exc),'warn')
    report = kill_device_owners(targets)
    report['killed'] = list({p['pid']:p for p in initial_report['killed']+report['killed']}.values())
    ENV.connected = ENV.dual_connected = False; ENV.dual_ports = {}
    HUB.state(connected=False,dual_connected=False)
    released = not report['remaining']
    HUB.log(f"[disconnect] 终止 {len(report['killed'])} 个占用进程；剩余 {len(report['remaining'])} 个，自动重连已暂停", 'ok' if released else 'warn')
    return ok(released=released, **report, note='全部设备已释放，自动重连已暂停' if released else '部分设备仍被占用，请查看释放结果')


def _home_targets(cfg: dict) -> list[dict]:
    if dual_enabled(cfg):
        entries = [(side, dual_side(cfg, side)) for side in DUAL_SIDES]
    else:
        follower = cfg.get('follower') or {}
        entries = [('left', {'follower_port':follower.get('port'),
                    'can_adapter':follower.get('can_adapter', CAN_ADAPTER),
                    'follower_type':FOLLOWER_TYPE,
                    'gripper_torque_ratio':follower.get('gripper_torque_ratio', .12)})]
    targets=[]; seen=set()
    for side, arm in entries:
        port=str(arm.get('follower_port') or '').strip()
        if not port:
            raise ApiError(f'{side} 从臂未配置串口，请先在设备连接页选择',400)
        identity=os.path.realpath(port)
        if identity in seen:
            raise ApiError('左右从臂不能使用同一串口或其别名',400)
        seen.add(identity)
        if arm.get('follower_type') != FOLLOWER_TYPE or arm.get('can_adapter') != 'damiao':
            raise ApiError('收工归零目前仅支持 B601-DM 的 damiao 串口桥，未发送任何动作',400)
        try:
            ratio=float(arm.get('gripper_torque_ratio', .12))
        except (TypeError, ValueError):
            raise ApiError('夹爪力矩比例无效',400)
        if not math.isfinite(ratio) or not .01 <= ratio <= 1:
            raise ApiError('夹爪力矩比例必须在 0.01～1 之间',400)
        targets.append({'side':side,'port':port,'gripper_torque_ratio':ratio})
    return targets


@app.post('/api/devices/home-and-disable')
def api_home_and_disable(payload: Optional[dict] = Body(default=None)):
    """Explicit homing job, using configured followers and preserving calibration."""
    global _ARM_PROC
    if (payload or {}).get('confirm') is not True:
        raise ApiError('收工归零需要二次确认',400)
    with STATE.lock:
        if STATE.running():
            raise ApiError('请先结束当前动作，再执行收工归零并失能',409)
        if DEVICES_RELEASED:
            raise ApiError('设备已断开，请先恢复设备或重新连接，再执行收工归零',409)
        require_env()
        cfg=load_config(); targets=_home_targets(cfg)
        if (payload or {}).get('expected_ports') is not None and payload['expected_ports'] != [a['port'] for a in targets]:
            raise ApiError('从臂配置已变化，请重新确认收工目标',409)
        TELEMETRY.stop_observers()
        if _ARM_PROC is not None and _ARM_PROC.poll() is None:
            # Retire only our legacy manual worker; no quit()/automatic homing.
            _ARM_PROC.terminate()
            try:_ARM_PROC.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise ApiError('从臂控制进程尚未释放串口，请先断开全部设备',409)
            _ARM_PROC=None
        sorting=globals().get('SORTING')
        if sorting and sorting.control:
            sorting.control.close();sorting.control=None;sorting.session=None
        from backend.device_release import owners
        if owners([target['port'] for target in targets]):
            raise ApiError('从臂串口仍被其他进程占用，请先结束占用进程，未开始归零',409)
        result=_spawn('home-and-disable','收工归零并失能',
                      _py()+[str(BACKEND_DIR/'shutdown_worker.py'), json.dumps({'arms':targets})],
                      env_extra=TELEMETRY.environment(monitor_ports(cfg)))
        # After transport release the next operation must explicitly reconnect.
        ENV.connected=ENV.dual_connected=False; ENV.dual_ports={}
        HUB.state(connected=False,dual_connected=False)
        return result


_ZERO_JOBS: dict[str, Job] = {}


@app.get('/api/devices/zero/capabilities')
def api_zero_capabilities():
    """Describe the loaded zero API without opening any hardware transport."""
    return {'ok':True,'api_version':1,'version':APP_VERSION,'build':APP_BUILD,
            'actions':['read','release','set-zero'],
            'worker_available':(BACKEND_DIR/'zero_adjust_worker.py').is_file()}


@app.post('/api/devices/zero/start')
def api_zero_start(payload: Optional[dict] = Body(default=None)):
    e=payload or {};action=e.get('action');side=e.get('side','left');joints=e.get('joints')
    if action not in ('read','release','set-zero') or side not in DUAL_SIDES:
        raise ApiError('无效的零点调整操作或从臂',400)
    if not isinstance(joints,list) or not joints or any(isinstance(i,bool) or not isinstance(i,int) or i not in range(1,8) for i in joints):
        raise ApiError('请选择 CAN ID 1～7 的关节',400)
    if action != 'read' and e.get('confirm') is not True:
        raise ApiError('释放力矩和设零需要二次确认',400)
    with STATE.lock:
        if STATE.running():raise ApiError('请先结束当前动作，再调整零点',409)
        if DEVICES_RELEASED:raise ApiError('设备已断开，请先恢复设备或重新连接',409)
        require_env();cfg=load_config()
        if dual_enabled(cfg):
            arm=dual_side(cfg,side)
            if arm['follower_type'] != FOLLOWER_TYPE:raise ApiError('仅支持 B601-DM 从臂零点调整',400)
            follower={'port':arm['follower_port'],'id':arm['follower_id'],'can_adapter':arm['can_adapter'],'gripper_torque_ratio':arm['gripper_torque_ratio']}
        else:
            if side!='left':raise ApiError('单臂模式只有当前从臂',400)
            follower=cfg.get('follower') or {}
        target=_home_targets({'follower':follower,'dual_arm':{'enabled':False}})[0]
        if e.get('expected_port') is not None and e['expected_port'] != target['port']:
            raise ApiError('目标串口已变化，请重新确认后操作',409)
        target.update(side=side,id=follower.get('id') or 'follower',action=action,joints=sorted(set(joints)))
        TELEMETRY.stop_observers()
        from backend.device_release import owners
        if owners([target['port']]):raise ApiError('所选从臂串口被占用，未发送零点操作',409)
        label={'read':'读取实测零点','release':'释放从臂力矩','set-zero':'写入并保存零点'}[action]
        result=_spawn('zero-adjust',label,_py()+[str(BACKEND_DIR/'zero_adjust_worker.py'),json.dumps(target)],
                      env_extra=TELEMETRY.environment(monitor_ports(cfg)))
        _ZERO_JOBS[STATE.job.job_id]=STATE.job
        while len(_ZERO_JOBS)>20:_ZERO_JOBS.pop(next(iter(_ZERO_JOBS)))
        if action!='read':
            ENV.connected=ENV.dual_connected=False;ENV.dual_ports={}
            HUB.state(connected=False,dual_connected=False)
        return result


@app.get('/api/devices/zero/result')
def api_zero_result(job_id: str):
    with STATE.lock:
        job=_ZERO_JOBS.get(job_id)
        if not job:raise ApiError('零点任务结果已过期，请重新读取',404)
        return {'ok':True,'job_id':job_id,'finished':job.finished.is_set(),'exit_code':job.exit_code,
                'result':job.result,'last_line':job.last_line}


@app.post('/api/devices/resume')
def api_devices_resume():
    global DEVICES_RELEASED
    DEVICES_RELEASED = False; TELEMETRY.paused = False; VISION.paused = False
    save_config({'device_session':{'paused':False}})
    TELEMETRY.publish({'type':'device-session','paused':False})
    return ok(released=False)


@app.get('/api/depth/status')
def api_depth_status():
    return DEPTH.status()


def depth_lease():
    if DEVICES_RELEASED: raise ApiError('设备已断开，请先恢复监视',409)
    try:return DEPTH.acquire()
    except CameraError as exc:raise ApiError(str(exc),503)


@app.get('/api/depth/snapshot')
def api_depth_snapshot(raw: bool=False):
    lease=depth_lease()
    try:
        meta,jpeg,png=lease.read()
        return Response(png if raw else jpeg,media_type='image/png' if raw else 'image/jpeg',
            headers={'Cache-Control':'no-store','X-Depth-Unit':'mm','X-Depth-Sequence':str(meta['sequence'])})
    finally: lease.release()


@app.get('/api/depth/point')
def api_depth_point(x:int,y:int):
    import cv2,numpy as np
    lease=depth_lease()
    try:
        meta,_,png=lease.read();depth=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_UNCHANGED)
        if not (0<=x<depth.shape[1] and 0<=y<depth.shape[0]):raise ApiError('像素超出深度图范围',400)
        value=int(depth[y,x]);return {'x':x,'y':y,'depth_mm':value if value else None,'valid':bool(value),'aligned_to_rgb':False,'sequence':meta['sequence']}
    finally:lease.release()


@app.get('/api/depth/stream')
async def api_depth_stream():
    lease=depth_lease()
    try: first=await asyncio.to_thread(lease.read)
    except CameraError as exc:lease.release();raise ApiError(str(exc),503)
    except BaseException:lease.release();raise
    async def frames():
        try:
            meta,jpeg,_=first
            while True:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpeg+b'\r\n'
                meta,jpeg,_=await asyncio.to_thread(lease.read,True)
        except CameraError:pass
        finally:lease.release()
    return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=frame',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})


@app.get("/api/camera/raw")
async def api_camera_raw(device: str = '0'):
    cap = camera_probe(device)
    try:
        first = await asyncio.to_thread(cap.read)
    except CameraError as exc:
        cap.release(); raise ApiError(str(exc),503)
    except BaseException:
        cap.release()
        raise
    async def packets():
        try:
            valid, frame = first
            while valid:
                yield frame_packet(cap.seq, cap.captured_at, frame)
                valid, frame = await asyncio.to_thread(cap.read)
        except CameraError as exc:
            if cap.session.error:
                HUB.log(f"[camera] {exc}", "warn")
        finally:
            cap.release()
    return StreamingResponse(packets(), media_type='application/x-rebot-frames',
        headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})


@app.get("/api/camera/snapshot")
def api_camera_snapshot(index: int = 0, device: Optional[str] = None):
    cap = camera_probe(device if device is not None else index)
    try:
        return Response(cap.jpeg(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except CameraError as exc:
        raise ApiError(str(exc), 503)
    finally:
        cap.release()




ORBBEC_PY = "/home/legion/miniconda3/envs/rebotarm/bin/python"
ORBBEC_RULES = Path("/home/legion/rebot_grasp-jetson/sdk/pyorbbecsdk/scripts/env_setup/99-obsensor-libusb.rules")


@app.get('/api/camera/orbbec/snapshot')
def api_orbbec_snapshot():
    cameras=load_config().get('cameras',[])
    camera=next((c for c in cameras if re.search(r'orbbec|astra|深度|depth',c.get('name',''),re.I)),None)
    if not camera:raise ApiError('请在摄像头页面配置深度相机的 RGB 视频设备',400)
    return api_camera_snapshot(device=str(camera['index_or_path']))


@app.post("/api/camera/orbbec/udev")
def api_orbbec_udev(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    pwd = str(e.get("password") or "")
    if not pwd:
        raise ApiError("需要 sudo 密码（或密码不正确）", 400)
    if not ORBBEC_RULES.is_file():
        raise ApiError(f"找不到 Orbbec udev 规则文件：{ORBBEC_RULES}", 404)
    rules = str(ORBBEC_RULES)
    cmd = (
        "set -e; "
        f"echo \"$PW\" | sudo -S -p '' cp '{rules}' /etc/udev/rules.d/99-obsensor-libusb.rules; "
        "echo \"$PW\" | sudo -S -p '' udevadm control --reload-rules; "
        "echo \"$PW\" | sudo -S -p '' udevadm trigger"
    )
    env = os.environ.copy()
    env["PW"] = pwd
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=30, env=env)
    except subprocess.TimeoutExpired:
        raise ApiError("安装 Orbbec udev 规则超时", 504)
    env.pop("PW", None)
    if r.returncode != 0:
        raise ApiError("安装 udev 规则失败（密码错误或无权限）：" +
                       ((r.stderr or r.stdout or "")[-300:]), 403)
    return ok(note="udev 规则已安装，请重新插拔 Orbbec 相机后再预览")


@app.get("/api/camera/stream")
async def api_camera_stream(index: int = 0, device: Optional[str] = None):
    cap = camera_probe(device if device is not None else index)
    try:
        first = await asyncio.to_thread(cap.jpeg)
    except BaseException as exc:
        cap.release()
        if isinstance(exc, CameraError):
            raise ApiError(str(exc), 503)
        raise

    async def gen():
        try:
            jpeg = first
            while True:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                jpeg = await asyncio.to_thread(cap.jpeg, True)
        except CameraError as exc:
            if cap.session.error or not cap.session.stop.is_set():
                HUB.log(f"[camera] {exc}", "warn")
        finally:
            cap.release()

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


# ---- 数据集 ----
@app.get("/api/datasets")
def api_datasets():
    return {"datasets": list_datasets(), "root": str(LEROBOT_HOME)}


@app.post("/api/record/start")
def api_record_start():
    cfg = load_config()
    # 开始前按所选分辨率同时打开全部相机试读一帧，带宽不足时直接给出明确原因，而不是等任务超时
    leases, problems = [], []
    try:
        for cam in cfg.get("cameras") or []:
            try:
                leases.append((cam, camera_probe(cam.get("index_or_path", 0))))
            except ApiError as exc:
                problems.append(f"{cam.get('name')}（{cam.get('width')}×{cam.get('height')}）：{exc.detail}")
        for cam, lease in leases:
            try:
                lease.read()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{cam.get('name')}（{cam.get('width')}×{cam.get('height')}@{cam.get('fps')}）：{_brief(exc)}")
    finally:
        for _, lease in leases:
            lease.release()
    if problems:
        raise ApiError("按当前相机分辨率无法同时取流，已取消采集：\n" + "\n".join(problems) +
                       "\n请在摄像头页降低分辨率/帧率，或把相机分散到不同 USB 接口后重试", 409)
    return start_job("record", "数据采集", cfg)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def viz_web_status() -> dict:
    proc = VIZ_WEB.get("proc")
    running = bool(proc is not None and proc.poll() is None)
    return {"running": running, "url": f"http://127.0.0.1:{RERUN_WEB_PORT}",
            "web_port": RERUN_WEB_PORT, "grpc_port": RERUN_GRPC_PORT}


@app.get("/api/viz/web")
def api_viz_web():
    return viz_web_status()


@app.post("/api/viz/web/start")
def api_viz_web_start():
    """启动常驻的 Rerun web 查看器：浏览器里看相机，录制进程 connect_grpc 到它。"""
    st = viz_web_status()
    if st["running"]:
        return ok(**st)
    busy = [p for p in (RERUN_GRPC_PORT, RERUN_WEB_PORT) if _port_in_use(p)]
    if busy:
        raise ApiError(
            "端口 " + "/".join(str(p) for p in busy) + " 已被占用（通常是上次残留的 Rerun 窗口）。"
            "请先关闭残留的 Rerun 查看器，或重启网页服务后再试", 409)
    try:
        popen_kw: dict[str, Any] = dict(
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT))
        if IS_WINDOWS:
            popen_kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            popen_kw["start_new_session"] = True
        proc = subprocess.Popen(
            [_bin("rerun"), "--serve-web", "--bind", "127.0.0.1",
             "--port", str(RERUN_GRPC_PORT), "--web-viewer-port", str(RERUN_WEB_PORT),
             "--hide-welcome-screen"], **popen_kw)
    except OSError as exc:
        raise ApiError(f"启动 Rerun 网页查看器失败：{_brief(exc)}", 500)
    VIZ_WEB["proc"] = proc
    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            VIZ_WEB["proc"] = None
            raise ApiError("Rerun 网页查看器启动失败（进程已退出），请检查 rerun 是否安装", 500)
        if _port_in_use(RERUN_WEB_PORT):
            HUB.log(f"[viz] 网页可视化已启动：{st['url']}（录制会自动连接它）", "ok")
            return ok(**viz_web_status())
        time.sleep(0.2)
    HUB.log("[viz] 网页可视化端口尚未就绪，可能仍在启动", "warn")
    return ok(**viz_web_status())


@app.post("/api/viz/web/stop")
def api_viz_web_stop():
    proc = VIZ_WEB.get("proc")
    if proc is not None and proc.poll() is None:
        _terminate(proc)
    VIZ_WEB["proc"] = None
    HUB.log("[viz] 网页可视化已关闭", "warn")
    return ok(**viz_web_status())


@app.post("/api/record/key")
def api_record_key(payload: Optional[dict] = Body(default=None)):
    key = str((payload or {}).get("key") or "right").lower()
    desc = {"right": "提前终止当前剧集，进入下一集", "left": "取消当前剧集并重新录制",
            "esc": "保存并编码当前剧集后结束会话"}
    mapping = {"right": "Right", "left": "Left", "esc": "Escape"}
    if key not in mapping:
        raise ApiError("按键只能是 right / left / esc", 400)
    with STATE.lock:
        job = STATE.job
        if job is None or job.finished.is_set():
            raise ApiError("当前没有正在运行的录制任务，按键未发送", 409)
        if job.kind != "record":
            raise ApiError(f"当前任务不是录制（{job.name}），按键未发送", 409)
    # 绝不能在发送按键前调 stop_all()：SIGTERM 会让 lerobot-record 以 -15 退出，
    # 当前剧集来不及保存/编码。ESC 只负责把按键送进录制进程，由它自己收尾。
    if shutil.which("xdotool") is None:
        raise ApiError(
            "未安装 xdotool，无法把按键送进录制进程。请在运行 start.py 的终端直接按 "
            f"{key.upper()}（{desc[key]}），或安装 xdotool 后重试：sudo apt install -y xdotool。"
            "注意：用「急停」会直接 SIGTERM，可能来不及保存当前剧集。", 409)
    try:
        r = subprocess.run(["xdotool", "key", mapping[key]], capture_output=True,
                           text=True, timeout=5,
                           encoding=SUBPROC_KW_ENCODING, errors="replace")
    except subprocess.TimeoutExpired:
        raise ApiError("发送按键超时（5 秒）", 504)
    except OSError as exc:
        raise ApiError(f"发送按键失败：{_brief(exc)}", 500)
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or f"xdotool 退出码 {r.returncode}"
        raise ApiError(f"xdotool 发送失败：{detail}。请在运行 start.py 的终端直接按键，"
                       "或确认网页服务进程有 DISPLAY 权限", 500)
    HUB.log(f"已发送按键 {key.upper()}：{desc[key]}", "ok")
    return ok(key=key)


def _dataset_view_root(repo_id: str) -> Path:
    raw = str(repo_id or "").strip().strip("/")
    if not raw or ".." in raw.split("/"):
        raise ApiError("数据集名称不合法", 400)
    root = (LEROBOT_HOME / raw).resolve()
    if LEROBOT_HOME.resolve() not in root.parents and root != LEROBOT_HOME.resolve():
        raise ApiError("数据集名称不合法", 400)
    if not (root / "meta" / "info.json").is_file():
        raise ApiError(f"数据集不存在：{raw}", 404)
    return root


@app.get("/api/dataset/view/episodes")
def api_dataset_view_episodes(repo_id: str):
    root = _dataset_view_root(repo_id)
    info = json.loads((root / "meta" / "info.json").read_text("utf-8"))
    import pandas as pd  # 延迟导入，避免拖慢启动

    meta_files = sorted((root / "meta" / "episodes").glob("chunk-*/*.parquet"))
    if not meta_files:
        raise ApiError("数据集缺少 episodes 元数据（不是 LeRobot v3 格式？）", 400)
    ep = pd.concat([pd.read_parquet(f) for f in meta_files])
    cameras = [k for k in (info.get("features") or {}) if k.startswith("observation.images.")]
    episodes = []
    for _, row in ep.iterrows():
        tasks = row.get("tasks")
        if hasattr(tasks, "tolist"):
            tasks = [str(x) for x in tasks.tolist()]
        elif isinstance(tasks, (list, tuple)):
            tasks = [str(x) for x in tasks]
        else:
            tasks = [str(tasks)]
        item = {"episode_index": int(row["episode_index"]),
                "length": int(row["length"]), "tasks": tasks, "cameras": {}}
        for cam in cameras:
            pre = f"videos/{cam}/"
            try:
                chunk=float(row.get(pre+'chunk_index',0));file=float(row.get(pre+'file_index',0))
                start=float(row.get(pre+'from_timestamp',float('nan')));end=float(row.get(pre+'to_timestamp',float('nan')))
                if not all(math.isfinite(x) for x in (chunk,file,start,end)) or min(chunk,file,start)<0 or end<=start:
                    raise ValueError('missing camera segment')
                item['cameras'][cam]={'chunk':int(chunk),'file':int(file),'from':start,'to':end,'available':True}
            except (ValueError,TypeError):
                item['cameras'][cam]={'available':False,'error':'本集缺少这一路相机的视频片段'}
        episodes.append(item)
    return {"ok": True, "repo_id": repo_id, "fps": float(info.get("fps") or 30),
            "cameras": cameras, "episodes": episodes}


@app.get("/api/dataset/view/episode")
def api_dataset_view_episode(repo_id: str, episode: int):
    root = _dataset_view_root(repo_id)
    info = json.loads((root / "meta" / "info.json").read_text("utf-8"))
    import pandas as pd

    parts=[]
    for f in sorted((root / "data").glob("chunk-*/*.parquet")):
        d = pd.read_parquet(f)
        if "episode_index" in d.columns and (d["episode_index"] == episode).any():
            parts.append(d[d["episode_index"] == episode])
    if not parts:
        raise ApiError(f"剧集 {episode} 没有数据", 404)
    df=pd.concat(parts,ignore_index=True).sort_values('timestamp').reset_index(drop=True)
    step = max(1, len(df) // 400)
    sub = df.iloc[::step]
    state = [[float(v) for v in x] for x in sub["observation.state"]]
    action = [[float(v) for v in x] for x in sub["action"]]
    ts = [float(x) for x in sub["timestamp"]]
    return {"ok": True, "repo_id": repo_id, "episode": episode,
            "fps": float(info.get("fps") or 30), "frames": int(len(df)),
            "step": int(step), "timestamps": ts, "state": state, "action": action,
            "features": {k: v for k, v in (info.get("features") or {}).items()
                         if k in ("observation.state", "action")}}


_VIEW_CACHE = ROOT / "outputs" / ".viewer_cache"
_VIEW_LOCK = threading.Lock()


def _dataset_video_path(root: Path, camera: str, chunk: int, file: int) -> Path:
    features=json.loads((root/'meta/info.json').read_text()).get('features') or {}
    if camera not in features or not camera.startswith('observation.images.') or min(chunk,file)<0:
        raise ApiError('相机不属于所选数据集或片段编号无效',400)
    video_root=(root/'videos').resolve()
    source=(video_root/camera/f'chunk-{chunk:03d}'/f'file-{file:03d}.mp4').resolve()
    if video_root not in source.parents:raise ApiError('相机路径不合法',400)
    if not source.is_file():raise ApiError('本路相机视频文件不存在',404)
    return source


@app.get("/api/dataset/view/clip")
def api_dataset_view_clip(repo_id: str, camera: str, chunk: int = 0, file: int = 0,
                          start: float = 0.0, end: float = 0.0, episode: int = 0):
    """把某集某相机转成 H.264 小片段（浏览器播放/拖动更稳），首次请求转码并缓存。"""
    root = _dataset_view_root(repo_id)
    src=_dataset_video_path(root,camera,chunk,file)
    if not all(math.isfinite(x) for x in (start,end)) or start<0 or end<=start:
        raise ApiError('相机视频时间范围无效',400)
    safe = str(repo_id).replace("/", "__")
    import hashlib
    stamp=src.stat()
    signature=hashlib.sha256(f'{src}:{stamp.st_mtime_ns}:{stamp.st_size}:{start}:{end}'.encode()).hexdigest()[:16]
    out = _VIEW_CACHE / safe / camera / f"ep{int(episode):04d}_c{chunk}_f{file}_{signature}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.is_file():
        dur = float(end) - float(start)
        with _VIEW_LOCK:
            if not out.is_file():
                temporary=out.with_name(out.stem+'.partial-'+os.urandom(4).hex()+'.mp4')
                cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, float(start)):.3f}",
                       "-i", str(src), "-t", f"{dur:.3f}",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(temporary)]
                try:
                    p = subprocess.run(cmd, capture_output=True, timeout=180)
                except subprocess.TimeoutExpired:
                    temporary.unlink(missing_ok=True)
                    raise ApiError("视频转码超时", 504)
                except OSError as exc:
                    temporary.unlink(missing_ok=True)
                    raise ApiError(f'无法启动视频转码，请确认已安装 ffmpeg：{exc}',503)
                if p.returncode != 0 or not temporary.is_file():
                    temporary.unlink(missing_ok=True)
                    raise ApiError("视频转码失败：" + (p.stderr or b"").decode(
                        "utf-8", "replace")[-300:], 500)
                os.replace(temporary,out)
    return FileResponse(out, media_type="video/mp4")


@app.get("/api/dataset/view/video")
def api_dataset_view_video(repo_id: str, camera: str, chunk: int = 0, file: int = 0):
    root = _dataset_view_root(repo_id)
    p=_dataset_video_path(root,camera,chunk,file)
    return FileResponse(p, media_type="video/mp4")


def _hf_cache_dir(model_id: str) -> Path:
    hub = os.environ.get("HF_HUB_CACHE") or str(Path.home() / ".cache" / "huggingface" / "hub")
    org, _, name = str(model_id).partition("/")
    return Path(hub) / f"models--{org}--{name.replace('/', '--')}"


def _hf_model_cached(model_id: str) -> tuple[bool, str]:
    mid = str(model_id or "").strip()
    if not mid:
        return False, ""
    if mid.startswith(("/", "./", "../")) or (ROOT / mid).exists():
        p = Path(mid) if mid.startswith("/") else ROOT / mid
        return p.exists(), str(p)
    cache = _hf_cache_dir(mid)
    if not cache.is_dir():
        return False, str(cache)
    for snap in cache.glob("snapshots/*"):
        if (snap / "config.json").is_file() and any(snap.glob("*.safetensors")):
            return True, str(snap)
    return False, str(cache)


def _hf_probe(url: str, timeout: float = 4.0) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rebot-web/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
        return True
    except Exception:
        return False


@app.get("/api/hf/status")
def api_hf_status(probe: bool = False):
    cfg = load_config().get("hf") or {}
    out = {"endpoint": str(cfg.get("endpoint") or ""), "auto": bool(cfg.get("auto", True))}
    if probe:
        out["official"] = _hf_probe(
            "https://huggingface.co/lerobot/smolvla_base/resolve/main/config.json")
        out["mirror"] = _hf_probe(
            "https://hf-mirror.com/lerobot/smolvla_base/resolve/main/config.json")
    return out


@app.get("/api/model/cache")
def api_model_cache(model_id: str):
    cached, path = _hf_model_cached(model_id)
    size_mb = 0.0
    try:
        p = Path(path)
        if p.is_dir():
            size_mb = round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1048576, 1)
    except OSError:
        pass
    return {"model_id": model_id, "cached": cached, "path": path, "size_mb": size_mb,
            "local": bool(str(path).startswith(str(ROOT)) or str(model_id).startswith("/"))}


@app.get("/api/dataset/root")
def api_dataset_root():
    return {"root": str(LEROBOT_HOME), "exists": LEROBOT_HOME.is_dir()}


@app.get("/api/dataset/browse")
def api_dataset_browse(path: str = ""):
    root = LEROBOT_HOME.resolve()
    base = Path(path).expanduser().resolve() if path else root
    if base != root and root not in base.parents:
        raise ApiError("只允许浏览平台数据集目录", 403)
    if not base.is_dir():
        raise ApiError("目录不存在", 404)
    entries = []
    for p in sorted(base.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        info = p / "meta" / "info.json"
        item = {"name": p.name, "path": str(p), "dataset": info.is_file()}
        if item["dataset"]:
            try:
                j = json.loads(info.read_text("utf-8"))
                item["episodes"] = j.get("total_episodes")
                item["fps"] = j.get("fps")
            except (OSError, json.JSONDecodeError):
                pass
        entries.append(item)
    return {"path": str(base), "root": str(root),
            "parent": str(base.parent) if base != root else None,
            "is_dataset": (base / "meta" / "info.json").is_file(),
            "entries": entries}


@app.get("/api/dataset/inspect")
def api_dataset_inspect(path: str):
    d = Path(path).expanduser().resolve()
    root = LEROBOT_HOME.resolve()
    if d != root and root not in d.parents:
        raise ApiError("只允许查看平台数据集目录", 403)
    info = d / "meta" / "info.json"
    if not info.is_file():
        raise ApiError("该目录不是有效数据集（缺少 meta/info.json）", 404)
    j = json.loads(info.read_text("utf-8"))
    cams = [k for k in (j.get("features") or {}) if k.startswith("observation.images.")]
    return {"ok": True, "path": str(d), "name": d.name,
            "episodes": j.get("total_episodes"), "fps": j.get("fps"),
            "frames": j.get("total_frames"), "cameras": cams}


@app.post("/api/dataset/delete-episode")
def api_dataset_delete_episode(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    repo = str(e.get("repo_id") or "").strip()
    ep = int(e.get("episode") or 0)
    if not e.get("confirm"):
        raise ApiError("删除剧集需要确认", 400)
    _dataset_view_root(repo)  # 校验数据集存在
    cmd = _lerobot("lerobot-edit-dataset") + [
        f"--repo_id={repo}",
        "--operation.type=delete_episodes",
        f"--operation.episode_indices=[{ep}]",
    ]
    return _spawn("dataset-edit", f"删除剧集 · {repo} #{ep}", cmd,
                  env_extra={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})


@app.post("/api/dataset/merge")
def api_dataset_merge(payload: Optional[dict] = Body(default=None)):
    """把多个结构相同的数据集合并成一个，用于一个训练任务。"""
    e = payload or {}
    ids = [str(x).strip() for x in (e.get("repo_ids") or []) if str(x).strip()]
    out = str(e.get("new_repo_id") or "").strip().strip("/")
    if len(ids) < 2:
        raise ApiError("至少选择两个数据集", 400)
    if not out:
        raise ApiError("请填写合并后的数据集名称", 400)
    if out in ids:
        raise ApiError("合并后的名称不能和输入的数据集重名，请换一个（如 merged_all）", 409)
    if (LEROBOT_HOME / out).exists():
        raise ApiError(f"已存在同名数据集：{out}，请换一个名字（如 {out}_all）", 409)
    for rid in ids:
        if not (LEROBOT_HOME / rid / "meta" / "info.json").is_file():
            raise ApiError(f"本地找不到数据集：{rid}", 404)
    require_env()
    cmd = _lerobot("lerobot-edit-dataset") + [
        f"--repo_id={out}",
        "--operation.type=merge",
        "--operation.repo_ids=" + json.dumps(ids, ensure_ascii=False),
    ]
    return _spawn("dataset-merge", f"合并数据集 · {out}", cmd,
                  env_extra={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})


@app.post("/api/dataset/viz")
def api_dataset_viz(payload: Optional[dict] = Body(default=None)):
    return start_job("dataset-viz", "数据集可视化", load_config(), payload)


@app.post("/api/dataset/replay")
def api_dataset_replay(payload: Optional[dict] = Body(default=None)):
    return start_job("replay", "数据集回放", load_config(), payload)


@app.get("/api/dataset/export")
def api_export_dataset(repo_id: str, mode: str = "all"):
    """一键导出已录制数据集（mode=all 导出完整数据集含数据+视频，mode=videos 仅导出视频 MP4）。"""
    import zipfile
    from starlette.background import BackgroundTask
    target = _safe_dataset_path(repo_id)
    if target is None or not target.is_dir():
        raise ApiError("数据集不存在或路径非法", 404)
    only_videos = (str(mode).lower() == "videos")
    src_root = (target / "videos") if only_videos else target
    if only_videos and not src_root.is_dir():
        raise ApiError("该数据集内没有已编码的视频目录（videos/）", 404)
    files = [f for f in sorted(src_root.rglob("*")) if f.is_file()]
    if not files:
        raise ApiError("没有可导出的文件", 404)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", repo_id.strip("/")) or "dataset"
    suffix = "_videos.zip" if only_videos else "_full.zip"
    fd, tmp_zip = tempfile.mkstemp(prefix=f"rebot_{safe_name}_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for f in files:
                rel = f.relative_to(target)
                zf.write(f, arcname=f"{safe_name}/{rel.as_posix()}")
    except Exception as exc:
        Path(tmp_zip).unlink(missing_ok=True)
        raise ApiError(f"打包导出失败：{_brief(exc)}", 500)
    HUB.log(f"[导出] 数据集 {repo_id} ({'仅视频' if only_videos else '完整数据+视频'}，共 {len(files)} 个文件)", "ok")
    return FileResponse(
        tmp_zip,
        media_type="application/zip",
        filename=f"{safe_name}{suffix}",
        background=BackgroundTask(lambda p=tmp_zip: Path(p).unlink(missing_ok=True)),
    )


@app.delete("/api/datasets/{repo_id:path}")
def api_delete_dataset(repo_id: str, confirm: bool = False):
    if not confirm:
        raise ApiError("删除数据集需要二次确认", 400)
    target = _safe_dataset_path(repo_id)
    if target is None:
        raise ApiError("非法的数据集路径", 400)
    if not target.exists():
        raise ApiError("数据集不存在", 404)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ApiError(f"删除失败：{_brief(exc)}", 500)
    HUB.log(f"数据集已删除：{target}", "warn")
    return ok(path=str(target))


# ---- 训练（可插拔引擎）/ 评估 / 异步推理 ----
@app.get("/api/train/backends")
def api_train_backends(force: int = 0):
    if force:
        STATE.backend_cache.pop("all", None)
    return {"backends": backend_availability(),
            "install_index": PIP_INDEX}


@app.post("/api/train/install-deps")
def api_train_install_deps(payload: Optional[dict] = Body(default=None)):
    require_env()
    backend = str((payload or {}).get("backend") or "")
    if backend not in TRAIN_BACKENDS:
        raise ApiError(f"未知训练引擎：{backend}", 400)
    py = _bin("python")
    if backend == "lerobot":
        raise ApiError("LeRobot 训练引擎请到「环境安装」页面执行「一键安装 LeRobot」", 400)
    deps = TRAIN_BACKENDS[backend]["deps"]
    cmd = [py, "-m", "pip", "install", *deps, *pip_flags(load_config())]
    return start_chain("train-deps", f"安装 {backend} 训练依赖", [("安装依赖（清华源）", cmd)],
                       load_config())


@app.post("/api/train/start")
def api_train_start(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    backend = str(e.get("backend") or "").strip()
    if backend not in TRAIN_BACKENDS:
        raise ApiError(f"请先选择训练引擎，可选：{', '.join(TRAIN_BACKENDS)}", 400)
    params = e.get("params") if isinstance(e.get("params"), dict) else {}
    return start_train(backend, params)


@app.post('/api/train/finetune/inspect')
def api_train_finetune_inspect(payload: Optional[dict] = Body(default=None)):
    params = _deep_merge(load_config()['train'], payload or {})
    try:
        source, policy = prepare_finetune(params, ROOT, LEROBOT_HOME)
    except ValueError as exc:
        raise ApiError(str(exc), 400) from exc
    return {'ok': True, 'base_model': source, 'policy_type': params.get('policy_type'),
            'input_features': (policy or {}).get('input_features', {}),
            'output_features': (policy or {}).get('output_features', {}),
            'note': '将加载已有权重，以所选数据集新建微调任务；优化器和步数重新开始，原模型保留。'}


_MODEL_EVAL_JOBS: dict[str, Job] = {}


@app.post('/api/model-eval/start')
def api_model_eval_start(payload: Optional[dict] = Body(default=None)):
    require_env()
    with STATE.lock:
        if STATE.running():
            raise ApiError('已有任务运行中，请等待当前任务结束后再评估', 409)
        values = _deep_merge(load_config().get('model_eval') or {}, payload or {})
        try:
            params = prepare_evaluation(values, ROOT, LEROBOT_HOME)
        except (ValueError, TypeError, OSError) as exc:
            raise ApiError(f'评估参数无效：{exc}', 400) from exc
        evaluation_id = os.urandom(12).hex()
        params['result_path'] = str(OUTPUTS_DIR / 'model_evaluations' / (evaluation_id + '.json'))
        result = _spawn('model-eval', 'ACT 离线模型评估', _py() + ['-u', str(BACKEND_DIR / 'model_eval_worker.py'),
                        json.dumps(params, ensure_ascii=False)],
                        env_extra={'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
        _MODEL_EVAL_JOBS[evaluation_id] = STATE.job
        while len(_MODEL_EVAL_JOBS) > 20: _MODEL_EVAL_JOBS.pop(next(iter(_MODEL_EVAL_JOBS)))
        save_config({'model_eval': {**{k: params[k] for k in ('base_model', 'repo_id', 'dataset_dir', 'episodes', 'max_samples', 'batch_size', 'device', 'video_backend')},
                                    'last_evaluation_id': evaluation_id}})
        return {**result, 'evaluation_id': evaluation_id}


@app.get('/api/model-eval/result')
def api_model_eval_result(evaluation_id: str = ''):
    ident = evaluation_id or str((load_config().get('model_eval') or {}).get('last_evaluation_id') or '')
    if not ident:
        return {'ok': True, 'finished': True, 'result': None, 'evaluation_id': ''}
    if not re.fullmatch(r'[0-9a-f]{24}', ident):
        raise ApiError('评估结果编号无效', 400)
    with STATE.lock:
        job = _MODEL_EVAL_JOBS.get(ident)
        path = OUTPUTS_DIR / 'model_evaluations' / (ident + '.json')
        report = job.result if job else None
        if report is None and path.is_file():
            try: report = json.loads(path.read_text('utf-8'))
            except (OSError, ValueError) as exc: raise ApiError(f'无法读取评估报告：{exc}', 500) from exc
        if job is None and report is None:
            raise ApiError('该次评估未保存完整报告，请重新评估并检查日志', 404)
        return {'ok': True, 'evaluation_id': ident, 'finished': job.finished.is_set() if job else True,
                'exit_code': job.exit_code if job else 0, 'result': report,
                'last_line': job.last_line if job else ''}


@app.post("/api/train/resume")
def api_train_resume(payload: Optional[dict] = Body(default=None)):
    return start_job("train-resume", "恢复训练", load_config(), payload)


@app.post("/api/eval")
def api_eval():
    return start_job("eval", "策略评估", load_config())


@app.post("/api/policy-server")
def api_policy_server():
    return start_job("policy-server", "异步推理服务", load_config())


@app.post("/api/robot-client")
def api_robot_client():
    return start_job("robot-client", "异步推理客户端", load_config())


@app.get("/api/checkpoints")
def api_checkpoints():
    return {"checkpoints": list_checkpoints()}


@app.delete("/api/checkpoints/{ckpt_path:path}")
def api_delete_checkpoint(ckpt_path: str, confirm: bool = False):
    """删除 outputs/ 下的模型产出（检查点或导出的 .pt/.xml）。"""
    if not confirm:
        raise ApiError("删除模型产出需要二次确认", 400)
    root = OUTPUTS_DIR.resolve()
    target = (ROOT / ckpt_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ApiError("只能删除 outputs/ 目录下的模型产出", 400)
    if target == root:
        raise ApiError("不能删除整个 outputs 目录", 400)
    if not target.exists() and not target.is_symlink():
        raise ApiError("路径不存在", 404)

    # pretrained_model 目录：连它所在的 step 目录（含 training_state）一起删
    if (target.is_dir() and target.name == "pretrained_model"
            and target.parent.parent.name == "checkpoints"):
        target = target.parent

    last_link = target.parent / "last" if target.parent.name == "checkpoints" else None
    last_points_here = False
    if last_link is not None and last_link.is_symlink():
        try:
            last_points_here = last_link.resolve(strict=False) == target.resolve()
        except OSError:
            last_points_here = False

    try:
        size = _dir_size(target) if target.is_dir() else target.stat().st_size
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise ApiError(f"删除失败：{_brief(exc)}", 500)
    if last_points_here and last_link is not None:
        try:
            last_link.unlink()
        except OSError:
            pass
    rel = str(target.relative_to(ROOT)).replace("\\", "/")
    HUB.log(f"[模型产出] 已删除 {rel}（释放 {size / 1048576:.1f} MB）", "warn")
    return ok(removed=rel, freed_mb=round(size / 1048576, 2))


# ---- 文档命令中心：点击即真实执行 ----
@app.post("/api/command/run")
def api_command_run(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    cmd = str(e.get("cmd") or "").strip()
    if not cmd:
        raise ApiError("命令不能为空", 400)
    if cmd not in {c["cmd"] for c in INSTALL_COMMANDS}:
        raise ApiError("只允许执行文档命令列表中的命令", 400)
    if IS_WINDOWS and any(m in cmd for m in ("/tmp/", "bash ", "sudo ", "chmod ", "/dev/")):
        raise ApiError(
            "这条是 Ubuntu 文档命令（含 Linux 路径或 bash/sudo/chmod），当前为 Windows，无法执行；"
            "请迁移到 Ubuntu 真机后再点。Windows 上仍可执行 git clone、pip install、conda create 等命令。", 400)
    password = str(e.get("password") or "")
    stdin_data = None
    if "sudo" in cmd and password:
        cmd = re.sub(r"\bsudo\b(?!\s+-S)", "sudo -S -p ''", cmd, count=1)
        stdin_data = password + "\n"
    argv = ["cmd", "/c", cmd] if IS_WINDOWS else ["bash", "-c", cmd]
    return _spawn("shell", "文档命令", argv, display=cmd, stdin_data=stdin_data)


# ---- 统一日志通道 ----
@app.get("/api/logs")
def api_logs(after: float = 0):
    """日志轮询降级接口：WebSocket 不可用（例如 uvicorn 缺 websockets）时前端改走这里。"""
    events = [e for e in HUB.snapshot(300) if float(e.get("ts") or 0) > after]
    return {"events": events, "server_ts": round(time.time(), 3)}


# ==================================================================
# 12.7 YOLO 视觉数据：导入 / 标注 / 训练
# ==================================================================
YOLO_ROOT = ROOT / "yolo_datasets"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _yolo_dir(name: str) -> Path:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ApiError("数据集名只能包含字母、数字、下划线、点、短横线（1-64 字符）", 400)
    root = YOLO_ROOT.resolve()
    p = (root / name).resolve()
    if p.parent != root:
        raise ApiError("非法的数据集路径", 400)
    return p


def _yolo_require(name: str) -> Path:
    p = _yolo_dir(name)
    if not p.is_dir():
        raise ApiError(f"数据集不存在：{name}", 404)
    return p


def _yolo_classes(p: Path) -> list[str]:
    f = p / "classes.txt"
    if not f.is_file():
        return []
    return [x.strip() for x in f.read_text("utf-8", errors="replace").splitlines() if x.strip()]


def _yolo_count(d: Path, exts: set[str]) -> int:
    if not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir() if f.is_file() and f.suffix.lower() in exts)


def _yolo_info(p: Path) -> dict:
    return {
        "name": p.name,
        "path": str(p.relative_to(ROOT)).replace("\\", "/"),
        "classes": _yolo_classes(p),
        "images_train": _yolo_count(p / "images/train", IMG_EXTS),
        "images_val": _yolo_count(p / "images/val", IMG_EXTS),
        "labels_train": _yolo_count(p / "labels/train", {".txt"}),
        "labels_val": _yolo_count(p / "labels/val", {".txt"}),
    }


def _yolo_split(p: Path, split: str) -> Path:
    split = (split or "train").lower()
    if split not in {"train", "val"}:
        raise ApiError("split 只能是 train 或 val", 400)
    return p / "images" / split


def _yolo_check_file(file: str) -> str:
    if not file or "/" in file or "\\" in file or ".." in file:
        raise ApiError("非法的文件名", 400)
    return file


@app.get("/api/yolo/datasets")
def api_yolo_datasets():
    YOLO_ROOT.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(YOLO_ROOT.iterdir()):
        if d.is_dir():
            try:
                out.append(_yolo_info(d))
            except OSError:
                continue
    return {"datasets": out}


@app.post("/api/yolo/datasets")
def api_yolo_create(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    name = str(e.get("name") or "").strip()
    classes = [str(c).strip() for c in (e.get("classes") or []) if str(c).strip()]
    p = _yolo_dir(name)
    if p.exists():
        raise ApiError(f"数据集已存在：{name}", 409)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    (p / "classes.txt").write_text("".join(c + "\n" for c in classes), "utf-8")
    HUB.log(f"[YOLO] 创建数据集 {name}（{len(classes)} 类）", "ok")
    return ok(dataset=_yolo_info(p))


@app.delete("/api/yolo/datasets/{name}")
def api_yolo_delete(name: str):
    p = _yolo_require(name)
    shutil.rmtree(p)
    HUB.log(f"[YOLO] 删除数据集 {name}", "warn")
    return ok()


@app.post("/api/yolo/datasets/{name}/import")
async def api_yolo_import(name: str, file: UploadFile = File(...)):
    p = _yolo_require(name)
    data = await file.read()
    if not data:
        raise ApiError("上传的压缩包为空", 400)
    if len(data) > 1_500_000_000:
        raise ApiError("压缩包过大（>1.5GB）", 400)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError("不是有效的 zip 压缩包", 400)
    with zf:
        for n in zf.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                raise ApiError(f"压缩包包含非法路径：{n}", 400)
        with tempfile.TemporaryDirectory() as td:
            zf.extractall(td)
            root = Path(td)
            images = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in IMG_EXTS]
            labels = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() == ".txt"
                      and f.name != "classes.txt"]
            if not images:
                raise ApiError("压缩包里没有图片（jpg/png/webp/bmp）", 400)
            stamp = int(time.time() * 1000) % 100000
            moved_img = moved_lbl = 0
            for f in images:
                # 导入的图片一律先进 train；标注保存时会自动复制到 val
                dest = p / "images" / "train" / f.name
                if dest.exists():
                    dest = p / "images" / "train" / f"{f.stem}_{stamp}{f.suffix}"
                shutil.copy2(f, dest)
                moved_img += 1
            for f in labels:
                dest = p / "labels" / "train" / f.name
                if dest.exists():
                    dest = p / "labels" / "train" / f"{f.stem}_{stamp}.txt"
                shutil.copy2(f, dest)
                moved_lbl += 1
            for cf in root.rglob("classes.txt"):
                names_txt = [x.strip() for x in
                             cf.read_text("utf-8", errors="replace").splitlines() if x.strip()]
                if names_txt:
                    (p / "classes.txt").write_text("".join(x + "\n" for x in names_txt), "utf-8")
                    break
    HUB.log(f"[YOLO] 导入 {name}：{moved_img} 张图片 / {moved_lbl} 个标签", "ok")
    return ok(images=moved_img, labels=moved_lbl, dataset=_yolo_info(p))


@app.get("/api/yolo/datasets/{name}/images")
def api_yolo_images(name: str, split: str = "train"):
    p = _yolo_require(name)
    d = _yolo_split(p, split)
    files = sorted([f.name for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in IMG_EXTS]) if d.is_dir() else []
    out = []
    for fn in files:
        out.append({"file": fn,
                    "labeled": (p / "labels" / split / (Path(fn).stem + ".txt")).is_file()})
    return {"split": split, "images": out}


@app.get("/api/yolo/datasets/{name}/image")
def api_yolo_image(name: str, split: str = "train", file: str = ""):
    p = _yolo_require(name)
    d = _yolo_split(p, split)
    target = d / _yolo_check_file(file)
    if not target.is_file():
        raise ApiError("图片不存在", 404)
    return FileResponse(target)


@app.get("/api/yolo/datasets/{name}/label")
def api_yolo_label_get(name: str, split: str = "train", file: str = ""):
    p = _yolo_require(name)
    _yolo_split(p, split)
    lf = p / "labels" / split / (Path(_yolo_check_file(file)).stem + ".txt")
    return {"text": lf.read_text("utf-8", errors="replace") if lf.is_file() else ""}


@app.put("/api/yolo/datasets/{name}/label")
def api_yolo_label_put(name: str, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    e = payload or {}
    split = str(e.get("split") or "train")
    _yolo_split(p, split)
    file = _yolo_check_file(str(e.get("file") or ""))
    classes = _yolo_classes(p)
    lines: list[str] = []
    for raw in str(e.get("text") or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split()
        if len(parts) < 5:
            raise ApiError(f"标签格式错误：{raw}", 400)
        try:
            cid = int(parts[0])
            vals = [float(x) for x in parts[1:5]]
        except ValueError:
            raise ApiError(f"标签格式错误：{raw}", 400)
        if classes and not (0 <= cid < len(classes)):
            raise ApiError(f"类别 id 超出范围：{cid}", 400)
        if any(v < 0.0 or v > 1.0 for v in vals):
            raise ApiError(f"坐标必须在 0..1 之间：{raw}", 400)
        lines.append(f"{cid} " + " ".join(f"{v:.6f}" for v in vals))
    lf = p / "labels" / split / (Path(file).stem + ".txt")
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("\n".join(lines) + ("\n" if lines else ""), "utf-8")

    # 标注保存后：把图片和标签复制一份到 val；清空标注则移除 val 副本
    if split == "train":
        img_src = p / "images" / "train" / file
        img_dst = p / "images" / "val" / file
        lbl_dst = p / "labels" / "val" / lf.name
        if lines:
            if img_src.is_file():
                img_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(img_src, img_dst)
            lbl_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lf, lbl_dst)
        else:
            for f in (img_dst, lbl_dst):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
    return ok(file=file, boxes=len(lines), val_copy=bool(lines))


@app.post("/api/yolo/datasets/{name}/classes/add")
def api_yolo_class_add(name: str, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    new = str((payload or {}).get("name") or "").strip()
    if not new:
        raise ApiError("类别名不能为空", 400)
    classes = _yolo_classes(p)
    if new in classes:
        raise ApiError(f"类别已存在：{new}", 409)
    classes.append(new)
    (p / "classes.txt").write_text("".join(c + "\n" for c in classes), "utf-8")
    HUB.log(f"[YOLO] {name} 添加类别：{new}（id={len(classes) - 1}）", "ok")
    return ok(dataset=_yolo_info(p), cid=len(classes) - 1)


@app.put("/api/yolo/datasets/{name}/classes/{cid}")
def api_yolo_class_rename(name: str, cid: int, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    classes = _yolo_classes(p)
    if cid < 0 or cid >= len(classes):
        raise ApiError("类别 id 不存在", 404)
    new = str((payload or {}).get("name") or "").strip()
    if not new:
        raise ApiError("类别名不能为空", 400)
    if new in classes and classes.index(new) != cid:
        raise ApiError(f"类别已存在：{new}", 409)
    classes[cid] = new
    (p / "classes.txt").write_text("".join(c + "\n" for c in classes), "utf-8")
    HUB.log(f"[YOLO] {name} 类别 {cid} 改名为：{new}", "ok")
    return ok(dataset=_yolo_info(p))


@app.delete("/api/yolo/datasets/{name}/classes/{cid}")
def api_yolo_class_delete(name: str, cid: int, confirm: bool = False):
    if not confirm:
        raise ApiError("删除类别需要确认", 400)
    p = _yolo_require(name)
    classes = _yolo_classes(p)
    if cid < 0 or cid >= len(classes):
        raise ApiError("类别 id 不存在", 404)
    removed = classes.pop(cid)
    # 重写所有标签：删掉该类别的框，后面的类别 id 前移一位
    rewritten = 0
    for split in ("train", "val"):
        d = p / "labels" / split
        if not d.is_dir():
            continue
        for lf in d.glob("*.txt"):
            try:
                lines = lf.read_text("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            out = []
            for ln in lines:
                parts = ln.strip().split()
                if len(parts) < 5:
                    continue
                try:
                    lc = int(parts[0])
                except ValueError:
                    continue
                if lc == cid:
                    continue
                if lc > cid:
                    lc -= 1
                out.append(str(lc) + " " + " ".join(parts[1:5]))
            lf.write_text("\n".join(out) + ("\n" if out else ""), "utf-8")
            rewritten += 1
    (p / "classes.txt").write_text("".join(c + "\n" for c in classes), "utf-8")
    HUB.log(f"[YOLO] {name} 删除类别：{removed}，重写 {rewritten} 个标签文件", "warn")
    return ok(dataset=_yolo_info(p), removed=removed)


@app.put("/api/yolo/datasets/{name}/classes")
def api_yolo_classes(name: str, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    classes = [str(c).strip() for c in ((payload or {}).get("classes") or []) if str(c).strip()]
    if not classes:
        raise ApiError("至少保留一个类别", 400)
    (p / "classes.txt").write_text("".join(c + "\n" for c in classes), "utf-8")
    HUB.log(f"[YOLO] {name} 类别更新：{', '.join(classes)}", "ok")
    return ok(dataset=_yolo_info(p))


@app.post("/api/yolo/datasets/{name}/upload-image")
def api_yolo_upload_image(name: str, payload: Optional[dict] = Body(default=None)):
    """保存浏览器预览里截下的那一帧（base64 dataURL）到 train。"""
    p = _yolo_require(name)
    data = str((payload or {}).get("image") or "")
    if not data.startswith("data:image/"):
        raise ApiError("图片数据格式不正确", 400)
    try:
        _, b64 = data.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        raise ApiError("图片解码失败", 400)
    if not raw:
        raise ApiError("图片为空", 400)
    if len(raw) > 20_000_000:
        raise ApiError("图片过大（>20MB）", 400)
    fn = time.strftime("cap_%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}.jpg"
    dest = p / "images" / "train" / fn
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(raw)
    except OSError as exc:
        raise ApiError(f"保存图片失败：{_brief(exc)}", 500)
    HUB.log(f"[YOLO] {name} 采集保存：{fn}", "ok")
    return ok(file=fn)


@app.delete("/api/yolo/datasets/{name}/image")
def api_yolo_image_delete(name: str, split: str = "train", file: str = ""):
    p = _yolo_require(name)
    d = _yolo_split(p, split)
    fn = _yolo_check_file(file)
    img = d / fn
    if not img.is_file():
        raise ApiError("图片不存在", 404)
    try:
        img.unlink()
    except OSError as exc:
        raise ApiError(f"删除图片失败：{_brief(exc)}", 500)
    lf = p / "labels" / split / (Path(fn).stem + ".txt")
    if lf.is_file():
        try:
            lf.unlink()
        except OSError:
            pass
    # 删除的是 train 图片时，同步清掉它的 val 副本
    if split == "train":
        for extra in (p / "images" / "val" / fn,
                      p / "labels" / "val" / (Path(fn).stem + ".txt")):
            if extra.is_file():
                try:
                    extra.unlink()
                except OSError:
                    pass
    HUB.log(f"[YOLO] {name} 删除图片：{fn}", "warn")
    return ok(file=fn)


@app.post("/api/yolo/datasets/{name}/capture")
def api_yolo_capture(name: str, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    e = payload or {}
    split = str(e.get("split") or "train")
    _yolo_split(p, split)
    raw_src = e.get("camera_source")
    if raw_src is None or str(raw_src).strip() == "":
        raw_src = e.get("camera_index", 0)
    src_str = str(raw_src).strip()
    src: Any = int(src_str) if src_str.lstrip("-").isdigit() else src_str
    cap = camera_probe(src)
    try:
        ret, frame = cap.read()
    finally:
        cap.release()
    if not ret or frame is None:
        raise ApiError(f"摄像头 {src} 读取画面失败", 500)
    try:
        import cv2  # type: ignore
    except ImportError:
        raise ApiError("未安装 opencv-python", 503)
    fn = time.strftime("cap_%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}.jpg"
    dest = p / "images" / split / fn
    dest.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dest), frame)
    HUB.log(f"[YOLO] {name} ({split}) 采集一张：{fn}", "ok")
    return ok(file=fn, split=split)


@app.get("/api/yolo/datasets/{name}/export")
def api_yolo_export(name: str):
    """一键导出 YOLO 数据集为标准 Ultralytics zip（含 images/、labels/、classes.txt、data.yaml）。"""
    import zipfile
    from starlette.background import BackgroundTask
    p = _yolo_require(name)
    classes = _yolo_classes(p)
    files = [f for f in sorted(p.rglob("*")) if f.is_file()]
    if not files:
        raise ApiError("该 YOLO 数据集为空，无法导出", 404)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", p.name) or "yolo_dataset"
    fd, tmp_zip = tempfile.mkstemp(prefix=f"yolo_{safe_name}_", suffix=".zip")
    os.close(fd)
    yaml_lines = [
        "path: .",
        "train: images/train",
        "val: images/val",
        f"nc: {len(classes)}",
        "names: " + json.dumps(classes, ensure_ascii=False),
        "",
    ]
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            has_yaml = False
            for f in files:
                rel = f.relative_to(p).as_posix()
                if rel == "data.yaml":
                    has_yaml = True
                zf.write(f, arcname=f"{safe_name}/{rel}")
            if not has_yaml:
                zf.writestr(f"{safe_name}/data.yaml", "\n".join(yaml_lines))
    except Exception as exc:
        Path(tmp_zip).unlink(missing_ok=True)
        raise ApiError(f"打包 YOLO 数据集失败：{_brief(exc)}", 500)
    HUB.log(f"[YOLO] 导出数据集 {p.name}.zip（共 {len(files)} 个文件）", "ok")
    return FileResponse(
        tmp_zip,
        media_type="application/zip",
        filename=f"{safe_name}_yolo.zip",
        background=BackgroundTask(lambda path=tmp_zip: Path(path).unlink(missing_ok=True)),
    )


@app.post("/api/yolo/datasets/{name}/split")
def api_yolo_split_set(name: str, payload: Optional[dict] = Body(default=None)):
    p = _yolo_require(name)
    e = payload or {}
    try:
        ratio = float(e.get("ratio") if e.get("ratio") is not None else 0.2)
    except (TypeError, ValueError):
        raise ApiError("ratio 必须是数字", 400)
    ratio = min(max(ratio, 0.05), 0.5)
    files = sorted([f for f in (p / "images/train").iterdir()
                    if f.is_file() and f.suffix.lower() in IMG_EXTS]) if (p / "images/train").is_dir() else []
    if len(files) < 2:
        raise ApiError("训练集至少需要 2 张图片才能划分验证集（当前 " + str(len(files)) + " 张）", 400)
    n = max(1, int(len(files) * ratio))
    moved = 0
    for f in files[-n:]:
        shutil.move(str(f), str(p / "images/val" / f.name))
        lf = p / "labels/train" / (f.stem + ".txt")
        if lf.is_file():
            shutil.move(str(lf), str(p / "labels/val" / lf.name))
        moved += 1
    HUB.log(f"[YOLO] {name} 划分验证集：{moved} 张", "ok")
    return ok(moved=moved, dataset=_yolo_info(p))


@app.get("/api/yolo/datasets/{name}/train-params")
def api_yolo_train_params(name: str):
    p = _yolo_require(name)
    info = _yolo_info(p)
    if info["images_train"] <= 0:
        raise ApiError("训练集还没有图片，先导入或采集", 400)
    return ok(dataset_dir=info["path"], classes=info["classes"])


@app.post("/api/yolo/predict")
def api_yolo_predict(payload: Optional[dict] = Body(default=None)):
    """用训练好的 .pt 对一张图片/摄像头画面做推理，返回检测框和可视化结果。"""
    e = payload or {}
    model = str(e.get("model") or "").strip()
    if not model:
        raise ApiError("请先选择模型（.pt）", 400)
    mp = (ROOT / model).resolve() if not Path(model).is_absolute() else Path(model).resolve()
    try:
        mp.relative_to(ROOT.resolve())
    except ValueError:
        raise ApiError("模型必须在本项目目录内", 400)
    if not mp.is_file() or mp.suffix.lower() != ".pt":
        raise ApiError(f"模型文件不存在：{model}", 404)

    tmp = None
    if e.get("dataset"):
        p = _yolo_require(str(e["dataset"]))
        split = str(e.get("split") or "train")
        fn = _yolo_check_file(str(e.get("file") or ""))
        img_path = p / "images" / split / fn
        if not img_path.is_file():
            raise ApiError("图片不存在", 404)
    else:
        idx = e.get("camera_source", e.get("camera_index", 0))
        cap = camera_probe(idx)
        try:
            ret, frame = cap.read()
        finally:
            cap.release()
        if not ret or frame is None:
            raise ApiError(f"摄像头 index={idx} 读取画面失败", 500)
        import cv2
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        cv2.imwrite(tmp, frame)
        img_path = Path(tmp)

    try:
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ApiError("未安装 ultralytics，无法运行 YOLO 验证。请在所选环境执行："
                           "pip install ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple", 503)
        try:
            conf = float(e.get("conf") if e.get("conf") is not None else 0.25)
        except (TypeError, ValueError):
            conf = 0.25
        conf = min(max(conf, 0.01), 0.99)
        m = YOLO(str(mp))
        results = m.predict(source=str(img_path), conf=conf, verbose=False)
        dets = []
        annotated = None
        if results:
            r = results[0]
            names = r.names or {}
            if r.boxes is not None:
                for b in r.boxes:
                    cls_id = int(b.cls[0].item())
                    cf = float(b.conf[0].item())
                    xyxy = [float(x) for x in b.xyxy[0].tolist()]
                    dets.append({"cls": cls_id,
                                 "name": names.get(cls_id, str(cls_id)),
                                 "conf": round(cf, 3),
                                 "xyxy": [round(x, 1) for x in xyxy]})
            annotated = r.plot()
        b64 = ""
        if annotated is not None:
            import cv2
            okk, buf = cv2.imencode(".jpg", annotated)
            if okk:
                b64 = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        return ok(detections=dets, count=len(dets), image=b64)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ==================================================================
# 14. 视觉抓取：标定 / IK 预演（worker 跑在 rebotarm 环境）
# ==================================================================
GRASP_PY = "/home/legion/miniconda3/envs/rebotarm/bin/python"
GRASP_WORKER = ROOT / "backend" / "grasp_worker.py"


def _grasp_worker(action: str, payload: Optional[dict] = None, timeout: int = 60) -> dict:
    if not Path(GRASP_PY).exists():
        raise ApiError(f"找不到 rebotarm 环境：{GRASP_PY}", 503)
    cmd = [GRASP_PY, str(GRASP_WORKER), action]
    if payload is not None:
        cmd.append(json.dumps(payload, ensure_ascii=False))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError("抓取 worker 超时（机械臂可能未连接或串口被占用）", 504)
    lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
    if not lines:
        raise ApiError("抓取 worker 无输出：" + (p.stderr or "")[-400:], 500)
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ApiError("抓取 worker 输出无法解析：" + lines[-1][:200], 500)
    if not data.get("ok"):
        raise ApiError("抓取失败：" + str(data.get("error") or (p.stderr or "")[-400:]), 500)
    return data


def _homography(points: list[dict]) -> list[list[float]]:
    import numpy as np
    if len(points) < 4:
        raise ApiError("至少需要 4 个标定点", 400)
    A = []
    for p in points:
        u, v, x, y = float(p["u"]), float(p["v"]), float(p["x"]), float(p["y"])
        A.append([u, v, 1, 0, 0, 0, -x * u, -x * v, -x])
        A.append([0, 0, 0, u, v, 1, -y * u, -y * v, -y])
    if np.linalg.matrix_rank(np.array(A, dtype=float)) < 8:
        raise ApiError("标定点共线或重复，请使用覆盖桌面的四个点",400)
    _, _, vt = np.linalg.svd(np.array(A, dtype=float))
    H = vt[-1].reshape(3, 3)
    if abs(H[2, 2]) < 1e-9:
        raise ApiError("标定点分布退化，请换不同位置重新记录", 400)
    H = H / H[2, 2]
    return [[float(x) for x in row] for row in H]


_GRASP_PLANS = {}
_GRASP_LOCK = threading.RLock()


def _grasp_context(payload=None):
    e = payload or {}; cfg = load_config(); settings = cfg.get('grasp') or {}
    side = str(e.get('side', settings.get('side', 'left')))
    if side not in ('left', 'right') or (side == 'right' and not dual_enabled(cfg)):
        raise ApiError('请选择当前模式下的有效机械臂', 400)
    source = e.get('camera_source', settings.get('camera_source', settings.get('camera_index', 0)))
    try: source_key(source)
    except CameraError as exc: raise ApiError(str(exc), 400)
    source = '/dev/video'+str(int(source)) if str(source).isdigit() else str(source)
    key = json.dumps([side, source], ensure_ascii=False)
    profiles = settings.get('calibrations') or {}
    profile = dict(profiles.get(key) or {})
    legacy_source = settings.get('camera_index', 0)
    if not profiles and side == 'left' and source == '/dev/video'+str(legacy_source):
        profile = {k:settings[k] for k in ('points','H','table_z') if k in settings}
    arm = ((cfg.get('dual_arm') or {}).get(side) or {}) if dual_enabled(cfg) else cfg.get('follower', {})
    port = str(arm.get('follower_port') or arm.get('port') or '')
    return cfg, settings, side, source, key, profile, port


def _grasp_save_profile(key, profile):
    save_config({'grasp':{'calibrations':{key:profile}}})


def _grasp_values(e, names):
    import math
    try:
        values = [float(e[n]) for n in names]
        if not all(math.isfinite(x) for x in values): raise ValueError()
        return values
    except (KeyError, TypeError, ValueError): raise ApiError('需要有效数值：'+ '/'.join(names), 400)


def _grasp_measured(side, port):
    feedback = TELEMETRY.snapshot()['arms'].get(side)
    if not feedback or feedback.get('port') != os.path.realpath(port) or time.time()-feedback['timestamp'] > 1.2:
        raise ApiError('尚无该臂的实时关节反馈，请连接设备并等待常驻模型显示实测反馈', 409)
    return list(feedback['joints'])


def _grasp_fingerprint(context):
    _, settings, side, source, _, profile, port = context
    return json.dumps([side,source,port,profile,settings.get('z_offset',.03),settings.get('rpy',[0,.7,0]),settings.get('approach',.08)],sort_keys=True)


@app.get('/api/grasp/calibration')
def api_grasp_cal(side: Optional[str]=None, camera_source: Optional[str]=None):
    context = _grasp_context({k:v for k,v in {'side':side,'camera_source':camera_source}.items() if v is not None})
    cfg, settings, selected, source, _, profile, port = context
    return {'grasp':dict(settings, **dict({'points':[],'H':None,'table_z':0},**profile)), 'side':selected, 'camera_source':source, 'port':port,
            'dual':dual_enabled(cfg), 'calibration':profile}


@app.post('/api/grasp/state')
def api_grasp_state(payload: Optional[dict]=Body(default=None)):
    context = _grasp_context(payload); _,_,side,_,_,_,port=context
    q = _grasp_measured(side,port)
    return _grasp_worker('fk',{'joints':q})


@app.post('/api/grasp/calibrate/point')
def api_grasp_point(payload: Optional[dict]=Body(default=None)):
    e=payload or {}; u,v=_grasp_values(e,('u','v'))
    if u<0 or v<0: raise ApiError('像素坐标不能为负数',400)
    context=_grasp_context(e); _,_,side,_,key,profile,port=context
    state=_grasp_worker('fk',{'joints':_grasp_measured(side,port)})
    x,y,z=state['xyz']; points=list(profile.get('points') or [])
    if any(abs(p['u']-u)+abs(p['v']-v)<2 for p in points): raise ApiError('该像素附近已有标定点，请选择另一个位置',400)
    points.append(dict(u=u,v=v,x=x,y=y,z=z));profile.update(points=points,H=None)
    _grasp_save_profile(key,profile)
    return ok(points=points,xyz=[x,y,z])


@app.post('/api/grasp/calibrate/solve')
def api_grasp_solve(payload: Optional[dict]=Body(default=None)):
    context=_grasp_context(payload); profile=context[5];points=profile.get('points') or []
    H=_homography(points)
    if max(p['z'] for p in points)-min(p['z'] for p in points)>.02:
        raise ApiError('标定点不在同一桌面高度（差值超过 2 cm），请重新核对',400)
    table_z=sum(p['z'] for p in points)/len(points)
    profile.update(H=H,table_z=table_z);_grasp_save_profile(context[4],profile)
    return ok(H=H,table_z=table_z,points=len(points))


@app.delete('/api/grasp/calibration')
def api_grasp_clear(side: Optional[str]=None,camera_source: Optional[str]=None):
    context=_grasp_context({k:v for k,v in {'side':side,'camera_source':camera_source}.items() if v is not None})
    _grasp_save_profile(context[4],{'points':[],'H':None,'table_z':0.0})
    return ok()


@app.post('/api/grasp/plan')
def api_grasp_plan(payload: Optional[dict]=Body(default=None)):
    import numpy as np
    import cv2
    import secrets
    e=payload or {};u,v=_grasp_values(e,('u','v'));context=_grasp_context(e)
    _, settings, side, source, _, profile, port=context
    H=profile.get('H');points=profile.get('points') or []
    if not H or len(points)<4: raise ApiError('所选相机与机械臂还没有完成四点标定',400)
    hull=cv2.convexHull(np.array([[p['u'],p['v']] for p in points],dtype=np.float32))
    if cv2.pointPolygonTest(hull,(u,v),False)<0:raise ApiError('目标超出标定区域，请在区域内选点或补充标定点',400)
    p=np.array(H)@np.array([u,v,1.0])
    if abs(p[2])<1e-9:raise ApiError('该像素无法映射到工作区',400)
    x,y=float(p[0]/p[2]),float(p[1]/p[2]);z=float(profile.get('table_z',0))+float(settings.get('z_offset',.03))
    rpy=list(settings.get('rpy') or [0,.7,0])
    if len(rpy)!=3:raise ApiError('抓取姿态需要三个角度',400)
    _grasp_values(dict(zip(('r','p','y'),rpy)),('r','p','y'))
    approach=float(settings.get('approach',.08))
    if not .01<=approach<=.25:raise ApiError('预抓取高度需要在 0.01～0.25 m 之间',400)
    q=_grasp_measured(side,port)
    args=dict(x=x,y=y,z=z,roll=rpy[0],pitch=rpy[1],yaw=rpy[2],joints=q)
    ik=_grasp_worker('ik',args)
    pre=_grasp_worker('ik',dict(args,z=z+approach))
    ident=secrets.token_urlsafe(24)
    plan=dict(x=x,y=y,z=z,rpy=rpy,approach=approach,port=port,side=side,seed=q,
              created=time.monotonic(),fingerprint=_grasp_fingerprint(context),context={'side':side,'camera_source':source})
    with _GRASP_LOCK:
        _GRASP_PLANS.clear();_GRASP_PLANS[ident]=plan
    return ok(plan_id=ident,xyz=[x,y,z],rpy=rpy,joints=ik['joints'],approach_joints=pre['joints'],
              ik_error=ik.get('error'),side=side,expires_in=120)


@app.post('/api/grasp/execute')
def api_grasp_execute(payload: Optional[dict]=Body(default=None)):
    e=payload or {}
    if e.get('confirm') is not True:raise ApiError('执行抓取需要明确确认',400)
    with _GRASP_LOCK, STATE.lock:
        plan=_GRASP_PLANS.get(str(e.get('plan_id','')))
        if not plan or time.monotonic()-plan['created']>120:raise ApiError('预演已失效，请重新预演',409)
        context=_grasp_context(plan['context'])
        if DEVICES_RELEASED:raise ApiError('设备已断开，请重新连接设备',409)
        if plan['fingerprint']!=_grasp_fingerprint(context):raise ApiError('标定或抓取参数已改变，请重新预演',409)
        q=_grasp_measured(plan['side'],plan['port'])
        if max(abs(a-b) for a,b in zip(q,plan['seed']))>.15:raise ApiError('机械臂位置已改变，请重新预演',409)
        _guard('grasp-execute')
        worker_payload={k:plan[k] for k in ('x','y','z','approach','port','seed')}
        worker_payload.update(roll=plan['rpy'][0],pitch=plan['rpy'][1],yaw=plan['rpy'][2])
        result=_spawn('grasp-execute','视觉抓取 · '+('左臂' if plan['side']=='left' else '右臂'),
            [GRASP_PY,'-u',str(GRASP_WORKER),'execute',json.dumps(worker_payload)],
            env_extra=dict(TELEMETRY.environment(monitor_ports(context[0])),PYTHONPATH=str(BACKEND_DIR)))
        _GRASP_PLANS.pop(str(e['plan_id']),None)
        return dict(result,started=True)


@app.get('/api/grasp/status')
def api_grasp_status():
    with STATE.lock:
        job=STATE.job
        if not job or job.kind!='grasp-execute':return ok(running=False,result=None)
        result=None
        for line in reversed(job.tail):
            try:
                candidate=json.loads(line)
                if isinstance(candidate,dict) and 'held' in candidate:result=candidate;break
            except (ValueError,TypeError):pass
        return ok(running=not job.finished.is_set(),exit_code=job.exit_code,result=result,last_line=job.last_line)


# ==================================================================
# 15. 视觉大模型（OpenAI 兼容接口）：AI 预标注 / AI 识别
# ==================================================================
def _llm_config() -> dict:
    cfg = load_config().get("llm") or {}
    return {
        "base_url": str(cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
        "model": str(cfg.get("model") or "deepseek-flash"),
        "api_key": str(cfg.get("api_key") or "").strip(),
        "prompt": str(cfg.get("prompt") or ""),
    }


def _llm_vision(image_data_url: str, prompt: str, timeout: int = 90) -> dict:
    cfg = _llm_config()
    if not cfg["api_key"]:
        raise ApiError("还没有配置视觉大模型 API key（到「视觉数据 → AI 设置」填写）", 400)
    body = {
        "model": cfg["model"],
        "temperature": 0,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]}],
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["api_key"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"视觉模型请求失败（HTTP {exc.code}）：{detail}", 502)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"视觉模型请求失败：{_brief(exc)}", 502)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ApiError("视觉模型返回格式异常", 502)
    if isinstance(content, list):
        content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    text = str(content or "").strip()
    if not text:
        # 推理模型可能把内容放在 reasoning_content；优先取其中的 JSON
        try:
            text = str(data["choices"][0]["message"].get("reasoning_content") or "").strip()
        except (KeyError, IndexError, TypeError):
            text = ""
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):]
    text = text.strip().strip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ApiError("视觉模型没有返回 JSON：" + text[:200], 502)
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ApiError("视觉模型 JSON 解析失败：" + text[:200], 502)
    raw_items = None
    if isinstance(obj, dict) and isinstance(obj.get("objects"), list):
        raw_items = obj["objects"]
    elif isinstance(obj, dict) and obj.get("bbox"):
        raw_items = [obj]
    elif isinstance(obj, list):
        raw_items = obj
    if not raw_items:
        raise ApiError("视觉模型没有返回 objects：" + text[:200], 502)
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        cls = str(it.get("class") or "").strip()
        bbox = it.get("bbox")
        if not cls or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            bbox = [float(x) for x in bbox]
        except (TypeError, ValueError):
            continue
        if any(x < 0 or x > 1 for x in bbox):
            continue
        items.append({"class": cls, "bbox": bbox})
    if not items:
        raise ApiError("视觉模型返回的 objects 都无效：" + text[:200], 502)
    return {"objects": items}


def _match_class(raw: str, classes: list[str]) -> str:
    """把模型返回的类别名对齐到已有类别；对齐不到就拒绝，绝不新增。"""
    raw = str(raw or "").strip()
    if raw in classes:
        return raw
    for c in classes:
        if c and (c in raw or raw in c):
            return c
    return ""


def _prompt_for(classes: list[str], prompt: str = "") -> str:
    cfg = _llm_config()
    tpl = prompt or cfg["prompt"]
    joined = "、".join(classes)
    # 兼容几种写法，方便在提示词里识别：{类别} 是推荐写法
    for token in ("{{类别}}", "{类别}", "{{classes}}", "{classes}"):
        tpl = tpl.replace(token, joined)
    return tpl


@app.post("/api/ai/detect")
def api_ai_detect(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    image = str(e.get("image") or "")
    if not image.startswith("data:image/"):
        raise ApiError("需要 image（dataURL）", 400)
    classes = [str(c).strip() for c in (e.get("classes") or []) if str(c).strip()]
    if not classes:
        raise ApiError("需要允许的类别列表 classes", 400)
    r = _llm_vision(image, _prompt_for(classes, str(e.get("prompt") or "")))
    matched, skipped = [], []
    for it in r["objects"]:
        cls = _match_class(it["class"], classes)
        if cls:
            matched.append({"class": cls, "bbox": it["bbox"]})
        else:
            skipped.append(it["class"])
    if not matched:
        raise ApiError("模型返回的类别都不在允许列表：" + "、".join(skipped), 400)
    return {"ok": True, "objects": matched, "skipped": skipped}


AI_BATCH = {"running": False, "total": 0, "done": 0, "succeeded": 0, "failed": 0,
            "current": "", "errors": [], "cancel": False}


def _ai_prelabel_one(p: Path, split: str, fn: str, prompt: str = "") -> dict:
    img = p / "images" / split / fn
    if not img.is_file():
        raise ApiError("图片不存在", 404)
    classes = _yolo_classes(p)
    if not classes:
        raise ApiError("该数据集还没有类别，先添加类别", 400)
    raw = base64.b64encode(img.read_bytes()).decode("ascii")
    suffix = img.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    r = _llm_vision(f"data:{mime};base64,{raw}", _prompt_for(classes, prompt))
    matched, skipped = [], []
    for it in r["objects"]:
        cls = _match_class(it["class"], classes)
        if cls:
            matched.append({"class": cls, "bbox": it["bbox"]})
        else:
            skipped.append(it["class"])
    if not matched:
        raise ApiError("模型返回的类别都不在允许列表：" + "、".join(skipped), 400)
    lines = []
    for it in matched:
        cid = classes.index(it["class"])
        x1, y1, x2, y2 = it["bbox"]
        cx, cy, bw, bh = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1), abs(y2 - y1)
        lines.append(f"{cid} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    text = "\n".join(lines) + "\n"
    lf = p / "labels" / split / (Path(fn).stem + ".txt")
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(text, "utf-8")
    if split == "train":
        img_dst = p / "images" / "val" / fn
        lbl_dst = p / "labels" / "val" / lf.name
        if img_dst.parent.is_dir():
            shutil.copy2(img, img_dst)
        if lbl_dst.parent.is_dir():
            shutil.copy2(lf, lbl_dst)
    HUB.log(f"[AI] 预标注 {p.name}/{fn} -> {len(matched)} 个框", "ok")
    return {"ok": True, "objects": matched, "skipped": skipped,
            "count": len(matched), "text": text}


@app.post("/api/ai/prelabel")
def api_ai_prelabel(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    p = _yolo_require(str(e.get("dataset") or ""))
    split = str(e.get("split") or "train")
    _yolo_split(p, split)
    fn = _yolo_check_file(str(e.get("file") or ""))
    return _ai_prelabel_one(p, split, fn, str(e.get("prompt") or ""))


def _ai_batch_worker(ds_name: str, split: str, skip_labeled: bool,
                     prompt: str, limit: int) -> None:
    try:
        p = _yolo_dir(ds_name)
        d = p / "images" / split
        files = sorted([f.name for f in d.iterdir()
                        if f.is_file() and f.suffix.lower() in IMG_EXTS]) if d.is_dir() else []
        if skip_labeled:
            files = [fn for fn in files
                     if not (p / "labels" / split / (Path(fn).stem + ".txt")).is_file()]
        if limit > 0:
            files = files[:limit]
        AI_BATCH.update(total=len(files), done=0, succeeded=0, failed=0,
                        current="", errors=[])
        for fn in files:
            if AI_BATCH["cancel"]:
                break
            AI_BATCH["current"] = fn
            try:
                _ai_prelabel_one(p, split, fn, prompt)
                AI_BATCH["succeeded"] += 1
            except Exception as exc:  # noqa: BLE001
                AI_BATCH["failed"] += 1
                if len(AI_BATCH["errors"]) < 20:
                    AI_BATCH["errors"].append(fn + "：" + _brief(exc))
            AI_BATCH["done"] += 1
        HUB.log(f"[AI] 批量预标注结束：{AI_BATCH['succeeded']} 成功 / {AI_BATCH['failed']} 失败", "ok")
    finally:
        AI_BATCH["running"] = False
        AI_BATCH["current"] = ""


@app.post("/api/ai/prelabel-batch")
def api_ai_batch_start(payload: Optional[dict] = Body(default=None)):
    if AI_BATCH["running"]:
        raise ApiError("批量预标注正在运行", 409)
    e = payload or {}
    p = _yolo_require(str(e.get("dataset") or ""))
    split = str(e.get("split") or "train")
    _yolo_split(p, split)
    if not _yolo_classes(p):
        raise ApiError("该数据集还没有类别，先添加类别", 400)
    try:
        limit = int(e.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    skip_labeled = bool(e.get("skip_labeled", True))
    d = p / "images" / split
    total = len([f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in IMG_EXTS]) if d.is_dir() else 0
    AI_BATCH.update(running=True, total=total, done=0, succeeded=0, failed=0,
                    current="", errors=[], cancel=False)
    th = threading.Thread(target=_ai_batch_worker,
                          args=(p.name, split, skip_labeled, str(e.get("prompt") or ""), limit),
                          daemon=True)
    th.start()
    return {"ok": True, "total": total}


@app.get("/api/ai/prelabel-batch")
def api_ai_batch_status():
    return {"ok": True, **AI_BATCH}


@app.post("/api/ai/prelabel-batch/cancel")
def api_ai_batch_cancel():
    AI_BATCH["cancel"] = True
    return {"ok": True}


_DINO_WORDS = {
    "黑": "black", "红": "red", "蓝": "blue", "绿": "green", "黄": "yellow",
    "白": "white", "灰": "gray", "橙": "orange", "紫": "purple", "棕": "brown",
    "粉": "pink", "青": "cyan",
    "方块": "cube", "立方体": "cube", "圆柱体": "cylinder", "圆柱": "cylinder",
    "球": "ball", "杯子": "cup", "杯": "cup", "瓶子": "bottle", "瓶": "bottle",
    "香蕉": "banana", "苹果": "apple", "盒子": "box", "箱子": "box", "箱": "box",
    "快递盒": "cardboard box", "纸箱": "cardboard box", "快递袋": "plastic parcel bag",
    "快递": "package", "包裹": "package", "袋子": "bag", "塑料袋": "plastic bag",
    "面单": "shipping label", "条码": "barcode", "标签": "label",
    "方块": "cube", "零件": "part", "物体": "object",
}


def _dino_prompt_for(classes: list[str]) -> list[str]:
    """把数据集类别自动转成 Grounding DINO 的英文提示词，不需要人工填写。"""
    out = []
    for c in classes:
        s = str(c).strip()
        if not s:
            continue
        if all(ord(ch) < 128 for ch in s):
            out.append(s)
            continue
        t = s.replace("色", "")
        for zh, en in sorted(_DINO_WORDS.items(), key=lambda kv: -len(kv[0])):
            t = t.replace(zh, " " + en + " ")
        t = " ".join(t.split())
        colors = {"black", "red", "blue", "green", "yellow", "white",
                  "gray", "orange", "purple", "brown", "pink", "cyan"}
        if t in colors:
            t = t + " object"
        out.append(t or s)
    return out


DINO_PY = "/home/legion/miniconda3/envs/lerobot/bin/python"
DINO_WORKER = ROOT / "backend" / "grounding_dino_worker.py"


@app.post("/api/ai/grounding")
def api_ai_grounding(payload: Optional[dict] = Body(default=None)):
    """本地 Grounding DINO：文字提示检测，写当前图片的 YOLO 标注。"""
    e = payload or {}
    p = _yolo_require(str(e.get("dataset") or ""))
    split = str(e.get("split") or "train")
    _yolo_split(p, split)
    fn = _yolo_check_file(str(e.get("file") or ""))
    img = p / "images" / split / fn
    if not img.is_file():
        raise ApiError("图片不存在", 404)
    classes = _yolo_classes(p)
    if not classes:
        raise ApiError("数据集还没有类别，先添加类别", 400)
    prompt_classes = [str(c).strip() for c in (e.get("prompt_classes") or []) if str(c).strip()]
    if not prompt_classes:
        prompt_classes = _dino_prompt_for(classes)
    prompt = ". ".join(prompt_classes) + "."
    try:
        proc = subprocess.run(
            [DINO_PY, str(DINO_WORKER), str(img), prompt,
             str(float(e.get("conf") or 0.3))],
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise ApiError("Grounding DINO 推理超时", 504)
    lines = [x for x in (proc.stdout or "").strip().splitlines() if x.strip()]
    if not lines:
        raise ApiError("Grounding DINO 无输出：" + (proc.stderr or "")[-400:], 500)
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ApiError("Grounding DINO 输出无法解析：" + lines[-1][:200], 500)
    raw = data.get("objects") or []
    try:
        max_boxes = int(e.get("max_boxes") or 100)
    except (TypeError, ValueError):
        max_boxes = 100
    raw = sorted(raw, key=lambda it: float(it.get("score") or 0), reverse=True)[:max_boxes]
    if not raw:
        raise ApiError("Grounding DINO 没有检测到目标", 400)
    matched, skipped = [], []
    for it in raw:
        label = str(it.get("label") or "").strip()
        cls = ""
        for i, pc in enumerate(prompt_classes):
            if pc and (pc.lower() in label.lower() or label.lower() in pc.lower()):
                cls = classes[i] if i < len(classes) else ""
                break
        if not cls:
            cls = _match_class(label, classes)
        if cls:
            matched.append({"class": cls, "bbox": it["bbox"], "score": it.get("score")})
        else:
            skipped.append(label)
    if not matched:
        raise ApiError("检测到的类别都不在数据集里：" + "、".join(skipped[:6]), 400)
    lines_out = []
    for it in matched:
        cid = classes.index(it["class"])
        x1, y1, x2, y2 = [float(v) for v in it["bbox"]]
        cx, cy, bw, bh = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1), abs(y2 - y1)
        lines_out.append(f"{cid} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    text = "\n".join(lines_out) + "\n"
    lf = p / "labels" / split / (Path(fn).stem + ".txt")
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(text, "utf-8")
    if split == "train":
        img_dst = p / "images" / "val" / fn
        lbl_dst = p / "labels" / "val" / lf.name
        if img_dst.parent.is_dir():
            shutil.copy2(img, img_dst)
        if lbl_dst.parent.is_dir():
            shutil.copy2(lf, lbl_dst)
    HUB.log(f"[DINO] {p.name}/{fn} -> {len(matched)} 个框", "ok")
    return {"ok": True, "objects": matched, "skipped": skipped,
            "count": len(matched), "text": text, "prompt": prompt}


@app.post("/api/ai/grounding-batch")
def api_ai_grounding_batch(payload: Optional[dict] = Body(default=None)):
    """批量运行本地 Grounding DINO 预标注（默认仅标注未标注图片，也可标注当前划分全部图片）。"""
    e = payload or {}
    ds = str(e.get("dataset") or "")
    p = _yolo_require(ds)
    split = str(e.get("split") or "train")
    d = _yolo_split(p, split)
    classes = _yolo_classes(p)
    if not classes:
        raise ApiError("数据集还没有类别，请先添加类别", 400)
    only_unlabeled = bool(e.get("only_unlabeled", True))
    files = sorted([f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMG_EXTS]) if d.is_dir() else []
    if only_unlabeled:
        files = [fn for fn in files if not (p / "labels" / split / (Path(fn).stem + ".txt")).is_file() or not (p / "labels" / split / (Path(fn).stem + ".txt")).read_text("utf-8", errors="ignore").strip()]
    if not files:
        return {"ok": True, "total": 0, "succeeded": 0, "failed": 0, "total_boxes": 0, "note": "当前没有待标注的图片"}
    conf = float(e.get("conf") or 0.2)
    max_boxes = int(e.get("max_boxes") or 100)
    succeeded, failed, total_boxes = 0, 0, 0
    errors = []
    for fn in files[:50]:
        try:
            r = api_ai_grounding({"dataset": ds, "split": split, "file": fn, "conf": conf, "max_boxes": max_boxes})
            succeeded += 1
            total_boxes += int(r.get("count") or 0)
        except Exception as exc:
            failed += 1
            if len(errors) < 10:
                errors.append(f"{fn}: {_brief(exc)}")
    HUB.log(f"[DINO批量] {ds}/{split}: 成功 {succeeded} 张（共 {total_boxes} 框），未检出/失败 {failed} 张", "ok")
    return {"ok": True, "total": len(files[:50]), "succeeded": succeeded, "failed": failed, "total_boxes": total_boxes, "errors": errors}


ARM_PY = "/home/legion/miniconda3/envs/rebotarm/bin/python"
ARM_DAEMON = ROOT / "backend" / "arm_daemon.py"

_ARM_PROC: Optional[subprocess.Popen] = None
_ARM_LOCK = threading.Lock()
_ARM_SEQ = 0


def _arm_send(action: str, payload: Optional[dict] = None, timeout: float = 90.0) -> dict:
    """向常驻从臂守护进程发指令；进程不在就先拉起。"""
    global _ARM_PROC, _ARM_SEQ
    with _ARM_LOCK:
        if DEVICES_RELEASED:raise ApiError('设备已断开，请先重新连接设备',409)
        if _ARM_PROC is None or _ARM_PROC.poll() is not None:
            if DEVICES_RELEASED:
                raise ApiError('设备已断开，请先重新连接设备',409)
            if STATE.running():
                raise ApiError('机器人任务正在运行，不能同时启动从臂控制',409)
            TELEMETRY.stop_observers()
            # External owners are released only by the explicit global button.
            from backend.device_release import owners
            busy = owners(list(monitor_ports(load_config()).values()))
            if busy:
                raise ApiError('串口已被其他进程占用，请使用顶部“断开全部设备”释放后重新连接',409)
            try:
                _ARM_PROC = subprocess.Popen(
                    [ARM_PY, "-u", str(ARM_DAEMON)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    bufsize=1, env=dict(os.environ, **TELEMETRY.environment(monitor_ports(load_config()))))
                # 等就绪
                deadline = time.time() + 30
                ready = False
                while time.time() < deadline:
                    line = _ARM_PROC.stdout.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.strip())
                    except json.JSONDecodeError:
                        if line.strip():
                            HUB.log(f"[arm_daemon] {line.strip()[:200]}", "warn")
                        continue
                    if msg.get("event") == "ready":
                        ready = True
                        break
                    if msg.get("ok") is False:
                        raise ApiError("从臂连接失败：" + str(msg.get("error"))[:300], 503)
                if not ready:
                    raise ApiError("从臂守护进程启动超时（串口被占用或未连接）", 504)
            except ApiError:
                _ARM_PROC = None
                raise
            except Exception as exc:  # noqa: BLE001
                _ARM_PROC = None
                raise ApiError(f"从臂守护进程启动失败：{_brief(exc)}", 500)

        _ARM_SEQ += 1
        rid = _ARM_SEQ
        try:
            _ARM_PROC.stdin.write(json.dumps(
                {"id": rid, "action": action, "payload": payload or {}},
                ensure_ascii=False) + "\n")
            _ARM_PROC.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            _ARM_PROC = None
            raise ApiError(f"从臂守护进程通信失败：{_brief(exc)}", 500)

        deadline = time.time() + timeout
        while time.time() < deadline:
            line = _ARM_PROC.stdout.readline()
            if not line:
                _ARM_PROC = None
                raise ApiError("从臂守护进程已退出", 500)
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                if line.strip():
                    HUB.log(f"[arm_daemon] {line.strip()[:200]}", "warn")
                continue
            if msg.get("id") != rid:
                continue
            if not msg.get("ok"):
                try:
                    HUB.log(f"[arm_daemon] 失败回复：{json.dumps(msg, ensure_ascii=False, default=str)[:400]}", "warn")
                except Exception:
                    HUB.log(f"[arm_daemon] 失败回复（原始）: {line.strip()[:400]}", "warn")
                raise ApiError("从臂指令失败：" + str(msg.get("error") or "原因未知")[:300], 500)
            return msg
        raise ApiError("从臂指令超时", 504)


@app.post("/api/arm/state")
def api_arm_state():
    return _arm_send("state")


@app.post("/api/arm/jog")
def api_arm_jog(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    return _arm_send("jog", {"index": int(e.get("index", 0)),
                                "delta_deg": float(e.get("delta_deg", 0.0))})


@app.post("/api/arm/gripper")
def api_arm_gripper(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    target = e.get("target")
    if target in ("open", "close"):
        pass
    else:
        target = float(target)
    payload = {"target": target}
    if e.get("torque_ratio") is not None:
        try:
            payload["torque_ratio"] = max(0.01, min(1.0, float(e["torque_ratio"])))
        except (TypeError, ValueError):
            pass
    return _arm_send("gripper", payload)


@app.post("/api/arm/pose")
def api_arm_pose(payload: Optional[dict] = Body(default=None)):
    e = payload or {}
    return _arm_send("pose", {
        "x": float(e["x"]), "y": float(e["y"]), "z": float(e["z"]),
        "roll": float(e.get("roll", 0.0)), "pitch": float(e.get("pitch", 0.0)),
        "yaw": float(e.get("yaw", 0.0)), "duration": float(e.get("duration", 2.0)),
    }, timeout=120)


@app.post("/api/arm/close")
def api_arm_close():
    global _ARM_PROC
    with _ARM_LOCK:
        if _ARM_PROC is None or _ARM_PROC.poll() is not None:
            _ARM_PROC = None
            return {"ok": True, "closed": False, "note": "本来就没有连接"}
        try:
            _ARM_PROC.stdin.write(json.dumps({"id": -1, "action": "quit", "payload": {}}) + "\n")
            _ARM_PROC.stdin.flush()
            deadline = time.time() + 30
            while time.time() < deadline:
                line = _ARM_PROC.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if msg.get("closed") or msg.get("bye"):
                    break
        except Exception:
            pass
        finally:
            try:
                _ARM_PROC.terminate()
            except Exception:
                pass
            _ARM_PROC = None
    HUB.log("[arm] 已收工：机械臂回零并卸力，串口已释放", "warn")
    return {"ok": True, "closed": True}


@app.post("/api/arm/home")
def api_arm_home():
    return _arm_send("home", None, timeout=60)


@app.post("/api/arm/hold")
def api_arm_hold():
    return _arm_send("hold")


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    q = HUB.subscribe()
    try:
        for ev in HUB.snapshot(300):
            await ws.send_json(ev)
        await ws.send_json({"type": "hello",
                            "leader_type": LEADER_TYPE, "follower_type": FOLLOWER_TYPE,
                            "env_ready": bool(ENV.active_env), "connected": ENV.connected})
        while True:
            await ws.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001
        HUB.log(f"[ws] 推送异常：{_brief(exc)}", "warn")
    finally:
        HUB.unsubscribe(q)


if __name__ == "__main__":
    import uvicorn
    print(f"打开 http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
