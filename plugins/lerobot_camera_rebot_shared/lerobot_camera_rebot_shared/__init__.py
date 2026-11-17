"""Lossless loopback camera transport: one physical owner, many consumers.

This plugin only reads images. Importing, parsing configuration and construction
never connects a robot or sends motor commands.
"""
from __future__ import annotations

import os
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np
from lerobot.cameras.camera import Camera
from lerobot.cameras.configs import CameraConfig

HEADER = struct.Struct('<4sQdIIII')
MAX_FRAME_BYTES = 128 * 1024 * 1024


@CameraConfig.register_subclass('rebot_shared')
@dataclass(kw_only=True)
class RebotSharedCameraConfig(CameraConfig):
    source: str = '0'
    broker_url: str = ''
    color_mode: str = 'rgb'
    resize_to_config: bool = False


def read_exact(stream, size):
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionError('共享视觉数据流已断开')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


class RebotSharedCamera(Camera):
    def __init__(self, config: RebotSharedCameraConfig):
        super().__init__(config)
        self.config = config
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.response = None
        self.thread = None
        self.frame = None
        self.sequence = self.consumed = 0
        self.captured_at = 0.0
        self.error = ''

    @property
    def is_connected(self):
        return bool(self.thread and self.thread.is_alive() and self.frame is not None and not self.error and not self.stop.is_set())

    @staticmethod
    def find_cameras():
        return []  # Device discovery belongs to the broker, never to a second owner.

    def connect(self, warmup=True):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.error, self.frame = '', None
        self.sequence = self.consumed = 0
        self.thread = threading.Thread(target=self._receive, daemon=True, name='rebot-shared-camera')
        self.thread.start()
        try:
            self.async_read(timeout_ms=12000)
        except BaseException:
            self.disconnect()
            raise

    def _receive(self):
        try:
            base = self.config.broker_url or os.environ.get('REBOT_CAMERA_BROKER_URL', 'http://127.0.0.1:8000')
            parsed = urllib.parse.urlparse(base)
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('采集视觉链路必须使用 Ubuntu 本机的共享视觉服务')
            url = base.rstrip('/') + '/api/camera/raw?device=' + urllib.parse.quote(self.config.source, safe='')
            if self.config.resize_to_config:
                url += '&width='+str(self.width)+'&height='+str(self.height)
            self.response = urllib.request.urlopen(url, timeout=12)
            while not self.stop.is_set():
                magic, seq, timestamp, width, height, channels, length = HEADER.unpack(read_exact(self.response, HEADER.size))
                if magic != b'RBFR' or channels != 3 or not (0 < length <= MAX_FRAME_BYTES) or width*height*channels != length:
                    raise ValueError('共享视觉帧头或图像尺寸不合法')
                frame = np.frombuffer(read_exact(self.response, length), dtype=np.uint8).reshape(height, width, channels)
                if self.config.color_mode.lower() == 'rgb':
                    frame = frame[:, :, ::-1].copy()
                else:
                    frame = frame.copy()
                if self.width and self.height and (width != self.width or height != self.height):
                    raise ValueError(f'相机实际尺寸 {width}×{height} 与数据集配置 {self.width}×{self.height} 不符')
                with self.condition:
                    self.width, self.height = width, height
                    self.frame, self.sequence, self.captured_at = frame, seq, timestamp
                    self.condition.notify_all()
        except Exception as exc:
            if not self.stop.is_set():
                with self.condition:
                    self.error = f'相机 {self.config.source} 共享采集失败：{exc}'
                    self.condition.notify_all()
        finally:
            if self.response:
                self.response.close()
            with self.condition:
                self.condition.notify_all()

    def async_read(self, timeout_ms=1000):
        with self.condition:
            wait_s = timeout_ms / 1000 if self.frame is not None else max(timeout_ms / 1000, 5.0)
            if self.frame is not None:
                wait_s = min(wait_s, 0.03)
            ready = self.condition.wait_for(lambda: self.error or self.stop.is_set() or
                (self.frame is not None and self.sequence != self.consumed), wait_s)
            if (not ready and self.frame is not None and not self.error and not self.stop.is_set()
                    and time.time() - self.captured_at <= 5.0):
                return self.frame.copy()
            if self.error:
                raise RuntimeError(self.error)
            if not ready or self.stop.is_set():
                raise TimeoutError(f'相机 {self.config.source} 等待新图像超时')
            if time.time() - self.captured_at > 2:
                raise TimeoutError('共享相机图像已过期，拒绝把旧帧作为当前观测')
            self.consumed = self.sequence
            return self.frame.copy()

    def read(self):
        return self.async_read(timeout_ms=2000)

    def read_latest(self, max_age_ms=1000):
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            if self.frame is None or (time.time()-self.captured_at)*1000 > max_age_ms:
                raise TimeoutError('没有新鲜的共享相机图像')
            return self.frame.copy()

    def disconnect(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        response, self.response = self.response, None
        if response:
            response.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

from .feedback import install as _install_feedback
_install_feedback()
