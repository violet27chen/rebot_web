"""
B601-DM 机械臂硬件显式安全归零与失能 Worker 进程 (Arm Safe Shutdown Worker)
=============================================================================

【模块背景与核心安全原则】
机械臂工作结束或下班“收工”时，若直接切断电机使能（Disable），
机械臂会在重力作用下从空中猛烈砸落在工作台面上，导致减速器齿轮打齿、工件砸碎甚至造成工伤事故。
本模块作为独立的子进程执行机械臂的受控分阶段慢速归零与平稳安全失能：
1. 严禁改写零点（Never Alter Zero Calibration）：
   本过程只根据预先标定好的原点执行复位，绝不发送 set_zero 覆写电机编码器或参数。
2. 双重确认模式回读：
   在真正开始平滑运动和失能前，必须先回读各电机的 RID 10（模式寄存器），
   确认 1~6 轴处于 POS_VEL（位置速度模式），7 轴夹爪处于 FORCE_POS（限力位置模式），
   绝不盲目在错误模式下驱动电机。
3. 科学分阶段顺序收回（Staged Order）：
   - 阶段一：底座与腕部（1, 4, 5, 6 轴）低速收回，降低整臂惯量；
   - 阶段二：肩部与肘部（2, 3 轴）低速收回，降落大臂；
   - 阶段三：限力夹爪（7 轴）以微弱力矩比例（默认 12% 额定力矩）贴向归零位。
4. 真实物理到位验证与失能确认：
   每个阶段均基于 Minimum-Jerk 轨迹插补，并检查跟踪误差（偏差不得超过 0.10 rad）。
   最终零位必须连续 8 次采样满足物理静止（|pos| < 0.035 rad, |vel| < 0.08 rad/s）后，
   才下发 disable()，随后必须回读确认 status_code == 0。
5. 协同双臂并行收工：通过 `parallel_shutdown` 与 `ShutdownGroup` 保持双臂步调一致。
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
    from .coordinated_shutdown import parallel_shutdown
except ImportError:
    from motor_mode import ensure_mode_checked
    from homing_profile import trajectory, HOME_SPEED_RAD_S
    from coordinated_shutdown import parallel_shutdown


def home_arm(bus, motors, *, position_mode, force_mode, ratio=0.12,
             speed=HOME_SPEED_RAD_S, sleep=time.sleep, clock=time.monotonic, log=print, publish=None, sync=None):
    """
    对单台机械臂执行基于电机原始弧度的两阶段平滑归零与失能验证。

    Args:
        bus: 达妙总线控制器实例。
        motors (dict): 电机 ID (1~7) 到驱动对象的字典。
        position_mode: 手臂关节预期的位置速度控制模式枚举。
        force_mode: 夹爪预期的力位混合控制模式枚举。
        ratio (float, optional): 夹爪收回时的力矩限制比例（0.01~1.0），默认 0.12。
        speed (float, optional): 归零角速度上限 (rad/s)，默认 0.15 rad/s。
        sleep (callable, optional): 延时函数。
        clock (callable, optional): 单调时钟。
        log (callable, optional): 日志回调。
        publish (callable, optional): 遥测广播回调。
        sync (ShutdownGroup, optional): 双臂协同同步屏障句柄。

    Raises:
        ValueError: 关节不全或速度比例非法。
        RuntimeError: 反馈缺失、温度超标、模式不符、跟踪超差或到位校验未通过。
    """
    if set(motors) != set(range(1, 8)):
        raise ValueError('收工要求完整的六轴与夹爪反馈（CAN ID 1～7）')
    if not (0.01 <= ratio <= 1.0) or not (0 < speed <= 0.3):
        raise ValueError('无效的归零速度或夹爪力矩比例')

    def read():
        """从 CAN 总线主动轮询读取全部 7 个关节的物理状态并校验合法性与温度。"""
        if sync:
            sync.check()
        for motor in motors.values():
            motor.request_feedback()
        sleep(0.015)
        bus.poll_feedback_once()
        states = {i: motor.get_state() for i, motor in motors.items()}
        for i, state in states.items():
            if state is None:
                raise RuntimeError(f'电机 {i} 没有实测反馈，未确认归零，不失能')
            if not all(math.isfinite(float(getattr(state, k))) for k in ('pos', 'vel', 'torq', 't_mos', 't_rotor')):
                raise RuntimeError(f'电机 {i} 反馈包含无效数值')
            # 严格电机温度保护：超过 80°C 中止收工
            if max(state.t_mos, state.t_rotor) > 80:
                raise RuntimeError(f'电机 {i} 温度超过 80°C，已中止收工')
            if state.status_code not in (0, 1):
                raise RuntimeError(f'电机 {i} 故障状态 {state.status_code}，已中止收工')
        if publish:
            publish([states[i].pos for i in range(1, 7)], [states[i].torq for i in range(1, 7)])
        return states

    def live_modes():
        """向电机硬件直接发起 RID 10 寄存器事务回读，严格核实当前控制模式。"""
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
        """向下位机电机下发目标位置与受限速度/力矩命令。"""
        if sync:
            sync.check()
        for i, motor in motors.items():
            if i == 7:
                motor.send_force_pos(target[i], speed, ratio)
            else:
                motor.send_pos_vel(target[i], speed)

    # 步骤 1：初次状态读取与多圈偏移异常拦截
    states = read()
    if any(abs(s.pos) > 4 * math.pi for s in states.values()):
        raise RuntimeError('实测角度超出收工范围，请检查校准与多圈偏移；未改写零点')

    # 步骤 2：校验并确保控制模式正确设置
    for i, motor in motors.items():
        ensure_mode_checked(motor, force_mode if i == 7 else position_mode, sleep=sleep)
    live_modes()

    if sync:
        sync.checkpoint()

    # 步骤 3：以当前真实实测姿态作为初始目标使能电机，绝不以全零作为使能初始点
    states = read()
    target = {i: state.pos for i, state in states.items()}
    send(target)
    for motor in motors.values():
        if sync:
            sync.check()
        motor.enable()

    # 步骤 4：分阶段平滑插值收工序列
    stages = [
        ('底座与腕部', (1, 4, 5, 6)),
        ('肩部与肘部', (2, 3)),
        ('限力夹爪', (7,))
    ]

    for label, active in stages:
        log(f'[收工] {label}低速归零', flush=True)
        states = read()
        target = {i: state.pos for i, state in states.items()}
        start = [target[i] for i in active]
        previous = dict(target)

        def hold():
            read()
            send(target)

        # 若处于双臂协同模式，对齐两臂所需的较长时间
        duration = sync.movement_duration(start, [0.0] * len(active), speed=speed, hold=hold) if sync else 2.0

        # 五次多项式 Minimum-Jerk 轨迹插补
        for interval, point in trajectory(start, [0.0] * len(active), speed=speed, hz=30, min_duration=duration):
            states = read()
            # 严格跟踪误差保护：若实际物理位置滞后超过 0.10 rad，立即停止并报错
            if any(abs(states[i].pos - previous[i]) > 0.10 for i in active):
                send({i: state.pos for i, state in states.items()})
                raise RuntimeError(f'{label}归零未到位，跟踪误差超过 0.10 rad；已停止继续收回')
            for i, value in zip(active, point):
                target[i] = value
            send(target)
            previous = dict(target)
            sleep(interval)

        # 连续 8 轮采样检验物理到位且完全静止
        settled = 0
        deadline = clock() + 6
        while clock() < deadline:
            states = read()
            send(target)
            settled = settled + 1 if all(abs(states[i].pos) < 0.035 and abs(states[i].vel) < 0.08 for i in active) else 0
            if settled >= 8:
                break
            sleep(1 / 30)
        else:
            raise RuntimeError(f'{label}归零未到位（可能受阻或夹有物品），未失能，请处理后重试')

        if sync:
            sync.checkpoint(hold)

    # 步骤 5：最终全量 7 轴到位确认
    live_modes()
    states = read()
    if any(abs(s.pos) >= 0.035 or abs(s.vel) >= 0.08 for s in states.values()):
        raise RuntimeError('最终零位反馈未通过，未失能')

    if sync:
        sync.checkpoint(lambda: (read(), send(target)))

    log('[收工] 七个电机已确认到位，正在失能', flush=True)

    # 步骤 6：发送失能并确认全部电机状态码为 0
    deadline = clock() + 3
    while clock() < deadline:
        if sync:
            sync.check()
        for motor in motors.values():
            motor.disable()
        states = read()
        if all(s.status_code == 0 for s in states.values()):
            log('[收工] 归零及失能反馈已确认', flush=True)
            return
        sleep(0.05)

    raise RuntimeError('已发送失能，但没有确认全部电机失能反馈；请检查设备状态')


def main():
    """CLI 入口：解析待收工手臂参数、注册中断信号并调用并行收工流水线。"""
    from motorbridge import Controller, Mode
    from telemetry_client import FeedbackPublisher
    import yaml

    payload = json.loads(sys.argv[1])
    hardware = yaml.safe_load(
        Path(__file__).resolve().parents[1].joinpath('rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml').read_text()
    )
    stopped = False

    def interrupt(*_):
        nonlocal stopped
        stopped = True
        raise InterruptedError('收工已停止；未执行盲目失能')

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)

    sessions = []
    publisher = FeedbackPublisher()
    try:
        # 打开并预先验证所有待收工手臂的连通性
        for arm in payload['arms']:
            bus = Controller.from_dm_serial(arm['port'], 921600)
            motors = {}
            sessions.append((arm, bus, motors))
            for joint in hardware['joints']:
                motors[int(joint['motor_id'])] = bus.add_damiao_motor(
                    joint['motor_id'], joint['feedback_id'], joint['model']
                )
            for motor in motors.values():
                motor.request_feedback()
            time.sleep(0.03)
            bus.poll_feedback_once()
            if len(motors) != 7 or any(m.get_state() is None for m in motors.values()):
                raise RuntimeError(f"{arm['side']} 反馈不完整，未开始归零")

        def finish(session, sync):
            arm, bus, motors = session
            if stopped:
                raise InterruptedError('收工已停止')
            print(f"[收工] {arm['side']} · {arm['port']} · 并行准备", flush=True)
            home_arm(
                bus,
                motors,
                position_mode=Mode.POS_VEL,
                force_mode=Mode.FORCE_POS,
                ratio=arm['gripper_torque_ratio'],
                log=lambda message, **kwargs: print(f"[{arm['side']}] {message}", **kwargs),
                publish=lambda q, t: publisher.send(arm['port'], q, t, gripper=motors[7].get_state().pos),
                sync=sync
            )

        print('[收工] 所选从臂并行慢速归零；每阶段等待两侧到位后继续，全部确认后失能', flush=True)
        parallel_shutdown(sessions, finish)
        print('[收工] 所有从臂归零、失能完成，正在释放串口；可重新连接启动', flush=True)
    except Exception as error:
        print(f'[收工失败] {error}', flush=True)
        return 1
    finally:
        # 退出清理底层总线句柄（不调用 disconnect，防止对失败手臂二次归零）
        for _, bus, motors in sessions:
            for motor in motors.values():
                try:
                    motor.close()
                except Exception:
                    pass
            try:
                bus.close()
            except Exception:
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
