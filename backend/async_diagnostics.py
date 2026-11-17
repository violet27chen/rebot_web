"""Real ACT/SmolVLA + shared-memory IPC + virtual followers. No hardware I/O."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--dataset-dir', type=Path, required=True)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--seconds', type=float, default=5.)
    parser.add_argument('--frame', type=int, default=0)
    parser.add_argument('--seed', type=int, help='可选：复现随机扩散预测，不修改模型')
    parser.add_argument('--flow-steps', type=int, default=0, help='0沿用模型；仅SmolVLA')
    parser.add_argument('--n-action-steps', type=int, default=0, help='每块执行步数；0沿用模型，上限chunk_size')
    parser.add_argument('--speed-deg-s', type=float, default=30., help='虚拟电机跟随速度，不是硬件设置')
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0 or not math.isfinite(args.speed_deg_s) or args.speed_deg_s <= 0 or args.frame < 0:
        parser.error('检查时长/模拟速度须为正数，帧号非负')
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.policies.factory import get_policy_class
    from lerobot.configs.policies import PreTrainedConfig
    from backend.async_act_runtime import AsyncACTPolicy

    model = args.model.expanduser().resolve()
    root = args.dataset_dir.expanduser().resolve()
    kind = json.loads((model / 'config.json').read_text())['type']
    if kind not in ('act', 'smolvla'):
        raise ValueError('此诊断入口支持 ACT / SmolVLA')
    get_policy_class(kind)
    config = PreTrainedConfig.from_pretrained(str(model))
    config.device = args.device
    config.pretrained_path = model
    dataset = LeRobotDataset('local/async_diagnostics', root=root, video_backend='pyav')
    item = dataset[args.frame]
    features = {k: v for k, v in dataset.features.items() if k.startswith('observation.') or k == 'action'}
    names = features['action']['names']
    if len(names) != 14:
        raise ValueError('需要现有双臂14维验证数据')
    observation = {}
    for key, spec in features.items():
        if not key.startswith('observation.'):
            continue
        value = item[key].cpu().numpy()
        if spec['dtype'] == 'video':
            value = np.rint(np.clip(value.transpose(1, 2, 0), 0, 1) * 255).astype(np.uint8)
        observation[key] = value
    physical = observation['observation.state'].copy()
    contract = json.loads((Path(__file__).resolve().parents[1] / 'sorting_operator/assets/robot_contract.json').read_text())
    joints = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_yaw', 'wrist_roll', 'gripper']
    expected = [f'{side}_{joint}.pos' for side in ('left', 'right') for joint in joints]
    if list(names) != expected or features['observation.state'].get('names') != expected:
        raise ValueError('数据关节顺序与双臂契约不一致')
    robot = SimpleNamespace(cameras={})
    for offset, side in ((0, 'left'), (7, 'right')):
        motors = {joint: SimpleNamespace(get_state=lambda index=index + offset: SimpleNamespace(pos=math.radians(float(physical[index]))))
                  for index, joint in enumerate(joints)}
        setattr(robot, side + '_arm', SimpleNamespace(motors=motors, config=SimpleNamespace(
            joint_directions=contract['joint_directions'], joint_limits=contract['joint_limits_deg'])))
    directions = np.array([contract['joint_directions'][joint] for _ in range(2) for joint in joints])
    client = AsyncACTPolicy(config, features, {'seed': args.seed, 'flow_matching_steps': args.flow_steps,
                                            'n_action_steps': args.n_action_steps})
    first = None
    frames = waits = limited = 0
    max_poll = max_error = max_step = 0.
    previous_command = physical.copy()
    try:
        client.start({})
        client.attach(robot, {}, dataset.fps)
        client.reset()
        began = previous_cycle = time.monotonic()
        while time.monotonic() - began < args.seconds:
            cycle = time.monotonic()
            dt = min(.1, cycle - previous_cycle)
            physical += np.clip(previous_command - physical, -args.speed_deg_s * dt, args.speed_deg_s * dt)
            previous_cycle = cycle
            observation['observation.state'] = physical.copy()
            started = time.monotonic()
            value = client.predict(observation, task=item.get('task', ''), robot_type='bi_seeed_b601_dm_follower')[0].numpy()
            max_poll = max(max_poll, time.monotonic() - started)
            command = value * directions
            max_step = max(max_step, float(np.max(np.abs(command - previous_command))))
            max_error = max(max_error, float(np.max(np.abs(command - physical))))
            previous_command = command
            if not client.waiting_for_prediction:
                frames += 1
                if first is None:
                    first = cycle - began
            elif first is not None:
                waits += 1
            limited += bool(client.tracking_status.get('lead_limited'))
            if client.failed:
                break
            time.sleep(max(0., 1 / dataset.fps - (time.monotonic() - cycle)))
        report = {'check': 'policy_process_IPC_and_virtual_feedback', 'policy_type': kind, 'model': str(model), 'device': args.device,
                  'hardware_opened': False, 'physical_success_verified': False,
                  'inference_pid': client.process.pid, 'prediction_frames': frames,
                  'action_chunk_steps': client.chunk_size, 'model_chunk_size': client.model_chunk_size,
                  'flow_matching_steps': client.flow_matching_steps,
                  'model_inference_passed': client.buffer.last_request > 0,
                  'command_validation_passed': not client.failed,
                  'waiting_frames_after_first_prediction': waits, 'lead_limited_frames': limited,
                  'first_prediction_s': first, 'max_control_call_ms': max_poll * 1000,
                  'max_motor_command_step_deg': max_step, 'max_motor_following_error_deg': max_error,
                  'stale_chunks': client.buffer.stale_chunks, 'failure_reason': client.failure_reason,
                  'passed': frames > 0 and not client.failed}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['passed'] else 1
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
