"""
共享视觉服务核心 Broker 进程 (Shared Vision Service Broker)
=============================================================

【模块背景与核心定位】
在多进程机械臂系统中，Web 预览流、快递扫码、抓取定位以及 LeRobot 录制若各自尝试通过
OpenCV VideoCapture 打开同一个 Linux /dev/video 设备，将因 Linux V4L2 设备的“排他独占性”
导致 "Device or resource busy" 崩溃。
本模块设计为【整个系统的唯一硬件物理图像采集进程】（Platform's Sole Vision Capture Process）：
1. 独占管理所有的物理 RGB 相机（CameraManager）与 OpenNI 深度传感器（DepthManager）。
2. 在本地通过 Unix 域套接字（AF_UNIX Socket，Windows 下回退至 127.0.0.1 TCP）提供高效的 RPC 服务。
3. 严格限制套接字权限为 0o600 并使用 32 字节高强度随机秘钥鉴权，杜绝本地越权。
4. 提供客户端请求分发线程池，支持多客户端同时订阅最新的 JPEG 压缩预览流或共享无损帧，
   并支持在 LeRobot 录制启动时优雅让出（exclusive 独占模式）。
"""
import os
import socket
import threading
from multiprocessing.connection import Listener

from .camera_manager import CameraManager, CameraError, list_devices
from .depth_manager import DepthManager


def serve(root, path, key, ready):
    """
    视觉 Broker 进程主监听服务循环。

    Args:
        root (str or Path): 项目根目录路径（用于定位 OpenNI 驱动库等资源）。
        path (str): 域套接字绑定的文件路径（如 /tmp/rebot-vision-xxx/capture.sock）。
        key (bytes): 客户端连接必须验证的共享安全密钥。
        ready (multiprocessing.Pipe): 向父进程通知服务已就绪的单向管道。
    """
    import cv2
    # 限制 OpenCV 内部图像编解码线程数为 2，避免争抢 CPU
    cv2.setNumThreads(2)

    cameras = CameraManager(idle_seconds=5.0)
    depth = DepthManager(root)
    stopping = threading.Event()

    # 步骤 1：创建本地通信监听端点（Linux 优先采用高性能 Unix Domain Socket）
    if os.name != 'nt' and hasattr(socket, 'AF_UNIX'):
        listener = Listener(path, family='AF_UNIX', authkey=key)
        os.chmod(path, 0o600)  # 严格限定权限：仅运行当前进程的用户可读写
        addr = path
    else:
        listener = Listener(('127.0.0.1', 0), family='AF_INET', authkey=key)
        addr = listener.address

    listener._listener._socket.settimeout(0.3)
    # 步骤 2：向父进程发送就绪信号 (包含当前 PID 与监听地址)
    ready.send((os.getpid(), addr))
    ready.close()

    def client(connection):
        """处理单个客户端长连接的 RPC 请求循环。"""
        lease = None
        try:
            while not stopping.is_set():
                request = connection.recv()
                method = request[0]
                try:
                    # 分支 1：获取 RGB 相机租约
                    if method == 'acquire-rgb':
                        lease = cameras.acquire(request[1], request[2])
                        value = True
                    # 分支 2：获取深度相机租约
                    elif method == 'acquire-depth':
                        lease = depth.acquire()
                        value = True
                    # 分支 3：读取最新 RGB 原始图像帧
                    elif method == 'read-rgb':
                        _, frame = lease.read()
                        size = request[1] if len(request) > 1 else None
                        if size and (frame.shape[1], frame.shape[0]) != tuple(size):
                            frame = cv2.resize(frame, tuple(size), interpolation=cv2.INTER_AREA)
                        value = (lease.seq, lease.captured_at, frame)
                    # 分支 4：读取最新 JPEG 压缩预览流
                    elif method == 'jpeg':
                        data = lease.jpeg(request[1])
                        value = (lease.seq, lease.session.captured_at, data)
                    # 分支 5：读取深度图与伪彩图
                    elif method == 'read-depth':
                        value = lease.read(request[1])
                    # 分支 6：零复制获取单张 RGB 快速快照
                    elif method == 'snapshot-rgb':
                        value = cameras.snapshot(request[1], request[2], start_capture=request[3])
                    # 分支 7：释放当前租约
                    elif method == 'release':
                        if lease:
                            lease.release()
                            lease = None
                        connection.send((True, None))
                        return
                    # 分支 8：查询状态与设备探测
                    elif method == 'rgb-status':
                        value = dict(cameras.status(), capture_pid=os.getpid())
                    elif method == 'depth-status':
                        value = dict(depth.status(), capture_pid=os.getpid())
                    elif method == 'close-rgb':
                        cameras.close_all(request[1])
                        value = True
                    elif method == 'close-devices':
                        cameras.close_devices(request[1])
                        value = True
                    elif method == 'close-depth':
                        depth.close()
                        value = True
                    elif method == 'discover':
                        value = list_devices()
                    elif method == 'shutdown':
                        stopping.set()
                        value = True
                    else:
                        raise CameraError('未知视觉服务请求')
                    connection.send((True, value))
                except Exception as exc:
                    connection.send((False, str(exc)))
        except (EOFError, OSError):
            pass
        finally:
            if lease:
                lease.release()
            connection.close()

    # 步骤 3：主监听循环，并发接受客户端连接并派发工作线程
    try:
        while not stopping.is_set():
            try:
                connection = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=client, args=(connection,), daemon=True, name='vision-client').start()
    finally:
        listener.close()
        try:
            depth.close()
        except CameraError:
            pass
        try:
            cameras.close_all()
        except CameraError:
            pass
