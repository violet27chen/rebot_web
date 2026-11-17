"""Upstream Wall-X / LeRobot 0.4.4 training entry, for the remote 8x4090D host."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys

from contract import DEFAULT_MAP, dataset_contract, read_json
from native_common import LAYOUT, WALLX_REVISION, check_repo


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallx-dir', type=Path, required=True)
    parser.add_argument('--dataset-dir', type=Path, required=True)
    parser.add_argument('--base-model', type=Path, required=True, help='X-Square-Robot 原始基座目录，不是 lerobot 转换权重')
    parser.add_argument('--processor-dir', type=Path, required=True, help='Qwen2.5-VL-3B-Instruct processor')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--camera-map', type=Path, default=DEFAULT_MAP)
    parser.add_argument('--gpus', type=int, choices=(2, 4, 8), default=8)
    parser.add_argument('--steps', type=int, default=2000, help='原生 Wall-X 优化器更新次数')
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--accumulation', type=int, default=4)
    parser.add_argument('--learning-rate', type=float, default=5e-5)
    parser.add_argument('--save-every', type=int, default=1000)
    parser.add_argument('--cpu-offload', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='标准库预检查，不写文件、不安装或下载')
    parser.add_argument('--check', action='store_true', help='额外核对 CUDA/版本，不训练')
    args = parser.parse_args(argv)
    if min(args.steps, args.batch_size, args.accumulation, args.save_every) < 1:
        parser.error('训练参数必须为正整数')
    if not math.isfinite(args.learning_rate) or args.learning_rate <= 0:
        parser.error('学习率必须为正数')
    return args


def prepare(args):
    repo = check_repo(args.wallx_dir)
    reference = dataset_contract(args.dataset_dir, args.camera_map)
    if reference['episodes'] < 2:
        raise ValueError('原生训练/验证划分至少需要两集')
    base = args.base_model.expanduser().resolve()
    architecture = read_json(base / 'config.json')
    if architecture.get('type') == 'wall_oss_05' or architecture.get('model_type') != 'qwen2_5_vl':
        raise ValueError('请使用 X-Square-Robot/wall-oss-0.5 原生 config/权重，不能混用 LeRobot 转换检查点')
    if architecture.get('dim_inputs') != [2048, 1024] or architecture.get('use_x_loss') is not True:
        raise ValueError('基座结构与 Wall-OSS-0.5 发布配置不一致，不能使用旧FLOW/FAST示例结构')
    if not (base / 'model.safetensors').is_file():
        raise ValueError('缺少原生 model.safetensors')
    processor = args.processor_dir.expanduser().resolve()
    for filename in ('preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json'):
        if not (processor / filename).is_file():
            raise ValueError('Qwen processor 不完整：' + filename)
    output = args.output_dir.expanduser().resolve()
    protected = [Path(reference['dataset_dir']), base, processor, repo]
    if output.exists() or any(output.is_relative_to(p) or p.is_relative_to(output) for p in protected):
        raise ValueError('使用新的独立输出目录；不得覆盖数据集、模型、processor或源码')
    config = {
        'model_type': 'qwen2_5',
        'task': {'dof_config': dict(LAYOUT), 'ar_dof_config': dict(LAYOUT),
                 'agent_pos_config': dict(LAYOUT), 'action_horizon': 32,
                 'action_horizon_flow': 32, 'use_state_string_representation': False},
        'model': {'backbone': 'qwen2_5', 'config_path': str(base / 'config.json'),
                  'processor_path': str(processor), 'pretrained_path': str(processor),
                  'attn_deterministic': True, 'use_ema': False,
                  'flow_loss_weight': 1., 'ar_loss_weight': .01},
        'hyperparams': {'num_epoch': max(100, args.steps), 'batch_size_per_gpu': args.batch_size,
                        'gradient_accumulation_steps': args.accumulation, 'seed': 10222,
                        'optimizer': {'optimizer_type': 'adamw', 'learning_rate': args.learning_rate,
                                      'max_grad_norm': 1., 'enable_grad_clip': True,
                                      'betas': [.9, .95], 'weight_decay': 1e-8, 'eps': 1e-8},
                        'scheduler': {'scheduler_type': 'cosine', 'num_warmup_steps': min(100, max(1, args.steps // 10)),
                                      'num_training_steps': args.steps, 'min_lr': 1e-6}},
        'distributed': {'use_fsdp': True, 'fsdp_sharding_strategy': 'full_shard',
                        'fsdp_save_policy': 'full', 'use_mixed_precision': True, 'bf16': True,
                        'use_gradient_checkpointing': True, 'fsdp_cpu_offload': args.cpu_offload},
        'data': {'dataset_type': 'lerobot', 'lerobot_config': {'repo_id': reference['dataset_dir'], 'root': None},
                 'key_mappings': {'camera': reference['camera_mapping'], 'state': 'observation.state', 'action': 'action'},
                 'norm_stats_path': str(output / 'norm_stats.json'), 'train_test_split': .9,
                 'num_workers': 2, 'max_length': 1024,
                 'resolution': {slot: 448 for slot in reference['camera_mapping'].values()}},
        'logging': {'log_name': 'rebot_parcel', 'log_project': 'rebot_parcel_wallx', 'use_wandb': False,
                    'log_interval': 1, 'save_interval': min(args.save_every, args.steps),
                    'val_interval': min(200, args.steps), 'epoch_save_interval': 1000000},
        'checkpoint': {'save_path': str(output / 'checkpoints'), 'resume_from': str(base / 'model.safetensors')},
        'debug': {'profile': False, 'nvtx': False}}
    commands = [
        [sys.executable, str(repo / 'scripts/compute_norm_stats.py'), '--train_config', str(output / 'config.yml'),
         '--data_root', reference['dataset_dir'], '--output_path', str(output / 'norm_stats.json')],
        [sys.executable, '-m', 'torch.distributed.run', '--standalone', f'--nproc_per_node={args.gpus}',
         str(Path(__file__).with_name('native_train_entry.py')), '--config', str(output / 'config.yml')]]
    reference.update(runtime='wall_x_native', wallx_revision=WALLX_REVISION, base_model=str(base),
                     processor_dir=str(processor), output_dir=str(output), norm_key='rebot_parcel',
                     gpus=args.gpus, effective_batch=args.gpus * args.batch_size * args.accumulation,
                     optimizer_steps=args.steps, commands=commands)
    return repo, config, reference


def check_environment(gpus):
    import importlib.metadata
    import torch
    import flash_attn
    import wall_x.model.core.ops._cuda_ext_bin
    if importlib.metadata.version('lerobot') != '0.4.4':
        raise RuntimeError('原生链路固定 lerobot==0.4.4，不安装前一套 requirements-train.txt')
    if not torch.__version__.startswith('2.10.0') or torch.version.cuda != '12.8':
        raise RuntimeError('此交付验证组合要求 torch 2.10.0+cu128')
    if torch.cuda.device_count() < gpus or not torch.cuda.is_bf16_supported():
        raise RuntimeError(f'需要 {gpus} 张可见 CUDA 显卡并支持BF16')
    print('CUDA/flash-attn/Wall-X算子/LeRobot版本检查通过', flush=True)


def main(argv=None):
    args = parse(argv)
    repo, config, reference = prepare(args)
    print(json.dumps(reference, ensure_ascii=False, indent=2))
    for command in reference['commands']:
        print(shlex.join(command), flush=True)
    if args.dry_run:
        return 0
    sys.path.insert(0, str(repo))
    check_environment(args.gpus)
    from wall_x.config.loader import load_config
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', encoding='utf-8') as file:
        json.dump(config, file, ensure_ascii=False)
        file.flush()
        parsed = load_config(file.name)
        if parsed.action_dim != 26 or parsed.propri_dim != 26:
            raise ValueError('原生配置解析后维度不是26')
    if args.check:
        return 0
    output = Path(reference['output_dir'])
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (('config.yml', config), ('dataset_reference.json', reference)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(repo) + os.pathsep + env.get('PYTHONPATH', '')
    env['WALLX_REBOT_REFERENCE'] = str(output / 'dataset_reference.json')
    env.setdefault('OMP_NUM_THREADS', '2')
    for command in reference['commands']:
        result = subprocess.run(command, cwd=repo, env=env, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print('原生 Wall-X 训练失败：' + str(error), file=sys.stderr)
        raise SystemExit(1)
