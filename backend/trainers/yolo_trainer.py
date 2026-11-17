#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ultralytics YOLO 目标检测模型真实训练引擎 (Ultralytics YOLO Real Trainer)
========================================================================

【模块背景与定位】
在工业视觉分拣中，包裹定位、运单检测与缺陷识别高度依赖 YOLO 系列目标检测模型。
本模块调用官方 Ultralytics Python API 进行真实的神经网络模型训练与迁移微调，
绝不伪造任何模拟数据，所有训练 Loss、验证集 mAP@0.5 与混淆矩阵均来自实际前向与反向传播。

【核心设计与特性】
1. 智能数据集结构适配 (`resolve_data_yaml`)：
   - 优先支持用户直接传入 `--data /path/to/data.yaml`；
   - 若传入 `--auto-yaml /path/to/dataset`，自动扫描目录结构（images/train, images/val,
     labels/train, labels/val），若缺少 `data.yaml` 则自动解析 `classes.txt` 动态生成
     符合 Ultralytics 规范的临时配置文件；
2. 训练钩子回调监听（Callback Hook）：
   通过注册 `on_fit_epoch_end` 训练事件回调，在每个 Epoch 结束时精准拦截当前的真实数值：
   - `loss`: train/loss 或 loss 项；
   - `map50`: metrics/mAP50(B) 边框平均精度；
   - 将其封装为标准 JSON 格式指标行（`{"type":"metric", "step":epoch, "loss":..., "map50":...}`）
     实时通过 stdout 推送到 Web 前端进行可视化展示。
3. 训练收尾与模型导出：
   训练完成后输出最终训练权重文件路径（`best.pt`、`last.pt`），并向标准输出上报完成事件。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback


