"""
双臂协同并行收工同步屏障与安全收回调度器 (Coordinated Parallel Shutdown Barrier)
========================================================================================

【模块背景与定位】
在双臂机器人系统执行“安全收工（Shutdown / Homing）”时，左臂与右臂分别连接在独立的 CAN 总线上。
如果单臂串行收回，先收回的一侧失能后，若另一侧在运动中发生碰撞报警，整个系统将陷入不确定状态；
如果双臂各走各的，速度快的一侧先到达并断电掉臂，速度慢的一侧还在高空，极易发生双臂在中心干涉撞击。

【核心设计准则】
1. 并发收回（Concurrent Execution）：
   使用线程池（ThreadPoolExecutor）对每个独立的机械臂总线派发独立的收工工作线程。
2. 阶段性同步检查点屏障 (`ShutdownGroup.checkpoint`)：
   将收工分解为明确的阶段（底座腕部 -> 肩肘 -> 夹爪）。每到一个检查点，
   先到达的臂必须在硬件层面持续刷新实测保持力矩（`hold()`），等待对侧到达；
   只有双臂均确认到位后，才同时步入下一阶段。
3. 对齐最慢速度规划 (`movement_duration`)：
   基于最小加加速度（Minimum-Jerk）算法，分别计算左臂与右臂完成当前位移所需的时长，
   取两臂中的最大值作为双臂统一的规划时长，使双臂在运动学上同频、匀速平稳收回。
4. 协同熔断（Coordinated Abort）：
   若任何一侧机械臂发生超时、总线断开或跟踪超差，立即触发全局 `abort()`。
   对侧收到中止通知后，立即停止继续收回并保持当前实测位置，绝不盲目断电失能！
"""
from concurrent.futures import ThreadPoolExecutor
import math
import threading
import time

try:
    from .homing_profile import HOME_SPEED_RAD_S, HOME_ACCEL_RAD_S2
except ImportError:
    from homing_profile import HOME_SPEED_RAD_S, HOME_ACCEL_RAD_S2


class ShutdownCancelled(RuntimeError):
    """当对侧机械臂收工失败或收到外部停止请求时抛出。"""
    pass


class ShutdownGroup:
    """
    多臂协同收工同步屏障对象。
    维护跨线程屏障检查点、运动时长协调与全局中止事件。
    """

    def __init__(self, participants, *, timeout=900):
        """
        初始化同步组。

        Args:
            participants (int): 参与协同收工的臂数（例如 2 代表双臂）。
            timeout (float, optional): 单个检查点等待对侧到达的最大超时时间（秒），默认 900s。
        """
        if participants < 1:
            raise ValueError('Shutdown requires at least one participant')
        self.participants = participants
        self.timeout = timeout
        self.cancelled = threading.Event()
        self.condition = threading.Condition()
        self.generation = self.arrivals = 0
        self.values, self.results = {}, {}

    def abort(self):
        """广播全局中止信号，唤醒并通知所有等待中的工作线程停止收工。"""
        self.cancelled.set()
        with self.condition:
            self.condition.notify_all()

    def check(self):
        """主动检查当前是否已收到全局中止请求。"""
        if self.cancelled.is_set():
            raise ShutdownCancelled('双臂收工已中止：另一侧失败或收到停止请求，保持目标，不继续收回或失能')

    def checkpoint(self, hold=None, *, value=0.0):
        """
        同步屏障检查点：等待同伴机械臂到达，期间持续调用 hold 回调维持本臂力矩。

        Args:
            hold (callable, optional): 等待期间周期性调用的力矩刷新函数。
            value (float, optional): 本臂贡献的标量数值（如计算出的推荐运动耗时）。

        Returns:
            float: 该检查点所有参与者上报的数值的最大值。

        Raises:
            ShutdownCancelled: 收工被中止。
            TimeoutError: 等待对侧到达超时。
        """
        self.check()
        deadline = time.monotonic() + self.timeout

        with self.condition:
            generation = self.generation
            self.values.setdefault(generation, []).append(value)
            self.arrivals += 1
            # 最后一个到达的线程：计算结果并翻转代数，唤醒所有等待者
            if self.arrivals == self.participants:
                result = self.results[generation] = max(self.values.pop(generation))
                self.arrivals = 0
                self.generation += 1
                self.condition.notify_all()
                return result

        # 先到达的线程：循环等待对侧到达，等待期间持续执行保持回调
        while True:
            self.check()
            with self.condition:
                if generation != self.generation:
                    return self.results[generation]
                self.condition.wait(0.03)
            self.check()
            if time.monotonic() >= deadline:
                self.abort()
                raise TimeoutError('等待另一臂收工超时，未进入下一阶段或失能')
            if hold is not None:
                hold()

    def movement_duration(self, start, target, *, speed=HOME_SPEED_RAD_S, acceleration=HOME_ACCEL_RAD_S2, hold=None):
        """
        计算双臂满足速度与加速度限制所需的最大时长，并在检查点同步对齐。

        Args:
            start (Sequence[float]): 起始角度列表。
            target (Sequence[float]): 目标角度列表。
            speed (float, optional): 最大角速度 (rad/s)。
            acceleration (float, optional): 最大角加速度 (rad/s²)。
            hold (callable, optional): 保持函数。

        Returns:
            float: 两臂中较慢一方所需的最长平滑插值时间 (秒)。
        """
        if len(start) != len(target) or not start or not all(math.isfinite(v) for v in (*start, *target)):
            raise ValueError('双臂收工需要完整的有限实测角度')
        if not (0 < speed <= 0.3) or not (0 < acceleration <= 0.4):
            raise ValueError('双臂收工速度或加速度无效')
        delta = max(abs(a - b) for a, b in zip(start, target))
        duration = max(2.0, 1.875 * delta / speed, math.sqrt((10 / math.sqrt(3)) * delta / acceleration))
        return self.checkpoint(hold, value=duration)


def parallel_shutdown(items, operation, *, checkpoint_count=None):
    """
    启动线程池并行执行多臂收工，并在返回前无条件 join 等待所有工作线程平稳退出。

    Args:
        items (Iterable): 机械臂配置或会话项列表。
        operation (callable): 针对单个机械臂执行的收工函数 (item, group) -> None。
        checkpoint_count (int, optional): 期望参与检查点的屏障总数，默认与 items 数量相同。

    Raises:
        Exception: 捕获并向外抛出真实的硬件错误原因，而不是从属中止异常。
    """
    items = list(items)
    if not items:
        return
    group = ShutdownGroup(checkpoint_count if checkpoint_count is not None else len(items))

    def run(item):
        try:
            operation(item, group)
            group.check()
        except BaseException:
            group.abort()
            raise

    pool = ThreadPoolExecutor(max_workers=len(items), thread_name_prefix='arm-shutdown')
    errors = []
    try:
        futures = [pool.submit(run, item) for item in items]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)
    except BaseException:
        group.abort()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if errors:
        # 优先报告真实的底层硬件通信异常，而非仅是对侧收到的 ShutdownCancelled
        raise next((exc for exc in errors if not isinstance(exc, ShutdownCancelled)), errors[0])
