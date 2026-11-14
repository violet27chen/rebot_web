#!/usr/bin/env python3
"""用 Grounding DINO（Transformers 版）做文字提示检测，输出归一化框 JSON。"""
import json
import os
import sys

# 模型已缓存：强制离线，避免每次联网检查 huggingface 卡住
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection

MODEL_ID = "IDEA-Research/grounding-dino-tiny"


def main() -> None:
    image_path = sys.argv[1]
    prompt = sys.argv[2]
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3
    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID).to(device)
    image = Image.open(image_path).convert("RGB")
    inputs = processor(images=image, text=prompt, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
    result = processor.post_process_grounded_object_detection(
        outputs, inputs.input_ids, threshold=threshold, text_threshold=0.25,
        target_sizes=[image.size[::-1]],
    )[0]
    width, height = image.size
    objects = []
    for box, score, label in zip(result["boxes"], result["scores"], result["labels"]):
        x1, y1, x2, y2 = [float(v) for v in box]
        objects.append({"label": str(label), "score": float(score),
                        "bbox": [x1 / width, y1 / height, x2 / width, y2 / height]})
    print(json.dumps({"objects": objects}, ensure_ascii=False))


if __name__ == "__main__":
    main()