def emit(message: str, **kw) -> None:
    """向标准输出打印一条指标/进度 JSON 行并立即刷新缓冲区。"""
    print(json.dumps({"type": "metric", "message": message, **kw}, ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2) -> None:
    """打印错误诊断信息并以非零代码终止脚本。"""
    print(json.dumps({"type": "error", "message": message}, ensure_ascii=False), flush=True)
    print(f"[错误] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def resolve_data_yaml(raw: str, auto: str, out: Path) -> str:
    """
    定位或自动生成合法的 YOLO 数据集配置文件 data.yaml。

    Args:
        raw (str): 显式传入的 data.yaml 路径。
        auto (str): 传入的数据集文件夹路径。
        out (Path): 训练输出目标路径。

    Returns:
        str: 可供 YOLO 加载的绝对路径字符串。
    """
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
    missing = [str(p) for p in (images_train, images_val, labels_train, labels_val) if not p.is_dir()]
    if missing:
        fail(
            "数据集不是 YOLO 结构，缺少目录：\n  " + "\n  ".join(missing) +
            "\n需要 images/train、images/val、labels/train、labels/val，"
            "或使用已包含 data.yaml 的标注包。"
        )

    # 尝试寻找类别定义文件 classes.txt
    classes_file = None
    for cand in (root / "classes.txt", root / "labels" / "classes.txt"):
        if cand.is_file():
            classes_file = cand
            break

    classes: list[str] = []
    if classes_file:
        for line in classes_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                classes.append(line)

    # 若无 classes.txt，扫描标注文件中的最大类别 ID
    if not classes:
        max_id = -1
        for txt in labels_train.glob("*.txt"):
            try:
                for line in txt.read_text(encoding="utf-8", errors="replace").splitlines():
                    parts = line.strip().split()
                    if parts and parts[0].isdigit():
                        max_id = max(max_id, int(parts[0]))
            except Exception:
                pass
        if max_id < 0:
            fail(f"{labels_train} 里没有读到任何标注框，无法推断类别数")
        classes = [f"class_{i}" for i in range(max_id + 1)]

    # 自动生成临时 data.yaml
    yaml_lines = [
        f"path: {root.resolve().as_posix()}",
        "train: images/train",
        "val: images/val",
        f"nc: {len(classes)}",
        "names:",
    ]
    for idx, name in enumerate(classes):
        yaml_lines.append(f"  {idx}: {name}")

    generated = out / "auto_generated_data.yaml"
    generated.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    emit(f"已根据目录自动生成标注文件（共 {len(classes)} 个类别）：{generated}", step=0)
    return str(generated.resolve())


def main() -> None:
    """命令行入口：配置 Ultralytics YOLO 训练超参数并挂接回调进行训练。"""
    ap = argparse.ArgumentParser(description="Ultralytics YOLO 真实目标检测模型训练脚本")
    ap.add_argument("--model", default="yolo11n.pt", help="预训练模型权重路径（如 yolo11n.pt, yolov8s.pt）")
    ap.add_argument("--data", default="", help="显式指定 data.yaml 文件路径")
    ap.add_argument("--auto-yaml", default="", help="自动从数据集目录寻找/生成 data.yaml")
    ap.add_argument("--output", required=True, help="训练输出保存目录")
    ap.add_argument("--epochs", type=int, default=10, help="训练轮数 (Epochs)")
    ap.add_argument("--batch-size", type=int, default=8, help="批大小 (Batch Size)")
    ap.add_argument("--imgsz", type=int, default=640, help="输入图片推理尺寸")
    ap.add_argument("--device", default="", help="指定设备（如 0, cpu 等，默认自动侦测）")
    ap.add_argument("--workers", type=int, default=2, help="数据加载工作线程数")
    args = ap.parse_args()

    if not args.data and not args.auto_yaml:
        fail("必须提供 --data <data.yaml> 或 --auto-yaml <dataset_dir> 之一")

    try:
        from ultralytics import YOLO
    except Exception as exc:  # noqa: BLE001
        fail(f"导入 ultralytics 失败：{exc}（请确认已在相应 conda 环境安装 ultralytics）")

    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    data_yaml = resolve_data_yaml(args.data, args.auto_yaml, out_dir)

    model_src = Path(args.model).expanduser()
    if not model_src.is_file() and not args.model.endswith((".pt", ".yaml")):
        fail(f"模型权重文件不存在：{args.model}")
    model_arg = str(model_src.resolve()) if model_src.is_file() else args.model

    emit(f"加载预训练权重：{model_arg}", step=0)
    try:
        model = YOLO(model_arg)
    except Exception as exc:  # noqa: BLE001
        fail(f"加载模型失败：{exc}\n{traceback.format_exc()}")

    # 注册每个 Epoch 结束时的指标捕获回调
    def on_fit_epoch_end(trainer):
        try:
            ep = int(getattr(trainer, "epoch", 0)) + 1
            max_ep = int(getattr(trainer, "epochs", args.epochs))
            metrics = getattr(trainer, "metrics", {}) or {}
            loss_items = getattr(trainer, "loss_items", None)

            loss_val = None
            if loss_items is not None:
                try:
                    loss_val = float(sum(loss_items))
                except Exception:
                    pass
            if loss_val is None:
                for k in ("train/loss", "loss", "train/box_loss"):
                    if k in metrics:
                        try:
                            loss_val = float(metrics[k])
                            break
                        except Exception:
                            pass

            map50 = None
            for k in ("metrics/mAP50(B)", "mAP50", "metrics/mAP50"):
                if k in metrics:
                    try:
                        map50 = float(metrics[k])
                        break
                    except Exception:
                        pass

            kw = {}
            if loss_val is not None:
                kw["loss"] = round(loss_val, 4)
            if map50 is not None:
                kw["map50"] = round(map50, 4)

            info_str = " · ".join(f"{k}={v}" for k, v in kw.items())
            emit(f"Epoch {ep}/{max_ep} 完成" + (f" ({info_str})" if info_str else ""), step=ep, **kw)
        except Exception:
            pass

    model.add_callback("on_fit_epoch_end", on_fit_epoch_end)

    emit(
        f"开始 YOLO 真实训练（共 {args.epochs} 轮，batch={args.batch_size}，imgsz={args.imgsz}）",
        step=0,
    )

    train_kwargs = {
        "data": data_yaml,
        "epochs": int(args.epochs),
        "batch": int(args.batch_size),
        "imgsz": int(args.imgsz),
        "project": str(out_dir),
        "name": "weights",
        "exist_ok": True,
        "verbose": False,
        "workers": int(args.workers),
    }
    if args.device:
        train_kwargs["device"] = args.device

    try:
        results = model.train(**train_kwargs)
    except Exception as exc:  # noqa: BLE001
        fail(f"训练异常中断：{exc}\n{traceback.format_exc()}")

    # 寻找最终生成的权重文件
    best_pt = out_dir / "weights" / "weights" / "best.pt"
    if not best_pt.is_file():
        candidates = list(out_dir.glob("**/best.pt"))
        if candidates:
            best_pt = candidates[0]

    last_pt = out_dir / "weights" / "weights" / "last.pt"
    if not last_pt.is_file():
        candidates = list(out_dir.glob("**/last.pt"))
        if candidates:
            last_pt = candidates[0]

    saved = str(best_pt.resolve()) if best_pt.is_file() else (str(last_pt.resolve()) if last_pt.is_file() else "")
    emit(
        f"YOLO 训练完成！权重已保存至：{saved or out_dir}",
        step=args.epochs,
        saved_model=saved,
    )


if __name__ == "__main__":
    main()
