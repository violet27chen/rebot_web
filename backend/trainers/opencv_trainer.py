#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCV 传统经典视觉分类模型真实训练引擎 (OpenCV Classical Vision Trainer)
========================================================================

【模块背景与定位】
在工业分拣与初级物品识别中，部分边缘计算板卡（如无独立 GPU 的工控机）
不具备部署大型深度学习模型的算力。
本模块提供真实、确定性且轻量高效的传统计算机视觉特征提取与分类器训练方案，
绝不伪造任何模拟数据，完全基于真实图像计算。

【特征工程与算法】
1. 特征融合（定长特征向量）：
   - HSV 颜色直方图：对色调 H (18 bins) 与饱和度 S (8 bins) 统计二维色彩分布并归一化；
   - 梯度方向直方图（HOG-like）：利用 Sobel 算子计算图像边缘梯度幅度与方向，
     筛选强梯度边缘点并统计 9 个角度区间的方向分布，表征物体外形轮廓；
   - 低分辨率灰度纹理：将灰度图降采样为 16x16（256维）直接拼接，捕捉全局明暗分布。
2. 分类器支持：
   - cv2.ml.SVM (支持 LINEAR、RBF 等核函数)；
   - cv2.ml.KNearest (KNN 最近邻分类器)。
3. 数据集结构要求：按类别划分的标准图像子文件夹：
       <dataset>/
           class_a/*.jpg
           class_b/*.png
4. 输出物：
   - <output>/model.xml: 训练好的 OpenCV 模型参数文件；
   - <output>/labels.json: 类别索引标签映射与特征维度元数据。
5. 通信协议：
   向 stdout 输出标准 JSON 格式指标行：
   `{"type": "metric", "message": ..., "step": N, "loss": ..., "accuracy": ...}`
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

# 支持的常见图像扩展名集合
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def emit(message: str, **kw) -> None:
    """向标准输出打印一条指标/进度 JSON 行并立即刷新输出缓冲区。"""
    print(json.dumps({"type": "metric", "message": message, **kw}, ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2) -> None:
    """输出错误结构体，打印诊断信息并以非零状态码退出。"""
    print(json.dumps({"type": "error", "message": message}, ensure_ascii=False), flush=True)
    print(f"[错误] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def extract_features(img, size: int, cv2, np):
    """
    从单张真实图像中提取融合特征向量（颜色 + 边缘梯度 + 纹理）。

    Args:
        img: OpenCV BGR 原始图像矩阵。
        size (int): 预处理缩放基准边长（如 128）。
        cv2: OpenCV 模块。
        np: NumPy 模块。

    Returns:
        np.ndarray: 扁平化的一维 float32 特征向量。
    """
    im = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    # 特征 1：HSV 二维色彩直方图 (18x8 = 144 维)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)

    # 特征 2：Sobel 边缘梯度方向直方图 HOG-like (9 维)
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    strong = mag > max(float(np.percentile(mag, 60)), 1e-6)
    hog, _ = np.histogram(ang[strong], bins=9, range=(0, 360))
    hog = hog.astype("float32")
    hog /= float(hog.sum() + 1e-6)

    # 特征 3：低频灰度纹理 (16x16 = 256 维)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA).astype("float32").ravel() / 255.0

    return np.concatenate([hist.ravel().astype("float32"), hog, small])


def load_dataset(dataset_dir: Path, image_size: int, cv2, np):
    """
    扫描数据集目录，加载所有子分类图像并提取特征矩阵。

    Args:
        dataset_dir (Path): 数据集根目录路径。
        image_size (int): 图像采样缩放尺寸。
        cv2, np: 依赖库。

    Returns:
        tuple: (特征矩阵 X, 标签向量 y, 类别名称列表)。
    """
    subdirs = sorted([d for d in dataset_dir.iterdir() if d.is_dir() and not d.name.startswith(".")])
    if len(subdirs) < 2:
        fail(f"数据集目录至少需要 2 个类别子目录（找到 {len(subdirs)} 个）。\n"
             f"请把图片按类别整理在子目录下，例如：{dataset_dir}/类别A/*.jpg")

    class_names = [d.name for d in subdirs]
    X_list, y_list = [], []
    per_class_counts = {}

    for label, d in enumerate(subdirs):
        files = sorted([p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS])
        per_class_counts[d.name] = len(files)
        for p in files:
            im = cv2.imread(str(p))
            if im is None or im.size == 0:
                continue
            feat = extract_features(im, image_size, cv2, np)
            X_list.append(feat)
            y_list.append(label)

    total_valid = len(X_list)
    breakdown = ", ".join(f"{k}:{v}" for k, v in per_class_counts.items())
    emit(f"发现 {len(class_names)} 个类别，共 {total_valid} 张有效样本（{breakdown}）", step=0)

    if total_valid < 4:
        fail(f"有效图片总数仅 {total_valid} 张，无法进行有效训练（至少需要 4 张真实图片）")

    X = np.asarray(X_list, dtype="float32")
    y = np.asarray(y_list, dtype="int32")
    return X, y, class_names


def build_classifier(kind: str, cv2):
    """根据参数构建 OpenCV 机器学习分类器实例。"""
    kind = kind.lower()
    if kind == "svm":
        clf = cv2.ml.SVM_create()
        clf.setType(cv2.ml.SVM_C_SVC)
        clf.setKernel(cv2.ml.SVM_LINEAR)
        clf.setC(1.0)
        return clf
    if kind == "knn":
        clf = cv2.ml.KNearest_create()
        clf.setDefaultK(3)
        clf.setIsClassifier(True)
        return clf
    fail(f"不支持的分类器类型：{kind}（支持 svm / knn）")


def train_and_eval(clf, X, y, cv2):
    """训练分类器并计算训练集准确率与经验误差。"""
    clf.train(X, cv2.ml.ROW_SAMPLE, y)
    _ret, preds = clf.predict(X)
    preds = preds.ravel().astype("int32")
    acc = float((preds == y).mean())
    loss = float(max(0.0, 1.0 - acc))
    return acc, loss


def main() -> None:
    """命令行训练入口：解析参数、读取数据集、分轮拟合与导出模型。"""
    ap = argparse.ArgumentParser(description="OpenCV 传统图像分类模型训练脚本")
    ap.add_argument("--dataset", required=True, help="数据集根目录")
    ap.add_argument("--output", required=True, help="模型导出目录")
    ap.add_argument("--classifier", default="svm", choices=["svm", "knn"], help="分类器类型")
    ap.add_argument("--image-size", type=int, default=128, help="特征预处理边长")
    ap.add_argument("--epochs", type=int, default=10, help="训练输出报告轮次")
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        fail(f"导入 cv2 / numpy 失败：{exc}")

    dataset_dir = Path(args.dataset).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    emit(f"启动 OpenCV 训练 —— 引擎：{args.classifier.upper()}，数据集：{dataset_dir}", step=0)
    X, y, class_names = load_dataset(dataset_dir, args.image_size, cv2, np)

    epochs = max(1, min(args.epochs, 50))
    n_samples = len(X)
    emit(f"开始分轮训练（共 {epochs} 轮），特征维度：{X.shape[1]}", step=0)

    # 按照样本子集逐步扩增分轮训练，向前端输出真实的准确率与损失变化曲线
    for epoch in range(1, epochs + 1):
        ratio = epoch / float(epochs)
        sub_n = max(len(class_names), int(round(n_samples * ratio)))
        sub_X = X[:sub_n]
        sub_y = y[:sub_n]

        clf = build_classifier(args.classifier, cv2)
        try:
            acc, loss = train_and_eval(clf, sub_X, sub_y, cv2)
        except Exception as exc:  # noqa: BLE001
            fail(f"第 {epoch} 轮拟合失败：{exc}\n{traceback.format_exc()}")

        emit(
            f"Epoch {epoch}/{epochs} (样本 {sub_n}/{n_samples}) -> "
            f"train_loss={loss:.4f}, accuracy={acc * 100:.1f}%",
            step=epoch,
            loss=loss,
            accuracy=round(acc, 4),
        )

    # 在全量训练样本上最终训练最终模型
    final_clf = build_classifier(args.classifier, cv2)
    final_acc, final_loss = train_and_eval(final_clf, X, y, cv2)

    model_path = out_dir / "model.xml"
    labels_path = out_dir / "labels.json"
    final_clf.save(str(model_path))

    labels_data = {
        "classes": class_names,
        "classifier": args.classifier,
        "feature_dim": int(X.shape[1]),
        "image_size": args.image_size,
        "train_samples": n_samples,
        "final_accuracy": final_acc,
    }
    labels_path.write_text(json.dumps(labels_data, ensure_ascii=False, indent=2), encoding="utf-8")

    emit(
        f"训练完成！模型已导出到：{model_path}，最终准确率：{final_acc * 100:.1f}%",
        step=epochs,
        loss=final_loss,
        accuracy=round(final_acc, 4),
        saved_model=str(model_path),
    )


if __name__ == "__main__":
    main()
