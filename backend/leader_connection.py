"""
示教主臂 (Leader Arm) 安全连接、状态自检与新鲜反馈校验模块
===========================================================

【模块背景与定位】
在远程遥操作（Teleoperation）与轨迹示教录制中，操作员通过物理拨动“主臂（Leader）”
来实时驱动“从臂（Follower）”。若主臂总线出现丢包、读数延迟或因电气干扰出现瞬态跳变，
如果从臂跟随了“陈旧缓存角度”或“异常跳变角度”，将导致从臂瞬间剧烈甩动撞击，造成严重人身与设备事故。

【核心安全设计原则】
1. 严禁导入期进行串口 I/O：模块被 import 时不得产生任何总线交互，防止阻塞主进程启动。
2. 杜绝陈旧缓存（No Stale Cache）：严格要求来自物理总线的“新鲜”实测样本，绝不沿用超时旧值。
3. 严格近零滤波（Near-Zero Filtering）：适配 Rust 底层驱动的单精度浮点过滤逻辑（0.2_f32），
   正确区分总线断开与物理处于零位，防止上电电平跳变导致的误判。
4. 有界时效预算（Bounded Recovery Budget）：设定严格超时截止时间（deadline），超时立即熔断，
   坚决禁止无限等待导致上位机界面假死。
"""
import math
import struct
import time

# Rust 驱动中针对近零异常比较使用的是 0.2_f32 单精度浮点数 (= 0.20000000298023224)
# Python 中使用 struct 打包再解包获取完全一致的单精度比较基准值
SDK_ZERO_EPS = struct.unpack('<f', struct.pack('<f', 0.2))[0]


