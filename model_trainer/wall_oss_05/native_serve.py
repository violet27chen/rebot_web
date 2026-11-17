"""Inference-only upstream Wall-X service with strict B601 14-joint inputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from native_common import LAYOUT, WS_CAMERAS, check_repo, joint_payload, native_checkpoint


def joint_observation(preprocessor, state, views, *, numpy, data_class):
    # This adapter only builds a model observation. It has no robot controller.
    if not isinstance(state, dict) or set(state) != {'follow1_joints', 'follow2_joints'}:
        raise ValueError('只接受 follow1_joints/follow2_joints；follow*_pos 是末端位姿，不能填七关节角度')
    if len(state['follow1_joints']) != 7 or len(state['follow2_joints']) != 7:
        raise ValueError('每个从臂必须有7个实测关节')
    values = joint_payload([*state['follow1_joints'], *state['follow2_joints']])
    data = data_class(config=preprocessor.config)
    for side, key in (('left', 'follow1_joints'), ('right', 'follow2_joints')):
        data.save_state_data_with_key(numpy.asarray(values[key], dtype=numpy.float64)[None],
                                      f'follow_{side}_arm_joint_pos')
    data.dof_mask = numpy.ones((1, preprocessor.config.action_horizon, 26), dtype=numpy.float32)
    data.dof_mask[..., 14:] = 0
    output = {'robot_state_action_data': data}
    for slot, key in WS_CAMERAS.items():
        if key not in views:
            raise ValueError('缺少相机视角：' + key + '；不会补黑图')
        view = numpy.asarray(views[key])
        if view.ndim == 4 and view.shape[0] == 1:
            view = view[0]
        if view.ndim != 3 or view.shape[-1] != 3 or min(view.shape) < 1:
            raise ValueError('相机不是有效 RGB 图像：' + key)
        output[slot] = view
    preprocessor.current_robot_state_action = data
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wallx-dir', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=44660)
    parser.add_argument('--cuda-id', type=int, default=0)
    parser.add_argument('--accelerated', action='store_true', help='启用 CUDA Graph/实验引擎；先验证基础路径')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.cuda_id < 0:
        parser.error('端口/GPU编号无效')
    repo = check_repo(args.wallx_dir)
    checkpoint, config, reference = native_checkpoint(args.checkpoint)
    print(json.dumps({'runtime': 'wall_x_native', 'checkpoint': str(checkpoint),
                      'listen': f'{args.host}:{args.port}', 'request_state': ['follow1_joints', 'follow2_joints'],
                      'response': 'predict_action [H,26], first14 are native command degrees',
                      'norm_key': reference['norm_key'], 'accelerated': args.accelerated,
                      'hardware_commands_sent': 0}, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda_id)
    os.environ['ENABLE_FAST_PREPROCESS'] = 'false'
    os.environ['WALLX_VISION_ATTN_IMPLEMENTATION'] = 'flash_attention_2'
    os.environ['ENABLE_CUDA_GRAPH'] = str(args.accelerated).lower()
    os.environ['ENABLE_EXPERIMENTAL_ENGINE'] = str(args.accelerated).lower()
    os.environ['ENABLE_EXPERIMENTAL_INFERENCE_ENGINE'] = str(args.accelerated).lower()
    os.environ['WALLX_SMOOTH_ACTION'] = 'false'
    os.environ['WALLX_SMOOTH_GRIPPER'] = 'false'
    sys.path.insert(0, str(repo))
    import numpy as np
    from wall_x._vendor.harrix.serving._wallx_infer import robot
    from wall_x._vendor.harrix.serving.launch_serving import Args, EnvMode, ServerModelConfig, main as serve
    def observation(self, state, views):
        return joint_observation(self, state, views, numpy=np, data_class=robot.RobotStateActionData)
    robot.EX001RobotPreprocessor.get_observation = observation
    # Keep original training paths for provenance. Resolve a temporary inference
    # config to this machine, instead of rewriting the delivered checkpoint.
    config['model'].update(config_path=str(checkpoint / 'config.json'),
                           processor_path=str(checkpoint), pretrained_path=str(checkpoint))
    config['data']['norm_stats_path'] = str(checkpoint / 'norm_stats.json')
    with tempfile.TemporaryDirectory(prefix='wallx-rebot-infer-') as folder:
        runtime_config = Path(folder) / 'config.yml'
        runtime_config.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        serve(Args(env=EnvMode.X2ROBOT, host=args.host, port=args.port, serialize_actions=False,
                   image_passing_mode='base64', enable_cuda_graph=args.accelerated,
                   enable_experimental_engine=args.accelerated,
                   model_config=ServerModelConfig(checkpoint_path=str(checkpoint),
                       train_config_path=str(runtime_config), robot_type='ex001',
                       robot_use_joint_angle_control=True, robot_action_interpolate_multiplier=1,
                       action_horizon=32, norm_key=reference['norm_key'])))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print('原生模型服务失败：' + str(error), file=sys.stderr)
        raise SystemExit(1)
