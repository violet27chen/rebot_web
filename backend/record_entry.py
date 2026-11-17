"""
原生 LeRobot 录制定制化入口包装脚本 (Native LeRobot Recording Entrypoint)
========================================================================

【模块背景与定位】
本脚本作为 `lerobot-record` 的定制化 CLI 替代入口。
在保持官方原版驱动架构与安全清理逻辑（Native robot cleanup）完全不变的前提下，
在前置拦截期依次加载：
1. 语音静音补丁 (`record_audio`)：规避无头环境下桌面语音合成导致的卡顿；
2. 进度监控补丁 (`record_progress`)：提供分阶段（图片写盘、统计、视频编码）日志；
3. 无头控制补丁 (`record_controls`)：打通 Web 前端的标准输入无头控制通道。
"""
import sys
from pathlib import Path

# 将项目根目录添加至 Python 模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lerobot.datasets import lerobot_dataset
from lerobot.scripts import lerobot_record
from lerobot.scripts.lerobot_record import main
from backend.record_progress import install, install_loop_progress
from backend.record_audio import install as disable_speech
from backend.record_controls import install as install_controls

# 安装定制补丁
disable_speech(lerobot_record)
install(lerobot_dataset)
install_loop_progress(lerobot_record)
_close_controls = install_controls(lerobot_record)

if __name__ == '__main__':
    # 重构 sys.argv 满足官方 CLI 解析预期
    sys.argv = ['lerobot-record'] + sys.argv[1:]
    try:
        main()
    finally:
        # 退出清理无头控制线程
        _close_controls()