def read_fresh_feedback(bus, joints, port, *, timeout=0.25,
                        clock=time.monotonic, sleep=time.sleep, on_recovery=None):
    """
    带时效预算保护的主臂新鲜反馈采集函数；严格保留底层驱动的近零滤波保护。

    【设计背景】
    将 SDK 的丢包容忍阈值（miss threshold）设置为 1，以便在物理应答缺失时立即抛出异常，
    从而将“实际接收到的新鲜物理角度”与“超时后驱动保留的陈旧缓存值”严格区分开。
    同时，底层 SDK 中 `reliable=False` 不仅表示网络丢包，也表示上电期间的 A->0->B 零点保护滤波。

    Args:
        bus: 舵机总线通讯接口实例（如 Feetech 或自研主臂串口总线）。
        joints (dict): 关节名称到物理舵机 ID 的映射字典（例如 {'joint_1': 1, ...}）。
        port (str): 主臂串口设备路径（如 /dev/ttyUSB0）。
        timeout (float, optional): 本次读取允许的最大超时预算（秒），默认为 0.25s。
        clock (callable, optional): 单调时间戳获取函数，默认 time.monotonic。
        sleep (callable, optional): 延时函数，默认 time.sleep。
        on_recovery (callable, optional): 当初期部分关节缺失但随后在预算内恢复时的回调通知函数。

    Returns:
        dict: 各关节最新实测物理角度（单位：度），形如 {'joint_1': 12.5, ...}。

    Raises:
        ValueError: 当超时预算不是正有限数时抛出。
        RuntimeError: 当超时预算耗尽仍有舵机未回复新鲜有效数据时抛出，阻止从臂跟随未知状态。
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('主臂反馈时效预算必须为正数')
        
    # 步骤 1：开启严格反馈模式（将丢包重试容忍设为 1，确保不回读 stale 历史数据）
    if not getattr(bus, '_rebot_strict_feedback', False):
        bus.set_loss_threshold(1)
        bus._rebot_strict_feedback = True

    started = clock()
    deadline = started + timeout
    positions = {}
    last_error = ''

    def angle(sample, field, *, individual=False):
        """内部辅助函数：校验样本是否为有效合法的物理角度。"""
        if sample is None:
            return None
        try:
            value = float(getattr(sample, field))
            if not math.isfinite(value):
                return None
            # 若底层 SDK 明确标记样本可靠（reliable=True），直接采信
            if getattr(sample, 'reliable', False):
                return value
            # 只有单个查询带有严格丢包异常验证时，才证明硬件确实应答了。
            # 当底层 SDK 确认原始输入在零点死区内时，保留其滤波姿态；但绝不能将原始未初始化的 0 误判为真实姿态。
            if individual:
                raw = float(sample.raw_deg)
                if math.isfinite(raw) and abs(raw) <= SDK_ZERO_EPS:
                    return value
            return None
        except (TypeError, ValueError, AttributeError):
            return None

    # 步骤 2：第一轮尝试：通过单次同步监听接口（sync_monitor）批量拉取所有舵机状态
    try:
        monitors = bus.sync_monitor(list(joints.values()))
        for name, sid in joints.items():
            value = angle(monitors.get(sid), 'angle_deg')
            if value is not None:
                positions[name] = value
    except (OSError, RuntimeError) as exc:
        last_error = str(exc)

    missing_initially = [name for name in joints if name not in positions]

    # 步骤 3：若第一轮批量拉取有遗漏舵机，在截止时间预算内以有界速率针对性单查缺失舵机
    while clock() < deadline:
        if len(positions) == len(joints):
            break
        for name, sid in joints.items():
            if name in positions:
                continue
            if clock() >= deadline:
                break
            try:
                # read_angle 进行单关节严格查询，利用 filtered_deg 规避上电瞬态毛刺
                sample = bus.read_angle(sid)
                value = angle(sample, 'filtered_deg', individual=True)
                if value is not None:
                    positions[name] = value
                else:
                    last_error = (f'最近拒绝样本 {name}(ID {sid}): '
                                  f'raw={getattr(sample, "raw_deg", None)}, '
                                  f'filtered={getattr(sample, "filtered_deg", None)}, '
                                  f'reliable={getattr(sample, "reliable", None)}')
            except (OSError, RuntimeError) as exc:
                last_error = str(exc)

        # 适当微延时（<=5ms），防止高频密集查询压垮串口总线
        if len(positions) != len(joints) and clock() < deadline:
            sleep(min(0.005, max(0.0, deadline - clock())))

    elapsed = clock() - started

    # 步骤 4：严格验收校验：必须全量关节到位且在时间预算内
    if len(positions) != len(joints) or elapsed > timeout:
        missing = [f'{name}(ID {sid})' for name, sid in joints.items() if name not in positions]
        detail = '、'.join(missing) if missing else '反馈超过时效预算'
        raise RuntimeError(f'主臂 {port} 反馈未恢复：{detail}；耗时 {elapsed:.3f}s，'
                           f'预算 {timeout:.3f}s；未使用缓存角度。{last_error}')

    # 步骤 5：若存在初期丢包但重试成功的关节，触发回调上报恢复事件
    if missing_initially and on_recovery:
        on_recovery(missing_initially, elapsed)

    return positions


def fresh_positions(monitors, joints):
    """
    从批量同步监听结果（monitors）中提取所有关节的新鲜角度；任何缓存旧值均视为无效。

    Args:
        monitors (dict): 舵机 ID 到状态监视器对象的映射字典。
        joints (dict): 关节名称到舵机 ID 的映射字典。

    Returns:
        dict: 关节名到有效角度（度）的字典。

    Raises:
        RuntimeError: 当存在未响应或 reliable 标志为 False 的关节时抛出，坚决阻断跟随驱动。
    """
    positions, invalid = {}, []
    for name, servo_id in joints.items():
        monitor = monitors.get(servo_id)
        if monitor is None or not getattr(monitor, 'reliable', False):
            invalid.append(f'{name}(ID {servo_id}，无新反馈)')
            continue
        angle = float(monitor.angle_deg)
        if not math.isfinite(angle):
            invalid.append(f'{name}(ID {servo_id}，角度无效)')
            continue
        positions[name] = angle
    if invalid:
        raise RuntimeError('主臂角度读取失败：' + '、'.join(invalid))
    return positions


def read_action_positions(read, *, attempts=3, sleep=time.sleep):
    """
    带瞬态重试的主臂目标动作角度读取封装函数。

    【容错设计】
    容忍总线上单次的偶发电气干扰（最多重试 attempts 次，每次间隔 10ms）。
    若重试全部失败，立即主动终止，绝不重放旧角度，防止机械臂发生“跳帧瞬移”。

    Args:
        read (callable): 无参读取回调函数。
        attempts (int, optional): 最大尝试次数，默认 3 次。
        sleep (callable, optional): 延时函数。

    Returns:
        dict: 读取到的有效关节角度字典。

    Raises:
        RuntimeError: 当连续重试均失败时抛出。
    """
    error = None
    for attempt in range(attempts):
        try:
            return read()
        except (OSError, RuntimeError) as exc:
            error = exc
            if attempt < attempts - 1:
                sleep(0.01)
    raise RuntimeError('主臂连续反馈异常，已停止产生遥操作目标；未沿用旧角度。' + str(error)) from error


def verify_servos(bus, joints, port, baudrate, *, attempts=4, sleep=time.sleep):
    """
    主臂上电启动前的全面舵机在线寻址与 Ping 校验函数。

    【检测逻辑】
    新打开 USB 虚拟串口后，首先预留 150ms 供串口硬件电平稳定。
    逐个 Ping 指定的舵机 ID。若有任何一个舵机未响应，给出极其详尽的排查提示，
    并绝对禁止自动改写舵机 ID 或默认覆写零点，避免破坏原有标定。

    Args:
        bus: 舵机总线实例。
        joints (dict): 关节名到舵机 ID 字典。
        port (str): 串口路径。
        baudrate (int): 串口波特率（如 1000000）。
        attempts (int, optional): 单个舵机最大 Ping 尝试次数，默认 4 次。
        sleep (callable, optional): 延时函数。

    Raises:
        RuntimeError: 当任何一个舵机缺失应答时抛出。
    """
    # 步骤 1：让新打开的 USB 转串口桥接器电平稳定
    sleep(0.15)
    responding, missing = [], []

    # 步骤 2：遍历逐个 Ping 舵机
    for name, servo_id in joints.items():
        found = False
        for attempt in range(attempts):
            try:
                found = bool(bus.ping(servo_id))
            except (OSError, RuntimeError):
                found = False
            if found:
                break
            if attempt < attempts - 1:
                sleep(0.05)
        (responding if found else missing).append((name, servo_id))

    # 步骤 3：若有缺失，汇总详尽诊断信息并抛出异常
    if missing:
        absent = ', '.join(f'{name}(ID {sid})' for name, sid in missing)
        present = ', '.join(str(sid) for _, sid in responding) or '无'
        raise RuntimeError(f'主臂舵机检测失败：{port}，波特率 {baudrate}；响应 ID：{present}；'
                           f'未响应：{absent}（每个 ID 已尝试 {attempts} 次）。'
                           '请检查所选主臂串口、供电及舵机总线连接；未自动改写舵机 ID 或零点。')


def initial_positions(read, names, *, attempts=4, sleep=time.sleep):
    """
    在遥操作启动前读取主臂物理初始实测角度。

    【安全保护】
    确保所有关节角度均为有限合法实测值，坚决禁止使用默认的 0 度启动遥操作，
    防止从臂因初始角度偏差过大而发生突然急速回弹。

    Args:
        read (callable): 角度读取函数。
        names (Sequence[str]): 期望获取的全部关节名称列表。
        attempts (int, optional): 最大重试轮数，默认 4 次。
        sleep (callable, optional): 延时函数。

    Returns:
        dict: 主臂初始物理姿态角度字典。

    Raises:
        RuntimeError: 当无法获得完整的初始实测角度时抛出。
    """
    error = None
    for attempt in range(attempts):
        try:
            positions = read()
            if set(positions) != set(names) or not all(math.isfinite(float(value)) for value in positions.values()):
                raise RuntimeError('主臂启动实测角度不完整或包含无效值')
            return dict(positions)
        except (OSError, RuntimeError) as exc:
            error = exc
            if attempt < attempts - 1:
                sleep(0.05)
    raise RuntimeError('主臂没有完整的初始实测角度，未使用默认零值启动遥操作') from error
