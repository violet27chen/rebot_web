"""Shared camera acquisition for previews and snapshots (never controls motors).

One capture thread owns each physical device. Consumers receive the latest frame;
an idle device is released automatically. LeRobot jobs take exclusive ownership
through ``exclusive`` so previews cannot steal their UVC devices.
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
    pass


def source_key(source):
    """Normalize identity without requiring a camera to be currently connected."""
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
                return value  # Valid but offline: keep it editable and reconnectable.
            raise CameraError(f"稳定设备路径 {value} 没有指向视频采集节点")
    raise CameraError("设备必须是非负编号、/dev/videoN 或 /dev/v4l/ 下的稳定设备路径")


def device_info(path):
    """VIDIOC_QUERYCAP does not start a stream; metadata nodes are excluded."""
    import fcntl
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, 0x80685600, buf, True)
        caps, device_caps = struct.unpack_from("II", buf, 84)
        caps = device_caps if caps & 0x80000000 else caps
        if not caps & (0x1 | 0x1000):
            return None
        decode = lambda raw: raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        aliases = [p for p in sorted(glob.glob('/dev/v4l/by-path/*') + glob.glob('/dev/v4l/by-id/*'))
                   if os.path.realpath(p) == os.path.realpath(path)]
        return {"path": path, "index": int(path.removeprefix('/dev/video')),
                "name": decode(buf[16:48]), "bus": decode(buf[48:80]),
                "aliases": aliases, "source": aliases[0] if aliases else path}
    finally:
        os.close(fd)


def list_devices():
    devices, errors = [], []
    for path in sorted(glob.glob('/dev/video*'), key=lambda p: int(re.search(r'\d+$', p)[0])):
        try:
            info = device_info(path)
            if info:
                devices.append(info)
        except OSError as exc:
            errors.append({"path": path, "error": str(exc)})
    return {"devices": devices, "errors": errors}


def validate_configs(cameras):
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
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or v != int(v):
                raise CameraError(f"{name} 的 {k} 必须是 {lo}～{hi} 的整数")
        if not re.fullmatch(r'[A-Za-z0-9]{4}', str(c.get('fourcc') or 'MJPG')):
            raise CameraError(f"{name} 的 FOURCC 必须是四位格式名称，例如 MJPG")


class _Session:
    def __init__(self, key, config, factory, idle_seconds):
        self.key, self.config, self.factory = key, config, factory
        self.idle_seconds = idle_seconds
        self.condition = threading.Condition()
        self.refs, self.last_release, self.seq = 0, time.monotonic(), 0
        self.frame = self.jpeg = None
        self.captured_at = 0.0
        self.error, self.actual = '', {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f'camera-{Path(key).name}')
        self.thread.start()

    def _run(self):
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
            # Set compression BEFORE the first read; raw YUYV exhausts a shared USB2 bus.
            fourcc = str(self.config.get('fourcc') or 'MJPG')
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.config.get('width') or 640))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.config.get('height') or 480))
            cap.set(cv2.CAP_PROP_FPS, int(self.config.get('fps') or 30))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            code = int(cap.get(cv2.CAP_PROP_FOURCC))
            self.actual = {'width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                           'height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                           'fps': cap.get(cv2.CAP_PROP_FPS),
                           'fourcc': ''.join(chr((code >> 8*i) & 255) for i in range(4)).strip('\0')}
            while not self.stop.is_set():
                with self.condition:
                    if not self.refs and time.monotonic() - self.last_release > self.idle_seconds:
                        self.stop.set()
                        break
                valid, frame = cap.read()
                if not valid:
                    raise CameraError(f'{self.key} 未返回图像。检查 USB 带宽、MJPG 格式、设备占用或重新插拔相机')
                encoded, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not encoded:
                    raise CameraError('JPEG 编码失败')
                with self.condition:
                    self.frame, self.jpeg = frame, jpeg.tobytes()
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
        with self.condition:
            ready = self.condition.wait_for(lambda: self.seq > after or self.error or self.stop.is_set(), timeout)
            if self.error:
                raise CameraError(self.error)
            if not ready or self.stop.is_set():
                raise CameraError(f'{self.key} 等待图像超时或预览已暂停；检查 USB 带宽和设备占用')
            return self.seq, self.frame, self.jpeg


class CameraLease:
    def __init__(self, session):
        self.session, self.seq, self.released = session, 0, False
        self.captured_at = 0.0

    def read(self):
        with self.session.condition:
            self.seq, frame, _ = self.session.wait(self.seq)
            self.captured_at = self.session.captured_at
            return True, frame.copy()

    def jpeg(self, fresh=False):
        self.seq, _, jpeg = self.session.wait(self.seq if fresh else 0)
        return jpeg

    def release(self):
        with self.session.condition:
            if not self.released:
                self.released = True
                self.session.refs -= 1
                self.session.last_release = time.monotonic()


class CameraManager:
    def __init__(self, factory=None, idle_seconds=1):
        self.factory, self.idle_seconds = factory, idle_seconds
        self.lock = threading.RLock()
        self.sessions = {}
        self.exclusive = False

    def acquire(self, source, config=None):
        key = source_key(source)
        with self.lock:
            if self.exclusive:
                raise CameraError('相机已交给采集 / 遥操作 / 评估任务；任务结束后可重新预览')
            session = self.sessions.get(key)
            if session and session.stop.is_set() and session.thread.is_alive():
                raise CameraError(f'{key} 正在释放，请稍后重试')
            if session is None or not session.thread.is_alive():
                session = _Session(key, config or {}, self.factory, self.idle_seconds)
                self.sessions[key] = session
            with session.condition:
                session.refs += 1
            return CameraLease(session)

    def close_devices(self, sources):
        """Reconfigure only affected devices; metadata edits keep streams running."""
        keys = {source_key(source) for source in sources}
        with self.lock:
            selected = [s for key, s in self.sessions.items() if key in keys]
            for session in selected:
                session.stop.set()
                with session.condition:
                    session.condition.notify_all()
            deadline = time.monotonic() + 4
            for session in selected:
                session.thread.join(max(0, deadline-time.monotonic()))
            if any(session.thread.is_alive() for session in selected):
                raise CameraError('相机驱动正在释放设备，请稍后重试')
            for key in keys:
                self.sessions.pop(key, None)

    def close_all(self, exclusive=False):
        with self.lock:
            self.exclusive = exclusive
            sessions = list(self.sessions.values())
            for s in sessions:
                s.stop.set()
                with s.condition:
                    s.condition.notify_all()
            deadline = time.monotonic() + 4
            for s in sessions:
                s.thread.join(max(0, deadline-time.monotonic()))
            if any(s.thread.is_alive() for s in sessions):
                raise CameraError('相机驱动仍在等待图像，尚未释放；请稍后重试或重新插拔该相机')
            self.sessions.clear()

    def status(self):
        with self.lock:
            return {'exclusive': self.exclusive, 'sessions': [
                {'device': s.key, 'source':s.config.get('index_or_path',s.key), 'clients': s.refs, 'frames': s.seq, 'error': s.error,
                 'active': s.thread.is_alive() and not s.stop.is_set(), 'actual': s.actual,
                 'age_ms': round((time.time()-s.captured_at)*1000) if s.captured_at else None}
                for s in self.sessions.values()]}
