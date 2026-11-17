"""
通用 UVC/V4L2 相机共享采集调度管理器 (Shared Camera Acquisition Manager)
========================================================================

【模块背景与定位】
在工业机械臂与视觉抓取系统中，相机视频流需同时满足多种并发业务需求：
1. Web 前端页面需要高频流畅（30fps）的实时低延时预览（JPEG）；
2. 快递扫码任务需要偶发性抓取单张高清晰度快照（Snapshot）；
3. 运单识别流程需要获取无损原始 BGR 帧；
4. LeRobot 数据集录制或评估任务启动时，需要接管并独占底层的物理 UVC 硬件设备。

若各个业务方自行打开 OpenCV VideoCapture，将发生 Linux V4L2 设备占用冲突或严重的 USB 带宽耗尽。
本模块设计为纯软件层面的“硬件单实例采集、多业务租约共享”管理器，坚决不参与任何电机运动控制。

【核心安全设计与防护机制】
1. 单设备单采集线程（One Thread per Device）：
   每个物理视频输入节点（如 `/dev/video0`）仅由一个专属后台采集线程 `_Session` 独占打开与解码，
   所有外部消费者（Lease）只从内存缓存中拉取最新帧，零额外硬件负载。
2. 强力规避 USB2.0 总线拥塞（Bus Bandwidth Protection）：
   在首次读取图像前，强制通过 `cv2.CAP_PROP_FOURCC` 设置为 MJPG 硬件压缩传输格式。
   杜绝使用未压缩的 YUYV 原始裸流（在多相机或带麦克风同 Hub 场景下极易触发
   "No space left on device" 错误导致整个 USB 链路崩溃）。
3. 严格设备标识标准化 (`source_key`)：
   将 Linux 下易变的 `/dev/videoN` 软链接规范化解析为 `/dev/v4l/by-id/` 或 `/dev/v4l/by-path/`
   稳定物理路径，且在设备暂时离线时保留配置，支持热插拔自动无缝重连。
4. Linux V4L2 节点智能过滤 (`device_info`)：
   通过调用底层 ioctl (`VIDIOC_QUERYCAP`) 直接查询内核驱动标志位，精准剔除不产生图像像素的
   纯元数据节点（V4L2_CAP_META_CAPTURE）。
5. 独占模式与自动空闲释放（Exclusive Ownership & Idle Cleanup）：
   当 LeRobot 数据集录制启动时，通过 `close_all(exclusive=True)` 主动平稳关闭所有预览采集线程，
   将硬件完全让给官方录制进程；在无活跃租约且超时超过 `idle_seconds` 时，自动释放物理设备，防止发热。
"""
from __future__ import annotations

import glob
import os
import platform
import re
import struct
import threading
import time
from pathlib import Path


class CameraError(RuntimeError):
    """相机设备未找到、权限不足、配置冲突或总线超时时抛出的基础异常。"""
    pass


def source_key(source):
    """
    将用户传入的相机标识（索引数字、/dev/videoN、或 /dev/v4l/ 路径）规范化为系统唯一的标准路径。

    【解析策略】
    - 在 Linux 下：如果是纯数字（如 "0"），解析为实节点 `/dev/video0` 的 realpath；
    - 如果是 `/dev/v4l/by-id/...` 稳定路径，解析其真实链接指向；若当前设备离线（lexists 为 False），
      保留原路径返回，以便用户能够在离线状态下编辑配置或等待插入后自动重连；
    - 严禁包含非法字符或非视频节点。

    Args:
        source (str or int): 输入的相机编号或路径。

    Returns:
        str: 规范化后的设备绝对路径或数字字符串。

    Raises:
        CameraError: 格式不合法或路径指向非视频采集节点时抛出。
    """
    value = str(source).strip()
    if re.fullmatch(r"\d+", value):
        return os.path.realpath(f"/dev/video{int(value)}") if platform.system() == "Linux" else str(int(value))
    if platform.system() == "Linux":
        if re.fullmatch(r"/dev/video\d+", value):
            return value
        if re.fullmatch(r"/dev/v4l/(?:by-id|by-path)/[^/\x00]+", value) and Path(value).name not in ('.', '..'):
            resolved = os.path.realpath(value)
            if re.fullmatch(r"/dev/video\d+", resolved):
                return resolved
            if not os.path.lexists(value):
                return value  # 合法但暂时离线的稳定路径：保留它以支持编辑和热插拔重连
            raise CameraError(f"稳定设备路径 {value} 没有指向视频采集节点")
    raise CameraError("设备必须是非负编号、/dev/videoN 或 /dev/v4l/ 下的稳定设备路径")


