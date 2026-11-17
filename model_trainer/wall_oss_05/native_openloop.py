"""Use official open-loop evaluator with B601 joint coordinates and plot labels."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

from contract import NAMES, read_json
from native_common import WS_CAMERAS, check_repo, joint_payload, read_config, require_joint_layout


def adapt_evaluator(module):
    module.build_state_payload = lambda state, train_config: joint_payload(state)
    module._dim_label = lambda index, dimension: NAMES[index] + ' (command deg)' if dimension == 14 else f'joint_{index}'
    module.build_camera_ws_mapping = lambda cfg: {
        key: WS_CAMERAS[slot] for key, slot in cfg['data']['key_mappings']['camera'].items()}
    original_observation = module.build_obs_payload
    def observation(episode, frame_idx, train_config, cam_ws_mapping, extra_view_keys):
        images = episode.get_frame_images(frame_idx)
        if set(cam_ws_mapping) - set(images):
            raise ValueError('开环输入缺少真实相机画面；不会补黑图')
        return original_observation(episode, frame_idx, train_config, cam_ws_mapping, extra_view_keys=[])
    module.build_obs_payload = observation
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--wallx-dir', type=Path, required=True)
    known, remaining = parser.parse_known_args(argv)
    repo = check_repo(known.wallx_dir)
    if '--train-config' not in remaining:
        raise ValueError('请指定 --train-config /导出的模型/config.yml')
    config = require_joint_layout(read_config(remaining[remaining.index('--train-config') + 1]))
    mapping = config['data']['key_mappings']['camera']
    if set(mapping.values()) != set(WS_CAMERAS) or len(mapping) != 3:
        raise ValueError('开环验证要求配置全部三路相机')
    if '--dataset-root' not in remaining:
        raise ValueError('请指定 --dataset-root /本机验证数据集')
    root = Path(remaining[remaining.index('--dataset-root') + 1]).expanduser()
    features = read_json(root / 'meta/info.json')['features']
    for key in ('action', 'observation.state'):
        if features.get(key, {}).get('shape') != [14] or features[key].get('names') != NAMES:
            raise ValueError('验证数据的关节顺序不匹配：' + key)
    if set(mapping) != {key for key in features if key.startswith('observation.images.')}:
        raise ValueError('验证集三相机键与模型不匹配')
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location('rebot_wallx_openloop', repo / 'scripts/draw_openloop_plot.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    adapt_evaluator(module)
    sys.argv = [str(repo / 'scripts/draw_openloop_plot.py'), *remaining]
    module.main()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print('原生开环验证失败：' + str(error), file=sys.stderr)
        raise SystemExit(1)
