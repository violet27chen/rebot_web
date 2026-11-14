"""Validate existing policy weights before starting a new fine-tuning run.

No torch imports or device operations: metadata checks run in the Web process.
"""
import json
from pathlib import Path


def training_mode(params):
    mode = params.get('training_mode') or ('finetune' if params.get('base_model') else 'scratch')
    if mode not in ('scratch', 'finetune'):
        raise ValueError('训练方式必须为从头训练或已有模型微调')
    return mode


def local_policy(source, root):
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    if not path.exists():
        return None
    if path.is_file():
        path = path.parent
    for candidate in (path, path / 'pretrained_model', path / 'checkpoints/last/pretrained_model'):
        if (candidate / 'config.json').is_file():
            return candidate.resolve()
    raise ValueError('未找到模型 config.json，请选择检查点的 pretrained_model 目录')


def read_policy(path):
    try:
        cfg = json.loads((path / 'config.json').read_text('utf-8'))
        if not isinstance(cfg, dict) or not cfg.get('type'):
            raise ValueError('模型缺少策略类型')
        return cfg
    except (OSError, ValueError) as exc:
        raise ValueError(f'模型配置无法读取：{exc}') from exc


def dataset_features(repo_id, dataset_dir, cache_root, root):
    directory = Path(dataset_dir).expanduser() if dataset_dir else Path(cache_root) / repo_id
    if not directory.is_absolute():
        directory = Path(root) / directory
    try:
        return json.loads((directory / 'meta/info.json').read_text('utf-8'))['features']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('无法读取微调数据集 meta/info.json，请先选择有效的本地数据集') from exc


def validate_features(policy, features):
    expected = {**(policy.get('input_features') or {}), **(policy.get('output_features') or {})}
    if 'action' not in expected or 'observation.state' not in expected:
        raise ValueError('模型缺少动作或状态特征，无法核对从臂维度')
    model_cameras = {k for k in expected if k.startswith('observation.images.')}
    data_cameras = {k for k in features if k.startswith('observation.images.')}
    if model_cameras != data_cameras:
        raise ValueError('微调相机名称或数量不一致：模型 ' + ', '.join(sorted(model_cameras)) +
                         '；数据集 ' + ', '.join(sorted(data_cameras)) + '。请使用同一技能、相同相机配置的数据')
    for key, spec in expected.items():
        actual = features.get(key)
        if not actual:
            raise ValueError(f'微调数据集缺少模型输入：{key}')
        shape = list(actual.get('shape') or [])
        # LeRobot datasets store video as HWC; policy configs use CHW.
        if key.startswith('observation.images.') and len(shape) == 3:
            shape = [shape[2], shape[0], shape[1]]
        if shape != list(spec.get('shape') or []):
            raise ValueError(f'微调特征不匹配：{key}，模型 {spec.get("shape")}，数据集 {shape}。不能混用单臂和双臂模型或改变相机分辨率')


def prepare_finetune(params, root, cache_root):
    """Return a normalized source and metadata without mutating saved weights."""
    source = str(params.get('base_model') or '').strip()
    if not source:
        raise ValueError('请先选择要微调的已有模型 pretrained_model 目录')
    policy_type = params.get('policy_type') or 'act'
    path = local_policy(source, root)
    if path is None:
        # Preserve the existing SmolVLA Hub flow; ACT must be locally inspected.
        if policy_type == 'smolvla' and len(source.split('/')) == 2 and not source.startswith(('.', '/')):
            return source, None
        raise ValueError('微调模型路径不存在，请选择本地 pretrained_model 目录（Hub 模型请先下载）')
    policy = read_policy(path)
    if policy['type'] != policy_type:
        raise ValueError(f'模型策略为 {policy["type"]}，当前选择为 {policy_type}，请先切换策略类型')
    if not (path / 'model.safetensors').is_file():
        raise ValueError('模型权重 model.safetensors 不存在，检查点可能尚未保存完成')
    if policy_type == 'act':
        features = dataset_features(str(params.get('repo_id') or ''),
                                    str(params.get('dataset_dir') or ''), cache_root, root)
        validate_features(policy, features)
    return str(path), policy
