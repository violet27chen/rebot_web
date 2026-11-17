"""
ACT 模型离线评估请求参数校验与误差累加模块 (Model Evaluation & Metrics Accumulator)
========================================================================================

【模块背景与定位】
在模仿学习策略训练完成后，需要在离线测试数据集上评估模型的动作预测精度。
本模块包含两个核心部分：
1. `prepare_evaluation`: 在 Web 主服务进程中运行，负责校验评估请求的各项参数、
   核对数据集有效性与剧集索引，纯元数据操作，零 Torch 依赖，保持主服务极速轻量；
2. `ActionErrors`: 掩码动作预测误差累加器。用于在排除填充帧（action_is_pad）的
   真实物理时间步上，精确统计各关节维度的 MAE（平均绝对误差）、RMSE（均方根误差）与最大绝对误差。
"""
import json
import math
from pathlib import Path

from backend.training import prepare_finetune


def prepare_evaluation(params, root, cache_root):
    """
    全面校验评估请求参数，定位本地模型权重与数据集目录。

    Args:
        params (dict): 前端提交的评估请求参数字典。
        root (str or Path): 平台根目录。
        cache_root (str or Path): 数据集缓存目录。

    Returns:
        dict: 规范化后的执行配置字典。

    Raises:
        ValueError: 当参数不合法、数据集无剧集或剧集编号超界时抛出。
    """
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
    if episodes is not None and (
        not isinstance(episodes, list) or any(
            isinstance(e, bool) or not isinstance(e, int) or e < 0 or e >= total for e in episodes
        )
    ):
        raise ValueError(f'评估剧集编号应为 0 至 {total - 1} 的整数，以逗号分隔')

    episodes = sorted(set(episodes)) if episodes else None
    samples = int(params.get('max_samples') or 200)
    batch = int(params.get('batch_size') or 4)
    if not (1 <= samples <= 10000) or not (1 <= batch <= 32):
        raise ValueError('评估采样帧数应为 1–10000，批大小应为 1–32')

    device = str(params.get('device') or 'cpu')
    if device not in ('cpu', 'cuda'):
        raise ValueError('评估设备应为 cpu 或 cuda')

    return {
        'base_model': source,
        'dataset_dir': str(dataset.resolve()),
        'repo_id': str(params.get('repo_id') or dataset.name),
        'episodes': episodes,
        'max_samples': samples,
        'batch_size': batch,
        'device': device,
        'video_backend': str(params.get('video_backend') or 'pyav')
    }


class ActionErrors:
    """
    多关节动作误差张量累加器。
    在隔离的 Worker 进程中配合 PyTorch 使用，精确累加有效动作步的 L1 绝对误差与 L2 平方误差。
    """

    def __init__(self):
        self.count = 0
        self.abs_sum = None
        self.sq_sum = None
        self.maximum = 0.0

    def add(self, prediction, target, valid):
        """
        向累加器中添加一批样本的预测误差（自动通过 valid 掩码过滤 padding 步）。

        Args:
            prediction (torch.Tensor): 模型预测动作张量 (B, ..., D)。
            target (torch.Tensor): 数据集真实示教动作张量 (B, ..., D)。
            valid (torch.Tensor): 布尔掩码张量，True 表示非填充的有效时间步。

        Raises:
            ValueError: 当预测或示教值包含 NaN 或 Inf 异常浮点数时抛出。
        """
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
        """
        计算并输出最终的评估指标统计报告。

        Returns:
            dict: 包含全局 mae, rmse, max_error, 各关节单独的 per_joint_mae/rmse 以及有效样本数 valid_actions。

        Raises:
            ValueError: 累加器中没有有效样本时抛出。
        """
        if not self.count:
            raise ValueError('没有可用于评估的有效动作，填充帧已排除')
        mae = (self.abs_sum / self.count).tolist()
        rmse = (self.sq_sum / self.count).sqrt().tolist()
        return {
            'mae': sum(mae) / len(mae),
            'rmse': math.sqrt(self.sq_sum.sum().item() / (self.count * len(mae))),
            'max_error': self.maximum,
            'per_joint_mae': mae,
            'per_joint_rmse': rmse,
            'valid_actions': self.count
        }
