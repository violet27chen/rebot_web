"""
OpenNI 深度传感器多租户线程化管理模块 (OpenNI Depth Manager)
================================================================

【模块背景与定位】
本模块作为共享视觉进程内部的核心组件，专门负责奥比中光（Orbbec）结构光/双目深度相机的
生命周期管理、深度帧捕获与多租户并发访问调度。

【核心设计与特性】
1. 硬件自动探测 (`depth_present`)：
   通过扫描 Linux 系统内核 `/sys/bus/usb/devices` 路径下的 USB 厂商编号（Vendor ID: 0x2bc5），
   快速确定是否插入了奥比中光深度设备，避免盲目初始化 C 库。
2. 自动化伪彩色可视化渲染：
   在后台捕获线程中，将原始 16 位毫米无符号整型深度矩阵归一化映射到 [200, 4000] mm 工作区间，
   利用 OpenCV Turbo 色彩空间（COLORMAP_TURBO）实时生成色彩对比强烈的伪彩深度图，无效深度点设为黑，
   同时输出用于高保真存储的 16 位灰度 PNG 压缩流与用于前端 Web 实时预览的 JPEG 流。
3. 租约引用计数与空闲自动关机 (`DepthLease`)：
   当有外部客户端订阅时，按需拉起后台捕获线程；当所有租约释放且连续 2 秒无新请求时，
   自动关闭硬件深度流，降低相机发热与 USB 带宽占用。
"""
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from .camera_manager import CameraError
from .openni_depth import OpenNIDepth


def depth_present():
    """
    通过检查 Linux sysfs 硬件树快速探测 Orbbec USB 深度模块是否存在。
    厂商 ID (idVendor) 固定为 2bc5，产品 ID 匹配常见 Astra/Gemini 模块（060f, 0401, 0403 等）。

    Returns:
        bool: True 表示物理硬件已连接。
    """
    for vendor in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        try:
            if vendor.read_text().strip() == '2bc5' and vendor.with_name('idProduct').read_text().strip() in (
                '060f', '0401', '0403'
            ):
                return True
        except OSError:
            pass
    return False


class DepthLease:
    """
    客户端持有的深度传感器租约句柄。
    提供帧等待同步与引用计数自动递减功能。
    """

    def __init__(self, manager):
        self.manager = manager
        self.generation = manager.generation
        self.sequence = 0
        self.released = False

    def read(self, fresh=False):
        """
        阻塞等待并读取下一帧深度图像数据。

        Args:
            fresh (bool, optional): 是否要求严格严格比当前 sequence 更晚的新鲜帧。

        Returns:
            tuple[dict, bytes, bytes]: (元数据字典, JPEG 伪彩色预览流, 16位无损 PNG 深度图)。

        Raises:
            CameraError: 当深度流已暂停、等待超时或图像过期时抛出。
        """
        m = self.manager
        with m.condition:
            ready = m.condition.wait_for(
                lambda: m.error or m.closed or m.generation != self.generation or (
                    m.meta and m.meta['sequence'] > (self.sequence if fresh else 0)
                ),
                12
            )
            if m.error:
                raise CameraError(m.error)
            if not ready or m.closed or m.generation != self.generation:
                raise CameraError('深度流已暂停或等待超时')
            if time.time() - m.meta['timestamp'] > 2:
                raise CameraError('深度图像已过期，请重新连接相机')
            self.sequence = m.meta['sequence']
            return dict(m.meta), m.jpeg, m.png

    def release(self):
        """释放当前租约，递减使用计数并记录释放时间戳。"""
        m = self.manager
        with m.condition:
            if self.released:
                return
            self.released = True
            if self.generation == m.generation:
                m.refs = max(0, m.refs - 1)
                m.last_release = time.monotonic()


