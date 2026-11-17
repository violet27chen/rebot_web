"""
ACT 模仿学习模型离线评估独立执行 Worker 进程 (ACT Evaluation Worker)
====================================================================

【模块背景与架构定位】
本脚本作为独立的子进程执行 ACT 策略在离线数据集上的量化误差评估。
【纯只读与完全物理隔离】：
仅从磁盘加载预训练模型权重与已录制的视频/状态数据集，绝不导入任何机械臂或舵机控制驱动，
零硬件设备占用风险。

【评估执行流水线】
1. 设备自适应：配置固定随机种子（seed=0），按参数将模型与数据流水线部署在 CPU 或 CUDA 上；
2. 完整模型加载：从本地目录加载权重，设置 `pretrained_backbone_weights=None`，
   完全利用已微调好的视觉 Backbone 权重，不需要任何网络连接；
3. 数据批量加载：构建 DataLoader，依据采样上限从指定测试剧集中均匀提取离线样本；
4. 误差累加统计：
   - 提取真实示教动作 `target` 与非填充掩码 `valid`；
   - 针对归一化动作空间（Normalized Space）与物理原始关节空间（Raw Robot Degree Space）
     分别累加 MAE 和 RMSE；
   - 统计平均单帧推理延迟（`inference_ms`）。
5. 结果落盘与标准输出：原子写入结果 JSON 文件并输出 `REBOT_MODEL_EVAL_RESULT`。
"""
import json
import os
from pathlib import Path
import sys
import time

# 将项目根目录添加至 Python 模块路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model_evaluation import ActionErrors


def evaluate(params):
    """
    加载模型与数据集执行批量前向推理并计算精度指标。

    Args:
        params (dict): 评估配置字典（由 prepare_evaluation 生成）。

    Returns:
        dict: 评估指标与元数据字典。
    """
    import torch
    from torch.utils.data import DataLoader, Subset
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset, LeRobotDatasetMetadata
    from lerobot.datasets.factory import resolve_delta_timestamps
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.policies.factory import make_pre_post_processors

    torch.manual_seed(0)
    device = params['device']
    if device == 'cuda' and not torch.cuda.is_available():
        raise ValueError('CUDA 不可用，请选择 CPU 评估')

    print('正在加载本地 ACT 权重与评估数据集…', flush=True)
    config = PreTrainedConfig.from_pretrained(params['base_model'])
    config.device = device
    config.pretrained_backbone_weights = None  # 离线完全使用保存的模型权重，无需在线拉取骨干网
    policy = ACTPolicy.from_pretrained(params['base_model'], config=config).to(device).eval()
    policy.reset()

    # 构建前处理（图像标准化、尺寸变换）与后处理（归一化动作反算为物理角度）流水线
    pre, post = make_pre_post_processors(
        config,
        pretrained_path=params['base_model'],
        preprocessor_overrides={'device_processor': {'device': device}}
    )

    meta = LeRobotDatasetMetadata(params['repo_id'], root=params['dataset_dir'])
    dataset = LeRobotDataset(
        params['repo_id'],
        root=params['dataset_dir'],
        episodes=params['episodes'],
        delta_timestamps=resolve_delta_timestamps(config, meta),
        video_backend=params['video_backend']
    )

    samples = min(len(dataset), params['max_samples'])
    if not samples:
        raise ValueError('所选剧集没有可评估的帧')

    # 在有效数据集范围内等间隔抽取评估帧索引
    indices = torch.linspace(0, len(dataset) - 1, samples).long().tolist()
    loader = DataLoader(Subset(dataset, indices), batch_size=params['batch_size'], shuffle=False, num_workers=0)

    raw_errors, normalized_errors = ActionErrors(), ActionErrors()
    processed, inference_s = 0, 0.0

    # 禁用梯度计算执行推理
    with torch.inference_mode():
        for batch in loader:
            target = batch['action'].clone().to(device)
            valid = ~batch['action_is_pad'].to(device)
            prepared = pre(batch)
            observation = {k: v for k, v in prepared.items() if k not in ('action', 'action_is_pad')}

            if device == 'cuda':
                torch.cuda.synchronize()
            started = time.perf_counter()
            # 执行多步动作块前向预测
            prediction = policy.predict_action_chunk(observation)
            if device == 'cuda':
                torch.cuda.synchronize()
            inference_s += time.perf_counter() - started

            # 统计归一化模型动作空间的 L1 误差
            normalized_errors.add(prediction, prepared['action'], valid)
            shape = prediction.shape
            # 后处理反归一化回物理角度
            raw_prediction = post(prediction.reshape(-1, shape[-1])).reshape(shape)
            raw_errors.add(raw_prediction, target, valid)
            processed += target.shape[0]
            print(f'离线评估：{processed}/{samples} 帧，MAE={raw_errors.result()["mae"]:.6f}', flush=True)

    result = raw_errors.result()
    result.update(
        normalized_l1=normalized_errors.result()['mae'],
        samples=processed,
        inference_ms=inference_s * 1000 / processed,
        action_names=meta.features['action'].get('names'),
        model_path=params['base_model'],
        dataset_dir=params['dataset_dir'],
        episodes=params['episodes'],
        device=device,
        created_at=time.time(),
        note='动作误差使用数据集原始单位；归一化 L1 为部署推理的重建误差，不含训练时的 KL 项。请使用未参与训练的测试示教；离线拟合误差不等同于实机分拣成功率。'
    )

    # 原子写入输出 JSON 文件
    path = Path(params['result_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), 'utf-8')
    os.replace(temporary, path)

    print('REBOT_MODEL_EVAL_RESULT ' + json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return result


if __name__ == '__main__':
    try:
        evaluate(json.loads(sys.argv[1]))
    except Exception as exc:
        print('[离线评估失败] ' + str(exc), flush=True)
        raise
