"""
异步 ACT 深度学习前向推理独立子进程 (ACT Inference Isolated Process)
========================================================================

【模块背景与定位】
在强化学习和具身智能大模型端到端推理时，PyTorch 的 Transformer 模型计算
会消耗大量的 GPU 算力并阻塞当前 Python 线程数十至上百毫秒。
为了确保机械臂底层硬件控制循环（50Hz / 20ms 一帧）绝对平稳、零卡顿，
本模块设计为纯计算型独立子进程（Model-Only Actor Process）：
1. 输入端：通过基于内存文件的 Linux 共享内存（`multiprocessing.shared_memory`）
   实现相机图像与关节状态的零拷贝（Zero-Copy）毫秒级传输；
2. 计算端：独占管理 PyTorch 与 CUDA 上下文，支持 AMP 自动混合精度加速推理；
3. 输出端：预先计算未来一个完整的动作块（Action Chunk，如 50 步），通过管道返回 CPU 浮点数组。
"""
import copy
import json
from multiprocessing import shared_memory
import os
from pathlib import Path
import time


def cached_processor_path(model_id):
    """Resolve SmolVLM metadata/tokenizer locally; its base weights are not needed."""
    from huggingface_hub import snapshot_download
    try:
        path = Path(model_id).expanduser()
        if not path.is_dir():
            path = Path(snapshot_download(model_id, local_files_only=True))
        required = ('config.json', 'preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json')
        return str(path.resolve()) if all((path / name).is_file() for name in required) else None
    except (OSError, ValueError):
        return None


def configure_flow_sampling(config, steps):
    """Override inference iterations only; never alter the trained horizon."""
    if not 0 <= float(steps) <= 50 or not float(steps).is_integer():
        raise ValueError('Flow Matching采样次数必须是0–50的整数')
    if steps:
        if config.type != 'smolvla':
            raise ValueError('Flow Matching采样仅适用于SmolVLA')
        config.num_steps = int(steps)


def configure_execution_steps(config, steps):
    """Override execution length while preserving the trained prediction horizon."""
    value = float(steps)
    if not 0 <= value <= 1000 or not value.is_integer():
        raise ValueError('n_action_steps必须是0–1000的整数')
    if value:
        config.n_action_steps = min(int(value), config.chunk_size)
    if not 1 <= config.n_action_steps <= config.chunk_size:
        raise ValueError('n_action_steps必须在1到chunk_size之间')


