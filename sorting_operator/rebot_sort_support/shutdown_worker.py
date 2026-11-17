"""Verified slow homing, extracted without its platform CLI dependencies."""
import math
import time
from .motor_mode import ensure_mode_checked
from .homing_profile import trajectory, HOME_SPEED_RAD_S

def home_arm(bus, motors, *, position_mode, force_mode, ratio=.12,
             speed=HOME_SPEED_RAD_S, sleep=time.sleep, clock=time.monotonic, log=print, publish=None, sync=None):
    """Use raw motor radians and the follower's established two-stage order."""
    if set(motors) != set(range(1, 8)):
        raise ValueError('收工要求完整的六轴与夹爪反馈（CAN ID 1～7）')
    if not .01 <= ratio <= 1 or not 0 < speed <= .3:
        raise ValueError('无效的归零速度或夹爪力矩比例')

    def read():
        if sync: sync.check()
        for motor in motors.values():
            motor.request_feedback()
        sleep(.015)
        bus.poll_feedback_once()
        states = {i: motor.get_state() for i, motor in motors.items()}
        for i, state in states.items():
            if state is None:
                raise RuntimeError(f'电机 {i} 没有实测反馈，未确认归零，不失能')
            if not all(math.isfinite(float(getattr(state, k))) for k in ('pos','vel','torq','t_mos','t_rotor')):
                raise RuntimeError(f'电机 {i} 反馈包含无效数值')
            if max(state.t_mos, state.t_rotor) > 80:
                raise RuntimeError(f'电机 {i} 温度超过 80°C，已中止收工')
            if state.status_code not in (0, 1):
                raise RuntimeError(f'电机 {i} 故障状态 {state.status_code}，已中止收工')
        if publish:
            publish([states[i].pos for i in range(1,7)], [states[i].torq for i in range(1,7)])
        return states

    def live_modes():
        # Motorbridge 0.5 exposes cached state without timestamps. RID10 uses a
        # fresh register transaction, so a silent bus cannot pass final checks.
        for i, motor in motors.items():
            expected = int(force_mode if i == 7 else position_mode)
            if hasattr(motor, "get_register_u32"):
                actual = motor.get_register_u32(10, timeout_ms=1500)
            elif hasattr(motor, "damiao_get_param_u32"):
                actual = motor.damiao_get_param_u32(10)
            elif hasattr(motor, "damiao_read_param_u32"):
                actual = motor.damiao_read_param_u32(10)
            else:
                raise AttributeError(f"电机 {i} 对象无模式回读方法: {dir(motor)}")
            if actual != expected:
                raise RuntimeError(f'电机 {i} 的控制模式回读不一致，未失能')

    def send(target):
        if sync: sync.check()
        for i, motor in motors.items():
            if i == 7:
                motor.send_force_pos(target[i], speed, ratio)
            else:
                motor.send_pos_vel(target[i], speed)

    states = read()
    if any(abs(s.pos) > 4*math.pi for s in states.values()):
        raise RuntimeError('实测角度超出收工范围，请检查校准与多圈偏移；未改写零点')
    for i, motor in motors.items():
        ensure_mode_checked(motor, force_mode if i == 7 else position_mode, sleep=sleep)
    live_modes()
    if sync: sync.checkpoint()
    # Seed every target from measurement, never from a default zero vector.
    states = read()
    target = {i: state.pos for i, state in states.items()}
    send(target)
    for motor in motors.values():
        if sync: sync.check()
        motor.enable()

    for label, active in [('底座与腕部', (1,4,5,6)), ('肩部与肘部', (2,3)), ('限力夹爪', (7,))]:
        log(f'[收工] {label}低速归零', flush=True)
        states = read()
        # Every stage starts from fresh measured values, including held joints.
        target = {i: state.pos for i,state in states.items()}
        start = [target[i] for i in active]
        previous = dict(target)
        def hold():
            read(); send(target)
        duration=sync.movement_duration(start,[0.]*len(active),speed=speed,hold=hold) if sync else 2.
        for interval, point in trajectory(start,[0.]*len(active),speed=speed,hz=30,min_duration=duration):
            states=read()
            if any(abs(states[i].pos-previous[i])>.10 for i in active):
                send({i:state.pos for i,state in states.items()})
                raise RuntimeError(f'{label}归零未到位，跟踪误差超过 0.10 rad；已停止继续收回')
            for i,value in zip(active,point):target[i]=value
            send(target);previous=dict(target);sleep(interval)
        settled=0; deadline=clock()+6
        while clock() < deadline:
            states=read();send(target)
            settled = settled+1 if all(abs(states[i].pos)<.035 and abs(states[i].vel)<.08 for i in active) else 0
            if settled >= 8:
                break
            sleep(1/30)
        else:
            raise RuntimeError(f'{label}归零未到位（可能受阻或夹有物品），未失能，请处理后重试')
        if sync:
            sync.checkpoint(hold)

    live_modes()
    states=read()
    if any(abs(s.pos)>=.035 or abs(s.vel)>=.08 for s in states.values()):
        raise RuntimeError('最终零位反馈未通过，未失能')
    if sync: sync.checkpoint(lambda: (read(), send(target)))
    log('[收工] 七个电机已确认到位，正在失能', flush=True)
    deadline=clock()+3
    while clock()<deadline:
        if sync: sync.check()
        for motor in motors.values():
            motor.disable()
        states=read()
        if all(s.status_code == 0 for s in states.values()):
            log('[收工] 归零及失能反馈已确认', flush=True)
            return
        sleep(.05)
    raise RuntimeError('已发送失能，但没有确认全部电机失能反馈；请检查设备状态')