def device_info(path):
    """
    通过底层系统调用 ioctl(VIDIOC_QUERYCAP) 直接查询 Linux V4L2 视频设备的能力标志位。

    【核心作用】
    - 仅查询设备描述，不启动数据流，零开销；
    - 检查 device_caps 是否包含 V4L2_CAP_VIDEO_CAPTURE (0x00000001) 或 V4L2_CAP_VIDEO_CAPTURE_MPLANE (0x00001000)；
    - 精准过滤并排除摄像头驱动暴露的纯 Metadata 节点（如统计信息或测光节点）；
    - 自动反查并收集该设备在 `/dev/v4l/by-id` 和 `/dev/v4l/by-path` 下的持久化符号链接别名。

    Args:
        path (str): Linux 字符设备路径（如 /dev/video0）。

    Returns:
        dict or None: 若属于图像采集节点则返回包含名称、总线、别名的信息字典，否则返回 None。
    """
    import fcntl
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        buf = bytearray(104)
        # 0x80685600 对应 Linux 内核头文件里的 VIDIOC_QUERYCAP 命令宏
        fcntl.ioctl(fd, 0x80685600, buf, True)
        caps, device_caps = struct.unpack_from("II", buf, 84)
        caps = device_caps if caps & 0x80000000 else caps
        if not caps & (0x1 | 0x1000):
            return None
        decode = lambda raw: raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        aliases = [
            p for p in sorted(glob.glob('/dev/v4l/by-path/*') + glob.glob('/dev/v4l/by-id/*'))
            if os.path.realpath(p) == os.path.realpath(path)
        ]
        return {
            "path": path,
            "index": int(path.removeprefix('/dev/video')),
            "name": decode(buf[16:48]),
            "bus": decode(buf[48:80]),
            "aliases": aliases,
            "source": aliases[0] if aliases else path
        }
    finally:
        os.close(fd)


def list_devices():
    """
    枚举操作系统中所有可用的物理摄像头设备，提取名称、稳定路径和别名。

    Returns:
        dict: 包含 'devices' 列表和 'errors' 列表的字典。
    """
    devices, errors = [], []
    if platform.system() != "Linux":
        try:
            import cv2
            backend = getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY) if platform.system() == "Windows" else cv2.CAP_ANY
            for idx in range(8):
                cap = cv2.VideoCapture(idx, backend)
                try:
                    if cap.isOpened():
                        devices.append({
                            "path": str(idx),
                            "index": idx,
                            "name": f"Camera {idx}",
                            "bus": platform.system(),
                            "aliases": [],
                            "source": str(idx),
                        })
                finally:
                    cap.release()
        except Exception as exc:
            errors.append({"path": "0", "error": str(exc)})
        return {"devices": devices, "errors": errors}

    # Linux 下按数字递增顺序扫描 /dev/video* 节点
    for path in sorted(glob.glob('/dev/video*'), key=lambda p: int(re.search(r'\d+$', p)[0])):
        try:
            info = device_info(path)
            if info:
                devices.append(info)
        except OSError as exc:
            errors.append({"path": path, "error": str(exc)})
    return {"devices": devices, "errors": errors}


