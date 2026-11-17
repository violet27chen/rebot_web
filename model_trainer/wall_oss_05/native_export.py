"""Copy complete upstream artifacts on the training host for local inference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from contract import read_json
from native_common import native_checkpoint, read_config, require_joint_layout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True, help='含完整 model.safetensors 的原生检查点')
    parser.add_argument('--reference', type=Path, help='可选：本次训练输出根目录 dataset_reference.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.checkpoint.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if output.exists() or output.is_relative_to(source):
        raise ValueError('使用新的独立导出目录')
    if not (source / 'model.safetensors').is_file():
        raise ValueError('没有完整 model.safetensors；请先用官方 merge_sharded_weights.py 合并，并补齐同次检查点的配置和统计')
    config = require_joint_layout(read_config(source / 'config.yml'))
    reference = read_json(args.reference or source / 'dataset_reference.json')
    if reference.get('runtime') != 'wall_x_native':
        raise ValueError('不是本次原生训练的数据引用')
    processor = Path(reference['processor_dir'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wallx-export-', dir=output.parent) as temp:
        stage = Path(temp) / 'model'
        stage.mkdir()
        for name in ('model.safetensors', 'config.json', 'norm_stats.json'):
            shutil.copy2(source / name, stage / name)
        # Copy all processor/tokenizer assets, including merges.txt and added
        # tokens that upstream's checkpoint metadata allowlist may omit.
        for file in processor.iterdir():
            if file.is_file() and file.suffix in ('.json', '.txt', '.model', '.tiktoken') and file.name not in ('config.json', 'generation_config.json'):
                shutil.copy2(file, stage / file.name)
        (stage / 'config.yml').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / 'dataset_reference.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
        native_checkpoint(stage)
        if output.exists():
            raise ValueError('导出目录被其他进程创建，未覆盖')
        stage.rename(output)
    print('Wall-X 原生推理交付完成：' + str(output))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print('原生模型导出失败：' + str(error), file=sys.stderr)
        raise SystemExit(1)
