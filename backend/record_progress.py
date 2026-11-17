"""
LeRobot 数据集原生保存阶段进度上报补丁 (LeRobot Dataset Save Progress Hook)
==============================================================================

【模块背景与定位】
在 LeRobot 录制完一集剧集数据后，执行 `save_episode` 阶段涉及大量 I/O 与计算操作：
1. 等待异步图片写入线程完全写盘 (`_wait_image_writer`)；
2. 统计并计算该剧集的特征均值、方差等元数据 (`_save_episode_data`)；
3. 将采集到的连续帧使用 FFmpeg (H.264 / AV1) 编码并封装成多路 MP4 视频。
在原版中，这一过程没有任何实时终端进度反馈，用户经常误以为系统卡死而强行中断，
导致视频文件损坏或数据集索引丢失。

【核心增强设计】
1. 细粒度保存阶段透明化：
   通过装饰器拦截 `save_episode`、`_wait_image_writer` 和 `_save_episode_data`，
   在独立后台线程中每隔 2 秒打印一次当前阶段耗时心跳：
   "等待采集图片写盘" -> "统计图像与关节数据" -> "编码并封装 N 路视频"。
2. 避免多进程死锁（Single-Process Sequential Encoding）：
   原生 LeRobot 若开启 `parallel_encoding=True`，会在多线程 Python 进程中执行 `multiprocessing.fork()`，
   导致子进程继承父进程持有的锁或 CUDA 上下文发生死锁。本补丁默认设置 `parallel_encoding=False`，
   以稳定的单进程串行编码保证百分之百安全落盘。
"""
import functools
import signal
import threading
import time


class _defer_signals:
    """在关键数据落盘与视频编码期间屏蔽 SIGINT/SIGTERM，避免保存被过早的中断信号打断。"""
    def __init__(self, signals=(signal.SIGINT, signal.SIGTERM)):
        self.signals = signals
        self.received = []
        self.old_handlers = {}

    def _handler(self, signum, frame):
        print(f"\n[保护] 正在执行剧集数据落盘与视频编码，停止信号已接收排队，落盘完成后将自动退出...", flush=True)
        self.received.append(signum)

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for sig in self.signals:
                try:
                    self.old_handlers[sig] = signal.signal(sig, self._handler)
                except (ValueError, OSError):
                    pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for sig, handler in self.old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        if self.received and exc_type is None:
            raise KeyboardInterrupt("剧集已安全保存完成，响应排队的停止指令")


def install(dataset_module):
    """
    向 LeRobotDataset 类注入保存阶段进度报告与安全单进程编码补丁。

    Args:
        dataset_module: 包含 LeRobotDataset 的数据集模块对象。
    """
    cls = dataset_module.LeRobotDataset
    if getattr(cls, '_rebot_progress', False):
        return
    cls._rebot_progress = True
    original = cls.save_episode
    wait_images = cls._wait_image_writer
    save_data = cls._save_episode_data

    @functools.wraps(wait_images)
    def wait(self, *args, **kwargs):
        """阶段 1：等待后台图片全部写盘完成。"""
        result = wait_images(self, *args, **kwargs)
        if hasattr(self, '_rebot_save_stage'):
            self._rebot_save_stage = '统计图像与关节数据'
        return result

    @functools.wraps(save_data)
    def data(self, *args, **kwargs):
        """阶段 2：统计数据并准备视频编码。"""
        result = save_data(self, *args, **kwargs)
        if hasattr(self, '_rebot_save_stage'):
            self._rebot_save_stage = f'编码并封装 {len(self.meta.video_keys)} 路视频（{self.vcodec}）'
        return result

    @functools.wraps(original)
    def save(self, *args, **kwargs):
        """保存全流程外层包装：启动后台心跳打印线程，并强制单进程编码规避 fork 死锁。"""
        # 原生 parallel_encoding 在多线程环境下 fork 会造成继承锁死锁，强制关闭并发编码
        if len(args) < 2:
            kwargs.setdefault('parallel_encoding', False)
        started = time.monotonic()
        stop = threading.Event()
        self._rebot_save_stage = '等待采集图片写盘'

        def report():
            """每 2 秒向终端刷新输出当前保存阶段与累计耗时。"""
            while not stop.wait(2):
                print(f'[剧集保存] {self._rebot_save_stage} · 已用 {time.monotonic() - started:.1f}s；扫码不参与此等待', flush=True)

        print('[剧集保存] 开始保存：等待图片写盘 → 统计 → 编码视频', flush=True)
        thread = threading.Thread(target=report, name='episode-save-progress', daemon=True)
        thread.start()
        try:
            with _defer_signals():
                result = original(self, *args, **kwargs)
            print(f'[剧集保存] 已完成 · 用时 {time.monotonic() - started:.1f}s', flush=True)
            return result
        except BaseException:
            print(f'[剧集保存] 失败于：{self._rebot_save_stage}；保留原始数据供排查', flush=True)
            raise
        finally:
            stop.set()
            thread.join(timeout=0.2)
            if hasattr(self, '_rebot_save_stage'):
                del self._rebot_save_stage

    original_finalize = cls.finalize

    @functools.wraps(original_finalize)
    def finalize_with_safety(self, *args, **kwargs):
        """收尾保护：关闭前检测是否存在未落盘的剧集帧，确保手动停止或异常中断时已录制数据不丢失。"""
        if hasattr(self, 'episode_buffer') and isinstance(self.episode_buffer, dict):
            buf_size = self.episode_buffer.get('size', 0)
            if buf_size > 0:
                print(f'\n[数据集保护] 剧集缓冲区包含 {buf_size} 帧未保存数据，自动执行保存落盘与视频编码...', flush=True)
                try:
                    self.save_episode(parallel_encoding=False)
                    print(f'[数据集保护] 未保存剧集已成功落盘编码！总剧集数: {self.num_episodes}', flush=True)
                except Exception as exc:
                    print(f'[数据集保护] 自动保存未完成：{exc}', flush=True)
        return original_finalize(self, *args, **kwargs)

    cls._wait_image_writer, cls._save_episode_data, cls.save_episode, cls.finalize = wait, data, save, finalize_with_safety


