#!/usr/bin/env python3
"""
独立夹爪张开动作测试脚本 (Stand-alone Gripper Open Test)
======================================================

【功能与应用场景】
用于排查从臂末端夹爪的连通性、响应性与运动平滑度。
完全不触动 1~6 轴臂部关节（手臂保持原位不动），仅对 7 号夹爪电机进行小幅张开动作测试。

【测试逻辑】
1. 连接机械臂控制器，读取 gripper 关节当前物理弧度；
2. 规划在当前弧度基础上向张开方向移动约 50 度（-0.85 rad），且限制在 [-4.2, -0.6] rad 安全行程区间内；
3. 以 1.5 rad/s 速度下发位置速度指令；
4. 采样回读 2 秒运动轨迹，打印位置变化日志，验证电机响应。
"""
import sys
import time

# 导入机器人控制 SDK 路径
SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

from reBotArm_control_py.actuator import RebotArm

# 初始化机械臂通信
arm = RebotArm()
arm.connect()

try:
    # 获取夹爪关节索引与当前角度
    g = arm.groups["gripper"]
    names = [jc.name for jc in arm._all_joints]
    q, _dq, _tau = arm.get_state()
    cur = float(q[names.index("gripper")])
    print(f"[test] current gripper = {cur:+.4f} rad ({cur * 57.2958:+.1f} deg)")

    # 配置为位置-速度模式并使能
    g.mode_pos_vel()
    g.enable()
    # 目标规划：张开方向为负值；相对当前位置张开约 50 度（-0.85 rad），硬限制在 -4.2 rad 以内
    target = max(min(cur - 0.85, -0.6), -4.2)
    print(f"[test] target  gripper = {target:+.4f} rad ({target * 57.2958:+.1f} deg)")
    g.send_pos_vel([target], vlim=[1.5])

    # 循环读取 2.0 秒，验证运动平滑到达
    for i in range(20):
        time.sleep(0.1)
        q, _dq, _tau = arm.get_state()
        pos = float(q[names.index("gripper")])
        if i % 4 == 0:
            print(f"[test] t={i * 0.1:.1f}s pos={pos:+.4f} rad ({pos * 57.2958:+.1f} deg)")
            
    # 输出最终关节位置
    q, _dq, _tau = arm.get_state()
    final = float(q[names.index("gripper")])
    print(f"[test] final   gripper = {final:+.4f} rad ({final * 57.2958:+.1f} deg)")

finally:
    # 退出断开通信
    try:
        arm.disconnect()
    except Exception:
        pass
