"""WALL training/inference handoff. Standard library only; no device access."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

LEROBOT_REVISION = '7910c20a75834ad3fa6a9792df4d44a614d1bba6'
BASE_MODEL = 'lerobot/wall-oss-0.5'
JOINTS = ('shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex',
          'wrist_yaw', 'wrist_roll', 'gripper')
NAMES = [f'{side}_{joint}.pos' for side in ('left', 'right') for joint in JOINTS]
SLOTS = {'face_view', 'left_wrist_view', 'right_wrist_view'}
DEFAULT_MAP = Path(__file__).with_name('camera_mapping.json')
LORA_TARGETS = (r'(model\.model\.layers\.\d+\.(self_attn\.(qkv_proj_experts|o_proj_experts)\.\d+'
                r'|moe\.experts\.\d+\.(gate_up_proj|down_proj))'
                r'|model\.action_preprocessor\.(w1|w2|w3|action_proj_back))')


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'{path} 必须是 JSON 对象')
    return value


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dataset_contract(root, mapping_path=DEFAULT_MAP):
    root = Path(root).expanduser().resolve()
    info = read_json(root / 'meta/info.json')
    if int(info.get('total_episodes', 0)) < 1 or int(info.get('total_frames', 0)) < 1:
        raise ValueError('数据集没有完成的剧集/帧')
    if info.get('codebase_version') != 'v3.0':
        raise ValueError('此入口要求 LeRobot v3 数据集；请在副本上转换旧格式')
    if not any((root / 'data').rglob('*.parquet')):
        raise ValueError('缺少数据集 parquet 文件')
    features = info['features']
    for key in ('observation.state', 'action'):
        spec = features.get(key, {})
        if spec.get('shape') != [14] or spec.get('names') != NAMES:
            raise ValueError(f'{key} 必须按左臂7关节、右臂7关节的原始顺序排列；不会猜测或重排关节')
    mapping = read_json(mapping_path)
    cameras = {k: v for k, v in features.items() if k.startswith('observation.images.')}
    if set(mapping) != set(cameras) or set(mapping.values()) != SLOTS or len(mapping) != 3:
        raise ValueError('相机映射必须覆盖数据集全部三路 RGB，并且三个 WALL 视角各用一次')
    for key, spec in cameras.items():
        shape = spec.get('shape', [])
        if spec.get('dtype') != 'video' or len(shape) != 3 or shape[-1] != 3 or min(shape) < 1:
            raise ValueError(f'{key} 必须是 HWC RGB 视频；设备名称不代表有深度图')
        if not any((root / 'videos' / key).rglob('*.mp4')):
            raise ValueError(f'缺少视频：{key}')
    stats = read_json(root / 'meta/stats.json')
    for key in ('observation.state', 'action'):
        quantiles = stats.get(key, {})
        for name in ('q01', 'q99'):
            values = quantiles.get(name, [])
            if len(values) != 14 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
                raise ValueError(f'{key} 缺少有效的14维 {name} 统计')
        if any(lo > hi for lo, hi in zip(quantiles['q01'], quantiles['q99'])):
            raise ValueError(f'{key} 的分位数范围反向')
    return {
        'schema_version': 1, 'policy_type': 'wall_oss_05',
        'lerobot_revision': LEROBOT_REVISION, 'dataset_dir': str(root),
        'episodes': info['total_episodes'], 'frames': info['total_frames'], 'fps': info['fps'],
        'state_names': NAMES, 'action_names': NAMES,
        'camera_mapping': mapping, 'camera_shapes_hwc': {k: v['shape'] for k, v in cameras.items()},
        'info_sha256': fingerprint(root / 'meta/info.json'),
        'stats_sha256': fingerprint(root / 'meta/stats.json'),
        'state_convention': 'raw_follower_motor_degrees',
        'action_convention': 'leader_command_degrees_before_follower_direction_scaling',
        'task_note': '使用数据集原始 task；不会自动补写堆叠、翻面或面单朝上标签。',
    }


def checkpoint_contract(model_dir, reference):
    model_dir = Path(model_dir).expanduser().resolve()
    config = read_json(model_dir / 'config.json')
    if config.get('type') != 'wall_oss_05':
        raise ValueError('不是 WALL-OSS-0.5 原生 LeRobot 检查点；ACT、wall_x 和上游原始权重不能直接互换')
    if config.get('use_peft') or (model_dir / 'adapter_config.json').exists():
        raise ValueError('推理交付应先用 export_model.py 合并 LoRA 并导出完整权重')
    expected = {**config.get('input_features', {}), **config.get('output_features', {})}
    for key in ('observation.state', 'action'):
        if expected.get(key, {}).get('shape') != [14]:
            raise ValueError(f'检查点 {key} 不是14维；请交付完成微调的模型，不能使用26维基座演示')
    images = {k: v for k, v in expected.items() if k.startswith('observation.images.')}
    if set(images) != set(reference['camera_mapping']) or config.get('camera_key_mapping') != reference['camera_mapping']:
        raise ValueError('检查点的相机键/视角映射与数据不一致')
    for key, spec in images.items():
        h, w, c = reference['camera_shapes_hwc'][key]
        if spec.get('shape') != [c, h, w]:
            raise ValueError(f'相机尺寸不匹配：{key}')
    handoff = read_json(model_dir / 'dataset_reference.json')
    for key in ('state_names', 'action_names', 'camera_mapping', 'state_convention', 'action_convention'):
        if handoff.get(key) != reference[key]:
            raise ValueError(f'交付数据契约不一致：{key}')
    # Evaluation may use unseen episodes: do not require the training dataset hash.
    for name, registry in (('policy_preprocessor.json', 'normalizer_processor'),
                           ('policy_postprocessor.json', 'unnormalizer_processor')):
        processor = read_json(model_dir / name)
        normalizers = [s for s in processor.get('steps', []) if s.get('registry_name') == registry]
        if len(normalizers) != 1 or not normalizers[0].get('state_file'):
            raise ValueError(f'{name} 缺少已保存的归一化统计')
        def visit(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == 'state_file' and isinstance(v, str):
                        target = (model_dir / v).resolve()
                        if not target.is_relative_to(model_dir) or not target.is_file() or target.stat().st_size == 0:
                            raise ValueError(f'处理器统计文件缺失/越界：{v}')
                    visit(v)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(processor)
    index = model_dir / 'model.safetensors.index.json'
    weights = [model_dir / 'model.safetensors']
    if index.exists():
        weights = [(model_dir / name).resolve() for name in set(read_json(index)['weight_map'].values())]
    if not weights or any(not p.is_relative_to(model_dir) or not p.is_file() or p.stat().st_size == 0 for p in weights):
        raise ValueError('模型权重或权重分片缺失')
    return model_dir, config, handoff
