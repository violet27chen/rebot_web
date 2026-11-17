"""
Web 服务侧视觉代理进程客户端 (Vision Process Web Proxy Client)
==================================================================

【模块背景与定位】
在平台设计中，FastAPI 主服务进程【绝对不直接打开任何视频采集硬件设备】，
而是通过本模块作为透明代理，与底层的 `rebot-vision` 守护子进程（基于 `vision_broker`）
通过本地 Socket RPC 进行跨进程通信。

【核心代理与安全特性】
1. 惰性按需拉起（Lazy Startup）：当首次发起预览、录制或拍照请求时，自动以 'spawn' 模式拉起子进程。
2. 权限隔离与临时隔离区：为套接字自动创建受限权限（0o700）的临时文件夹，使用 32 字节高熵令牌防篡改。
3. 优雅租约封装（`RPCLease`）：上层业务持有 `RPCLease` 句柄，像操作本地对象一样调用 `read()` 或 `jpeg()`，
   底层通过 Socket 协议交互，退出时通过 context manager 或 `release()` 自动回收。
4. 独占互锁代理：支持通过 `close_all(exclusive=True)` 让视觉子进程释放所有 UVC 相机，
   以便 LeRobot 官方录制/评估子进程可以直接独占物理硬件设备。
"""
import multiprocessing as mp
import os
import secrets
import shutil
import tempfile
import threading
from pathlib import Path
from multiprocessing.connection import Client

from .camera_manager import CameraError
from .depth_manager import depth_present
from .vision_broker import serve


class RPCLease:
    """
    跨进程视觉数据租约客户端代理句柄。
    提供读帧、JPEG 流获取与自动释放功能。
    """

    def __init__(self, connection, depth=False):
        self.connection = connection
        self.depth = depth
        self.seq = 0
        self.captured_at = 0.0
        self.released = False
        self.lock = threading.RLock()
        self.session = self
        self.error = ''

    def request(self, *args):
        """向服务端发送同步 RPC 请求并等待解包返回。"""
        with self.lock:
            if self.released:
                raise CameraError('视觉连接已释放')
            try:
                self.connection.send(args)
                if not self.connection.poll(15):
                    raise CameraError('视觉服务等待超时')
                valid, value = self.connection.recv()
                if not valid:
                    raise CameraError(value)
                return value
            except (OSError, EOFError) as exc:
                self.error = '共享视觉进程已断开，请重新连接画面'
                raise CameraError(self.error) from exc

    def read(self, fresh=False, output_size=None):
        """读取最新无损图像帧。"""
        if self.depth:
            return self.request('read-depth', fresh)
        self.seq, self.captured_at, frame = self.request('read-rgb', output_size)
        return True, frame

    def jpeg(self, fresh=False):
        """读取最新 JPEG 格式编码图片。"""
        self.seq, self.captured_at, jpeg = self.request('jpeg', fresh)
        return jpeg

    def release(self):
        """关闭连接并通知服务端释放对应设备的引用计数。"""
        with self.lock:
            if self.released:
                return
            self.released = True
            try:
                self.connection.send(('release',))
            except (OSError, EOFError):
                pass
            self.connection.close()


