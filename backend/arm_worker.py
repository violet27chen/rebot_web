#!/usr/bin/env python3
"""
从臂单次动作执行 Worker 进程 (Arm Action Worker)
=================================================

【模块背景与定位】
在非守护进程模式或独立执行单次机械臂控制指令（例如前端单次点击“点动”、“回零”、“夹爪开合”或“移动到坐标”）时，
本模块作为独立的 CLI Worker 进程被主后端拉起执行。

【防掉臂力矩保持机制】
在传统控制中，一旦 Python 控制进程退出并断开串口，电机会因为看门狗超时立刻失能，
导致机械臂在高空由于自身重力猛烈下坠（掉臂砸落）。
本模块在每次运动完成退出前，通过 `hold_all` 将所有电机的底层 CAN 超时阈值重设为 120 秒（120000ms），
使得机械臂在进程退出后仍能在硬件层面保持力矩直至下一次指令到达，实现平稳静态悬停。

【支持的操作指令】
- state: 查询当前 6 自由度臂部关节角度、夹爪开度以及正运动学末端位姿 (xyz, rpy)。
- jog: 指定单关节按微步弧度增量进行平滑点动插值。
- gripper: 夹爪开合（支持 "open"、"close" 或绝对弧度目标）。
- pose: 基于 Pinocchio 逆向运动学（IK）和五次样条进行末端执行器笛卡尔空间点到点轨迹插补。
- home: 执行基于安全角速度限制的平滑机械回零（safe_home）。
- hold: 刷新底层电机的力矩保持时间。
"""
from __future__ import annotations

import json
import math
import sys
import time

# 动态配置机械臂 Python 控制 SDK 路径
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

# 电机底层硬件看门狗/通信超时阈值：120 秒 (2分钟)，用于运动后保持静止悬停
HOLD_MS = 120000


def hold_all(arm: RebotArm) -> None:
    """
    遍历机械臂的所有电机驱动，将硬件看门狗超时时间设置为 120 秒。
    确保即使当前 Worker 进程退出、串口句柄释放后，下位机电机仍保持锁轴力矩，防止掉臂。

    Args:
        arm (RebotArm): 机械臂底层执行器句柄。
    """
    for m in arm._motor_map.values():
        try:
            m.set_can_timeout_ms(HOLD_MS)
        except Exception:
            pass


def arm_joints(arm: RebotArm, q: np.ndarray) -> np.ndarray:
    """
    从整臂（包含夹爪等）的全量状态向量 q 中，按正确顺序筛选出属于 arm 组的 6 个主关节角。

    Args:
        arm (RebotArm): 机械臂实例。
        q (np.ndarray): 全关节实测状态向量。

    Returns:
        np.ndarray: 6 个主臂关节的弧度浮点数组。
    """
    names = [jc.name for jc in arm._all_joints]
    arm_names = list(arm.groups["arm"].joint_names)
    return np.array([q[names.index(n)] for n in arm_names], dtype=float)


def fk_pose(model, q_arm: np.ndarray):
    """
    基于 Pinocchio 机器人模型计算给定 6 关节弧度下的末端执行器正运动学（FK）位姿。

    Args:
        model: Pinocchio 机器人动力学模型。
        q_arm (np.ndarray): 6 关节角度数组 (rad)。

    Returns:
        tuple[list[float], list[float]]:
            - xyz: 末端空间笛卡尔坐标 [x, y, z] (米)；
            - rpy: 末端空间姿态欧拉角 [roll, pitch, yaw] (弧度)。
    """
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    rpy = pin.rpy.matrixToRpy(rot)
    return [float(x) for x in pos], [float(x) for x in rpy]


def main() -> None:
    """
    Worker 进程主入口：
    解析命令行参数 sys.argv[1] (action) 和 sys.argv[2] (payload JSON)，
    连接机械臂执行目标动作，并向 stdout 输出结果 JSON。
    """
    action = sys.argv[1] if len(sys.argv) > 1 else "state"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    
    # 加载刚体几何运动学模型
    model = load_robot_model()
    arm = RebotArm()
    arm.connect()

    try:
        # 指令 1：查询机械臂全状态
        if action == "state":
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            names = [jc.name for jc in arm._all_joints]
            grip = float(q[names.index("gripper")])
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({
                "ok": True,
                "joints": [float(x) for x in qa],
                "gripper": grip,
                "xyz": xyz,
                "rpy": rpy
            }))
            return

        # 指令 2：单关节相对微步点动 (jog)
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
            # 分 20 步平滑插值下发，防止阶跃冲击
            steps = 20
            for i in range(1, steps + 1):
                cmd = qa + (target - qa) * i / steps
                g.send_pos_vel([float(x) for x in cmd], vlim=[1.0])
                time.sleep(0.03)
            # 运动完成后立即刷新硬件力矩保持
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({
                "ok": True,
                "joints": [float(x) for x in qa],
                "xyz": xyz,
                "rpy": rpy
            }))
            return

        # 指令 3：夹爪开合动作 (gripper)
        if action == "gripper":
            target = payload.get("target")
            if target == "open":
                target = 0.0      # 0 rad 标定位置
            elif target == "close":
                target = -3.0     # 闭合目标弧度
            target = float(target)
            names = [jc.name for jc in arm._all_joints]
            q, _dq, _tau = arm.get_state()
            cur = float(q[names.index("gripper")])
            g = arm.groups["gripper"]
            g.mode_pos_vel()
            g.enable()
            # 分 25 步插值下发
            steps = 25
            for i in range(1, steps + 1):
                g.send_pos_vel([cur + (target - cur) * i / steps], vlim=[1.5])
                time.sleep(0.03)
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            print(json.dumps({"ok": True, "gripper": float(q[names.index("gripper")])}))
            return

        # 指令 4：末端空间位姿运动 (pose)
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
            print(json.dumps({
                "ok": bool(ok),
                "joints": [float(x) for x in qa],
                "xyz": xyz,
                "rpy": rpy
            }))
            return

        # 指令 5：安全受限速度回零 (home)
        if action == "home":
            ctl = RebotArmEndPose(arm, arm_control_mode="posvel")
            ctl.start()
            ctl.safe_home()
            time.sleep(0.5)
            ctl.end()
            print(json.dumps({"ok": True}))
            return

        # 指令 6：纯刷新力矩保持 (hold)
        if action == "hold":
            hold_all(arm)
            q, _dq, _tau = arm.get_state()
            qa = arm_joints(arm, q)
            xyz, rpy = fk_pose(model, qa)
            print(json.dumps({
                "ok": True,
                "joints": [float(x) for x in qa],
                "xyz": xyz,
                "rpy": rpy
            }))
            return

        print(json.dumps({"ok": False, "error": f"unknown action: {action}"}))

    finally:
        # 退出断开连接（由于前面设置了 HOLD_MS，电机不会失力下坠）
        try:
            arm.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