def validate_configs(cameras):
    """
    全面校验一组相机配置参数的合法性与互斥性。

    【校验规则】
    1. 必须是字典列表；
    2. 每路相机必须有非空的独一无二名称（name 唯一）；
    3. 每路相机绑定的底层硬件 source_key 绝对不能重复（防止一个设备被多路配置争抢）；
    4. 分辨率与帧率必须在工业合法数值范围内（宽 16~7680，高 16~4320，FPS 1~120）；
    5. FOURCC 编码格式必须为合法 4 字符标识（如 MJPG, YUYV 等）。

    Args:
        cameras (list[dict]): 待校验的相机配置列表。

    Raises:
        CameraError: 当校验未通过时抛出详细原因。
    """
    if not isinstance(cameras, list):
        raise CameraError("相机配置必须是列表")
    names, sources = set(), set()
    for c in cameras:
        if not isinstance(c, dict):
            raise CameraError("相机配置格式错误")
        name = str(c.get('name', '')).strip()
        if not name or name in names:
            raise CameraError("每路相机需要一个不重复的名称")
        names.add(name)
        key = source_key(c.get('index_or_path', ''))
        if key in sources:
            raise CameraError(f"设备 {key} 已被另一条相机配置使用")
        sources.add(key)
        for k, lo, hi in [('width', 16, 7680), ('height', 16, 4320), ('fps', 1, 120)]:
            v = c.get(k, {'width': 640, 'height': 480, 'fps': 30}[k])
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi) or v != int(v):
                raise CameraError(f"{name} 的 {k} 必须是 {lo}～{hi} 的整数")
        if not re.fullmatch(r'[A-Za-z0-9]{4}', str(c.get('fourcc') or 'MJPG')):
            raise CameraError(f"{name} 的 FOURCC 必须是四位格式名称，例如 MJPG")


class _Session:
    """
    针对单个物理相机的后台单例捕获会话。
    在独立的守护线程中持续拉取帧、进行 JPEG 压缩并通知所有订阅者。
    """

    def __init__(self, key, config, factory, idle_seconds):
        self.key, self.config, self.factory = key, config, factory
        self.idle_seconds = idle_seconds
        self.condition = threading.Condition()
        self.refs = 0
        self.last_release = time.monotonic()
        self.seq = 0
        self.frame = self.jpeg = None
        self.captured_at = 0.0
        self.error = ''
        self.actual = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f'camera-{Path(key).name}')
        self.thread.start()

    def _run(self):
        """后台独立捕获循环：配置硬件属性、读取连续帧、编码 JPEG 并唤醒等待者。"""
        cap = None
        try:
            import cv2
            if platform.system() == 'Linux' and self.factory is None:
                if not os.path.exists(self.key):
                    raise CameraError(f'相机当前离线：{self.key}。请检查 USB 连接；配置已保留，可自动重连')
                if device_info(self.key) is None:
                    raise CameraError(f'{self.key} 是元数据节点，不能采集图像')
            if self.factory:
                cap = self.factory(self.key)
            elif platform.system() == 'Linux':
                cap = cv2.VideoCapture(self.key, cv2.CAP_V4L2)
            else:
                cap = cv2.VideoCapture(int(self.key), cv2.CAP_DSHOW if platform.system() == 'Windows' else cv2.CAP_ANY)
            if not cap.isOpened():
                raise CameraError(f'无法打开 {self.key}，请检查设备连接、权限和占用情况')

            # 核心优化：在进行第一次 read() 前必须预先设置 MJPG 硬件压缩格式！
            # 原始未压缩的 YUYV 格式会瞬间打满 USB 2.0 共享带宽，导致多相机无法同时出流。
            fourcc = str(self.config.get('fourcc') or 'MJPG')
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.config.get('width') or 640))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.config.get('height') or 480))
            cap.set(cv2.CAP_PROP_FPS, int(self.config.get('fps') or 30))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 严格将缓冲区深度设为 1，确保永远读取零延时实时画面

            code = int(cap.get(cv2.CAP_PROP_FOURCC))
            self.actual = {
                'width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                'height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                'fps': cap.get(cv2.CAP_PROP_FPS),
                'fourcc': ''.join(chr((code >> 8 * i) & 255) for i in range(4)).strip('\0')
            }

            while not self.stop.is_set():
                # 检查空闲状态：无活跃客户端且超过 idle_seconds，自动休眠关闭
                with self.condition:
                    if not self.refs and time.monotonic() - self.last_release > self.idle_seconds:
                        self.stop.set()
                        break

                valid, frame = cap.read()
                if not valid:
                    w, h = self.actual.get('width', 0), self.actual.get('height', 0)
                    if self.seq == 0 and w * h > 640 * 480:
                        raise CameraError(
                            f'{self.key} 在 {w}×{h} 下无法启动取流（常见原因：USB 2.0 带宽预留失败，'
                            'v4l2 报 No space left on device；多个相机/音频设备挂在同一 USB 控制器时容易出现）。'
                            '处理：先点「暂停全部」，只预览这一路再试；或把相机插到不同 USB 控制器的接口；或改用 640×480 / 降低帧率'
                        )
                    raise CameraError(f'{self.key} 未返回图像。检查 USB 带宽、MJPG 格式、设备占用或重新插拔相机')

                encoded, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not encoded:
                    raise CameraError('JPEG 编码失败')

                with self.condition:
                    self.frame = frame
                    self.jpeg = jpeg.tobytes()
                    self.captured_at = time.time()
                    self.seq += 1
                    self.condition.notify_all()

                self.stop.wait(0.001)
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()
        finally:
            if cap is not None:
                cap.release()
            self.stop.set()
            with self.condition:
                self.condition.notify_all()

    def wait(self, after=0, timeout=8):
        """同步阻塞等待产生比 after 序号更新的帧或捕获错误。"""
        with self.condition:
            ready = self.condition.wait_for(
                lambda: self.seq > after or self.error or self.stop.is_set(),
                timeout
            )
            if self.error:
                raise CameraError(self.error)
            if not ready or self.stop.is_set():
                raise CameraError(f'{self.key} 等待图像超时或预览已暂停；检查 USB 带宽和设备占用')
            return self.seq, self.frame, self.jpeg


