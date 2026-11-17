"""
异步动作块重叠对齐缓冲与物理安全斜率限幅器 (Async Action Buffer & Safety Limiter)
========================================================================================

【模块背景与定位】
在异步 ACT 策略推理中，前向模型预测的是未来的一个动作序列块（Action Chunk，例如接下来 50 步的目标角度）。
由于推理耗时动态波动，新预测出来的动作块与当前正在执行的动作块在时间轴上必然存在重叠区间。
如果盲目将新动作追加在末尾，会导致明显的动作断层与物理延迟；
如果直接覆盖当前动作，又会导致机械臂剧烈突变抽动。

【核心算法与安全机制】
1. `ActionBuffer`:
   - 基于时间滴答（Tick）进行绝对时间对齐，按执行时间戳将多步动作放入字典；
   - 重叠块渐变融合（Temporal Blending）：当新的一批动作块到达时，在重叠的前 `blend_steps` 步内，
     采用线性插值权重 alpha 对新旧动作进行平滑过渡（`previous * (1-alpha) + new * alpha`），
     彻底消除多块拼接处的加速度瞬变。
   - 过期块丢弃机制：若新到达的动作块由于通信延时其观测时间已经严重滞后（超过 `max_age`），直接拒收。
2. `limit_action`:
   - 在执行器真实物理电机角度空间进行安全约束校验；
   - 斜率限幅（Slew Rate Limiting）：限制单步最大变化量 `max_step`（默认 <= 2°），防止阶跃指令造成冲击；
   - 物理软限位截断（Motor Soft Limits）：依据真实硬件标定范围，超限立即提前拦截；
   - 跟踪误差过大安全熔断：若经限幅后的目标命令偏离实测角度超过跳跃预算（`max_jump`，默认 30°），
     判定机械臂遭到严重外力卡死或规划失控，立即抛出 `AsyncActionError` 触发系统安全停机。
"""
import math
import numpy as np


class AsyncActionError(RuntimeError):
    """异步动作预测超时、数据维度异常或安全校验超限时抛出的专用异常。"""
    pass


