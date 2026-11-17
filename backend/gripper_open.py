#!/usr/bin/env python3
"""
从臂末端夹爪独立位置/相对位移控制工具 (Gripper Direct Control Tool)
==================================================================

【功能与应用场景】
用于直接控制机械臂末端的电动夹爪，支持指定绝对弧度目标（--target）或
相对当前角度增量（--delta），便于在调试、物料释放或异常卡死时进行手动脱开。

【参数规范】
- 角度定义：0.0 rad 对应闭合状态；负弧度（如 -3.0 ~ -4.2 rad）对应张开状态。
- --target: 绝对目标角度（单位：rad，有效范围受限于 [-4.2, 0.0]）。
- --delta: 相对当前角度增量（单位：rad，默认 -0.5，负值代表向张开方向移动）。
- --vlim: 速度上限（rad/s），默认 1.2。
"""
import argparse
import sys
import time

# 导入机器人控制 SDK 路径
SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

from reBotArm_control_py.actuator import RebotArm

# 命令行解析配置
ap = argparse.ArgumentParser(description="从臂夹爪独立位置/步进控制工具")
ap.add_argument("--target", type=float, default=None, help="绝对目标角（rad，0=闭合，负值=张开）")
ap.add_argument("--delta", type=float, default=-0.5, help="相对当前角度偏移量（rad，默认-0.5向张开方向）")
ap.add_argument("--vlim", type=float, default=1.2, help="运动最大角速度限制（rad/s）")
args = ap.parse_args()

# 建立机械臂通信连接
arm = RebotArm()
arm.connect()

try:
    # 提取夹爪电机组 (gripper)
    g = arm.groups["gripper"]
    names = [jc.name for jc in arm._all_joints]
    idx = names.index("gripper")
    
    # 获取夹爪当前物理读数
    q, _dq, _tau = arm.get_state()
    cur = float(q[idx])
    
    # 计算目标弧度，并进行硬限位截断保护（防止超出机械结构限制造成打齿）
    target = args.target if args.target is not None else cur + args.delta
    target = max(-4.2, min(0.0, target))
    print(f"[gripper] current={cur:+.4f} rad ({cur * 57.2958:+.1f} deg) "
          f"target={target:+.4f} rad ({target * 57.2958:+.1f} deg)")
    
    # 切换至位置-速度模式并使能电机
    g.mode_pos_vel()
    g.enable()
    # 下发目标位置与速度限制指令
    g.send_pos_vel([target], vlim=[args.vlim])
    
    # 轮询 2.5 秒观察夹爪物理运动到位状态
    for i in range(25):
        time.sleep(0.1)
        q, _dq, _tau = arm.get_state()
        if i % 5 == 0:
            p = float(q[idx])
            print(f"[gripper] t={i * 0.1:.1f}s pos={p:+.4f} rad ({p * 57.2958:+.1f} deg)")
            
    # 输出最终达到的物理关节角度
    q, _dq, _tau = arm.get_state()
    final = float(q[idx])
    print(f"[gripper] final={final:+.4f} rad ({final * 57.2958:+.1f} deg)")

finally:
    # 退出前释放通信端口
    try:
        arm.disconnect()
    except Exception:
        pass
