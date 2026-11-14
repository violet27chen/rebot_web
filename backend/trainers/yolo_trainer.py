#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO 训练引擎 —— 真实 Ultralytics 训练（不产生任何模拟数据）
============================================================
使用 Ultralytics YOLO Python API 训练目标检测模型，
通过 on_fit_epoch_end 回调把每个 epoch 的真实 loss / mAP 推给网页。

数据集（二选一）：
  1) --data /path/to/data.yaml          标准 YOLO 标注文件
  2) --auto-yaml /path/to/dataset       自动从 YOLO 目录结构生成 data.yaml：
        <dataset>/images/train, images/val
        <dataset>/labels/train, labels/val
        <dataset>/classes.txt （可选，每行一个类别名）

stdout 每行一个 JSON：{"type":"metric","message":...,"step":N,"loss":...,"map50":...}
后端解析成 Loss 曲线与日志；错误以 {"type":"error"} 输出并返回非零退出码。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path


def emit(message: str, **kw) -> None:
    print(json.dumps({"type": "metric", "message": message, **kw},
                     ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2) -> None:
    print(json.dumps({"type": "error", "message": message}, ensure_ascii=False), flush=True)
    print(f"[错误] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def resolve_data_yaml(raw: str, auto: str, out: Path) -> str:
    if raw:
        p = Path(raw).expanduser()
        if not p.is_file():
            fail(f"data.yaml 不存在：{p}")
        return str(p.resolve())

    root = Path(auto).expanduser()
    if not root.is_dir():
        fail(f"数据集目录不存在：{root}")
    for candidate in (root / "data.yaml", root / "data.yml"):
        if candidate.is_file():
            emit(f"使用数据集自带的标注文件：{candidate}", step=0)
            return str(candidate.resolve())

    images_train, images_val = root / "images" / "train", root / "images" / "val"
    labels_train, labels_val = root / "labels" / "train", root / "labels" / "val"
    missing = [str(p) for p in (images_train, images_val, labels_train, labels_val)
               if not p.is_dir()]
    if missing:
        fail("数据集不是 YOLO 结构，缺少目录：\n  " + "\n  ".join(missing) +
             "\n需要 images/train、images/val、labels/train、labels/val，"
             "或提供 --data 指向已有的 data.yaml")
    if not any(images_train.iterdir()):
        fail(f"训练图片目录为空：{images_train}")

    classes_file = root / "classes.txt"
    if classes_file.is_file():
        names = [x.strip() for x in classes_file.read_text("utf-8").splitlines() if x.strip()]
    else:
        ids = set()
        for f in labels_train.glob("*.txt"):
            try:
                for line in f.read_text("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        ids.add(int(line.split()[0]))
            except (ValueError, IndexError, OSError):
                continue
        if not ids:
            fail("无法从标签推断类别，请在数据集目录放置 classes.txt（每行一个类别名）")
        names = [f"class_{i}" for i in range(max(ids) + 1)]

    val_split = "images/val"
    if not any(images_val.iterdir()):
        emit("验证集为空：临时用训练集作为验证集（只用于跑通训练，评估指标会偏乐观）", step=0)
        val_split = "images/train"
    payload = {"path": str(root.resolve()), "train": "images/train",
               "val": val_split, "names": {i: n for i, n in enumerate(names)}}
    target = out / "data.yaml"
    try:
        try:
            import yaml  # Ultralytics 自带依赖
            text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        except ImportError:
            lines = [f"path: {payload['path']}", "train: images/train", "val: images/val",
                     "names:"]
            lines += [f"  {i}: {n}" for i, n in enumerate(names)]
            text = "\n".join(lines) + "\n"
        target.write_text(text, "utf-8")
    except OSError as exc:
        fail(f"生成 data.yaml 失败：{exc}")
    emit(f"已自动生成标注文件：{target}（{len(names)} 个类别）", step=0)
    return str(target.resolve())


def main() -> None:
    ap = argparse.ArgumentParser(description="YOLO 目标检测训练")
    ap.add_argument("--data", default="", help="YOLO data.yaml 路径")
    ap.add_argument("--auto-yaml", default="", help="数据集目录，自动生成 data.yaml")
    ap.add_argument("--model", default="yolov8n.pt", help="预训练权重或本地 .pt 路径")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--device", default="cpu", help="cpu / 0 / cuda:0")
    ap.add_argument("--output", default="outputs/train/yolo")
    args = ap.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        fail(f"缺少 YOLO 训练依赖 ultralytics：{exc}。请在所选 conda 环境执行："
             "pip install ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple")

    if not args.data and not args.auto_yaml:
        fail("请提供 --data（data.yaml）或 --auto-yaml（数据集目录）之一")
    if args.epochs < 1 or args.epochs > 1000:
        fail("--epochs 必须在 1~1000 之间")

    out = Path(args.output).expanduser().resolve()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建输出目录 {out}：{exc}")

    data_yaml = resolve_data_yaml(args.data, args.auto_yaml, out)
    emit(f"YOLO 引擎启动：model={args.model}  epochs={args.epochs}  "
         f"imgsz={args.imgsz}  batch={args.batch}  device={args.device}", step=0)
    emit(f"标注文件：{data_yaml}", step=0)

    try:
        model = YOLO(args.model)
    except Exception as exc:  # noqa: BLE001
        fail(f"加载模型「{args.model}」失败：{exc}。"
             "若为在线权重请检查网络，或改用本地 .pt 文件绝对路径")

    def on_fit_epoch_end(trainer) -> None:
        try:
            epoch = int(getattr(trainer, "epoch", 0)) + 1
            total = int(getattr(trainer, "epochs", args.epochs))
            loss_val = None
            tloss = getattr(trainer, "tloss", None)
            if tloss is not None:
                try:
                    loss_val = float(tloss.mean())
                except Exception:  # noqa: BLE001
                    loss_val = None
            metrics = getattr(trainer, "metrics", {}) or {}
            map50 = metrics.get("metrics/mAP50(B)")
            extra = {}
            if loss_val is not None:
                extra["loss"] = round(loss_val, 6)
            if map50 is not None:
                try:
                    extra["map50"] = round(float(map50), 6)
                except (TypeError, ValueError):
                    pass
            emit(f"epoch {epoch}/{total}  loss={loss_val if loss_val is not None else '-'}  "
                 f"mAP50={extra.get('map50', '-')}", step=epoch, **extra)
        except Exception as exc:  # noqa: BLE001
            emit(f"指标回调异常（不影响训练）：{exc}", step=0)

    try:
        model.add_callback("on_train_start",
                           lambda trainer: emit("训练开始，数据集与权重加载完成", step=0))
        model.add_callback("on_fit_epoch_end", on_fit_epoch_end)
    except Exception as exc:  # noqa: BLE001
        emit(f"注册训练回调失败：{exc}（仍会继续训练，但 Loss 曲线可能为空）", step=0)

    try:
        results = model.train(data=data_yaml, epochs=args.epochs, imgsz=args.imgsz,
                              batch=args.batch, device=args.device,
                              project=str(out), name="run", exist_ok=True, verbose=False)
        save_dir = getattr(results, "save_dir", None) or (out / "run")
        emit(f"训练完成，权重与日志保存在：{save_dir}", step=args.epochs)
        emit(f"最佳权重：{Path(save_dir) / 'weights' / 'best.pt'}", step=args.epochs)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(f"YOLO 训练失败：{exc}")


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
