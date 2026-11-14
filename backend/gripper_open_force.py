#!/usr/bin/env python3
"""用 motorbridge 直接给夹爪电机发 FORCE_POS，开到最大（不经过 SDK 关节限位）。"""
import math
import sys
import time

from motorbridge import Controller

PORT = "/dev/ttyACM0"
ctrl = Controller.from_dm_serial(PORT, 921600)
motor = ctrl.add_damiao_motor(7, 23, "4310")
try:
    motor.enable()
    time.sleep(0.2)
    st = motor.get_state()
    cur = math.degrees(st.pos) if st is not None else 0.0
    print(f"[force] current motor = {cur:+.1f} deg")

    target = -260.0
    print(f"[force] target motor = {target:+.1f} deg (vlim 60 deg/s, force 6%)")
    motor.send_force_pos(math.radians(target), math.radians(60.0), 0.06)
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
    try:
        motor.request_feedback()
        ctrl.poll_feedback_once()
    except Exception:
        pass
    st = motor.get_state()
    final = math.degrees(st.pos) if st is not None else 0.0
    print(f"[force] final motor = {final:+.1f} deg")
finally:
    # 不 disable：保持力矩，让夹爪停在完全张开位置
    try:
        ctrl.close()
    except Exception:
        pass
