"""
无副作用高频遥测单向发布客户端 (Best-effort Telemetry Publisher)
========================================================================

【模块背景与定位】
在录制、遥操作、模型评估或独立观察子进程中，各进程需要将各自从硬件 CAN 总线上读到的
最新关节弧度与外力矩数据上报给中央服务，以便前端 3D 页面展示。
为了确保遥测上报【绝对不干扰主控制循环的高频实时性】，本客户端遵循纯“尽力而为（Best-effort）”设计：
1. 纯只读数据广播：绝不向下位机或总线反向发送任何控制命令；
2. 非阻塞 UDP 数据报：基于 Unix Domain Socket DGRAM（Linux）或 127.0.0.1 UDP，
   使用 `setblocking(False)`，无论服务端是否存活，发送操作均在数微秒内完成，绝不阻塞主循环；
3. 严格数据有效性校验：校验各关节角度是否为有限数值，丢弃 NaN 与异常浮点数。
"""
import json
import math
import os
import socket
import time


class FeedbackPublisher:
    """
    轻量级无阻塞 UDP 遥测数据发布客户端。
    从环境变量中自动提取套接字路径、共享令牌和串口映射表。
    """

    def __init__(self):
        """初始化客户端套接字与目标端点。"""
        self.path = os.environ.get('REBOT_TELEMETRY_SOCKET', '')
        self.token = os.environ.get('REBOT_TELEMETRY_KEY', '')
        self.mapping = json.loads(os.environ.get('REBOT_TELEMETRY_PORTS', '{}'))
        self.target = None
        self.socket = None
        if self.path:
            # 判断是网络 UDP 还是 Linux Unix 域套接字
            if ':' in self.path and (os.name == 'nt' or not hasattr(socket, 'AF_UNIX') or self.path.startswith('127.0.0.1:')):
                host, _, port_s = self.path.rpartition(':')
                self.target = (host or '127.0.0.1', int(port_s))
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                self.target = self.path
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            # 设为非阻塞模式，保证在高并发或缓冲区满时直接丢弃，绝不卡顿控制主线程
            self.socket.setblocking(False)

    def send(self, port, joints, torque=None, gripper=None):
        """
        向中央遥测服务单向广播单帧关节物理状态。

        Args:
            port (str): 发送方绑定的物理串口绝对路径。
            joints (Sequence[float]): 1~6 轴当前物理关节弧度列表。
            torque (Sequence[float], optional): 1~6 轴当前估计外力矩 (N·m)。
            gripper (float, optional): 夹爪当前开度弧度。
        """
        if not self.socket or len(joints) < 6 or not all(math.isfinite(float(q)) for q in joints):
            return
        side = self.mapping.get(os.path.realpath(port))
        if not side:
            return
        packet = {
            'token': self.token,
            'side': side,
            'port': os.path.realpath(port),
            'timestamp': time.time(),
            'joints': list(joints[:6]),
            'torque': list(torque or []),
            'unit': 'rad'
        }
        if gripper is not None and math.isfinite(float(gripper)):
            packet['gripper'] = float(gripper)
        try:
            self.socket.sendto(json.dumps(packet).encode(), self.target)
        except (OSError, ValueError):
            pass
