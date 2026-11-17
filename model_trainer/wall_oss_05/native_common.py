"""Contracts for the upstream Wall-X runtime, separate from LeRobot wall_oss_05."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess

from contract import NAMES, read_json

WALLX_REVISION = 'ce2a1ae31d9ee1028b97ca40e3f66034cf8c0e5f'
LAYOUT = {'follow_left_arm_joint_pos': 7, 'follow_right_arm_joint_pos': 7, 'action_padding': 12}
WS_CAMERAS = {'face_view': 'camera_front', 'left_wrist_view': 'camera_left', 'right_wrist_view': 'camera_right'}


def check_repo(path):
    path = Path(path).expanduser().resolve()
    for name in ('scripts/compute_norm_stats.py', 'wall_x/trainer/fsdp_trainer/train_fsdp.py',
                 'wall_x/_vendor/harrix/serving/launch_serving.py'):
        if not (path / name).is_file():
            raise ValueError('Wall-X 源码不完整：' + name)
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True)
    if result.stdout.strip() != WALLX_REVISION:
        raise ValueError(f'Wall-X 源码版本未对齐，应使用 {WALLX_REVISION}；请在独立副本核对，不会自动切分支')
    return path


def require_joint_layout(config):
    if config.get('model_type') != 'qwen2_5':
        raise ValueError('此入口仅接受 Wall-X 原生 qwen2_5 配置')
    for key in ('dof_config', 'ar_dof_config', 'agent_pos_config'):
        value = config.get('task', {}).get(key)
        if list((value or {}).items()) != list(LAYOUT.items()):
            raise ValueError(f'{key} 必须是左7、右7、补零12，不能套用末端XYZ/RPY配置')
    return config


def joint_payload(values):
    values = [float(v) for v in values]
    if len(values) != 14 or not all(math.isfinite(v) for v in values):
        raise ValueError('需要14维有限实测关节角度；不能用零值补缺失反馈')
    return {'follow1_joints': values[:7], 'follow2_joints': values[7:]}


def read_config(path):
    # Our generated config.yml uses JSON syntax, a YAML subset; external native
    # YAML checkpoints additionally need PyYAML, supplied by Wall-X.
    text = Path(path).read_text(encoding='utf-8')
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        import yaml
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError('训练配置必须是对象')
    return value


def native_checkpoint(path):
    path = Path(path).expanduser().resolve()
    for name in ('model.safetensors', 'config.json', 'config.yml', 'norm_stats.json',
                 'preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json',
                 'dataset_reference.json'):
        if not (path / name).is_file() or (path / name).stat().st_size == 0:
            raise ValueError('原生推理交付缺少 ' + name + '；分片权重须先在训练机合并')
    if read_json(path / 'config.json').get('type') == 'wall_oss_05':
        raise ValueError('这是 LeRobot 转换格式，不能交给 Wall-X 原生服务')
    config = require_joint_layout(read_config(path / 'config.yml'))
    reference = read_json(path / 'dataset_reference.json')
    if reference.get('runtime') != 'wall_x_native' or reference.get('action_names') != NAMES or reference.get('state_names') != NAMES:
        raise ValueError('原生交付缺少正确的14维关节契约')
    if reference.get('norm_key') != 'rebot_parcel':
        raise ValueError('必须使用本项目的 rebot_parcel 统计，不能套用 ex_normal/x2_normal')
    if config['data']['key_mappings']['camera'] != reference.get('camera_mapping'):
        raise ValueError('相机映射与交付引用不一致')
    stats = read_json(path / 'norm_stats.json')['norm_stats']
    for key in ('observation.state', 'action'):
        for quantile in ('q01', 'q99'):
            values = stats.get(key, {}).get(quantile, [])
            if len(values) != 14 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
                raise ValueError(f'交付统计 {key}.{quantile} 不是有效14维数据')
    return path, config, reference
