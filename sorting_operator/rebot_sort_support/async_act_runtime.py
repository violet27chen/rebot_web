"""
非阻塞异步 ACT 控制客户端运行时 (Non-blocking Async ACT Runtime Client)
========================================================================

【模块背景与定位】
在 LeRobot 录制与真实机械臂闭环评估控制主循环中，本模块作为高层策略代理对象
（`AsyncACTPolicy`）运行在机器人主控制线程内。
【设计原则】：
GPU 深度模型推理全部运行在独立子进程（`act_inference_process`）中，
主控制进程【独占】机械臂硬件总线，绝不初始化 GPU CUDA 上下文，
保证控制循环以严格的 30Hz/50Hz 时钟周期稳定运转。

【核心特性与安全机制】
1. 实测位置防掉臂保持 (`hold_feedback`):
   将电机物理角度准确映射回从臂控制命令空间（正确处理各个关节的方向符号和夹爪 -6.0 减速比），
   在剧集重置期或等待推理期间，安全发送当前实测角度进行平稳力矩悬停，杜绝掉臂。
2. 提前预取流水线（Prefetch Pipeline）:
   当缓冲区剩余步数降至阈值（`lead = chunk_size * prefetch_ratio`）时，
   自动向共享内存写入最新帧并提前触发下一批动作块推理，使推理延迟与物理执行完美重叠。
3. 严格相机时间戳防过期（Stale Frame Guard）:
   校验所有相机的捕获时间戳，若超过 `max_age`（如 1.5s 无新图像），判定相机掉线，
   立即安全停止推理，绝不用旧视觉画面盲开。
4. 异常安全收尾熔断:
   捕获任何子进程崩溃、数据异常或推理超时（`wait_timeout`），
   自动将退出事件置位（`exit_early = True, stop_recording = True`），
   以当前实测位置平滑保持并触发机械臂正常收尾。
"""
import copy
import json
import math
import multiprocessing as mp
from multiprocessing import shared_memory
import os
import time
import numpy as np

from rebot_sort_support.async_action_buffer import ActionBuffer, AsyncActionError, FeedbackProgressWatchdog, TargetProjectionWatchdog, limit_action
from rebot_sort_support.act_inference_process import run_actor


def hold_feedback(robot, names):
    """
    将机械臂当前物理电机的实时编码器读数转换为 `send_action` 的目标控制命令空间。

    【重要设计考量】
    从臂观察状态（observation）记录的是底层电机的原始物理度数，
    而 `send_action` 在下发底层前会自动乘以各个关节的 `joint_directions` 映射系数
    （例如 7 号夹爪关节包含约 -6.0 的齿轮减速比反向系数）。
    如果直接把原始 state 作为 action 重新发送，会导致机械臂特别是夹爪反向剧烈抽动！
    因此必须严格通过除以 direction 还原为一致的命令空间数值。

    Args:
        robot: 机械臂控制实例。
        names (list[str]): 关节特征名称列表（如 ['left_joint_1.pos', ...]）。

    Returns:
        tuple[np.ndarray, np.ndarray]: (换算后的保持目标角度数组, 关节方向映射系数数组)。

    Raises:
        AsyncActionError: 当有电机缺失有效物理反馈时抛出。
    """
    values, directions = [], []
    for name in names:
        joint = name.removesuffix('.pos')
        arm = robot
        # 兼容双臂机器人结构
        for side in ('left', 'right'):
            if joint.startswith(side + '_') and hasattr(robot, side + '_arm'):
                arm = getattr(robot, side + '_arm')
                joint = joint[len(side) + 1:]
                break
        direction = float(arm.config.joint_directions.get(joint, 0))
        motor = arm.motors.get(joint)
        state = motor.get_state() if motor else None
        if not direction or not math.isfinite(direction) or state is None or not math.isfinite(state.pos):
            raise AsyncActionError(f'{name} 缺少有效实测反馈/方向映射，未用零值代替')
        values.append(math.degrees(state.pos) / direction)
        directions.append(direction)
    return np.asarray(values, np.float32), np.asarray(directions, np.float32)


