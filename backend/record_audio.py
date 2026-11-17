"""
无头环境录制语音播报静音与日志重定向补丁 (Headless Record Speech Bypass)
========================================================================

【模块背景与定位】
在原版 LeRobot 数据集录制脚本中，每当剧集开始、倒计时或录制结束时，
系统默认尝试调用桌面语音合成引擎（如 espeak、say 或 gTTS）进行语音提示。
在工业无头（Headless）边缘计算设备、Docker 容器或无物理音频声卡输出的环境中，
调用语音合成接口会因为音频设备不可用或 D-Bus 服务无响应而导致整个录制进程
发生数秒至数十秒的严重卡顿，甚至导致录制控制线程阻塞。

【核心设计】
本模块提供简洁的 `install` 函数，将录制器中的 `log_say` 方法彻底劫持替换为
纯 Python 标准日志输出 (`logging.info`)，彻底剥离对任何桌面音频服务的依赖，
确保录制进程平稳、确定性地执行。
"""
import logging


def install(recorder):
    """
    拦截并替换指定录制器模块内的语音播报函数为纯日志记录。

    Args:
        recorder: 目标录制模块（如 `lerobot.scripts.lerobot_record`）。
    """
    def log_only(text, play_sounds=True, blocking=False):
        """丢弃音频播放指令，仅将提示文本输出至标准日志。"""
        logging.info(text)

    # 覆写录制器内部的语音方法
    recorder.log_say = log_only
