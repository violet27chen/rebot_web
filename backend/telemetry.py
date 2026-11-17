"""
中央关节角度与力矩遥测分发服务 (Central Telemetry Hub & WebSocket Feed)
========================================================================

【模块背景与定位】
在双臂协作与 Web 数字孪生监控中，各个独立任务（示教、录制、评估、观察进程）
通过轻量 UDP 上报实时机械臂状态。
本模块作为系统常驻的中央遥测集线器（Telemetry Hub）：
1. 提供本地 UDP 数据报接收服务，校验 24 字节安全令牌（Token），防止伪造注入；
2. 基于单调递增的代数序列号（`binding_generation`）严格管理左右臂与底层物理串口的绑定关系，
   当串口配置发生变更时，瞬间作废旧的缓存角度，绝不允许将旧串口的数据串入新分配的机械臂；
3. 将最新的左右臂状态通过单一的高效 WebSocket 广播流推送到所有已打开的前端页面，
   保持毫秒级端到端同步。
"""
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time


def _use_unix_dgram():
    """判断当前操作系统是否支持 Unix 域数据报套接字（Linux/macOS 支持）。"""
    return os.name != 'nt' and hasattr(socket, 'AF_UNIX')


class Telemetry:
    """
    中央遥测服务端：
    监听 UDP 端口拉取最新电机实测状态，向已连接的 WebSocket 客户端广播推送。
    """

    def __init__(self, root):
        self.root = Path(root)
        self.token = secrets.token_hex(24)
        self.directory = Path(tempfile.mkdtemp(prefix='rebot-feedback-'))
        self.path = self.directory / 'feedback.sock'
        self.address = None
        self.socket = None
        self.thread = None
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.latest = {}
        self.subscribers = set()
        self.loop = None
        self.observers = {}
        self.ports = {}
        self.bindings = {}
        self.binding_generation = 0
        self.binding_since = 0.0
        self.paused = False

    def start(self, loop):
        """
        启动 UDP 套接字监听后台线程，并绑定当前 FastAPI 的 asyncio 事件循环。

        Args:
            loop: asyncio 事件循环对象。
        """
        self.loop = loop
        if _use_unix_dgram():
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.socket.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.address = str(self.path)
        else:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind(('127.0.0.1', 0))
            self.address = f"127.0.0.1:{self.socket.getsockname()[1]}"

        self.socket.settimeout(0.25)
        self.thread = threading.Thread(target=self._receive, daemon=True, name='joint-feedback')
        self.thread.start()

    def configure(self, ports):
        """
        更新机械臂左右侧角色与底层串口的绑定关系。

        【安全设计】
        当端口映射发生变化时，清空当前所有缓存的位姿并递增 `binding_generation`，
        绝不盲目相信旧观察器在换端口前抓取的滞后数据。

        Args:
            ports (dict): 左右侧与串口映射字典，形如 {'left': '/dev/ttyACM0', 'right': ...}。

        Returns:
            bool: True 表示配置发生变化并广播了新绑定事件。
        """
        bindings = {
            side: {'port': str(port), 'canonical_port': os.path.realpath(port)}
            for side, port in ports.items()
            if side in ('left', 'right') and port
        }
        grouped = {}
        for side, binding in bindings.items():
            grouped.setdefault(binding['canonical_port'], []).append(side)
        for binding in bindings.values():
            binding['ambiguous'] = len(grouped[binding['canonical_port']]) != 1
        mapping = {port: sides[0] for port, sides in grouped.items() if len(sides) == 1}

        with self.lock:
            if bindings == self.bindings:
                return False
            self.bindings = bindings
            self.ports = mapping
            self.latest = {}
            self.binding_generation += 1
            self.binding_since = time.time()
            event = {
                'type': 'bindings',
                'bindings': dict(bindings),
                'binding_generation': self.binding_generation,
                'time': self.binding_since
            }
        self.publish(event)
        return True

    def environment(self, ports):
        """生成供子进程注入的环境变量字典。"""
        self.configure(ports)
        with self.lock:
            mapping = dict(self.ports)
        return {
            'REBOT_TELEMETRY_SOCKET': str(self.address or self.path),
            'REBOT_TELEMETRY_KEY': self.token,
            'REBOT_TELEMETRY_PORTS': json.dumps(mapping)
        }

    def accept(self, packet):
        """
        严格校验接收到的遥测数据包，并按标准化的硬件设备节点进行分发。

        Args:
            packet (dict): 接收到的原始遥测数据字典。

        Returns:
            bool: True 表示校验通过并广播成功，False 表示被拦截丢弃。
        """
        packet = dict(packet)
        # 步骤 1：时间恒定性比对安全令牌，防时序侧信道反推
        if not secrets.compare_digest(str(packet.pop('token', '')), self.token):
            return False

        source = packet.get('port')
        if not isinstance(source, str) or not source or (os.name != 'nt' and not os.path.exists(source)):
            return False

        port = os.path.realpath(source)
        values = packet.get('joints')
        timestamp = packet.get('timestamp')

        finite = lambda q: isinstance(q, (int, float)) and not isinstance(q, bool) and math.isfinite(q)
        if not isinstance(values, list) or len(values) != 6 or not all(finite(q) for q in values):
            return False
        # 丢弃时滞超过 2 秒的陈旧数据包
        if not finite(timestamp) or abs(time.time() - timestamp) > 2:
            return False
        if 'gripper' in packet and not finite(packet['gripper']):
            return False

        with self.lock:
            side = self.ports.get(port)
            # 若处于暂停状态、未绑定该侧或数据包时间早于本代绑定生效时间，直接丢弃
            if self.paused or side not in ('left', 'right') or timestamp < self.binding_since:
                return False
            previous = self.latest.get(side)
            if previous and timestamp < previous['timestamp']:
                return False

            packet.update(
                side=side,
                port=port,
                binding_port=self.bindings[side]['port'],
                binding_generation=self.binding_generation
            )
            self.latest[side] = packet
            event = {
                'type': 'feedback',
                'arm': packet,
                'binding_generation': self.binding_generation,
                'server_time': time.time()
            }

        self.publish(event)
        return True

    def _receive(self):
        """后台独立 UDP 接收线程循环。"""
        while not self.stop.is_set():
            try:
                self.accept(json.loads(self.socket.recv(8192)))
            except (OSError, ValueError, TypeError):
                pass

    def publish(self, packet):
        """向所有已连接的 WebSocket 订阅队列投递异步消息。"""
        if not self.loop:
            return

        def deliver():
            for queue in tuple(self.subscribers):
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    queue.put_nowait(packet)
                except asyncio.QueueFull:
                    pass

        try:
            self.loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass

    def subscribe(self):
        """
        创建一个新的客户端订阅队列，用于 WebSocket 推送。

        Returns:
            asyncio.Queue: 容量为 2 的异步队列（缓冲最新帧，自动丢弃堆积帧）。
        """
        queue = asyncio.Queue(maxsize=2)
        self.subscribers.add(queue)
        return queue

    def snapshot(self):
        """
        获取当前机械臂状态快照，用于新 WebSocket 客户端连接建立时的首帧同步。

        Returns:
            dict: 包含当前左右臂最新实测数据、暂停标志、串口绑定关系及代数序列号的快照字典。
        """
        with self.lock:
            return {
                'type': 'snapshot',
                'arms': dict(self.latest),
                'paused': self.paused,
                'layout': 'parallel',
                'bindings': dict(self.bindings),
                'binding_generation': self.binding_generation,
                'time': time.time()
            }

    def stop_observers(self):
        """
        停止所有正在运行的空闲状态反馈观察器子进程。
        """
        for process in list(self.observers.values()):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        self.observers.clear()

    def observe_idle(self, ports, connected):
        """
        在机械臂处于空闲状态（无手动控制、示教录制或自主推理任务）时，
        自动为未被占用的串口启动 feedback_observer.py 后台读取子进程，保持 Web 界面角度刷新。

        【串口占用与竞态保护】
        启动前使用 `fuser` 命令探测目标串口，若发现已有其他进程（如主控守护进程）正在使用，
        则绝不强行打开，避免引发底层串口总线冲突。

        Args:
            ports (dict): 机械臂配置字典 {'left': port, 'right': port}。
            connected (bool): 机械臂逻辑连接状态。
        """
        self.configure(ports)
        if self.paused or not connected:
            self.stop_observers()
            return

        self.environment(ports)
        wanted = set(ports.values())

        # 清理已不需要或已经退出的观察器
        for port, process in list(self.observers.items()):
            if port not in wanted or process.poll() is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                self.observers.pop(port, None)

        hardware = self.root / 'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml'
        if not hardware.exists():
            return

        # 为每个尚未被观察且空闲的串口启动独立观察器
        for side, port in ports.items():
            if not port or not os.path.exists(port) or port in self.observers:
                continue

            # 使用 fuser 检查串口是否正被其他进程（例如 arm_daemon 或录制进程）占用
            result = subprocess.run(['fuser', port], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
            if result.returncode == 0:
                continue

            env = os.environ.copy()
            env.update(self.environment(ports))
            self.observers[port] = subprocess.Popen(
                [
                    sys.executable,
                    str(self.root / 'backend/feedback_observer.py'),
                    '--port', port,
                    '--hardware', str(hardware)
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

    def close(self):
        """
        彻底关闭遥测中心：
        停止所有观察器子进程、关闭套接字并删除临时套接字文件与目录。
        """
        self.stop.set()
        self.stop_observers()
        if self.socket:
            self.socket.close()
        try:
            self.path.unlink()
            self.directory.rmdir()
        except OSError:
            pass