def physical_limits(robot, names):
    """从机械臂配置文件中提取所有参与关节的物理软限位区间。"""
    bounds = []
    for name in names:
        joint = name.removesuffix('.pos')
        arm = robot
        for side in ('left', 'right'):
            if joint.startswith(side + '_') and hasattr(robot, side + '_arm'):
                arm = getattr(robot, side + '_arm')
                joint = joint[len(side) + 1:]
                break
        limits = getattr(arm.config, 'joint_limits', {}).get(joint)
        if limits is None:
            return None
        bounds.append(limits)
    return bounds


class IdentityProcessor:
    """轻量级空操作预处理器占位符。"""
    def reset(self):
        pass


class AsyncACTPolicy:
    """
    异步 ACT 策略客户端核心类。
    管理跨进程共享内存生命周期、非阻塞预测流水线以及安全限制。
    """

    def __init__(self, config, features, settings=None, actor_target=run_actor):
        """
        初始化异步策略客户端。

        Args:
            config: ACT 预训练策略配置。
            features (dict): 机器人数据集特征映射。
            settings (dict, optional): 异步流水线调优参数。
            actor_target (callable, optional): 推理子进程启动目标函数。
        """
        self.model_config = copy.deepcopy(config)
        self.config = copy.deepcopy(config)
        self.policy_type = getattr(config, 'type', 'act')
        self.policy_label = 'SmolVLA' if self.policy_type == 'smolvla' else 'ACT'
        if self.policy_type not in ('act', 'smolvla'):
            raise AsyncActionError('异步动作块仅支持 ACT / SmolVLA')
        self.config.device = 'cpu'  # 严格将主控制循环端的设备标识设为 cpu，杜绝在主循环初始化 CUDA
        self.features = features
        self.names = list(features['action']['names'])
        if len(self.names) != config.output_features['action'].shape[0]:
            raise AsyncActionError('检查点动作维度与当前机械臂不一致')

        self.context = mp.get_context('spawn')
        self.mailbox_lock = self.context.Lock()
        self.settings = settings or {}
        flow_steps = self.settings.get('flow_matching_steps') or getattr(config, 'num_steps', 0)
        if not math.isfinite(float(flow_steps)) or not float(flow_steps).is_integer() or not 0 <= float(flow_steps) <= 50:
            raise AsyncActionError('Flow Matching采样次数必须是0–50的整数')
        self.flow_matching_steps = int(flow_steps)
        if self.policy_type != 'smolvla' and self.flow_matching_steps:
            raise AsyncActionError('Flow Matching采样仅适用于SmolVLA')
        execution_steps = float(self.settings.get('n_action_steps', self.settings.get('chunk_size', 0)))
        if not math.isfinite(execution_steps) or not execution_steps.is_integer() or not 0 <= execution_steps <= 1000:
            raise AsyncActionError('n_action_steps必须是0–1000的整数')
        # The actor reloads checkpoint JSON; forward CLI overrides explicitly.
        self.chunk_size = int(execution_steps or getattr(config, 'n_action_steps', 0))
        if getattr(config, 'chunk_size', None):
            self.chunk_size = min(self.chunk_size, config.chunk_size)
        self.ratio = float(self.settings.get('prefetch_ratio', .6))
        self.max_age = float(self.settings.get('max_age_s', 1.5))
        self.wait_timeout = float(self.settings.get('wait_timeout_s', 2.0))
        self.max_step = float(self.settings.get('max_step_deg', 2.0))
        self.feedback_lead_deg = 6.0
        self.feedback_watchdog = FeedbackProgressWatchdog(lead_deg=self.feedback_lead_deg)
        self.projection_watchdog = TargetProjectionWatchdog()
        self.last_projection_log = -math.inf
        self.tracking_status = {}

        if (
            not (0 <= self.chunk_size <= 1000) or
            not (0.1 <= self.ratio <= 0.95) or
            not (0.1 <= self.max_age <= 5.0) or
            not (0.1 <= self.wait_timeout <= 10.0) or
            not (0.1 <= self.max_step <= 5.0)
        ):
            raise AsyncActionError('异步推理参数超出允许范围')

        self.actor_target = actor_target
        self.memories, self.slots, self.specs = [], {}, {}
        self.connection = self.process = self.buffer = None
        self.pending = None
        self.sequence = 0
        self.last_inference_s = 0.0
        self.robot = self.events = None
        self.waiting_for_prediction = True
        self.previous = None
        self.failed = False
        self.failure_reason = ''
        self.last_log = 0.0

    def start(self, processor_options, startup_timeout=120):
        """
        创建观察输入的共享内存块，拉起推理子进程，并阻塞等待模型完成预热（Warmup）。
        模型完成预热前绝不连接或打开物理机械臂，避免机械臂处于空闲带电等待。
        """
        if self.process:
            return
        try:
            # 步骤 1：为各个观察输入创建跨进程共享内存
            for name, feature in self.features.items():
                if not name.startswith('observation.'):
                    continue
                shape = tuple(feature['shape'])
                dtype = np.dtype('uint8' if feature['dtype'] in ('video', 'image') else feature['dtype'])
                memory = shared_memory.SharedMemory(create=True, size=math.prod(shape) * dtype.itemsize)
                self.memories.append(memory)
                self.slots[name] = np.ndarray(shape, dtype=dtype, buffer=memory.buf)
                self.specs[name] = {'shape': shape, 'dtype': str(dtype), 'memory': memory.name}

            # 步骤 2：创建管道并拉起独立推理子进程
            self.connection, child = self.context.Pipe()
            actor_options = copy.deepcopy(processor_options or {})
            if self.settings.get('seed') is not None:
                actor_options['_rebot_seed'] = int(self.settings['seed'])
            if self.policy_type == 'smolvla' and self.flow_matching_steps:
                actor_options['_rebot_flow_steps'] = self.flow_matching_steps
            self.process = self.context.Process(
                target=self.actor_target,
                args=(
                    child,
                    self.mailbox_lock,
                    self.specs,
                    str(self.model_config.pretrained_path),
                    self.model_config.device,
                    self.model_config.use_amp,
                    actor_options,
                    self.chunk_size
                ),
                name='rebot-policy-inference',
                daemon=True
            )
            print(f'[异步 {self.policy_label}] 正在独立进程加载并预热模型；完成前不连接机械臂…', flush=True)
            self.process.start()
            child.close()

            # 步骤 3：等待子进程发送 ready 就绪握手
            deadline = time.monotonic() + startup_timeout
            while not self.connection.poll(0.1):
                if not self.process.is_alive():
                    raise AsyncActionError('ACT 推理进程在加载期间退出')
                if time.monotonic() >= deadline:
                    raise AsyncActionError('ACT 模型加载/预热超时，未连接机械臂')

            try:
                reply = self.connection.recv()
            except EOFError as exc:
                raise AsyncActionError(f'ACT初始化通道已关闭，推理进程退出码 {self.process.exitcode}') from exc

            if reply['type'] != 'ready':
                raise AsyncActionError(reply.get('message', 'ACT 初始化失败'))
            if reply.get('policy_type', self.policy_type) != self.policy_type:
                raise AsyncActionError('推理进程加载的策略类型不一致')

            self.chunk_size = reply['chunk_size']
            self.model_chunk_size = reply.get('model_chunk_size', self.chunk_size)
            self.flow_matching_steps = reply.get('flow_matching_steps', self.flow_matching_steps)
            detail = dict(policy_type=self.policy_type, action_chunk_steps=self.chunk_size,
                          n_action_steps=self.chunk_size,
                          model_chunk_size=self.model_chunk_size, flow_matching_steps=self.flow_matching_steps,
                          inference_pid=reply['pid'])
            print('REBOT_INFERENCE_READY ' + json.dumps(detail), flush=True)
            print(
                f'[异步 {self.policy_label}] 模型已预热 · 推理 PID {reply["pid"]} · 每块 {self.chunk_size} 步 · 提前补充阈值 {self.ratio:.0%}',
                flush=True
            )
            if self.policy_type == 'smolvla':
                print(f'[异步 SmolVLA] Action Chunk {self.chunk_size}步 / 模型{self.model_chunk_size}步 · '
                      f'Flow Matching {self.flow_matching_steps}次采样迭代 · Async独立进程', flush=True)
        except BaseException:
            self.close()
            raise

    def attach(self, robot, events, fps):
        """绑定当前运行的机械臂实体、事件字典与控制采样频率。"""
        if not fps or not math.isfinite(fps) or fps <= 0:
            raise AsyncActionError('控制频率必须为正数')
        self.robot, self.events, self.fps = robot, events, fps

    def reset(self):
        """重置动作缓冲区，递增代数序列号使旧异步数据作废。"""
        self.buffer = ActionBuffer(len(self.names), self.fps, self.max_age, preserve_existing=True)
        self.generation = getattr(self, 'generation', 0) + 1
        self.buffer.generation = self.generation
        self.epoch = None
        self.tick = -1
        self.previous = None
        self.waiting_for_prediction = True
        self.failed = False
        self.failure_reason = ''
        self.starved_at = None
        self.last_log = 0.0
        self.feedback_watchdog.reset()
        self.projection_watchdog.reset()
        self.last_projection_log = -math.inf
        self.tracking_status = {}

    def deactivate(self):
        """临时脱离机器人控制上下文。"""
        if self.buffer:
            self.buffer.reset()
        self.robot = self.events = None
        self.waiting_for_prediction = True

    def _receive(self, now, tick):
        """非阻塞轮询管道，接收子进程返回的动作块并灌入 ActionBuffer。"""
        if not self.process:
            raise AsyncActionError(f'{self.policy_label} 推理进程已退出')
        while self.connection.poll():
            try:
                result = self.connection.recv()
            except EOFError:
                break
            if result['type'] == 'error':
                raise AsyncActionError(result['message'])
            if result['type'] != 'chunk':
                continue
            if self.pending and result['request_id'] == self.pending['request_id']:
                self.pending = None
            if self.buffer.put(result, now, tick):
                self.last_inference_s = max(result['inference_s'], now - result['observed_at'])

        if not self.process.is_alive():
            raise AsyncActionError(f'{self.policy_label} 推理进程已退出（退出码 {self.process.exitcode}）')
        if self.pending and now - self.pending['submitted_at'] > self.wait_timeout:
            raise AsyncActionError(f'{self.policy_label} 推理响应超时，停止接收策略动作')

    def _submit(self, observation, now, tick, task, robot_type):
        """将最新的传感器观测数据写入共享内存，并向子进程下发轻量推理请求。"""
        if self.pending or not self.mailbox_lock.acquire(block=False):
            return
        try:
            for name, slot in self.slots.items():
                value = np.asarray(observation[name])
                if value.shape != slot.shape or (np.issubdtype(value.dtype, np.floating) and not np.isfinite(value).all()):
                    raise AsyncActionError(f'观测 {name} 的尺寸或数值无效')
                np.copyto(slot, value, casting='same_kind')
        finally:
            self.mailbox_lock.release()

        self.sequence += 1
        message = {
            'type': 'infer',
            'generation': self.buffer.generation,
            'request_id': self.sequence,
            'start_tick': tick,
            'observed_at': now,
            'submitted_at': now,
            'task': task,
            'robot_type': robot_type
        }
        self.connection.send(message)
        self.pending = message

    def predict(self, observation, task='', robot_type=''):
        """
        控制循环调用的核心预测接口：
        非阻塞消费缓冲区动作，智能触发提前推理，并实施物理安全限幅。
        """
        import torch
        now = time.monotonic()
        if self.epoch is None:
            self.epoch = now
        tick = max(self.tick + 1, math.floor((now - self.epoch) * self.fps))
        self.tick = tick

        # 获取当前实测角度
        measured, directions = hold_feedback(self.robot, self.names)
        if self.previous is None:
            self.previous = measured.copy()
        target = self.previous.copy()

        try:
            # 步骤 1：校验相机画面新鲜度
            for camera in getattr(self.robot, 'cameras', {}).values():
                stamp = getattr(camera, 'captured_at', None)
                if stamp is not None and (stamp <= 0 or time.time() - stamp > self.max_age):
                    raise AsyncActionError('相机画面已过期，拒绝使用旧观测继续推理')

            # 步骤 2：接收推理响应并按需触发提前下发
            if not self.failed:
                self._receive(now, tick)
                available = self.buffer.remaining(tick)
                lead = max(math.ceil(self.chunk_size * self.ratio), math.ceil(self.last_inference_s * self.fps) + 2)
                if available <= min(self.chunk_size, lead):
                    self._submit(observation, now, tick, task, robot_type)

                # 步骤 3：从缓冲区取出当前 tick 的规划动作
                predicted = self.buffer.take(tick)
                self.waiting_for_prediction = predicted is None
                if predicted is not None:
                    target = predicted
                    self.starved_at = None
                else:
                    if self.starved_at is None:
                        self.starved_at = now
                    if now - self.starved_at > self.wait_timeout:
                        raise AsyncActionError('异步动作缓冲持续为空，已请求正常停止')

            # 步骤 4：物理安全限幅与偏差保护
            result = limit_action(
                target,
                measured,
                self.previous,
                directions,
                max_step=self.max_step,
                joint_names=self.names,
                motor_limits=physical_limits(self.robot, self.names),
                max_lead=self.feedback_lead_deg,
                diagnostics=self.tracking_status
            )
            self.feedback_watchdog.update(result, measured, directions, self.names, now)
            projected = self.tracking_status.get('projected_targets', [])
            self.projection_watchdog.update(projected, now)
            if projected and now - self.last_projection_log >= 2:
                self.last_projection_log = now
                detail = '；'.join(f'{item["joint"]}: {item["raw_motor_deg"]:.2f}° → {item["projected_motor_deg"]:.2f}°'
                                  for item in projected)
                print(f'[异步 {self.policy_label}] 目标限幅：{detail}；继续按实测反馈限速执行', flush=True)
        except (AsyncActionError, OSError, EOFError, KeyError, ValueError) as exc:
            # 发生任何异常，安全停机，保持实测位置
            self.failed = True
            self.waiting_for_prediction = True
            self.failure_reason = str(exc)
            self.events['exit_early'] = self.events['stop_recording'] = True
            print(f'[异步 {self.policy_label}] 安全停止：{exc}；本帧按实测位置保持，随后按既有配置收尾', flush=True)
            result = measured.copy()

        self.previous = result

        # 定期输出流水线运行状态日志（2秒一次）
        if now - self.last_log >= 2:
            self.last_log = now
            state = '保持等待下一段' if self.waiting_for_prediction else (
                '按实测反馈限速跟随' if self.tracking_status.get('lead_limited') else '提前推理与执行并行')
            print(
                f'[异步 {self.policy_label}] {state} · 预测延迟 {self.last_inference_s * 1000:.0f} ms · '
                f'缓冲 {self.buffer.remaining(tick + 1)} 步 · 过期块 {self.buffer.stale_chunks} · '
                f'跟随差 {self.tracking_status.get("following_error_deg", 0):.2f}°',
                flush=True
            )

        return torch.from_numpy(result).unsqueeze(0)

    def close(self):
        """释放所有跨进程共享内存与终止推理子进程。"""
        if self.connection:
            try:
                self.connection.send({'type': 'close'})
            except (EOFError, OSError):
                pass
        if self.process and self.process.pid is not None:
            self.process.join(timeout=0.3)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=0.3)
        self.process = None
        if self.connection:
            self.connection.close()
            self.connection = None
        self.slots.clear()
        for memory in self.memories:
            memory.close()
            memory.unlink()
        self.memories.clear()


# Keep old ACT callers working while exposing the shared ACT/SmolVLA executor.
AsyncChunkPolicy = AsyncACTPolicy
