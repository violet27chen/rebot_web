"""Read saved observations and ACT weights; never imports robot control drivers."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model_evaluation import ActionErrors


def evaluate(params):
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
    # Saved full-policy weights replace the vision backbone; no network needed.
    config.pretrained_backbone_weights = None
    policy = ACTPolicy.from_pretrained(params['base_model'], config=config).to(device).eval()
    policy.reset()
    pre, post = make_pre_post_processors(config, pretrained_path=params['base_model'],
        preprocessor_overrides={'device_processor': {'device': device}})
    meta = LeRobotDatasetMetadata(params['repo_id'], root=params['dataset_dir'])
    dataset = LeRobotDataset(params['repo_id'], root=params['dataset_dir'], episodes=params['episodes'],
        delta_timestamps=resolve_delta_timestamps(config, meta), video_backend=params['video_backend'])
    samples = min(len(dataset), params['max_samples'])
    if not samples:
        raise ValueError('所选剧集没有可评估的帧')
    indices = torch.linspace(0, len(dataset)-1, samples).long().tolist()
    loader = DataLoader(Subset(dataset, indices), batch_size=params['batch_size'], shuffle=False, num_workers=0)
    raw_errors, normalized_errors = ActionErrors(), ActionErrors()
    processed, inference_s = 0, 0.
    with torch.inference_mode():
        for batch in loader:
            target = batch['action'].clone().to(device)
            valid = ~batch['action_is_pad'].to(device)
            prepared = pre(batch)
            observation = {k: v for k, v in prepared.items() if k not in ('action', 'action_is_pad')}
            if device == 'cuda': torch.cuda.synchronize()
            started = time.perf_counter()
            prediction = policy.predict_action_chunk(observation)
            if device == 'cuda': torch.cuda.synchronize()
            inference_s += time.perf_counter() - started
            normalized_errors.add(prediction, prepared['action'], valid)
            shape = prediction.shape
            raw_prediction = post(prediction.reshape(-1, shape[-1])).reshape(shape)
            raw_errors.add(raw_prediction, target, valid)
            processed += target.shape[0]
            print(f'离线评估：{processed}/{samples} 帧，MAE={raw_errors.result()["mae"]:.6f}', flush=True)
    result = raw_errors.result()
    result.update(normalized_l1=normalized_errors.result()['mae'], samples=processed,
        inference_ms=inference_s * 1000 / processed, action_names=meta.features['action'].get('names'),
        model_path=params['base_model'], dataset_dir=params['dataset_dir'], episodes=params['episodes'],
        device=device, created_at=time.time(),
        note='动作误差使用数据集原始单位；归一化 L1 为部署推理的重建误差，不含训练时的 KL 项。请使用未参与训练的测试示教；离线拟合误差不等同于实机分拣成功率。')
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
