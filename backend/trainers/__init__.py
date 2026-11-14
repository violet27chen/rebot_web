# -*- coding: utf-8 -*-
"""训练引擎注册表：LeRobot / YOLO / OpenCV。

每个引擎对应一份真实可运行的训练代码，由 backend/main.py 的
train_command() 按用户选择分发，不写死单一实现。
"""
from . import opencv_trainer  # noqa: F401
from . import yolo_trainer    # noqa: F401

ENGINES = ("lerobot", "yolo", "opencv")
