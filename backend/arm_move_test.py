#!/usr/bin/env python3
"""不用主臂：先向上伸，再向左前方伸（末端位姿指令）。"""
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

arm = RebotArm()
ctl = RebotArmEndPose(arm, arm_control_mode="posvel")
try:
    ctl.start()
    model = load_robot_model()
    names = [jc.name for jc in arm._all_joints]
    arm_names = list(arm.groups["arm"].joint_names)
    q, _dq, _tau = arm.get_state()
    q_arm = np.array([q[names.index(n)] for n in arm_names], dtype=float)
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    rpy = pin.rpy.matrixToRpy(rot)
    x, y, z = [float(v) for v in pos]
    r, p, yw = [float(v) for v in rpy]
    print(f"[move] current xyz=({x:.3f},{y:.3f},{z:.3f}) rpy=({r:.3f},{p:.3f},{yw:.3f})")

    # 1) 向上伸
    z1 = z + 0.13
    print(f"[move] up -> z={z1:.3f}")
    ok = ctl.move_to_traj(x, y, z1, r, p, yw, duration=2.5)
    print(f"[move] up IK={ok}")
    time.sleep(3.2)

    # 2) 向左前方伸
    x2, y2 = x + 0.08, y + 0.12
    print(f"[move] left-front -> x={x2:.3f} y={y2:.3f} z={z1:.3f}")
    ok2 = ctl.move_to_traj(x2, y2, z1, r, p, yw, duration=2.5)
    print(f"[move] left-front IK={ok2}")
    time.sleep(3.2)

    q, _dq, _tau = arm.get_state()
    q_arm = np.array([q[names.index(n)] for n in arm_names], dtype=float)
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    rpy = pin.rpy.matrixToRpy(rot)
    print(f"[move] final xyz=({pos[0]:.3f},{pos[1]:.3f},{pos[2]:.3f}) "
          f"rpy=({rpy[0]:.3f},{rpy[1]:.3f},{rpy[2]:.3f})")
finally:
    # 安全收尾：先用受限速度 safe_home（0.5 rad/s）回零，再断开；
    # 绝不能直接 arm.disconnect()，否则电机关闭后机械臂会靠重力掉下来。
    try:
        ctl.end()
    except Exception as exc:
        print(f"[move] safe_home failed: {exc}")
    finally:
        try:
            arm.disconnect()
        except Exception:
            pass
