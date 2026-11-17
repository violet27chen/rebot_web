#!/usr/bin/env python3
"""
Grounding DINO 零样本开集目标检测推理 Worker 进程 (Grounding DINO Object Detection)
====================================================================================

【模块背景与定位】
在视觉分拣与机械臂抓取任务中，传统的闭集目标检测器（如仅识别固定 80 类物体的 COCO 模型）
无法识别任意自定义物品（例如：“绿色的快递袋”、“红色的圆柱体”）。
本模块作为独立的 CLI 子进程，基于 HuggingFace Transformers 实现的 Grounding DINO 开集模型，
通过自然语言文本提示词（Text Prompt）实现零样本目标检测定位。

【运行机制与特性】
1. 强制离线模式（Offline Guarantee）：
   在导入前显式配置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，
   完全使用本地磁盘缓存的权重文件，避免每次调用时请求 HuggingFace 网站引发网络挂起。
2. 设备自适应：
   优先使用 CUDA GPU 加速，无显卡时自动回退到 CPU 推理。
3. 归一化坐标输出：
   后处理将绝对像素坐标框转换为标准相对归一化矩形框 [x1/W, y1/H, x2/W, y2/H]，
   并向标准输出输出包含 `objects: [{label, score, bbox}]` 的单行 JSON。
"""
import json
import os
import sys

# 模型已在本地环境缓存：强制完全离线运行，严禁联网检查模型更新导致超时
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection

# 默认使用的轻量化 Grounding DINO 预训练权重标识符
MODEL_ID = "IDEA-Research/grounding-dino-tiny"


def main() -> None:
    """
    Worker 进程主入口：
    接收三个命令行参数：
        sys.argv[1]: 待检测图像本地路径 (image_path)
        sys.argv[2]: 自然语言目标描述词 (prompt, 如 'express box. plastic bottle.')
        sys.argv[3]: 检测置信度阈值 (threshold, 默认 0.3)
    """
    image_path = sys.argv[1]
    prompt = sys.argv[2]
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3

    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID).to(device)

    # 读取输入图像并确保为 RGB 格式
    image = Image.open(image_path).convert("RGB")
    inputs = processor(images=image, text=prompt, return_tensors="pt").to(device)

    # 执行模型前向推理
    with torch.no_grad():
        outputs = model(**inputs)

    # 后处理提取符合置信度阈值的检测框
    result = processor.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        threshold=threshold,
        text_threshold=0.25,
        target_sizes=[image.size[::-1]],  # [H, W]
    )[0]

    width, height = image.size
    objects = []
    for box, score, label in zip(result["boxes"], result["scores"], result["labels"]):
        x1, y1, x2, y2 = [float(v) for v in box]
        # 归一化至 [0, 1] 相对图像宽高比例
        objects.append({
            "label": str(label),
            "score": float(score),
            "bbox": [x1 / width, y1 / height, x2 / width, y2 / height]
        })

    # 输出结构化结果供上位机调用者捕获
    print(json.dumps({"objects": objects}, ensure_ascii=False))


if __name__ == "__main__":
    main()
