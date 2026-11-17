"""Bounded, read-only checks for Leader startup. No serial I/O on import."""
import math
import struct
import time

SDK_ZERO_EPS = struct.unpack('<f', struct.pack('<f', .2))[0]

def read_fresh_feedback(bus, joints, port, *, timeout=.25,
                        clock=time.monotonic, sleep=time.sleep, on_recovery=None):
    """Collect transport-verified joint samples; preserve SDK near-zero filtering.

    Setting the SDK miss threshold to one makes a missing reply raise, so a
    returned angle sample can be distinguished from its timeout cache. The SDK's
    reliable=False ALSO denotes its A->0->B zero filter, not just packet loss.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('主臂反馈时效预算必须为正数')
    if not getattr(bus, '_rebot_strict_feedback', False):
        bus.set_loss_threshold(1)
        bus._rebot_strict_feedback = True
    started = clock()
    deadline = started + timeout
    positions = {}
    last_error = ''

    def angle(sample, field, *, individual=False):
        if sample is None:
            return None
        try:
            value = float(getattr(sample, field))
            if not math.isfinite(value):
                return None
            if getattr(sample, 'reliable', False):
                return value
            # Only individual queries with strict loss exceptions prove receipt.
            # Keep the SDK filtered pose while it confirms near-zero raw input;
            # never reinterpret raw zero as the actual joint pose.
            if individual:
                raw = float(sample.raw_deg)
                # Rust compares f32 against 0.2_f32 (=0.20000000298023224),
                # which is slightly larger than Python's double literal 0.2.
                if math.isfinite(raw) and abs(raw) <= SDK_ZERO_EPS:
                    return value
            return None
        except (TypeError, ValueError, AttributeError):
            return None

    try:
        monitors = bus.sync_monitor(list(joints.values()))
        for name, sid in joints.items():
            value = angle(monitors.get(sid), 'angle_deg')
            if value is not None:
                positions[name] = value
    except (OSError, RuntimeError) as exc:
        last_error = str(exc)
    missing_initially = [name for name in joints if name not in positions]
    # Query at a bounded rate until the time budget expires. A fixed round count
    # previously stopped after 88ms despite a declared 250ms recovery budget.
    while clock() < deadline:
        if len(positions) == len(joints):
            break
        for name, sid in joints.items():
            if name in positions:
                continue
            if clock() >= deadline:
                break
            try:
                # filtered_deg/reliable retain the SDK's power-cycle glitch protection.
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
        if len(positions) != len(joints) and clock() < deadline:
            sleep(min(.005, max(0., deadline - clock())))
    elapsed = clock() - started
    if len(positions) != len(joints) or elapsed > timeout:
        missing = [f'{name}(ID {sid})' for name, sid in joints.items() if name not in positions]
        detail = '、'.join(missing) if missing else '反馈超过时效预算'
        raise RuntimeError(f'主臂 {port} 反馈未恢复：{detail}；耗时 {elapsed:.3f}s，'
                           f'预算 {timeout:.3f}s；未使用缓存角度。{last_error}')
    if missing_initially and on_recovery:
        on_recovery(missing_initially, elapsed)
    return positions


def fresh_positions(monitors, joints):
    """A held SDK cache is not a new measurement and cannot drive followers."""
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
    """Retry transient bus loss, then stop normally instead of replaying old angles."""
    error = None
    for attempt in range(attempts):
        try:
            return read()
        except (OSError, RuntimeError) as exc:
            error = exc
            if attempt < attempts - 1:
                sleep(.01)
    raise RuntimeError('主臂连续反馈异常，已停止产生遥操作目标；未沿用旧角度。' + str(error)) from error


def verify_servos(bus, joints, port, baudrate, *, attempts=4, sleep=time.sleep):
    sleep(.15)  # Let a newly opened USB bridge settle before the first request.
    responding, missing = [], []
    for name, servo_id in joints.items():
        found = False
        for attempt in range(attempts):
            try:
                found = bool(bus.ping(servo_id))
            except (OSError, RuntimeError):
                found = False
            if found:
                break
            if attempt < attempts-1:
                sleep(.05)
        (responding if found else missing).append((name, servo_id))
    if missing:
        absent = ', '.join(f'{name}(ID {sid})' for name, sid in missing)
        present = ', '.join(str(sid) for _, sid in responding) or '无'
        raise RuntimeError(f'主臂舵机检测失败：{port}，波特率 {baudrate}；响应 ID：{present}；'
                           f'未响应：{absent}（每个 ID 已尝试 {attempts} 次）。'
                           '请检查所选主臂串口、供电及舵机总线连接；未自动改写舵机 ID 或零点。')


def initial_positions(read, names, *, attempts=4, sleep=time.sleep):
    error = None
    for attempt in range(attempts):
        try:
            positions = read()
            if set(positions) != set(names) or not all(math.isfinite(float(value)) for value in positions.values()):
                raise RuntimeError('主臂启动实测角度不完整或包含无效值')
            return dict(positions)
        except (OSError, RuntimeError) as exc:
            error = exc
            if attempt < attempts-1:
                sleep(.05)
    raise RuntimeError('主臂没有完整的初始实测角度，未使用默认零值启动遥操作') from error
