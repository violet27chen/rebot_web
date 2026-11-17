#!/usr/bin/env python3
"""
机械臂空间运动测试脚本 (Cartesian Space Arm Movement Test)
=========================================================

【脚本定位与测试目的】
本脚本用于在无示教主臂参与的情况下，独立对执行从臂（RebotArm）进行笛卡尔空间运动学与动力学测试。
验证机械臂的正向运动学（FK）、逆向运动学（IK）以及末端位姿控制器（RebotArmEndPose）
的平滑轨迹规划与执行能力。

【动作测试序列】
1. 连接机械臂控制器，启动位姿控制引擎；
2. 读取当前关节角并计算初始末端笛卡尔位姿 (x, y, z, roll, pitch, yaw)；
3. 第一阶段动作：向上运动 13cm（z 轴方向增加 0.13m）；
4. 第二阶段动作：向左前方运动（x 增加 8cm，y 增加 12cm）；
5. 运动收尾与安全回零保护：动作完成后调用 safe_home 受限速度回零，并安全断开连接。
"""
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

# 实例化机械臂底层执行器与末端位姿控制器（工作在 posvel 位置-速度双闭环模式）
arm = RebotArm()
ctl = RebotArmEndPose(arm, arm_control_mode="posvel")

try:
    # 启动控制器后台循环与运动学状态同步
    ctl.start()
    # 加载 Pinocchio 机械臂刚体动力学与运动学模型
    model = load_robot_model()
    names = [jc.name for jc in arm._all_joints]
    arm_names = list(arm.groups["arm"].joint_names)
    
    # 获取机械臂当前状态：关节位置 q、角速度 dq、关节力矩 tau
    q, _dq, _tau = arm.get_state()
    # 提取六自由度臂部关节角度
    q_arm = np.array([q[names.index(n)] for n in arm_names], dtype=float)
    # 正向运动学计算末端执行器当前世界坐标位置与旋转矩阵
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    # 旋转矩阵转换为 RPY 欧拉角 (Roll, Pitch, Yaw)
    rpy = pin.rpy.matrixToRpy(rot)
    x, y, z = [float(v) for v in pos]
    r, p, yw = [float(v) for v in rpy]
    print(f"[move] current xyz=({x:.3f},{y:.3f},{z:.3f}) rpy=({r:.3f},{p:.3f},{yw:.3f})")

    # 阶段 1：末端竖直向上移动 13 厘米 (z + 0.13m)
    z1 = z + 0.13
    print(f"[move] up -> z={z1:.3f}")
    # 下发笛卡尔平滑插值移动，规划运行时间 2.5 秒
    ok = ctl.move_to_traj(x, y, z1, r, p, yw, duration=2.5)
    print(f"[move] up IK={ok}")
    # 留出 3.2 秒等待机械臂平稳到位并稳定
    time.sleep(3.2)

    # 阶段 2：末端向左前方水平探出 (x + 0.08m, y + 0.12m)
    x2, y2 = x + 0.08, y + 0.12
    print(f"[move] left-front -> x={x2:.3f} y={y2:.3f} z={z1:.3f}")
    ok2 = ctl.move_to_traj(x2, y2, z1, r, p, yw, duration=2.5)
    print(f"[move] left-front IK={ok2}")
    time.sleep(3.2)

    # 读取最终运动完成后的实际物理状态，校验位姿误差
    q, _dq, _tau = arm.get_state()
    q_arm = np.array([q[names.index(n)] for n in arm_names], dtype=float)
    pos, rot, _ = compute_fk(model, pad_q_for_model(model, q_arm, len(q_arm)))
    rpy = pin.rpy.matrixToRpy(rot)
    print(f"[move] final xyz=({pos[0]:.3f},{pos[1]:.3f},{pos[2]:.3f}) "
          f"rpy=({rpy[0]:.3f},{rpy[1]:.3f},{rpy[2]:.3f})")

finally:
    # 安全收尾保护：
    # 必须先调用 safe_home 以受限角速度（<=0.5 rad/s）平滑将机械臂复位到安全机械零位；
    # 严禁直接断开电机连接！否则电机瞬间掉电失能，机械臂将在重力作用下砸落在工作台造成撞击。
    try:
        ctl.end()
    except Exception as exc:
        print(f"[move] safe_home failed: {exc}")
    finally:
        try:
            arm.disconnect()
        except Exception:
            pass