def run_actor(connection, lock, specs, model_path, device, use_amp, processor_options, chunk_size):
    """
    独立推理子进程主函数：初始化深度模型、执行预热并持续处理推理请求。

    Args:
        connection: 与主控制循环通信的双向管道。
        lock (multiprocessing.Lock): 保护共享内存并发读写的互斥锁。
        specs (dict): 共享内存观察特征描述字典（包含 shape, dtype, memory 句柄名）。
        model_path (str): ACT 模型预训练目录路径。
        device (str): 目标推理硬件设备（'cuda' 或 'cpu'）。
        use_amp (bool): 是否启用 CUDA 自动混合精度 (Automatic Mixed Precision)。
        processor_options (dict): 图像前处理配置字典。
        chunk_size (int): 外部期望的最大动作块步数。
    """
    memories, slots = [], {}
    try:
        # 步骤 1：限制各类底层数学计算库线程数为 1，防止打满 CPU 核心
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[key] = '1'

        import numpy as np
        import torch
        from contextlib import nullcontext
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class, make_pre_post_processors
        from lerobot.policies.utils import prepare_observation_for_inference

        torch.set_num_threads(1)
        options = copy.deepcopy(processor_options or {})
        seed = options.pop('_rebot_seed', None)
        flow_steps = options.pop('_rebot_flow_steps', 0)
        if seed is not None:
            torch.manual_seed(int(seed))
        kind = json.loads((Path(model_path) / 'config.json').read_text())['type']
        if kind not in ('act', 'smolvla'):
            raise ValueError('异步动作块支持 ACT / SmolVLA；当前检查点类型：' + str(kind))
        policy_class = get_policy_class(kind)  # Also registers the matching config.
        config = PreTrainedConfig.from_pretrained(model_path)
        configure_flow_sampling(config, flow_steps)
        configure_execution_steps(config, chunk_size)
        if getattr(config, 'temporal_ensemble_coeff', None) is not None:
            raise ValueError('该 ACT 已启用模型内部时序集成；请关闭本页异步选项，避免重复集成')
        if getattr(getattr(config, 'rtc_config', None), 'enabled', False):
            raise ValueError('检查点已启用RTC，不能与此动作块调度器重复叠加')

        config.device = device
        processor_path = None
        if kind == 'act':
            config.pretrained_backbone_weights = None
        else:
            # The fine-tuned state dict already contains the VLM weights.
            # Keep the cached VLM config/tokenizer, without loading base weights twice.
            config.load_vlm_weights = False
            processor_path = cached_processor_path(config.vlm_model_name)
            if processor_path:
                config.vlm_model_name = processor_path
        policy = policy_class.from_pretrained(model_path, config=config, local_files_only=True).to(device).eval()

        overrides = options.get('preprocessor_overrides') or {}
        overrides['device_processor'] = {**(overrides.get('device_processor') or {}), 'device': device}
        if processor_path and not (overrides.get('tokenizer_processor') or {}).get('tokenizer_name'):
            overrides['tokenizer_processor'] = {**(overrides.get('tokenizer_processor') or {}), 'tokenizer_name': processor_path}
        options['preprocessor_overrides'] = overrides
        pre, post = make_pre_post_processors(config, pretrained_path=model_path, **options)

        # 步骤 2：挂载各观察输入的共享内存缓冲区
        slots = {}
        for name, spec in specs.items():
            memory = shared_memory.SharedMemory(name=spec['memory'])
            memories.append(memory)
            slots[name] = np.ndarray(spec['shape'], dtype=spec['dtype'], buffer=memory.buf)

        count = min(chunk_size or config.n_action_steps, config.chunk_size, config.n_action_steps)
        if count < 1:
            raise ValueError('ACT 检查点没有可执行动作块')

        def predict(observation, task='', robot_type=''):
            """执行单次端到端前向推理与后处理。"""
            with torch.inference_mode(), (torch.autocast('cuda') if device.startswith('cuda') and use_amp else nullcontext()):
                value = prepare_observation_for_inference(observation, torch.device(device), task, robot_type)
                value = pre(value)
                predicted = policy.predict_action_chunk(value)[:, :count]
                shape = predicted.shape
                predicted = post(predicted.reshape(-1, shape[-1])).reshape(shape)
                if not torch.isfinite(predicted).all():
                    raise ValueError('模型预测包含非有限值')
                return predicted[0].float().cpu().numpy()

        # 步骤 3：模型冷启动预热（Warmup）：下发全零虚拟张量执行一次前向推理，触发 CUDA 显存分配与内核编译
        started = time.monotonic()
        # SmolVLA samples flow noise. Warmup must not consume the random stream
        # intended for real observations or alter a seeded evaluation.
        with torch.random.fork_rng(devices=[torch.cuda.current_device()] if device.startswith('cuda') else []):
            predict({name: np.zeros_like(slot) for name, slot in slots.items()})
        policy.reset()
        pre.reset()
        post.reset()

        # 发送就绪消息通知主进程可以安全连接机械臂
        connection.send({
            'type': 'ready',
            'pid': os.getpid(),
            'chunk_size': count,
            'policy_type': kind,
            'model_chunk_size': config.chunk_size,
            'flow_matching_steps': config.num_steps if kind == 'smolvla' else 0,
            'warmup_s': time.monotonic() - started
        })

        generation = None
        # 步骤 4：主推理监听循环
        while True:
            message = connection.recv()
            if message['type'] == 'close':
                break

            # 在进程锁保护下零拷贝复制共享内存中的最新画面与状态
            with lock:
                observation = {name: slot.copy() for name, slot in slots.items()}

            # 跨剧集重置策略与预处理器内部状态
            if generation != message['generation']:
                policy.reset()
                pre.reset()
                post.reset()
                generation = message['generation']

            started = time.monotonic()
            actions = predict(observation, message.get('task', ''), message.get('robot_type', ''))
            connection.send({
                **message,
                'type': 'chunk',
                'actions': actions,
                'inference_s': time.monotonic() - started
            })

    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            connection.send({'type': 'error', 'message': str(exc)})
        except (EOFError, OSError):
            pass
    finally:
        slots.clear()
        for memory in memories:
            memory.close()
        connection.close()
