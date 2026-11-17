"""
运单条码/OCR 进程级隔离运行管理器 (Waybill Scanner Process Manager)
====================================================================

【模块背景与架构定位】
条码识别（ZXing-C++）和深度学习 OCR（ONNX Runtime / RapidOCR）在解析恶劣图像时，
可能发生 C/C++ 动态链接库段错误（Segmentation Fault）、死循环或过度抢占 CPU，
若直接在 FastAPI 核心服务进程中执行，一旦崩溃会导致整个机械臂控制服务全部瘫痪。

【核心隔离与防护设计】
1. 进程级隔离（Process Boundary）：
   使用 multiprocessing 的 'spawn' 独立子进程执行条码解码与 OCR 推理，
   主子进程间仅通过匿名管道（Pipe）传递纯 JPEG 二进制字节，零内存共享。
2. CPU 资源与线程限制：
   子进程启动后将 OMP_NUM_THREADS、OPENBLAS_NUM_THREADS 等均设置为 1，
   并将进程静态优先级调低（nice 10），且绑定单核 CPU 亲和性，
   坚决杜绝 OCR 算法争抢机器人运动控制线程的 CPU 算力。
3. 超时强杀与无感自愈（Timeout Kill & Self-Healing）：
   外部设定严格扫描超时（默认 6.0s）。若子进程卡死，自动 terminate 并 kill，
   同时对外抛出 ScanError，上位机采集与相机预览不受任何影响，下次请求时自动重新拉起干净的子进程。
4. 防并发击穿（Non-blocking Concurrency Control）：
   使用非阻塞线程互斥锁（lock.acquire(blocking=False)），在已有扫码任务进行时直接拒绝堆叠请求，
   返回 ScanBusy，防止并发挤压。
"""
import multiprocessing as mp
import os
import threading
import time


class ScanBusy(RuntimeError):
    """当前已有正在进行的扫码任务时抛出，指示请求直接丢弃不排队。"""
    pass


class ScanError(RuntimeError):
    """扫码过程中通信超时、图像损坏或子进程异常时抛出。"""
    pass


