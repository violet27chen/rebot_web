"""Explicit, isolated B601-DM homing. Never sets encoder/calibration zero.

The transport is only opened by main(); importing this module is hardware free.
Position feedback and mode readback must succeed before movement and disabling.
Failure releases the transport without a blind disable or a second homing attempt.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import signal
import sys
import time
try:
    from .motor_mode import ensure_mode_checked
    from .homing_profile import trajectory, HOME_SPEED_RAD_S
except ImportError:
    from motor_mode import ensure_mode_checked
    from homing_profile import trajectory, HOME_SPEED_RAD_S


def home_arm(bus, motors, *, position_mode, force_mode, ratio=.12,
             speed=HOME_SPEED_RAD_S, sleep=time.sleep, clock=time.monotonic, log=print, publish=None):
    """Use raw motor radians and the follower's established two-stage order."""
    if set(motors) != set(range(1, 8)):
        raise ValueError('收工要求完整的六轴与夹爪反馈（CAN ID 1～7）')
    if not .01 <= ratio <= 1 or not 0 < speed <= .3:
        raise ValueError('无效的归零速度或夹爪力矩比例')

    def read():
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
            if motor.damiao_read_param_u32(10) != expected:
                raise RuntimeError(f'电机 {i} 的控制模式回读不一致，未失能')

    def send(target):
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
    # Seed every target from measurement, never from a default zero vector.
    states = read()
    target = {i: state.pos for i, state in states.items()}
    send(target)
    for motor in motors.values():
        motor.enable()

    for label, active in [('底座与腕部', (1,4,5,6)), ('肩部与肘部', (2,3)), ('限力夹爪', (7,))]:
        log(f'[收工] {label}低速归零', flush=True)
        states = read()
        # Every stage starts from fresh measured values, including held joints.
        target = {i: state.pos for i,state in states.items()}
        start = [target[i] for i in active]
        previous = dict(target)
        for interval, point in trajectory(start,[0.]*len(active),speed=speed,hz=30):
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

    live_modes()
    states=read()
    if any(abs(s.pos)>=.035 or abs(s.vel)>=.08 for s in states.values()):
        raise RuntimeError('最终零位反馈未通过，未失能')
    log('[收工] 七个电机已确认到位，正在失能', flush=True)
    deadline=clock()+3
    while clock()<deadline:
        for motor in motors.values():
            motor.disable()
        states=read()
        if all(s.status_code == 0 for s in states.values()):
            log('[收工] 归零及失能反馈已确认', flush=True)
            return
        sleep(.05)
    raise RuntimeError('已发送失能，但没有确认全部电机失能反馈；请检查设备状态')


def main():
    from motorbridge import Controller, Mode
    from telemetry_client import FeedbackPublisher
    import yaml
    payload=json.loads(sys.argv[1])
    hardware=yaml.safe_load(Path(__file__).resolve().parents[1].joinpath(
        'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml').read_text())
    stopped=False
    def interrupt(*_):
        nonlocal stopped
        stopped=True
        raise InterruptedError('收工已停止；未执行盲目失能')
    signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
    sessions=[]
    publisher=FeedbackPublisher()
    try:
        # Open and verify all selected ports before moving either follower.
        for arm in payload['arms']:
            bus=Controller.from_dm_serial(arm['port'],921600)
            motors={};sessions.append((arm,bus,motors))
            for joint in hardware['joints']:
                motors[int(joint['motor_id'])]=bus.add_damiao_motor(
                    joint['motor_id'],joint['feedback_id'],joint['model'])
            for motor in motors.values():motor.request_feedback()
            time.sleep(.03);bus.poll_feedback_once()
            if len(motors)!=7 or any(m.get_state() is None for m in motors.values()):
                raise RuntimeError(f"{arm['side']} 反馈不完整，未开始归零")
        for arm,bus,motors in sessions:
            if stopped:raise InterruptedError('收工已停止')
            print(f"[收工] {arm['side']} · {arm['port']}",flush=True)
            home_arm(bus,motors,position_mode=Mode.POS_VEL,force_mode=Mode.FORCE_POS,
                     ratio=arm['gripper_torque_ratio'],
                     publish=lambda q,t:publisher.send(arm['port'],q,t,gripper=motors[7].get_state().pos))
        print('[收工] 所有从臂归零、失能完成，正在释放串口；可重新连接启动',flush=True)
    except Exception as error:
        print(f'[收工失败] {error}',flush=True)
        return 1
    finally:
        # No disconnect() helper that might home again or disable a failed arm.
        for _,bus,motors in sessions:
            for motor in motors.values():
                try:motor.close()
                except Exception:pass
            try:bus.close()
            except Exception:pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