class DepthManager:
    """
    深度传感器核心管理器：
    负责 OpenNI 底层驱动初始化、后台多帧捕获线程调度以及帧共享通知。
    """

    def __init__(self, root):
        self.root = Path(root)
        self.condition = threading.Condition(threading.RLock())
        self.thread = None
        self.stopping = threading.Event()
        self.generation = 0
        self.closed = True
        self.refs = 0
        self.last_release = 0
        self.meta = self.jpeg = self.png = None
        self.error = ''

    def acquire(self):
        """
        获取深度相机的访问租约；若底层采集线程未运行，自动唤醒并启动。

        Returns:
            DepthLease: 深度数据租约对象。

        Raises:
            CameraError: 硬件不存在或正在释放时抛出。
        """
        with self.condition:
            if self.thread and self.thread.is_alive() and self.closed:
                raise CameraError('深度连接正在释放，请稍后重试')
            if not self.thread or not self.thread.is_alive():
                if not depth_present():
                    raise CameraError('未检测到 Orbbec 深度模块，请检查 USB 连接')
                self.generation += 1
                self.refs = 0
                self.closed = False
                self.stopping.clear()
                self.error = ''
                self.meta = self.jpeg = self.png = None
                self.thread = threading.Thread(
                    target=self._capture,
                    args=(self.generation,),
                    daemon=True,
                    name='openni-depth'
                )
                self.thread.start()
            self.refs += 1
            return DepthLease(self)

    def _capture(self, generation):
        """后台独立采集线程：以约 15Hz 频率轮询读取硬件深度帧，并生成伪彩与 PNG 压缩流。"""
        camera = OpenNIDepth(self.root / 'vendor/openni2')
        try:
            metadata = camera.open()
            counter = 0
            published = 0
            while not self.stopping.is_set():
                # 从底层 C SDK 阻塞等待读取深度帧（超时 1000ms）
                depth, index, device_time = camera.read(1000)
                now = time.time()
                with self.condition:
                    # 空闲回收：无客户端租用且超过 2 秒未被使用，自动休眠关闭
                    if not self.refs and time.monotonic() - self.last_release > 2:
                        self.closed = True
                        break

                # 频率限制：约 15fps（每帧间隔 >= 65ms）
                if now - published < 0.065:
                    continue
                published = now
                counter += 1

                # 归一化并渲染 Turbo 伪彩深度图 (将 200~4000mm 映射为 0~255)
                value = np.clip((depth.astype(np.float32) - 200) * 255 / 3800, 0, 255).astype(np.uint8)
                view = cv2.applyColorMap(value, cv2.COLORMAP_TURBO)
                view[depth == 0] = 0  # 测距无效区域置为纯黑
                valid = depth[depth > 0]

                # 编码为 PNG (16位无损) 与 JPEG (Web 伪彩色)
                ok_png, png = cv2.imencode('.png', depth, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                ok_jpg, jpeg = cv2.imencode('.jpg', view, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok_png or not ok_jpg:
                    raise CameraError('深度图编码失败')

                meta = dict(
                    metadata,
                    sequence=counter,
                    frame_index=index,
                    timestamp=now,
                    device_timestamp=device_time,
                    color_range_mm=[200, 4000],
                    valid_ratio=float((depth > 0).mean()),
                    minimum_mm=int(valid.min()) if valid.size else None,
                    maximum_mm=int(valid.max()) if valid.size else None,
                    capture_pid=os.getpid()
                )

                with self.condition:
                    if self.generation != generation:
                        break
                    self.meta, self.jpeg, self.png = meta, jpeg.tobytes(), png.tobytes()
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()
        finally:
            camera.close()
            with self.condition:
                if self.generation == generation:
                    self.closed = True
                self.condition.notify_all()

    def close(self):
        """主动强制关闭深度相机，释放 USB 资源。"""
        with self.condition:
            self.closed = True
            self.refs = 0
            self.generation += 1
            self.stopping.set()
            self.condition.notify_all()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise CameraError('深度 SDK 尚未释放连接')

    def status(self):
        """查询深度相机当前运行状态快照。"""
        with self.condition:
            return dict(
                available=depth_present(),
                active=bool(self.thread and self.thread.is_alive() and not self.closed),
                clients=self.refs,
                error=self.error,
                profile=self.meta,
                age_ms=round((time.time() - self.meta['timestamp']) * 1000) if self.meta else None
            )