class ActionBuffer:
    """
    异步动作块时序对齐与多步平滑融合缓冲区。
    以离散 tick 为索引，支持按需提取和重叠融合。
    """

    def __init__(self, dimension, fps, max_age=1.5, blend_steps=5, preserve_existing=False):
        """
        初始化动作缓冲区。

        Args:
            dimension (int): 动作向量维度（对应控制关节数）。
            fps (float): 控制频率 (Hz)。
            max_age (float, optional): 动作块允许的最大有效时序延迟（秒），默认 1.5s。
            blend_steps (int, optional): 重叠融合步数，默认 5 步。
        """
        self.dimension = dimension
        self.fps = fps
        self.max_age = max_age
        self.blend_steps = blend_steps
        self.preserve_existing = preserve_existing
        self.reset()

    def reset(self):
        """清空缓冲区状态，递增代数序列号（generation）使旧异步响应自动失效。"""
        self.generation = getattr(self, 'generation', 0) + 1
        self.actions = {}
        self.last_tick = -1
        self.last_request = -1
        self.stale_chunks = 0

    def put(self, result, now, tick):
        """
        将推理子进程返回的新动作块放入缓冲区，并执行重叠渐变融合。

        Args:
            result (dict): 包含 actions, generation, request_id, start_tick, observed_at 等字段的推理响应字典。
            now (float): 当前单调时间。
            tick (int): 当前控制循环所在的逻辑 tick。

        Returns:
            bool: True 表示至少成功接收并融合了 1 步未来动作。
        """
        if result['generation'] != self.generation or result['request_id'] <= self.last_request:
            return False
        age = now - result['observed_at']
        if age < 0 or age > self.max_age:
            self.stale_chunks += 1
            return False

        values = np.asarray(result['actions'], dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != self.dimension or not np.isfinite(values).all():
            raise AsyncActionError('异步策略返回的动作维度或数值无效')

        first = max(tick, self.last_tick + 1)
        accepted = 0
        for offset, value in enumerate(values):
            position = result['start_tick'] + offset
            # 丢弃已经过去的历史时间步
            if position < first:
                continue
            previous = self.actions.get(position)
            if previous is not None and self.preserve_existing:
                # ACT has no explicit task phase. Replacing an unexecuted
                # suffix with a new prefix can indefinitely restart the
                # approach/hold phase. Finish the committed actions first.
                continue
            # 若该时间步已有上一轮预测的动作，执行线性平滑融合过渡
            if previous is not None:
                alpha = min(1.0, (position - first + 1) / max(1, self.blend_steps))
                value = previous * (1.0 - alpha) + value * alpha
            self.actions[position] = value.copy()
            accepted += 1

        self.last_request = result['request_id']
        return accepted > 0

    def take(self, tick):
        """
        取出并移除指定 tick 的动作向量，自动清理更早的历史缓存。

        Args:
            tick (int): 目标执行时间戳序号。

        Returns:
            np.ndarray or None: 动作向量，若缓冲为空则返回 None。
        """
        for position in list(self.actions):
            if position < tick:
                self.actions.pop(position)
        if tick <= self.last_tick:
            return None
        self.last_tick = tick
        return self.actions.pop(tick, None)

    def remaining(self, tick):
        """查询从当前 tick 开始未来连续可用的动作步数。"""
        count = 0
        while tick + count in self.actions:
            count += 1
        return count


def limit_action(target, measured, previous, directions, max_step=2.0, max_jump=30.0, *, joint_names=None, motor_limits=None, max_lead=None, diagnostics=None):
    """
    在执行器电机物理角度空间执行严格的工业级运动学安全限幅保护。

    【安全保护层级】
    1. 输入合法性检查：所有数值必须为有限浮点数，方向系数不能为 0；
    2. 电机软限位拦截（Motor Soft Limits）：检查目标与实测是否超出机器人的物理行程；
    3. 斜率限幅（Slew Rate Limiting）：限制单步变化量不超过 max_step（如 2.0°/步），
       ACT 输出的是目标长远角度，必须分步平滑逼近，防止电机阶跃冲击；
    4. 实时跟踪误差熔断：若当前规划输出与电机实测位置偏差超过 max_jump（如 30°），
       说明机械臂在运动过程中受阻、卡死或编码器严重跳动，立即拒绝动作并抛出异常。

    Args:
        target: 模型预测的目标动作数组。
        measured: 当前各关节实测角度数组。
        previous: 上一帧实际下发的目标角度数组。
        directions: 关节正反方向映射系数（包含夹爪速比）。
        max_step (float, optional): 单步最大允许角位移增量（电机度数），默认 2.0°。
        max_jump (float, optional): 允许的最大瞬态跟踪误差阈值（电机度数），默认 30.0°。
        joint_names (list[str], optional): 关节名称列表。
        motor_limits (list or np.ndarray, optional): 各关节最小/最大物理限位。

    Returns:
        np.ndarray: 经过严格限幅修剪后可安全发送给电机的动作数组 (np.float32)。

    Raises:
        AsyncActionError: 当输入无效、超出限位或跟踪误差过大时抛出。
    """
    target, measured, directions = [np.asarray(v, dtype=np.float64) for v in (target, measured, directions)]
    if (
        target.shape != measured.shape or
        target.shape != directions.shape or
        not all(np.isfinite(v).all() for v in (target, measured, directions)) or
        np.any(directions == 0)
    ):
        raise AsyncActionError('无法取得完整的有限关节反馈或方向映射，未执行策略动作')

    names = list(joint_names) if joint_names is not None else [f'joint[{i}]' for i in range(target.size)]
    if len(names) != target.size:
        raise AsyncActionError('关节名称与动作维度不一致')

    budgets = np.full(target.shape, float(max_jump))
    if not all(math.isfinite(v) and v > 0 for v in (max_step, max_jump)):
        raise AsyncActionError('动作限速/跟随保护参数无效')
    projected_targets = []
    if motor_limits is not None:
        bounds = np.asarray(motor_limits, dtype=np.float64)
        if bounds.shape != (target.size, 2) or not np.isfinite(bounds).all() or np.any(bounds[:, 0] >= bounds[:, 1]):
            raise AsyncActionError('物理关节限位配置无效')
        raw = target * directions
        clipped = np.clip(raw, bounds[:, 0], bounds[:, 1])
        measured_motor = measured * directions
        feedback_overflow = np.abs(measured_motor - np.clip(measured_motor, bounds[:, 0], bounds[:, 1]))
        if np.max(feedback_overflow) > 5:
            i = int(np.argmax(feedback_overflow))
            raise AsyncActionError(
                f'{names[i]} 实测反馈超出物理限位：实测 {measured_motor[i]:.2f}°，范围 {bounds[i].tolist()}，未继续推进'
            )
        # A learned goal is not a measured actuator position. Match the native
        # follower's projection, while rejecting corrections larger than the
        # existing 30-degree trust budget. Actual joint bounds stay unchanged.
        target_overflow = np.abs(raw - clipped)
        if np.max(target_overflow) > max_jump:
            i = int(np.argmax(target_overflow))
            raise AsyncActionError(
                f'{names[i]} 策略目标严重超出物理限位：目标 {raw[i]:.2f}°，范围 {bounds[i].tolist()}，'
                f'所需修正 {target_overflow[i]:.2f}° > {max_jump:g}°，已拒绝动作'
            )
        projected_targets = [dict(joint=names[i], raw_motor_deg=float(raw[i]),
                                  projected_motor_deg=float(clipped[i]), overshoot_motor_deg=float(target_overflow[i]))
                             for i in range(target.size) if target_overflow[i] > 1e-6]
        target = clipped / directions

    reference = measured if previous is None else np.asarray(previous, dtype=np.float64)
    if reference.shape != target.shape or not np.isfinite(reference).all():
        raise AsyncActionError('上一帧命令无效')

    # A feedback leash must not hide an already unsafe tracking error (for
    # example an external displacement or a stale/incorrect previous command).
    previous_error = np.abs((reference - measured) * directions)
    if max_lead is not None and np.any(previous_error > max_jump):
        i = int(np.argmax(previous_error))
        raise AsyncActionError(f'{names[i]} 上一命令已失跟：偏差 {previous_error[i]:.2f}° > {max_jump:g}°，未继续推进')

    # 第一步：斜率限幅（Slew Rate Limiting）
    step = np.clip((target - reference) * directions, -max_step, max_step)
    limited = False
    if max_lead is not None:
        if not math.isfinite(max_lead) or not 0 < max_lead < max_jump:
            raise AsyncActionError('命令领先量必须小于失跟保护阈值')
        offset = (reference - measured) * directions
        # Only slow advances away from feedback. A command retreating toward
        # feedback is allowed, still under the same per-frame slew limit.
        room = np.where(step >= 0, max_lead - offset, max_lead + offset)
        scale = np.ones_like(step)
        moving = np.abs(step) > 1e-12
        scale[moving] = np.clip(room[moving] / np.abs(step[moving]), 0., 1.)
        # Keep the arm joints on the same interpolation fraction. Gripper
        # contact is expected while holding a parcel: limit it independently,
        # so contact cannot freeze both arms indefinitely.
        arms = np.array([not name.removesuffix('.pos').endswith('gripper') for name in names])
        if arms.any():
            scale[arms] = np.min(scale[arms])
        limited = bool(np.any(scale < 1. - 1e-8))
        step *= scale
    result = reference + step / directions

    # 第二步：限位再次截断
    if motor_limits is not None:
        result = np.clip(result * directions, bounds[:, 0], bounds[:, 1]) / directions

    # 第三步：跟踪误差过大熔断检查
    errors = np.abs((result - measured) * directions)
    if np.any(errors > budgets):
        i = int(np.argmax(errors / budgets))
        raise AsyncActionError(
            f'{names[i]} 实际待发送命令偏离实测：本帧 {result[i] * directions[i]:.2f}°，实测 {measured[i] * directions[i]:.2f}°，'
            f'偏差 {errors[i]:.2f}° > {budgets[i]:g}°，ACT原目标 {target[i] * directions[i]:.2f}°（电机角度），已拒绝动作'
        )

    if diagnostics is not None:
        diagnostics.update(lead_limited=limited, following_error_deg=float(np.max(errors)),
                           projected_targets=projected_targets)
    return result.astype(np.float32)


class TargetProjectionWatchdog:
    """Do not hide a model persistently requesting out-of-range joint goals."""

    def __init__(self, timeout_s=2., significant_deg=5.):
        self.timeout_s, self.significant_deg = timeout_s, significant_deg
        self.reset()

    def reset(self):
        self.since = {}

    def update(self, targets, now):
        significant = {item['joint']: item for item in targets
                       if item['overshoot_motor_deg'] > self.significant_deg}
        self.since = {joint: stamp for joint, stamp in self.since.items() if joint in significant}
        for joint, item in significant.items():
            stamp = self.since.setdefault(joint, now)
            if now - stamp >= self.timeout_s:
                raise AsyncActionError(
                    f'{joint} 模型持续预测越界目标：{item["raw_motor_deg"]:.2f}°，'
                    f'已限幅至 {item["projected_motor_deg"]:.2f}°；持续 {self.timeout_s:g}s，'
                    '请检查模型与关节契约，未继续执行')


class FeedbackProgressWatchdog:
    """Stop an unresponsive arm; bounded setpoints must not conceal a stall."""

    def __init__(self, lead_deg=6., timeout_s=2., progress_deg=.5):
        self.lead_deg, self.timeout_s, self.progress_deg = lead_deg, timeout_s, progress_deg
        self.reset()

    def reset(self):
        self.tracking = {}

    def update(self, command, measured, directions, names, now):
        motor = np.asarray(measured, np.float64) * directions
        error = (np.asarray(command, np.float64) - measured) * directions
        for index, name in enumerate(names):
            if name.removesuffix('.pos').endswith('gripper'):
                continue  # Force-limited gripper contact is not an arm stall.
            direction = float(np.sign(error[index]))
            if abs(error[index]) < self.lead_deg * .8:
                self.tracking.pop(name, None)
                continue
            previous = self.tracking.get(name)
            if previous is None or previous[2] != direction:
                self.tracking[name] = (float(motor[index]), now, direction)
                continue
            anchor, stamp, _ = previous
            if direction * (motor[index] - anchor) >= self.progress_deg:
                self.tracking[name] = (float(motor[index]), now, direction)
            elif now - stamp >= self.timeout_s:
                raise AsyncActionError(
                    f'{name} 跟随停滞：命令领先 {abs(error[index]):.2f}°，'
                    f'{self.timeout_s:g}s 内向目标移动不足 {self.progress_deg:g}°；已停止继续推进')
