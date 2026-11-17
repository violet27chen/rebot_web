"""Run on the TRAINING machine: export a full, portable WALL policy for inference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from contract import checkpoint_contract, dataset_contract, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True, help='checkpoints/NNNNNN/pretrained_model')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu', help='CPU 合并需要足够内存，不连接硬件')
    args = parser.parse_args(argv)
    source = args.checkpoint.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if output.exists() or output.is_relative_to(source):
        raise ValueError('导出必须使用新的独立目录')
    reference = read_json(source / 'dataset_reference.json')
    dataset = Path(reference['dataset_dir'])
    if read_json(source / 'config.json').get('type') != 'wall_oss_05':
        raise ValueError('不是原生 LeRobot WALL-OSS-0.5 检查点')
    import torch
    from lerobot.policies.wall_oss_05.configuration_wall_oss_05 import WallOSS05Config
    from lerobot.configs import PreTrainedConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDatasetMetadata
    from lerobot.policies.factory import make_policy
    config = PreTrainedConfig.from_pretrained(str(source))
    config.device = args.device
    config.pretrained_path = source
    meta = LeRobotDatasetMetadata('local/rebot_parcel', root=dataset)
    policy = make_policy(config, ds_meta=meta)
    if hasattr(policy, 'merge_and_unload'):
        policy = policy.merge_and_unload()
    policy.config.use_peft = False
    policy.config.pretrained_path = None
    policy.config.pretrained_revision = None
    # Tokenizer assets come with the handoff; the local validator resolves this
    # stable model identity to the export's directory, without remote base weights.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wall-export-', dir=output.parent) as temp:
        stage = Path(temp) / 'model'
        stage.mkdir()
        policy.to('cpu').save_pretrained(stage)
        for pattern in ('policy_preprocessor*', 'policy_postprocessor*'):
            for file in source.glob(pattern):
                if file.is_file():
                    shutil.copy2(file, stage / file.name)
        policy._get_processor().save_pretrained(stage)
        (stage / 'dataset_reference.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=temp, encoding='utf-8') as mapping:
            json.dump(reference['camera_mapping'], mapping, ensure_ascii=False)
            mapping.flush()
            expected = dataset_contract(dataset, mapping.name)
            checkpoint_contract(stage, expected)
        if output.exists():
            raise ValueError('导出目录已被其他进程创建；未覆盖')
        stage.rename(output)
    print('导出完成：' + str(output))
    print('请复制整个目录到推理设备；不能只复制 model.safetensors。')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print(f'导出失败：{error}', file=sys.stderr)
        raise SystemExit(1)
