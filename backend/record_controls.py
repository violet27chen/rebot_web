"""
无头环境网页录制控制标准输入管道通道 (Headless Web Recording Controls)
========================================================================

【模块背景与定位】
原生 LeRobot 录制脚本默认依赖桌面图形系统的全局键盘监听库（`pynput`）与 OpenCV 弹窗
来接收操作员的按键指令（如按 Esc 停止录制、按 Left 重录当前剧集、按 Right 继续下一集）。
在 Linux 无桌面（Headless）、远程 SSH 会话或 Web 端控制场景中，由于缺少 X11/Wayland 的
DISPLAY 环境变量，`pynput` 启动会立即抛出异常或静默失效，导致无法在 Web 前端控制录制流程。

【核心设计与协议机制】
1. 管道通信协议：
   利用父子进程之间现成的标准输入（sys.stdin）建立单向文本控制管道：
   - 每行命令格式：`REBOT_RECORD_CONTROL <right|left|esc>`
   - 当就绪时向 stdout 输出握手标记：`REBOT_RECORD_CONTROL_READY`
2. 动作映射：
   - 'left': 对应重录当前剧集 (`rerecord_episode = True, exit_early = True`)；
   - 'esc': 对应停止并保存录制 (`stop_recording = True, exit_early = True`)；
   - 'right': 对应正常结束当前段并继续。
3. 异常断开安全自愈：
   当 Web 主进程意外退出或关闭 stdin 时（readline 返回空串），自动标记
   `stop_recording=True, exit_early=True`，触发录制正常收尾保存，绝不丢弃已录制数据。
"""
import logging
import os
import sys
import threading

# 协议命令前缀与握手标记
PREFIX = 'REBOT_RECORD_CONTROL '
READY = 'REBOT_RECORD_CONTROL_READY'


def apply_command(events, line):
    """
    解析来自标准输入的控制命令行并更新录制事件标志字典。

    Args:
        events (dict): LeRobot 录制控制事件字典（包含 exit_early, rerecord_episode, stop_recording）。
        line (str): 从标准输入读取的原始单行字符串。

    Returns:
        bool: True 表示成功匹配并应用了控制指令，False 表示忽略非控制行。
    """
    if not line.startswith(PREFIX):
        return False
    key = line[len(PREFIX):].strip()
    if key not in ('right', 'left', 'esc'):
        return False
    if not events['stop_recording']:
        if key == 'left':
            events['rerecord_episode'] = True
        elif key == 'esc':
            events['stop_recording'] = True
            events['rerecord_episode'] = False
        events['exit_early'] = True
    logging.info('[录制控制] 已接收 %s', key)
    return True


def install(recorder, *, stream=None):
    """
    向录制模块注入无头键盘监听器替换实现。

    Args:
        recorder: 目标录制模块（如 `lerobot.scripts.lerobot_record`）。
        stream (file-like, optional): 文本输入流，默认为 sys.stdin。

    Returns:
        callable: 用于通知后台监听线程退出的停止函数。
    """
    # 仅在明确启用环境变量标志时安装
    if os.environ.get('REBOT_WEB_RECORD_CONTROL') != '1':
        return lambda: None
    stop = threading.Event()
    stream = sys.stdin if stream is None else stream

    def keyboard_listener():
        """替代原生 pynput 的自定义后台线程监听器。"""
        events = dict(exit_early=False, rerecord_episode=False, stop_recording=False)

        def receive():
            """持续读取输入流的后台线程循环。"""
            while not stop.is_set():
                try:
                    line = stream.readline()
                except (OSError, ValueError):
                    break
                if stop.is_set():
                    return
                if not line:
                    # Web 父进程已退出或关闭管道：安全保存当前剧集并正常退出
                    events.update(stop_recording=True, exit_early=True)
                    return
                apply_command(events, line)

        threading.Thread(target=receive, name='record-web-controls', daemon=True).start()
        # 发送握手就绪标记供父进程捕获
        print(READY, flush=True)
        logging.info('[录制控制] 使用网页命令通道；相机画面由常驻监视显示，无需 DISPLAY/pynput')
        return None, events

    # 替换录制器内部工厂函数，并标记为 headless 模式
    recorder.init_keyboard_listener = keyboard_listener
    recorder.is_headless = lambda: True
    return stop.set
