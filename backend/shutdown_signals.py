"""
设备收尾释放期间 POSIX 信号屏蔽保护模块 (Shutdown Signal Guard)
========================================================================

【模块背景与定位】
在机械臂控制、串口关闭或相机资源释放（Cleanup）的短暂临界区内，
如果操作员在终端中因焦虑连续多次按下 Ctrl+C（发送 SIGINT 信号），
可能会在 Python 正在执行底层 C 库资源析构或下位机握手时打断上下文，
导致串口句柄处于半开状态（D-State 僵死）或电机保持寄存器未生效造成掉臂。

【核心设计】
本模块提供上下文管理器 `finish_cleanup()`：
- 在主线程中临时将 SIGINT 处理屏蔽（`signal.signal(signal.SIGINT, signal.SIG_IGN)`）；
- 临界区代码执行完毕后，在 finally 块中无条件恢复原先的信号处理器；
- 故意不屏蔽 SIGTERM 信号，确保系统级急停与强杀通道仍然畅通无阻。
"""
from contextlib import contextmanager
import signal
import threading


@contextmanager
def finish_cleanup():
    """
    设备清理临界区上下文管理器：屏蔽 SIGINT 键盘中断，保护底层硬件释放流程完整执行。

    Yields:
        None
    """
    # 仅在主线程中能够调用 signal.signal，子线程中不作操作
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if main else None
    try:
        yield
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
