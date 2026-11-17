"""Inference-device offline validation. No serial, CAN, camera or robot commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from contract import DEFAULT_MAP, checkpoint_contract, dataset_contract


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--dataset-dir', type=Path, required=True, help='本机已完成采集的验证数据集')
    parser.add_argument('--camera-map', type=Path, default=DEFAULT_MAP)
    parser.add_argument('--infer', action='store_true', help='运行真实模型，用已保存的视频，不驱动机械臂')
    parser.add_argument('--device', choices=('cuda', 'cpu'), default='cuda')
    parser.add_argument('--start-frame', type=int, default=0)
    parser.add_argument('--frames', type=int, default=10)
    parser.add_argument('--task', help='默认使用验证帧中记录的原始任务指令')
    parser.add_argument('--report', type=Path, help='可选 JSON 报告，必须是新文件')
    args = parser.parse_args(argv)
    if args.start_frame < 0 or args.frames < 1:
        parser.error('起始帧非负，验证帧数为正')
    return args


def validate(args):
    expected = dataset_contract(args.dataset_dir, args.camera_map)
    model, config, handoff = checkpoint_contract(args.model, expected)
    report = {'mode': 'metadata', 'model': str(model), 'contract_valid': True,
              'policy_type': config['type'], 'action_dimension': 14,
              'camera_mapping': expected['camera_mapping'],
              'training_dataset_info_sha256': handoff.get('info_sha256'),
              'validation_dataset_info_sha256': expected['info_sha256'],
              'hardware_commands_sent': 0,
              'scope': '接口/离线动作检查；不能证明快递分拣成功率或实机动作安全'}
    if not args.infer:
        return report
    import torch
    from lerobot.policies.wall_oss_05.configuration_wall_oss_05 import WallOSS05Config
    from lerobot.configs import PreTrainedConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.policies.factory import make_policy, make_pre_post_processors
    if args.device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA 不可用；请检查独立推理环境')
    root = Path(expected['dataset_dir'])
    ds = LeRobotDataset('local/rebot_validation', root=root, video_backend='pyav')
    if args.start_frame + args.frames > len(ds):
        raise ValueError('验证帧超出数据集范围')
    cfg = PreTrainedConfig.from_pretrained(str(model))
    cfg.device = args.device
    cfg.pretrained_path = model
    if (model / 'preprocessor_config.json').is_file():
        cfg.pretrained_name_or_path = str(model)
        cfg.tokenizer_revision = None
    policy = make_policy(cfg, ds_meta=ds.meta).eval()
    # Saved TRAINING statistics are authoritative; never replace them with the
    # validation dataset's distribution.
    overrides = {'device_processor': {'device': args.device}}
    if (model / 'preprocessor_config.json').is_file():
        overrides['wall_oss_05_tokenizer'] = {'processor_name': str(model), 'revision': None}
    pre, post = make_pre_post_processors(
        cfg, pretrained_path=str(model), dataset_meta=ds.meta,
        preprocessor_overrides=overrides)
    policy.reset()
    pre.reset()
    post.reset()
    times, rows = [], []
    if args.device == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        for index in range(args.start_frame, args.start_frame + args.frames):
            item = ds[index]
            batch = {key: item[key].unsqueeze(0) for key in cfg.input_features}
            task = args.task if args.task is not None else item.get('task')
            if not isinstance(task, str) or not task.strip():
                raise ValueError('验证数据缺少语言任务，请提供 --task')
            batch['task'] = [task]
            batch = pre(batch)
            if args.device == 'cuda':
                torch.cuda.synchronize()
            started = time.perf_counter()
            predicted = policy.predict_action_chunk(batch)
            shape = predicted.shape
            if predicted.ndim != 3 or shape[0] != 1 or shape[-1] != 14:
                raise ValueError(f'模型返回错误维度：{tuple(shape)}')
            predicted = post(predicted.reshape(-1, shape[-1])).reshape(shape)
            if args.device == 'cuda':
                torch.cuda.synchronize()
            elapsed = time.perf_counter() - started
            if not torch.isfinite(predicted).all():
                raise ValueError('动作存在 NaN/Inf')
            # Compare in recorded command coordinates; state and action use
            # different sign/gear conventions, so do not subtract state here.
            first = predicted[0, 0].float().cpu()
            target = item['action'].float().cpu()
            error = (first - target).abs()
            rows.append({'frame': index, 'task': task, 'prediction_command_degrees': first.tolist(),
                         'first_action_mae_command_degrees': error.mean().item(),
                         'first_action_max_error_command_degrees': error.max().item(),
                         'chunk_steps': shape[1], 'inference_s': elapsed})
            times.append(elapsed)
    report.update(mode='offline_inference', frames=rows, device=args.device,
                  first_inference_s=times[0],
                  steady_mean_inference_s=sum(times[1:]) / len(times[1:]) if len(times) > 1 else None,
                  nominal_chunk_s=config.get('chunk_size', 32) / expected['fps'],
                  peak_cuda_allocated_gib=torch.cuda.max_memory_allocated() / 2**30 if args.device == 'cuda' else None)
    return report


def main(argv=None):
    args = parse(argv)
    report = validate(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        with args.report.expanduser().open('x', encoding='utf-8') as file:
            file.write(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print(f'WALL 验证失败：{error}', file=sys.stderr)
        raise SystemExit(1)
