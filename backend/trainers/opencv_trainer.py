#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCV 训练引擎 —— 真实经典视觉训练（不产生任何模拟数据）
==========================================================
算法：HSV 颜色直方图 + 梯度方向直方图(HOG-like) + 低分辨率灰度特征
      -> cv2.ml.SVM (LINEAR) 或 cv2.ml.KNearest 分类器
      -> 按训练样本比例逐轮重训，输出真实的训练准确率曲线

数据集目录结构（必须按类别分子目录）：
    <dataset>/
        class_a/*.jpg
        class_b/*.png
        ...

输出：
    <output>/model.xml          训练好的 OpenCV 模型
    <output>/labels.json        类别名与特征维度

stdout 每行一个 JSON：{"type":"metric","message":...,"step":N,"loss":...,"accuracy":...}
后端会解析成 Loss 曲线与日志；错误以 {"type":"error"} 输出并返回非零退出码。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def emit(message: str, **kw) -> None:
    print(json.dumps({"type": "metric", "message": message, **kw},
                     ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2) -> None:
    print(json.dumps({"type": "error", "message": message}, ensure_ascii=False), flush=True)
    print(f"[错误] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def extract_features(img, size: int, cv2, np):
    """从一张真实图片提取定长特征向量。"""
    im = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    strong = mag > max(float(np.percentile(mag, 60)), 1e-6)
    hog, _ = np.histogram(ang[strong], bins=9, range=(0, 360))
    hog = hog.astype("float32")
    hog /= float(hog.sum() + 1e-6)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA).astype("float32").ravel() / 255.0
    return np.concatenate([hist.ravel().astype("float32"), hog, small])


def build_dataset(root: Path, size: int, cv2, np):
    classes = sorted([d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")])
    if not classes:
        fail(f"数据集目录中没有类别子目录：{root}\n"
             "OpenCV 引擎要求 <dataset>/<类别名>/*.jpg 的结构")
    features, labels, counts = [], [], {}
    for ci, cls in enumerate(classes):
        files = [f for f in sorted(cls.iterdir()) if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
        if not files:
            fail(f"类别「{cls.name}」下没有可用图片（支持 {', '.join(sorted(IMAGE_EXTS))}）")
        good = 0
        for f in files:
            try:
                img = cv2.imread(str(f))
                if img is None:
                    continue
                features.append(extract_features(img, size, cv2, np))
                labels.append(ci)
                good += 1
            except Exception as exc:  # noqa: BLE001
                emit(f"跳过无法处理的图片 {f.name}：{exc}")
        if good == 0:
            fail(f"类别「{cls.name}」的图片全部无法解码，请检查文件是否损坏")
        counts[cls.name] = good
    if len(classes) < 2:
        fail("至少要有 2 个类别才能训练分类器，当前只有 1 个类别目录")
    return classes, np.float32(features), np.int32(labels), counts


def fit_and_score(X, y, classifier: str, cv2, np):
    if classifier == "svm":
        model = cv2.ml.SVM_create()
        model.setType(cv2.ml.SVM_C_SVC)
        model.setKernel(cv2.ml.SVM_LINEAR)
        model.setC(2.67)
        model.setGamma(5.383)
        model.setTermCriteria((cv2.TERM_CRITERIA_MAX_ITER + cv2.TERM_CRITERIA_EPS, 1000, 1e-6))
        model.train(X, cv2.ml.ROW_SAMPLE, y)
        _, pred = model.predict(X)
    else:
        model = cv2.ml.KNearest_create()
        model.train(X, cv2.ml.ROW_SAMPLE, y)
        _, pred, _, _ = model.findNearest(X, k=min(3, len(X)))
    acc = float((pred.ravel().astype("int32") == y).mean())
    return model, acc


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenCV 经典视觉训练")
    ap.add_argument("--dataset", required=True, help="按类别分子目录的图片数据集")
    ap.add_argument("--output", required=True, help="模型输出目录")
    ap.add_argument("--epochs", type=int, default=20, help="训练轮次（学习曲线采样轮数）")
    ap.add_argument("--classifier", choices=["svm", "knn"], default="svm")
    ap.add_argument("--img-size", type=int, default=64)
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        fail(f"缺少 OpenCV 训练依赖：{exc}。请在所选 conda 环境执行："
             "pip install opencv-python numpy -i https://pypi.tuna.tsinghua.edu.cn/simple")

    if args.epochs < 1 or args.epochs > 500:
        fail("--epochs 必须在 1~500 之间")
    if args.img_size < 16 or args.img_size > 512:
        fail("--img-size 必须在 16~512 之间")

    root = Path(args.dataset).expanduser()
    if not root.is_dir():
        fail(f"数据集目录不存在：{root}")
    out = Path(args.output).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建输出目录 {out}：{exc}")

    emit(f"OpenCV 引擎启动：分类器={args.classifier}  特征尺寸={args.img_size}", step=0)
    classes, X, y, counts = build_dataset(root, args.img_size, cv2, np)
    emit(f"真实数据集：{len(classes)} 个类别 / {len(X)} 张图片 / 特征维度 {X.shape[1]}",
         step=0, samples=len(X))
    emit("类别分布：" + ", ".join(f"{k}={v}" for k, v in counts.items()), step=0)

    rng = np.random.default_rng(42)
    order = rng.permutation(len(X))
    X, y = X[order], y[order]

    model, last_acc = None, 0.0
    for epoch in range(1, args.epochs + 1):
        n = max(len(classes), int(len(X) * epoch / args.epochs))
        xb, yb = X[:n], y[:n]
        if len(np.unique(yb)) < 2:
            emit(f"轮次 {epoch}/{args.epochs}  样本={n}  子集仅含单一类别，跳过本轮", step=epoch)
            continue
        model, acc = fit_and_score(xb, yb, args.classifier, cv2, np)
        last_acc = acc
        loss = 1.0 - acc
        emit(f"轮次 {epoch}/{args.epochs}  样本={n}  训练准确率={acc:.4f}  误差={loss:.4f}",
             step=epoch, loss=round(float(loss), 6), accuracy=round(float(acc), 6), samples=n)

    try:
        if model is None:
            fail("训练未能产生有效模型：子集始终只含单一类别，请增加样本数量或打乱类别分布")
        model_path = out / "model.xml"
        if not model.save(str(model_path)):
            # cv2.ml.StatModel.save 返回 None 也属正常，用文件存在性确认
            if not model_path.exists():
                fail(f"模型保存失败：{model_path}")
        meta = {"engine": "opencv", "classifier": args.classifier,
                "classes": [c.name for c in classes], "feature_dim": int(X.shape[1]),
                "samples": int(len(X)), "epochs": args.epochs,
                "train_accuracy": round(float(last_acc), 6)}
        (out / "labels.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(f"保存模型失败：{exc}")

    emit(f"训练完成，模型已保存：{out / 'model.xml'}（最终训练准确率 {last_acc:.4f}）",
         step=args.epochs, loss=round(float(1.0 - last_acc), 6))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"type": "error", "message": f"训练过程异常：{exc}"},
                         ensure_ascii=False), flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
