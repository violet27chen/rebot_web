"""
无硬件依赖、有界速度与加速度的最小加加速度（Minimum-Jerk）平滑归零轨迹生成器
================================================================================

【模块背景与定位】
在机械臂复位（Homing / 回零）过程中，如果机械臂以过快速度或阶跃式加速度突然动作，
会导致极大的机械冲击、震动甚至损坏减速器齿轮，还会产生严重的工伤风险。
因此，必须采用严格限制最大角速度（Velocity Bounded）、最大角加速度（Acceleration Bounded）
并具备 C2 阶连续性（加加速度有界 / Minimum-Jerk）的五次多项式平滑轨迹规划算法。

【核心设计原则】
1. 无硬件依赖（Hardware-Free）：纯数学与运动学轨迹计算，无任何硬件驱动依赖，可安全被
   独立厂商驱动（vendor drivers）、CLI 工具、仿真器以及上位机服务复用。
2. 严格保界：根据起始姿态与目标姿态之间的最大关节角差，严格由预设的最高角速度与最高加速度
   计算所需的最小运行时间，且绝不低于最小安全时长（min_duration）。
3. 确定性采样：按固定控制频率（如 50Hz）逐帧产生插值点，绝不跳步，绝不因延时抖动突变到终点。
"""
import math

# 默认全局安全运动学边界常量
HOME_SPEED_RAD_S = 0.20      # 默认回零最高关节角速度 (rad/s)，约 11.5 deg/s
HOME_ACCEL_RAD_S2 = 0.25     # 默认回零最高关节角加速度 (rad/s²)，保持平滑起停
HOME_HZ = 50.0               # 默认控制循环离散采样频率 (50Hz，每步 20ms)


def trajectory(start, target, *, speed=HOME_SPEED_RAD_S, acceleration=HOME_ACCEL_RAD_S2, hz=HOME_HZ, min_duration=2.0):
    """
    基于五次多项式（Minimum-Jerk）生成满足速度与加速度限制的多关节平滑回零插值轨迹。

    【数学原理】
    使用标准五次平滑插值多项式（C2连续，位置、速度、加速度在端点均为0）：
        blend(s) = 10 * s^3 - 15 * s^4 + 6 * s^5, 其中归一化时间 s = t / duration in [0, 1]
    导数分析：
        - 速度项一阶导峰值: max(d(blend)/ds) = 15/8 = 1.875
        - 加速度二阶导峰值: max(d²(blend)/ds²) = 10 / sqrt(3) ≈ 5.7735
    因此，对于最大关节位移 delta = max(|target_i - start_i|)：
        - 由速度限制得出的时间需求: T_vel = 1.875 * delta / speed
        - 由加速度限制得出的时间需求: T_acc = sqrt( (10 / sqrt(3)) * delta / acceleration )
        - 实际总耗时: duration = max(min_duration, T_vel, T_acc)

    Args:
        start (Sequence[float]): 各关节起始实测角度列表或元组 (单位: rad)。
        target (Sequence[float]): 各关节目标回零角度列表或元组 (单位: rad)。
        speed (float, optional): 最大允许角速度 (rad/s)，必须在 (0, 0.3] 之间。
        acceleration (float, optional): 最大允许角加速度 (rad/s²)，必须在 (0, 0.4] 之间。
        hz (float, optional): 轨迹输出采样频率 (Hz)，必须在 [10, 100] 之间。
        min_duration (float, optional): 轨迹最短执行耗时 (秒)，默认 2.0s，防止极短距离剧烈抖动。

    Yields:
        Tuple[float, Tuple[float, ...]]: 每一帧生成 (interval, joint_positions)，其中
            - interval: 该帧与前一帧之间的理论推荐休眠间隔 (秒)；
            - joint_positions: 该时刻各关节的目标插值角度元组 (rad)。

    Raises:
        ValueError: 当输入的关节维度不匹配、包含非法浮点数（NaN/Inf）或运动学参数超出安全范围时抛出。
    """
    # 步骤 1：严格校验关节角度序列的完整性与数值有效性
    start = tuple(float(v) for v in start)
    target = tuple(float(v) for v in target)
    if len(start) != len(target) or not start or not all(math.isfinite(v) for v in start + target):
        raise ValueError('归零需要完整的有限实测角度')

    # 步骤 2：校验运动学约束参数的物理合理性（严格限定上限，杜绝配置危险高速）
    if not all(math.isfinite(v) for v in (speed, acceleration, hz, min_duration)) or \
       not (0 < speed <= 0.3) or not (0 < acceleration <= 0.4) or not (10 <= hz <= 100) or min_duration < 0:
        raise ValueError('归零速度、加速度或采样频率无效')

    # 步骤 3：计算各关节中最大的运动位移（弧度绝对值）
    delta = max(abs(b - a) for a, b in zip(start, target))

    # 步骤 4：基于 Minimum-Jerk 导数峰值理论推导满足速度与加速度边界所需的最短规划时长
    # Peak first derivative = 1.875, Peak second derivative = 10 / sqrt(3)
    duration = max(min_duration, 1.875 * delta / speed, math.sqrt((10 / math.sqrt(3)) * delta / acceleration))

    # 步骤 5：按规划频率计算总采样步数与单步时间步长
    steps = max(1, math.ceil(duration * hz))
    interval = duration / steps

    # 步骤 6：按步迭代生成各关节平滑插值角度
    for step in range(steps + 1):
        s = step / steps
        # 五次多项式展开形式: 10s^3 - 15s^4 + 6s^5
        blend = s * s * s * (10 + s * (-15 + 6 * s))
        # 对每个关节进行同相平滑插值
        yield interval, tuple(a + (b - a) * blend for a, b in zip(start, target))
