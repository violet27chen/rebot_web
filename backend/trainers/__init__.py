# -*- coding: utf-8 -*-
"""
具身智能与计算机视觉训练引擎统一注册表 (Training Engines Registry)
========================================================================

【模块背景与定位】
平台支持多层次、多模态的视觉与机械臂动作策略训练任务：
1. "lerobot": 针对机械臂模仿学习与行为克隆（ACT、Diffusion Policy、SmolVLA 等）的大模型训练；
2. "yolo": 基于 Ultralytics YOLO 架构的目标检测与实例分割（物料识别、快递包裹检测）模型训练；
3. "opencv": 基于经典计算机视觉特征（HSV 色彩直方图 + 梯度方向直方图 HOG + 灰度纹理 + SVM/KNN）的
   超轻量级视觉分类器训练，适合无 GPU 的轻量边缘端部署。

本模块作为训练引擎的统一分发注册表，由 `backend/main.py` 的 `train_command()` 依据用户所选的
引擎类型动态调用对应的可执行脚本，不将系统硬性绑定在单一实现上。
"""
from . import opencv_trainer  # noqa: F401
from . import yolo_trainer    # noqa: F401

# 系统支持的真实训练引擎类型元组
ENGINES = ("lerobot", "yolo", "opencv")
