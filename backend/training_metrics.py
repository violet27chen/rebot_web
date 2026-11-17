"""
训练终端日志数值指标提取与单位无损转换器 (Training Metrics Text Parser)
========================================================================

【模块背景与定位】
在监控各类训练引擎（PyTorch/LeRobot、YOLO、OpenCV）的终端标准输出时，
训练日志的格式各异（例如 `train_loss: 0.045, step: 20K` 或 `loss=1.23, epoch: 5`）。
为了在前端动态绘制实时的 Loss 曲线与监控图表，需要一套健壮的正则解析机制。

【核心特性】
1. 支持科学计数法（`1.2e-4`）与千分位逗号（`1,000`）；
2. 国际化单位缩写支持：精准解析并按 10 的乘方无损还原 K (10^3), M (10^6), B (10^9), T (10^12) 步数；
3. 严格边界验证：过滤 NaN、Inf 与布尔值，防止脏数据击穿图表组件。
"""
import math
import re

# 正则表达式匹配浮点数（支持正负号、小数点、千分位逗号、科学计数法）
NUMBER = r'[+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
# 匹配 Loss 字段正则
LOSS_RE = re.compile(r'\b(?:train_loss|loss)\s*[=:]?\s*(' + NUMBER + r')(?![\w.])', re.I)
# 匹配步数/轮次字段正则（附带单位后缀 K, M, B, T 等）
STEP_RE = re.compile(r'\b(?:global_step|iterations?|steps?|epochs?)\s*[=:]?\s*(' + NUMBER + r')([KMBTQ]?)(?![\w.])', re.I)

# 数量级乘数映射表
MULTIPLIER = {
    '': 1,
    'K': 10**3,
    'M': 10**6,
    'B': 10**9,
    'T': 10**12,
    'Q': 10**15
}


def numeric_step(value):
    """
    将带单位的步数字符串（如 '10K', '1.5M', '500'）精确转换为标准 Python 整数。

    Args:
        value (str or int): 输入的步数表示。

    Returns:
        int: 转换后的整数步数。

    Raises:
        ValueError: 输入为布尔值、包含非法字符、转换结果非有限数或非整数时抛出。
    """
    if isinstance(value, bool):
        raise ValueError('boolean is not a training step')
    match = re.fullmatch(r'(' + NUMBER + r')([KMBTQ]?)', str(value).strip(), re.I)
    if not match:
        raise ValueError('invalid training step')
    result = float(match[1].replace(',', '')) * MULTIPLIER[match[2].upper()]
    if not math.isfinite(result) or result < 0 or not result.is_integer():
        raise ValueError('invalid training step')
    return int(result)


def parse_metric(line):
    """
    从单行训练日志文本中提取当前的 loss 浮点值和对应的 step 步数。

    Args:
        line (str): 终端输出文本行。

    Returns:
        dict or None: 若匹配成功返回 {'value': float, 'step': int or None}，否则返回 None。
    """
    loss = LOSS_RE.search(line)
    if not loss:
        return None
    value = float(loss[1].replace(',', ''))
    if not math.isfinite(value):
        return None
    step = STEP_RE.search(line)
    return {
        'value': value,
        'step': numeric_step(step[1] + step[2]) if step else None
    }
