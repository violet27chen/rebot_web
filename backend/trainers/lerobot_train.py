"""
LeRobot 官方训练脚本入口增强包装 (LeRobot Training Entrypoint Wrapper)
========================================================================

【模块背景与定位】
在原版 LeRobot 训练中，日志输出类 `MetricsTracker` 会为了美观将步数（steps）进行
紧凑四舍五入缩写（例如将 9800 步显示为 "10K"），这导致 Web 前端的实时 Loss 折线图
在横坐标上丢失精确位置，产生重复或台阶状绘制失真。

【核心设计】
本模块在拉起官方 `lerobot.scripts.lerobot_train` 前，通过 Monkey-Patch 拦截
`MetricsTracker.__str__` 方法，使用正则替换仅将开头的 `step:10K` 还原为未四舍五入的
精确数值 `step:9800`，保留官方其他所有 Loss、学习率、吞吐率输出原汁原味不变。
"""
import re


def exact_step_formatter(original):
    """
    高阶函数：包装 MetricsTracker 的字符串序列化方法，强制输出完整未缩写的精确步数。

    Args:
        original (callable): 原生的 `MetricsTracker.__str__` 方法。

    Returns:
        callable: 带有精确步数格式化能力的替换函数。
    """
    def render(tracker):
        # 仅替换行首的 step:xxx 字符串，将其替换为真实的整数 tracker.steps
        return re.sub(r'^step:\S+', 'step:' + str(tracker.steps), original(tracker), count=1)
    return render


def main():
    """主执行入口：注入精确步数格式化器并执行 LeRobot 官方训练脚本。"""
    from lerobot.utils.logging_utils import MetricsTracker
    MetricsTracker.__str__ = exact_step_formatter(MetricsTracker.__str__)
    from lerobot.scripts.lerobot_train import main as official_main
    official_main()


if __name__ == '__main__':
    main()
