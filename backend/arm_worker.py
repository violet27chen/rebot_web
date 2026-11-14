#!/usr/bin/env python3
"""从臂控制 worker（rebotarm 环境）：state / jog / gripper / pose / home / hold。

每次动作结束后把电机 CAN 超时设为 120 秒，让机械臂保持力矩直到下一次指令，
不会像之前那样进程退出就掉下来。
"""
from __future__ import annotations

import json
import math
import sys
import time

SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

import numpy as np  # noqa: E402
import pinocchio as pin  # noqa: E402
from reBotArm_control_py.actuator import RebotArm  # noqa: E402
from reBotArm_control_py.controllers import RebotArmEndPose  # noqa: E402
from reBotArm_control_py.kinematics.forward_kinematics import compute_fk  # noqa: E402
from reBotArm_control_py.kinematics.robot_model import (  # noqa: E402
    load_robot_model, pad_q_for_model,
)

HOLD_MS = 120000


def hold_all(arm: RebotArm) -> None:
    for m in arm._motor_map.values():
        try:
            m.set_can_timeout_ms(HOLD_MS)
        except Exception:
            pass


def arm_joints(arm: RebotArm, q) -> np.ndarray:
    names = [jc.name for jc in arm._all_joints]
    arm_names = list(arm.groups["arm"].joint_names)
    return np.array([q[names.index(n)] for n in arm_names], dtype=float)


def fk_pose(model, q_arm):
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    rpy = pin.rpy.matrixToRpy(rot)
    return [float(x) for x in pos], [float(x) for x in rpy]


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "state"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    model = load_robot_model()
    arm = RebotArm()
    arm.connect()
    try:
        if action == "state":
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            names = [jc.name for jc in arm._all_joints]
            grip = float(q[names.index("gripper")])
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({"ok": True, "joints": [float(x) for x in qa],
                              "gripper": grip, "xyz": xyz, "rpy": rpy}))
            return

        if action == "jog":
            idx = int(payload.get("index", 0))
            delta = math.radians(float(payload.get("delta_deg", 0.0)))
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            if idx < 0 or idx >= len(qa):
                print(json.dumps({"ok": False, "error": "joint index out of range"}))
                return
            target = qa.copy()
            target[idx] += delta
            g = arm.groups["arm"]
            g.mode_pos_vel()
            g.enable()
            steps = 20
            for i in range(1, steps + 1):
                cmd = qa + (target - qa) * i / steps
                g.send_pos_vel([float(x) for x in cmd], vlim=[1.0])
                time.sleep(0.03)
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({"ok": True, "joints": [float(x) for x in qa],
                              "xyz": xyz, "rpy": rpy}))
            return

        if action == "gripper":
            target = payload.get("target")
            if target == "open":
                target = 0.0
            elif target == "close":
                target = -3.0
            target = float(target)
            names = [jc.name for jc in arm._all_joints]
            q, _dq, _tau = arm.get_state()
            cur = float(q[names.index("gripper")])
            g = arm.groups["gripper"]
            g.mode_pos_vel()
            g.enable()
            steps = 25
            for i in range(1, steps + 1):
                g.send_pos_vel([cur + (target - cur) * i / steps], vlim=[1.5])
                time.sleep(0.03)
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            print(json.dumps({"ok": True, "gripper": float(q[names.index("gripper")])}))
            return

        if action == "pose":
            x, y, z = float(payload["x"]), float(payload["y"]), float(payload["z"])
            roll = float(payload.get("roll", 0.0))
            pitch = float(payload.get("pitch", 0.0))
            yaw = float(payload.get("yaw", 0.0))
            duration = float(payload.get("duration", 2.0))
            ctl = RebotArmEndPose(arm, arm_control_mode="posvel")
            ctl.start()
            ok = ctl.move_to_traj(x, y, z, roll, pitch, yaw, duration=duration)
            time.sleep(duration + 0.8)
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({"ok": bool(ok), "joints": [float(x) for x in qa],
                              "xyz": xyz, "rpy": rpy}))
            return

        if action == "home":
            ctl = RebotArmEndPose(arm, arm_control_mode="posvel")
            ctl.start()
            ctl.safe_home()
            time.sleep(0.5)
            ctl.end()
            print(json.dumps({"ok": True}))
            return

        if action == "hold":
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({"ok": True, "joints": [float(x) for x in qa],
                              "xyz": xyz, "rpy": rpy}))
            return

        print(json.dumps({"ok": False, "error": f"unknown action: {action}"}))
    finally:
        try:
            arm.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
