#!/usr/bin/env python3
"""
底层直通力位混合控制夹爪全开工具 (Low-Level Force-Position Gripper Opener)
========================================================================

【功能与应用场景】
绕过上位机高层 SDK 的软件关节限位与保护逻辑，直接通过 motorbridge 驱动库
向 7 号夹爪达妙 4310 电机下发 `send_force_pos` 指令。
常用于在机械夹爪因外力卡滞、初始位置超限或需要以极限行程完全张开到物理机械限位点时的底层恢复。

【控制特点】
- 目标角度：-260.0°（直接面向达妙电机转子角度）。
- 速度限制：60.0 deg/s（低速运行，防止撞击硬限位）。
- 力矩限制：设定为 6%（0.06）小力矩，确保即便贴靠物理限位也不会因堵转损坏电机或减速齿轮。
- 退出行为：操作完成后保留电机力矩使能状态（不执行 disable），依靠微弱保持力使夹爪停在全开位。
"""
import math
import sys
import time

from motorbridge import Controller

# 默认电机通讯端口与波特率（CAN-USB 桥接虚拟串口）
PORT = "/dev/ttyACM0"
ctrl = Controller.from_dm_serial(PORT, 921600)
# 添加 7 号电机：CAN ID=7, Master/Feedback ID=23, 型号=4310
motor = ctrl.add_damiao_motor(7, 23, "4310")

try:
    # 使能夹爪电机
    motor.enable()
    time.sleep(0.2)
    # 获取电机初始状态
    st = motor.get_state()
    cur = math.degrees(st.pos) if st is not None else 0.0
    print(f"[force] current motor = {cur:+.1f} deg")

    # 设定全开目标为 -260 度，以小力矩（6% 额定力矩）贴向机械止点
    target = -260.0
    print(f"[force] target motor = {target:+.1f} deg (vlim 60 deg/s, force 6%)")
    motor.send_force_pos(math.radians(target), math.radians(60.0), 0.06)

    # 循环轮询 2.5 秒，采集电机运动反馈
    for i in range(25):
        time.sleep(0.1)
        try:
            motor.request_feedback()
            ctrl.poll_feedback_once()
        except Exception:
            pass
        st = motor.get_state()
        if i % 5 == 0 and st is not None:
            print(f"[force] t={i * 0.1:.1f}s pos={math.degrees(st.pos):+.1f} deg")

    # 最终状态轮询与回读
    try:
        motor.request_feedback()
        ctrl.poll_feedback_once()
    except Exception:
        pass
    st = motor.get_state()
    final = math.degrees(st.pos) if st is not None else 0.0
    print(f"[force] final motor = {final:+.1f} deg")

finally:
    # 注意：此处不调用 motor.disable()，保持微弱力矩，防止重力或弹簧导致夹爪回弹
    try:
        ctrl.close()
    except Exception:
        pass
