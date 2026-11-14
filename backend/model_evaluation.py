"""Local ACT evaluation requests and masked prediction error accumulation."""
import json
import math
from pathlib import Path

from backend.training import prepare_finetune


def prepare_evaluation(params, root, cache_root):
    params = dict(params, policy_type='act')
    source, _ = prepare_finetune(params, root, cache_root)
    dataset = Path(params.get('dataset_dir') or Path(cache_root) / str(params.get('repo_id') or '')).expanduser()
    if not dataset.is_absolute():
        dataset = Path(root) / dataset
    info = json.loads((dataset / 'meta/info.json').read_text('utf-8'))
    total = int(info.get('total_episodes') or 0)
    if total < 1:
        raise ValueError('评估数据集没有有效剧集')
    episodes = params.get('episodes')
    if isinstance(episodes, str):
        episodes = [int(s.strip()) for s in episodes.split(',') if s.strip()]
    if episodes is not None and (not isinstance(episodes, list) or any(isinstance(e, bool) or not isinstance(e, int) or e < 0 or e >= total for e in episodes)):
        raise ValueError(f'评估剧集编号应为 0 至 {total-1} 的整数，以逗号分隔')
    episodes = sorted(set(episodes)) if episodes else None
    samples = int(params.get('max_samples') or 200)
    batch = int(params.get('batch_size') or 4)
    if not 1 <= samples <= 10000 or not 1 <= batch <= 32:
        raise ValueError('评估采样帧数应为 1–10000，批大小应为 1–32')
    device = str(params.get('device') or 'cpu')
    if device not in ('cpu', 'cuda'):
        raise ValueError('评估设备应为 cpu 或 cuda')
    return {'base_model': source, 'dataset_dir': str(dataset.resolve()),
            'repo_id': str(params.get('repo_id') or dataset.name), 'episodes': episodes,
            'max_samples': samples, 'batch_size': batch, 'device': device,
            'video_backend': str(params.get('video_backend') or 'pyav')}


class ActionErrors:
    """Torch is supplied by the isolated worker, keeping the server lightweight."""
    def __init__(self):
        self.count = 0
        self.abs_sum = None
        self.sq_sum = None
        self.maximum = 0.

    def add(self, prediction, target, valid):
        error = (prediction - target)[valid].detach().double().cpu()
        if not error.numel():
            return
        if not error.isfinite().all():
            raise ValueError('评估遇到非有限预测或示教值，未生成成功结果')
        absolute, squared = error.abs().sum(0), error.square().sum(0)
        self.abs_sum = absolute if self.abs_sum is None else self.abs_sum + absolute
        self.sq_sum = squared if self.sq_sum is None else self.sq_sum + squared
        self.count += error.shape[0]
        self.maximum = max(self.maximum, error.abs().max().item())

    def result(self):
        if not self.count:
            raise ValueError('没有可用于评估的有效动作，填充帧已排除')
        mae = (self.abs_sum / self.count).tolist()
        rmse = (self.sq_sum / self.count).sqrt().tolist()
        return {'mae': sum(mae) / len(mae), 'rmse': math.sqrt(self.sq_sum.sum().item() / (self.count * len(mae))),
                'max_error': self.maximum, 'per_joint_mae': mae, 'per_joint_rmse': rmse,
                'valid_actions': self.count}
