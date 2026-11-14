#!/usr/bin/env python3
"""直接控制从臂夹爪：--target 绝对角度(rad) 或 --delta 相对角度(rad，负=张开)。"""
import argparse
import sys
import time

SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

from reBotArm_control_py.actuator import RebotArm

ap = argparse.ArgumentParser()
ap.add_argument("--target", type=float, default=None, help="绝对目标角（rad）")
ap.add_argument("--delta", type=float, default=-0.5, help="相对当前角度（rad，负=张开）")
ap.add_argument("--vlim", type=float, default=1.2)
args = ap.parse_args()

arm = RebotArm()
arm.connect()
try:
    g = arm.groups["gripper"]
    names = [jc.name for jc in arm._all_joints]
    idx = names.index("gripper")
    q, _dq, _tau = arm.get_state()
    cur = float(q[idx])
    target = args.target if args.target is not None else cur + args.delta
    target = max(-4.2, min(0.0, target))
    print(f"[gripper] current={cur:+.4f} rad ({cur * 57.2958:+.1f} deg) "
          f"target={target:+.4f} rad ({target * 57.2958:+.1f} deg)")
    g.mode_pos_vel()
    g.enable()
    g.send_pos_vel([target], vlim=[args.vlim])
    for i in range(25):
        time.sleep(0.1)
        q, _dq, _tau = arm.get_state()
        if i % 5 == 0:
            p = float(q[idx])
            print(f"[gripper] t={i * 0.1:.1f}s pos={p:+.4f} rad ({p * 57.2958:+.1f} deg)")
    q, _dq, _tau = arm.get_state()
    final = float(q[idx])
    print(f"[gripper] final={final:+.4f} rad ({final * 57.2958:+.1f} deg)")
finally:
    try:
        arm.disconnect()
    except Exception:
        pass