class CameraLease:
    """外部调用者持有的单路相机使用租约，支持上下文管理与自动释放。"""

    def __init__(self, session):
        self.session = session
        self.seq = 0
        self.released = False
        self.captured_at = 0.0

    def read(self):
        """读取最新无损 BGR 原始图像矩阵副本。"""
        with self.session.condition:
            self.seq, frame, _ = self.session.wait(self.seq)
            self.captured_at = self.session.captured_at
            return True, frame.copy()

    def jpeg(self, fresh=False):
        """读取最新压缩 JPEG 字节流。"""
        self.seq, _, jpeg = self.session.wait(self.seq if fresh else 0)
        return jpeg

    def release(self):
        """归还租约，减少会话引用计数。"""
        with self.session.condition:
            if not self.released:
                self.released = True
                self.session.refs -= 1
                self.session.last_release = time.monotonic()


class CameraManager:
    """
    相机总管理器：
    负责会话的注册管理、独占互锁、快照获取以及批量关闭。
    """

    def __init__(self, factory=None, idle_seconds=1):
        self.factory = factory
        self.idle_seconds = idle_seconds
        self.lock = threading.RLock()
        self.sessions = {}
        self.exclusive = False

    def snapshot(self, source, config=None, start_capture=True, timeout=0.35):
        """
        快速获取当前相机的单张已有 JPEG 快照（零副作用拷贝）。

        【安全策略】
        在数据录制进行中，传入 start_capture=False，坚决禁止后台为了扫码而重新打开
        已被关闭的物理 UVC 硬件设备。
        """
        key = source_key(source)
        with self.lock:
            if self.exclusive:
                raise CameraError('相机当前由其他任务独占')
            session = self.sessions.get(key)
        lease = None
        if session is None or session.stop.is_set():
            if not start_capture:
                raise CameraError('采集中该相机尚无共享帧，扫码已跳过，不重新打开设备')
            lease = self.acquire(source, config)
            session = lease.session
        try:
            with session.condition:
                session.last_release = time.monotonic()
                session.wait(timeout=timeout)
                if time.time() - session.captured_at > 2:
                    raise CameraError('扫码共享帧已过期，请检查相机画面')
                return session.seq, session.captured_at, session.jpeg
        finally:
            if lease:
                lease.release()

    def acquire(self, source, config=None):
        """
        获取指定相机的访问租约。若参数发生变化（分辨率或格式），自动重启底层采集会话。

        Args:
            source (str or int): 相机标识。
            config (dict, optional): 期望的宽、高、帧率与格式。

        Returns:
            CameraLease: 租约对象。

        Raises:
            CameraError: 当前处于独占模式或设备正忙时抛出。
        """
        key = source_key(source)
        with self.lock:
            if self.exclusive:
                raise CameraError('相机已交给采集 / 遥操作 / 评估任务；任务结束后可重新预览')
            session = self.sessions.get(key)
            if session and not session.stop.is_set() and session.thread.is_alive() and config:
                def _prof(cfg):
                    return (
                        int(cfg.get('width') or 640),
                        int(cfg.get('height') or 480),
                        int(cfg.get('fps') or 30),
                        str(cfg.get('fourcc') or 'MJPG').upper(),
                    )
                if _prof(session.config) != _prof(config):
                    self.close_devices([key])
                    session = None
            if session and session.stop.is_set() and session.thread.is_alive():
                session.thread.join(timeout=2.0)
                if session.thread.is_alive():
                    raise CameraError(f'{key} 正在释放，请稍后重试')
                session = None
            if session is None or not session.thread.is_alive():
                session = _Session(key, config or {}, self.factory, self.idle_seconds)
                self.sessions[key] = session
            with session.condition:
                session.refs += 1
            return CameraLease(session)

    def close_devices(self, sources):
        """
        精准停止并释放指定的设备集合，其余运行正常的设备保持出流不受干扰。
        """
        keys = {source_key(source) for source in sources}
        with self.lock:
            selected = [s for key, s in self.sessions.items() if key in keys]
            for session in selected:
                session.stop.set()
                with session.condition:
                    session.condition.notify_all()
            deadline = time.monotonic() + 4
            for session in selected:
                session.thread.join(max(0.0, deadline - time.monotonic()))
            if any(session.thread.is_alive() for session in selected):
                raise CameraError('相机驱动正在释放设备，请稍后重试')
            for key in keys:
                self.sessions.pop(key, None)

    def close_all(self, exclusive=False):
        """
        停止所有物理相机捕获线程，并根据参数切换独占模式标志。
        用于在录制/遥操作启动前完全让出所有硬件句柄。
        """
        with self.lock:
            self.exclusive = exclusive
            sessions = list(self.sessions.values())
            for s in sessions:
                s.stop.set()
                with s.condition:
                    s.condition.notify_all()
            deadline = time.monotonic() + 4
            for s in sessions:
                s.thread.join(max(0.0, deadline - time.monotonic()))
            if any(s.thread.is_alive() for s in sessions):
                raise CameraError('相机驱动仍在等待图像，尚未释放；请稍后重试或重新插拔该相机')
            self.sessions.clear()

    def status(self):
        """查询所有会话的运行健康度快照。"""
        with self.lock:
            return {
                'exclusive': self.exclusive,
                'sessions': [
                    {
                        'device': s.key,
                        'source': s.config.get('index_or_path', s.key),
                        'clients': s.refs,
                        'frames': s.seq,
                        'error': s.error,
                        'active': s.thread.is_alive() and not s.stop.is_set(),
                        'actual': s.actual,
                        'age_ms': round((time.time() - s.captured_at) * 1000) if s.captured_at else None
                    }
                    for s in self.sessions.values()
                ]
            }
