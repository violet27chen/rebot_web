"""
异步进程化 ACT 策略推理向 LeRobot 录制循环注入钩子 (Async ACT Hook)
========================================================================

【模块背景与定位】
在 LeRobot 的评估（eval）或模仿学习闭环控制执行中，原版的策略预测 `predict_action`
是在机械臂控制循环同一个主线程内同步调用的。对于大型 ACT（Action Chunking with Transformers）
网络，前向推理耗时可能达到 50ms~150ms，会严重拖慢甚至破坏底层机械臂的固定控制频率（如 30Hz/50Hz），
导致电机出现明显的卡顿甚至丢步。

【核心设计与挂接机制】
1. 策略劫持：
   拦截 `make_policy`，若策略类型为 'act'，将其替换为隔离子进程运行的 `AsyncACTPolicy`。
2. 预处理器透传：
   拦截 `make_pre_post_processors`，由于图像张量归一化和预处理已在异步子进程内完成，
   向主循环返回轻量级的空透传处理器 `IdentityProcessor`。
3. 剧集重置期防掉臂保护：
   官方重置循环在没有示教器输入时会产生忙等待。本钩子在重置循环期间主动调用 `hold_feedback`，
   向机械臂下发当前实测角度进行平稳力矩保持，绝不回零、绝不掉电失能，等待下一剧集平滑开始。
"""
import functools
import os
import time

from backend.async_act_runtime import AsyncACTPolicy, IdentityProcessor, hold_feedback


def install(module):
    """
    向 LeRobot 录制/评估模块注入异步 ACT 策略和控制循环包装。

    Args:
        module: 目标脚本模块（如 `lerobot.scripts.lerobot_record`）。

    Returns:
        callable: 资源清理函数，退出时终止所有拉起的异步推理进程。
    """
    original_policy = module.make_policy
    original_processors = module.make_pre_post_processors
    original_predict = module.predict_action
    original_loop = module.record_loop
    created = []

    # 从环境变量解析异步调度微调参数
    settings = {
        'chunk_size': int(os.environ.get('REBOT_ASYNC_CHUNK', '0')),
        'prefetch_ratio': float(os.environ.get('REBOT_ASYNC_PREFETCH', '.6')),
        'max_age_s': float(os.environ.get('REBOT_ASYNC_MAX_AGE', '1.5')),
        'wait_timeout_s': float(os.environ.get('REBOT_ASYNC_WAIT', '2')),
        'max_step_deg': float(os.environ.get('REBOT_ASYNC_MAX_STEP', '2'))
    }

    def make_policy(cfg, ds_meta=None, **kwargs):
        """若为 ACT 策略则构建异步进程化代理策略对象。"""
        if cfg.type not in ('act', 'smolvla'):
            raise ValueError('异步动作块仅支持 ACT / SmolVLA，当前类型：' + str(cfg.type))
        import torch
        policy = AsyncACTPolicy(cfg, ds_meta.features, dict(settings, seed=torch.initial_seed()))
        created.append(policy)
        return policy

    def processors(policy_cfg, pretrained_path=None, **kwargs):
        """启动异步推理子进程，并返回空操作处理器。"""
        if policy_cfg.type not in ('act', 'smolvla'):
            return original_processors(policy_cfg, pretrained_path=pretrained_path, **kwargs)
        created[-1].start(kwargs)
        return IdentityProcessor(), IdentityProcessor()

    def predict_action(*args, **kwargs):
        """转发动作预测至异步策略对象。"""
        policy = kwargs.get('policy', args[1] if len(args) > 1 else None)
        if not isinstance(policy, AsyncACTPolicy):
            return original_predict(*args, **kwargs)
        observation = kwargs.get('observation', args[0] if args else None)
        return policy.predict(observation, kwargs.get('task', ''), kwargs.get('robot_type', ''))

    @functools.wraps(original_loop)
    def record_loop(*args, **kwargs):
        """包装录制控制主循环，增强重置期的力矩悬停与故障感知。"""
        policy = kwargs.get('policy', args[8] if len(args) > 8 else None)
        robot = kwargs.get('robot', args[0] if args else None)
        events = kwargs.get('events', args[1] if len(args) > 1 else None)
        fps = kwargs.get('fps', args[2] if len(args) > 2 else None)

        # 剧集重置期安全维持：当 policy 为 None 且无示教输入时，平稳维持当前实测姿态
        if policy is None and kwargs.get('teleop') is None and kwargs.get('dataset') is None and created:
            deadline = time.monotonic() + float(kwargs.get('control_time_s') or 0)
            measured, _ = hold_feedback(robot, created[-1].names)
            hold = dict(zip(created[-1].names, measured.tolist()))
            print('[异步 ACT] 剧集重置：保持当前姿态，等待设定时间；不回零、不失能', flush=True)
            while time.monotonic() < deadline and not events.get('stop_recording'):
                if events.get('exit_early'):
                    events['exit_early'] = False
                    break
                hold_feedback(robot, created[-1].names)  # 严格校验反馈有效性
                robot.send_action(hold)
                time.sleep(max(0.0, min(1 / fps, deadline - time.monotonic())))
            return

        if not isinstance(policy, AsyncACTPolicy):
            return original_loop(*args, **kwargs)

        policy.attach(robot, events, fps)
        try:
            result = original_loop(*args, **kwargs)
            if policy.failed:
                raise RuntimeError(f'异步 {policy.policy_label} 已停止：' + (policy.failure_reason or '未返回有效预测；请查看前面的安全停止日志'))
            return result
        finally:
            policy.deactivate()

    # 替换模块内的目标接口
    module.make_policy, module.make_pre_post_processors = make_policy, processors
    module.predict_action, module.record_loop = predict_action, record_loop

    def close():
        """清理所有创建的异步策略子进程。"""
        for p in created:
            p.close()

    return close
