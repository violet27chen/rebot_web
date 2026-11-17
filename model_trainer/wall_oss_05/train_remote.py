"""Remote training only. Dry-run uses stdlib; never imports robot/camera drivers."""
from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys

from contract import BASE_MODEL, DEFAULT_MAP, LEROBOT_REVISION, LORA_TARGETS, dataset_contract, read_json


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-dir', type=Path, required=True, help='训练机上数据集的实际绝对路径')
    parser.add_argument('--repo-id', default='local/rebot_parcel')
    parser.add_argument('--camera-map', type=Path, default=DEFAULT_MAP)
    parser.add_argument('--base-model', default=BASE_MODEL, help='原生 LeRobot 基座或已有微调模型目录')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--mode', choices=('full', 'lora'), default='full')
    parser.add_argument('--gpus', type=int, choices=(1, 2, 4, 8), default=8)
    parser.add_argument('--steps', type=int, default=10000)
    parser.add_argument('--batch-size', type=int, default=1, help='每张卡的 microbatch')
    parser.add_argument('--accumulation', type=int, default=4)
    parser.add_argument('--workers', type=int, default=2, help='每个训练进程的 DataLoader workers')
    parser.add_argument('--learning-rate', type=float, default=5e-5)
    parser.add_argument('--save-every', type=int, default=500)
    parser.add_argument('--eval-split', type=float, default=0.1, help='按剧集留出验证数据，非机器人执行')
    parser.add_argument('--cpu-offload', action='store_true', help='FSDP 参数/优化器卸载，降低显存、增加CPU和传输开销')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check', action='store_true', help='检查训练环境，不下载基座、不启动训练')
    args = parser.parse_args(argv)
    if min(args.steps, args.batch_size, args.accumulation, args.save_every) < 1 or args.workers < 0:
        parser.error('步数、batch、累积、保存频率须为正，workers 非负')
    if not math.isfinite(args.learning_rate) or args.learning_rate <= 0 or not 0 <= args.eval_split < 1:
        parser.error('学习率/验证集比例无效')
    if args.cpu_offload and args.gpus == 1:
        parser.error('cpu-offload 仅用于多卡 FSDP')
    return args