class VisionProcess:
    """
    管理底层独立的视觉采集 Broker 子进程生命周期与 Socket 连接。
    """

    def __init__(self, root):
        self.root = str(root)
        self.context = mp.get_context('spawn')
        self.lock = threading.RLock()
        self.process = None
        self.folder = None
        self.path = None
        self.address = None
        self.key = secrets.token_bytes(32)
        self.paused = False
        self.rgb = RGBCameras(self)
        self.depth = DepthCamera(self)

    def alive(self):
        """检查底层视觉子进程是否存活。"""
        return bool(self.process and self.process.is_alive())

    def start(self):
        """按需创建临时安全目录并启动底层独立的视觉采集守护进程。"""
        with self.lock:
            if self.paused:
                raise CameraError('设备已断开，请先恢复监视或重新连接设备')
            if self.alive():
                return
            if self.process:
                self.process.join(timeout=0.1)
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = tempfile.mkdtemp(prefix='rebot-vision-')
            os.chmod(self.folder, 0o700)
            self.path = str(Path(self.folder) / 'capture.sock')
            receive, send = self.context.Pipe(duplex=False)
            self.process = self.context.Process(
                target=serve,
                args=(self.root, self.path, self.key, send),
                name='rebot-vision',
                daemon=True
            )
            self.process.start()
            send.close()
            try:
                if not receive.poll(10):
                    raise CameraError('共享视觉进程启动超时')
                info = receive.recv()
                self.address = (
                    tuple(info[1])
                    if isinstance(info, (list, tuple)) and len(info) == 2 and isinstance(info[1], (list, tuple))
                    else (info[1] if isinstance(info, (list, tuple)) and len(info) == 2 else self.path)
                )
            except (EOFError, OSError) as exc:
                raise CameraError('共享视觉进程启动失败') from exc
            finally:
                receive.close()

    def connect(self, start=True):
        """建立与视觉守护进程的底层 Socket 客户端连接。"""
        if start:
            self.start()
        if not self.alive():
            raise CameraError('共享视觉进程尚未启动')
        addr = self.address or self.path
        family = 'AF_UNIX' if isinstance(addr, str) else 'AF_INET'
        try:
            return Client(addr, family=family, authkey=self.key)
        except (OSError, EOFError) as exc:
            raise CameraError('共享视觉进程连接已断开') from exc

    def request(self, *args, start=True):
        """发送单次短连接 RPC 指令并接收返回值。"""
        connection = self.connect(start)
        try:
            connection.send(args)
            if not connection.poll(2 if args[0].endswith('status') else 15):
                raise CameraError('共享视觉进程响应超时')
            valid, value = connection.recv()
            if not valid:
                raise CameraError(value)
            return value
        except (EOFError, OSError) as exc:
            raise CameraError('共享视觉进程已退出') from exc
        finally:
            connection.close()

    def acquire(self, method, *args, depth=False):
        """创建长连接租约。"""
        connection = self.connect()
        lease = RPCLease(connection, depth)
        try:
            lease.request(method, *args)
            return lease
        except BaseException:
            lease.release()
            raise

    def discover(self):
        """枚举当前系统所有物理可用相机。"""
        return self.request('discover')

    def stop(self):
        """停止视觉子进程并清理临时通信目录。"""
        with self.lock:
            if self.alive():
                try:
                    self.request('shutdown', start=False)
                except CameraError:
                    pass
                self.process.join(timeout=3)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=2)
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)


class RGBCameras:
    """提供 RGB 相机高层操作接口（acquire, snapshot, close_all 等）。"""

    def __init__(self, vision):
        self.vision = vision

    def acquire(self, source, config=None):
        return self.vision.acquire('acquire-rgb', source, config or {})

    def snapshot(self, source, config=None, start_capture=True):
        if not start_capture and not self.vision.alive():
            raise CameraError('采集中共享视觉进程尚无画面，扫码已跳过')
        return self.vision.request('snapshot-rgb', source, config or {}, bool(start_capture), start=start_capture)

    def close_all(self, exclusive=False):
        if self.vision.alive():
            return self.vision.request('close-rgb', exclusive, start=False)

    def close_devices(self, sources):
        if sources and self.vision.alive():
            return self.vision.request('close-devices', list(sources), start=False)

    def status(self):
        if self.vision.alive():
            try:
                return self.vision.request('rgb-status', start=False)
            except CameraError:
                pass
        return {'sessions': [], 'exclusive': False, 'capture_pid': None}


class DepthCamera:
    """提供深度相机高层操作接口。"""

    def __init__(self, vision):
        self.vision = vision

    def acquire(self):
        return self.vision.acquire('acquire-depth', depth=True)

    def close(self):
        if self.vision.alive():
            return self.vision.request('close-depth', start=False)

    def status(self):
        if self.vision.alive():
            try:
                return self.vision.request('depth-status', start=False)
            except CameraError:
                pass
        return {
            'available': depth_present(),
            'active': False,
            'clients': 0,
            'profile': None,
            'age_ms': None,
            'error': '',
            'capture_pid': None
        }