def decode_batch(items):
    """
    在子进程中执行一批图像数据的解压缩与条码/OCR 解码。

    Args:
        items (list[dict]): 待处理项列表，每个元素包含 'name', 'source', 'jpeg' (字节流) 等字段。

    Returns:
        list[dict]: 各相机的识别结果列表。
    """
    import cv2
    import numpy as np
    from .waybill_decoder import _decode_waybill_frame, _render_waybill_preview_b64

    results = []
    for item in items:
        result = {
            'name': item['name'],
            'source': item['source'],
            'ok': False,
            'sharpness': 0.0,
            'found': 0,
            'label_rois': 0,
            'width': 0,
            'height': 0,
            'boxes': [],
            'preview': '',
            'codes': []
        }
        try:
            # 从二进制 JPEG 字节流解码为 OpenCV 图像
            frame = cv2.imdecode(np.frombuffer(item['jpeg'], np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('共享相机帧无法解码')
            h, w = frame.shape[:2]
            decoded = _decode_waybill_frame(frame)
            include_preview = item.get('include_preview', True)
            result.update(
                ok=True,
                sharpness=decoded.get('sharpness', 0),
                codes=decoded.get('codes') or [],
                found=len(decoded.get('codes') or []),
                label_rois=len(decoded.get('boxes') or []),
                width=int(w),
                height=int(h),
                boxes=decoded.get('boxes') or [],
                exposure_hint=decoded.get('exposure_hint', ''),
                preview=_render_waybill_preview_b64(frame, item['name'], decoded) if include_preview else ''
            )
        except Exception as exc:
            result['error'] = str(exc)
        results.append(result)
    return results


def run_worker(connection, decoder=decode_batch):
    """
    隔离子进程的工作循环入口：限制自身线程与 CPU 优先级，循环从 Pipe 接收任务并回传结果。

    Args:
        connection: 多进程 Pipe 双向通信端点。
        decoder (callable): 批处理解码函数。
    """
    # 步骤 1：限制各类数学计算库（BLAS/OMP）的并发线程数为 1，防止算力争抢
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[name] = '1'
    try:
        # 将子进程调度优先级设为较低的 nice=10
        os.nice(10)
        # 绑定至编号最大的单个 CPU 核心运行
        allowed = os.sched_getaffinity(0)
        if len(allowed) > 1:
            os.sched_setaffinity(0, {max(allowed)})
    except (AttributeError, OSError):
        pass

    try:
        import cv2
        cv2.setNumThreads(1)
        # 步骤 2：监听管道任务循环
        while True:
            items = connection.recv()
            if items is None:
                break
            try:
                connection.send((True, decoder(items)))
            except Exception as exc:
                connection.send((False, str(exc)))
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


class WaybillProcess:
    """
    运单识别工作进程的生命周期管理客户端。
    负责子进程的按需生成、管道通信、超时监测以及异常崩溃后的强杀自愈。
    """

    def __init__(self, timeout=6.0, decoder=decode_batch):
        """
        初始化管理器。

        Args:
            timeout (float, optional): 单次扫描总超时时间（秒），默认 6.0s。
            decoder (callable, optional): 解码执行回调，便于单元测试 mock。
        """
        self.context = mp.get_context('spawn')
        self.lock = threading.Lock()
        self.process = self.connection = None
        self.timeout = timeout
        self.decoder = decoder

    def _stop(self):
        """安全终止并清理子进程和管道连接。"""
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=0.3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=0.3)
            self.process = None

    def scan(self, items):
        """
        向隔离子进程提交待解码图片，同步等待返回解码结果。

        【执行保护】
        - 若当前已有任务正在执行，直接抛出 ScanBusy，不排队；
        - 使用后台发送线程防止大数据量图片填满 Pipe 缓冲区造成死锁；
        - 设置严格截止时间（deadline），超时强杀卡死的子进程并抛出 ScanError；
        - 不影响主服务的正常相机帧流采集。

        Args:
            items (list[dict]): 待处理相机帧列表。

        Returns:
            list[dict]: 识别结果列表。

        Raises:
            ScanBusy: 当前正忙于上一轮识别。
            ScanError: 通信或识别超时或崩溃。
        """
        # 非阻塞锁：已有任务正在处理时直接放弃本次，防止积压
        if not self.lock.acquire(blocking=False):
            raise ScanBusy('扫码识别正在进行，本次不排队；采集继续运行')
        try:
            deadline = time.monotonic() + self.timeout
            # 若子进程未创建或已退出，重新拉起新的子进程
            if not self.process or not self.process.is_alive():
                self._stop()
                self.connection, child = self.context.Pipe()
                self.process = self.context.Process(
                    target=run_worker,
                    args=(child, self.decoder),
                    name='rebot-waybill',
                    daemon=True
                )
                self.process.start()
                child.close()

            # 通过独立线程异步向管道推送数据，防止大图阻塞导致超时
            connection, sent, errors = self.connection, threading.Event(), []

            def deliver():
                try:
                    connection.send(items)
                except (OSError, EOFError, ValueError, TypeError) as exc:
                    errors.append(exc)
                finally:
                    sent.set()

            threading.Thread(target=deliver, name='waybill-delivery', daemon=True).start()

            # 等待数据交付完成
            if not sent.wait(max(0.0, deadline - time.monotonic())):
                self._stop()
                raise ScanError('扫码帧传输超时，已回收识别进程；采集与相机继续运行')
            if errors:
                raise errors[0]

            # 轮询等待管道返回数据
            if not connection.poll(max(0.0, deadline - time.monotonic())):
                self._stop()
                raise ScanError('扫码识别超时，已回收识别进程；采集与相机继续运行，可再次扫码')

            valid, value = connection.recv()
            if not valid:
                raise ScanError(value)
            return value
        except (EOFError, OSError, ValueError, TypeError) as exc:
            self._stop()
            raise ScanError('扫码识别进程已退出；采集与相机未关闭，可重试') from exc
        finally:
            self.lock.release()

    def close(self):
        """外部释放资源接口。"""
        with self.lock:
            self._stop()