def install_loop_progress(record_module):
    """
    向 LeRobot record_loop 注入实时控制进度与关节角度心跳日志。
    在评估或录制期间每 2 秒向终端推送耗时、进度百分比与左右臂关键角度，杜绝无输出静默假死。
    """
    if getattr(record_module, '_rebot_loop_monitored', False):
        return
    record_module._rebot_loop_monitored = True
    orig_loop = record_module.record_loop

    @functools.wraps(orig_loop)
    def wrapped_record_loop(*args, **kwargs):
        policy = kwargs.get('policy', args[8] if len(args) > 8 else None)
        robot = kwargs.get('robot', args[0] if args else None)
        control_time_s = float(kwargs.get('control_time_s', args[12] if len(args) > 12 else 30.0) or 30.0)

        def run_loop():
            if policy is not None and robot is not None and hasattr(robot, 'send_action'):
                orig_send = robot.send_action
                start_t = time.monotonic()
                last_log_t = start_t
                initial_pos = {}
                has_moved = False

                def monitored_send_action(action):
                    nonlocal last_log_t, has_moved, initial_pos
                    now = time.monotonic()
                    if isinstance(action, dict):
                        q_pos = {k.removesuffix('.pos'): float(v) for k, v in action.items() if str(k).endswith('.pos') and isinstance(v, (int, float))}
                    else:
                        q_pos = {}
                    if not initial_pos and q_pos:
                        initial_pos = dict(q_pos)
                    if not has_moved and initial_pos and q_pos:
                        max_diff = max(abs(q_pos[k] - initial_pos.get(k, q_pos[k])) for k in q_pos)
                        if max_diff >= 2.0:
                            has_moved = True

                    if now - last_log_t >= 2.0:
                        last_log_t = now
                        elapsed = now - start_t
                        pct = min(100.0, (elapsed / max(1.0, control_time_s)) * 100)
                        parts = []
                        if 'left_shoulder_lift' in q_pos:
                            parts.append(f"左臂[肩{q_pos.get('left_shoulder_lift', 0.0):.1f}°/肘{q_pos.get('left_elbow_flex', 0.0):.1f}°]")
                        elif 'shoulder_lift' in q_pos:
                            parts.append(f"臂[肩{q_pos.get('shoulder_lift', 0.0):.1f}°/肘{q_pos.get('elbow_flex', 0.0):.1f}°]")
                        if 'right_shoulder_lift' in q_pos:
                            parts.append(f"右臂[肩{q_pos.get('right_shoulder_lift', 0.0):.1f}°/肘{q_pos.get('right_elbow_flex', 0.0):.1f}°]")
                        pos_str = (" · " + " ".join(parts)) if parts else ""
                        if not has_moved and elapsed >= 3.0:
                            print(f"[策略评估] 运行中 ({elapsed:.1f}s / {control_time_s:.0f}s · {pct:.0f}%){pos_str} · 机械臂在起始待命位等待动作推进...", flush=True)
                        else:
                            print(f"[策略评估] 运行中 ({elapsed:.1f}s / {control_time_s:.0f}s · {pct:.0f}%){pos_str}", flush=True)

                    return orig_send(action)

                robot.send_action = monitored_send_action
                try:
                    return orig_loop(*args, **kwargs)
                finally:
                    robot.send_action = orig_send
            return orig_loop(*args, **kwargs)

        try:
            return run_loop()
        except KeyboardInterrupt:
            print("\n[录制/评估] 收到手动停止信号 (SIGINT/Ctrl+C)，开始安全收尾并保存当前剧集...", flush=True)
            events = kwargs.get('events', args[1] if len(args) > 1 else None)
            dataset = kwargs.get('dataset', args[10] if len(args) > 10 else None)
            if isinstance(events, dict):
                events["stop_recording"] = True
                events["exit_early"] = True
                has_frames = False
                if dataset is not None and hasattr(dataset, "episode_buffer"):
                    buf_size = dataset.episode_buffer.get("size", 0)
                    if buf_size > 0:
                        print(f"[录制控制] 当前剧集已采集 {buf_size} 帧，执行正常保存与视频编码落盘...", flush=True)
                        events["rerecord_episode"] = False
                        has_frames = True
                if not has_frames:
                    print("[录制控制] 当前剧集未采集到有效帧，跳过本集保存", flush=True)
                    events["rerecord_episode"] = True
            return None

    record_module.record_loop = wrapped_record_loop
