#!/usr/bin/env python3
"""只动夹爪：不经过主臂，直接给 7 号夹爪电机发位置指令（张开方向）。"""
import sys
import time

SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

from reBotArm_control_py.actuator import RebotArm

arm = RebotArm()
arm.connect()
try:
    g = arm.groups["gripper"]
    names = [jc.name for jc in arm._all_joints]
    q, _dq, _tau = arm.get_state()
    cur = float(q[names.index("gripper")])
    print(f"[test] current gripper = {cur:+.4f} rad ({cur * 57.2958:+.1f} deg)")

    g.mode_pos_vel()
    g.enable()
    # 张开方向为负；相对当前位置再张开约 50 度，限制在 -4.2 rad 以内
    target = max(min(cur - 0.85, -0.6), -4.2)
    print(f"[test] target  gripper = {target:+.4f} rad ({target * 57.2958:+.1f} deg)")
    g.send_pos_vel([target], vlim=[1.5])

    for i in range(20):
        time.sleep(0.1)
        q, _dq, _tau = arm.get_state()
        pos = float(q[names.index("gripper")])
        if i % 4 == 0:
            print(f"[test] t={i * 0.1:.1f}s pos={pos:+.4f} rad ({pos * 57.2958:+.1f} deg)")
    q, _dq, _tau = arm.get_state()
    final = float(q[names.index("gripper")])
    print(f"[test] final   gripper = {final:+.4f} rad ({final * 57.2958:+.1f} deg)")
finally:
    try:
        arm.disconnect()
    except Exception:
        pass
