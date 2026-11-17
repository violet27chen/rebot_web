"""
模型微调前置元数据与特征空间一致性校验模块 (Training Pre-flight Validation)
========================================================================

【模块背景与定位】
在强化学习与行为克隆（ACT）微调（Fine-tuning）训练前，如果用户选择的已有预训练模型
与当前准备使用的数据集存在特征维度不匹配（例如：预训练模型是 6 自由度单臂模型，
而数据集是 14 自由度双臂数据集；或者模型使用了两路相机，数据集只有一路相机；
亦或相机图像分辨率发生改变），如果直接启动 PyTorch 训练，将在初始化数十秒后
由于权重张量形状不匹配发生崩溃，甚至破坏原有预训练检查点。

【设计准则】
本模块纯粹基于 JSON 元数据和文件系统进行静态前置校验，【零 Torch 导入】，
运行在 Web 主服务线程中，几毫秒内即可迅速拦截所有不匹配配置，提供清晰准确的中文排查指引。
"""
import json
from pathlib import Path


def training_mode(params):
    """提取并校验训练模式（从头训练 scratch 还是基于已有模型微调 finetune）。"""
    mode = params.get('training_mode') or ('finetune' if params.get('base_model') else 'scratch')
    if mode not in ('scratch', 'finetune'):
        raise ValueError('训练方式必须为从头训练或已有模型微调')
    return mode


def local_policy(source, root):
    """
    智能搜索定位本地预训练模型策略目录，自动寻找包含 config.json 的标准目录。

    Args:
        source (str or Path): 用户传入的模型路径。
        root (str or Path): 平台根目录。

    Returns:
        Path or None: 找到的包含 config.json 的目录路径，不存在则返回 None。

    Raises:
        ValueError: 路径存在但找不到 config.json 时抛出。
    """
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
    """读取并解析模型的 config.json 配置文件。"""
    try:
        cfg = json.loads((path / 'config.json').read_text('utf-8'))
        if not isinstance(cfg, dict) or not cfg.get('type'):
            raise ValueError('模型缺少策略类型')
        return cfg
    except (OSError, ValueError) as exc:
        raise ValueError(f'模型配置无法读取：{exc}') from exc


def dataset_features(repo_id, dataset_dir, cache_root, root):
    """从数据集目录下的 meta/info.json 中提取特征描述字典 (features)。"""
    directory = Path(dataset_dir).expanduser() if dataset_dir else Path(cache_root) / repo_id
    if not directory.is_absolute():
        directory = Path(root) / directory
    try:
        return json.loads((directory / 'meta/info.json').read_text('utf-8'))['features']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('无法读取微调数据集 meta/info.json，请先选择有效的本地数据集') from exc


def validate_features(policy, features):
    """
    严格比对模型预期输入输出与微调数据集特征维度的兼容性。

    【校验内容】
    1. 必须包含 action 与 observation.state 特征；
    2. 图像观察（observation.images.*）的相机名称集合与数量必须 100% 严格一致；
    3. 维度严格比对：LeRobot 数据集以 [H, W, C] 格式存储视频，而模型策略以 [C, H, W] 定义，
       校验时自动进行转置比对，坚决防止混用单臂/双臂模型或改动相机分辨率导致矩阵乘法失败。

    Args:
        policy (dict): 模型 config.json 解析字典。
        features (dict): 数据集 meta/info.json 的 features 字典。

    Raises:
        ValueError: 特征不匹配时抛出清晰中文异常。
    """
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
        # 数据集存储视频为 HWC；模型配置定义为 CHW，进行维度转置比对
        if key.startswith('observation.images.') and len(shape) == 3:
            shape = [shape[2], shape[0], shape[1]]
        if shape != list(spec.get('shape') or []):
            raise ValueError(f'微调特征不匹配：{key}，模型 {spec.get("shape")}，数据集 {shape}。不能混用单臂和双臂模型或改变相机分辨率')


def prepare_finetune(params, root, cache_root):
    """
    执行微调前置准备工作：规范化模型路径、校验权重文件完整性并检查特征一致性。

    Args:
        params (dict): 训练参数。
        root (str or Path): 项目根路径。
        cache_root (str or Path): 缓存根路径。

    Returns:
        tuple[str, dict or None]: (规范化后的模型绝对路径, 策略配置字典)。

    Raises:
        ValueError: 路径无效、策略类型不符或特征不匹配时抛出。
    """
    source = str(params.get('base_model') or '').strip()
    if not source:
        raise ValueError('请先选择要微调的已有模型 pretrained_model 目录')
    policy_type = params.get('policy_type') or 'act'
    path = local_policy(source, root)
    if path is None:
        # 支持 SmolVLA 官方 HuggingFace 远程 hub 模型引用格式
        if policy_type == 'smolvla' and len(source.split('/')) == 2 and not source.startswith(('.', '/')):
            return source, None
        raise ValueError('微调模型路径不存在，请选择本地 pretrained_model 目录（Hub 模型请先下载）')

    policy = read_policy(path)
    if policy['type'] != policy_type:
        raise ValueError(f'模型策略为 {policy["type"]}，当前选择为 {policy_type}，请先切换策略类型')
    if not (path / 'model.safetensors').is_file():
        raise ValueError('模型权重 model.safetensors 不存在，检查点可能尚未保存完成')

    # 若为 ACT 策略，核对数据集特征
    if policy_type == 'act':
        features = dataset_features(
            str(params.get('repo_id') or ''),
            str(params.get('dataset_dir') or ''),
            cache_root,
            root
        )
        validate_features(policy, features)

    return str(path), policy