def prepare(args):
    reference = dataset_contract(args.dataset_dir, args.camera_map)
    output = args.output_dir.expanduser().resolve()
    data = Path(reference['dataset_dir'])
    if output.exists() or output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError('训练输出须为新的目录，且必须与原始数据集分开')
    source = args.base_model
    if Path(source).expanduser().exists():
        source = str(Path(source).expanduser().resolve())
        policy = read_json(Path(source) / 'config.json')
        if policy.get('type') != 'wall_oss_05':
            raise ValueError('已有模型不是 wall_oss_05；不能拿 ACT 权重作为 VLA 微调基座')
        if policy.get('use_peft') or (Path(source) / 'adapter_config.json').exists():
            raise ValueError('加新数据请先 export_model.py 合并已有 adapter，再作为新微调基座')
        if not any((Path(source) / name).is_file() for name in ('model.safetensors', 'model.safetensors.index.json')):
            raise ValueError('本地微调基座缺少权重')
    elif source != BASE_MODEL:
        raise ValueError('基座仅接受官方 LeRobot WALL-OSS-0.5 或本地原生模型目录')
    if args.eval_split and reference['episodes'] < 2:
        raise ValueError('留出验证集至少需要两集；单集冒烟检查可设 --eval-split 0')
    native = [f'--dataset.root={data}', f'--dataset.repo_id={args.repo_id}',
              '--dataset.video_backend=pyav', f'--dataset.eval_split={args.eval_split}',
              f'--policy.path={source}', '--policy.device=cuda',
              '--policy.input_features=null', '--policy.output_features=null',
              '--policy.camera_key_mapping=' + json.dumps(reference['camera_mapping'], ensure_ascii=False),
              '--policy.recipe=null', '--policy.use_language_recipe=false',
              '--policy.text_loss_weight=0', '--policy.push_to_hub=false',
              f'--policy.optimizer_lr={args.learning_rate}',
              f'--policy.scheduler_warmup_steps={min(100, max(1, args.steps // 10))}',
              f'--policy.scheduler_decay_steps={max(2, args.steps)}',
              f'--output_dir={output}', '--job_name=rebot_parcel_wall_oss_05',
              f'--steps={args.steps}', f'--batch_size={args.batch_size}',
              f'--num_workers={args.workers}', f'--save_freq={min(args.save_every, args.steps)}',
              '--log_freq=10', '--env_eval_freq=0', '--trackio.enable=false',
              f'--eval_steps={min(500, args.steps) if args.eval_split else 0}', '--max_eval_samples=128',
              '--accelerator.mixed_precision=bf16',
              f'--accelerator.gradient_accumulation.steps={args.accumulation}']
    if args.gpus > 1:
        native += [f'--parallelism.dp_shard={args.gpus}',
                   '--accelerator.fsdp.wrap_modules=["WallOSS05DecoderLayer","Qwen2_5_VLVisionBlock"]',
                   '--accelerator.fsdp.reshard_after_forward=true',
                   f'--accelerator.fsdp.cpu_offload={str(args.cpu_offload).lower()}']
    if args.mode == 'lora':
        native += ['--peft.method_type=LORA', '--peft.r=16', '--peft.lora_alpha=32',
                   '--peft.target_modules=' + LORA_TARGETS]
    command = [sys.executable, '-m', 'torch.distributed.run', '--standalone',
               f'--nproc_per_node={args.gpus}', '--module', 'lerobot.scripts.lerobot_train', *native]
    reference.update(base_model=source, output_dir=str(output), training_mode=args.mode,
                     gpus=args.gpus, effective_batch=args.batch_size * args.accumulation * args.gpus,
                     steps=args.steps, command=command,
                     created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    return command, reference


def check_environment(gpus):
    if sys.version_info < (3, 12):
        raise RuntimeError('此固定版本的 LeRobot 要求 Python 3.12 或更高')
    import torch
    from lerobot.policies.wall_oss_05.modeling_wall_oss_05 import WallOSS05Policy
    from lerobot.configs.accelerator import AcceleratorConfig
    from lerobot.scripts import lerobot_train
    import peft
    if not torch.cuda.is_available() or torch.cuda.device_count() < gpus:
        raise RuntimeError(f'需要 {gpus} 张可见 CUDA 显卡；当前 {torch.cuda.device_count()} 张')
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError('训练配置要求 BF16')
    report = {'python': sys.version, 'torch': torch.__version__, 'torch_cuda': torch.version.cuda,
              'lerobot': importlib.metadata.version('lerobot'),
              'gpus': [torch.cuda.get_device_name(i) for i in range(gpus)]}
    direct = json.loads(importlib.metadata.distribution('lerobot').read_text('direct_url.json') or '{}')
    commit = direct.get('vcs_info', {}).get('commit_id')
    if commit != LEROBOT_REVISION:
        raise RuntimeError('LeRobot 来源不匹配，请在独立训练环境安装本目录 requirements-train.txt；不会升级控制环境')
    return report


def main(argv=None):
    args = parse(argv)
    command, reference = prepare(args)
    print(json.dumps(reference, ensure_ascii=False, indent=2))
    print('\n训练命令：\n' + shlex.join(command), flush=True)
    print('三路视角必须核对实物；保留原始关节方向/单位，训练不自动校准硬件。', flush=True)
    if args.dry_run:
        return 0
    report = check_environment(args.gpus)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.check:
        return 0
    output = Path(reference['output_dir'])
    output.parent.mkdir(parents=True, exist_ok=True)
    # Native trainer creates output_dir itself; the sidecar survives a failed run.
    sidecar = output.with_name(output.name + '.dataset_reference.json')
    with sidecar.open('x', encoding='utf-8') as file:
        json.dump(reference, file, ensure_ascii=False, indent=2)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('REBOT_', 'LEROBOT_RECORD_'))}
    env.setdefault('OMP_NUM_THREADS', '2')
    env.setdefault('TOKENIZERS_PARALLELISM', 'false')
    result = subprocess.run(command, env=env, check=False)
    for config in output.glob('checkpoints/*/pretrained_model/config.json'):
        (config.parent / 'dataset_reference.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
    if output.is_dir():
        (output / 'environment.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print(f'WALL 训练未启动或失败：{error}', file=sys.stderr)
        raise SystemExit(1)
