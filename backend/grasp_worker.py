#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视觉引导抓取运动学与物理执行 Worker 进程 (Grasp Kinematics & Execution Worker)
================================================================================

【模块背景与定位】
在视觉抓取（Visual Grasping）系统中，上位机通过相机检测到物体表面 3D 抓取位姿后，
需要先进行纯数学运动学逆解（IK 求解关节角）与正解（FK 计算位姿），并在用户确认后
执行物理抓取控制序列。

【双重运行模式设计】
1. 纯计算模式 (Pure FK/IK):
   - action="fk": 输入 6 维关节角，纯数学计算输出笛卡尔 xyz 与 rpy，绝不打开串口、不使能电机。
   - action="ik": 输入目标末端笛卡尔位姿与当前种子姿态，基于 Pinocchio 和阻尼最小二乘求解 6 轴逆解，
     输出解出的关节角与收敛残差，同样无任何硬件副作用。
2. 物理抓取执行模式 (action="execute"):
   - 动态加载硬件 URDF 与端口映射配置，连接机械臂；
   - 校验当前实测物理角度是否与界面预演时的 seed 姿态一致（偏差需 <= 0.15 rad），防止环境变动产生碰撞；
   - 启动动力学抓取控制器 `GraspController`；
   - 执行标准化工业抓取动作序列：
     张开夹爪 -> 移至预抓取点 (z + approach) -> 垂直下降到抓取点 (z) ->
     限力抓取判定 -> 垂直抬起 (z + approach) -> 若抓取成功则保持悬停并原路平稳放回 -> 退回预备点。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

# 动态配置机械臂 Python 控制 SDK 路径
SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

import numpy as np  # noqa: E402
from reBotArm_control_py.actuator import RebotArm  # noqa: E402
from reBotArm_control_py.kinematics.forward_kinematics import compute_fk  # noqa: E402
from reBotArm_control_py.kinematics.inverse_kinematics import (  # noqa: E402
    IKParams, pos_rot_to_se3, solve_ik,
)
from reBotArm_control_py.kinematics.robot_model import (  # noqa: E402
    get_end_effector_frame_id, load_robot_model, pad_q_for_model,
)


def main() -> None:
    """
    Worker 进程主入口：解析命令行指令 action 与载荷 JSON，分发执行。
    """
    action = sys.argv[1] if len(sys.argv) > 1 else "state"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    
    # 加载 Pinocchio 几何与连杆运动学模型
    model = load_robot_model()
    frame = get_end_effector_frame_id(model)

    # 模式 1：纯正/逆运动学计算（绝不打开硬件串口）
    if action in ('fk', 'ik'):
        q = np.asarray(payload.get('joints', [0.0] * 6), dtype=float)
        if len(q) != 6 or not np.isfinite(q).all():
            raise ValueError('无效的六维关节角')
        q_pad = pad_q_for_model(model, q, 6)
        
        # 1a: 正向运动学
        if action == 'fk':
            import pinocchio as pin
            pos, rot, _ = compute_fk(model, q_pad)
            print(json.dumps(dict(
                ok=True,
                joints=q.tolist(),
                xyz=pos.tolist(),
                rpy=pin.rpy.matrixToRpy(rot).tolist()
            )))
        # 1b: 逆向运动学
        else:
            target = pos_rot_to_se3(
                np.array([payload['x'], payload['y'], payload['z']]),
                roll=float(payload.get('roll', 0)),
                pitch=float(payload.get('pitch', 0)),
                yaw=float(payload.get('yaw', 0))
            )
            res = solve_ik(model, model.createData(), frame, target, q_pad, IKParams(), controlled_joints=6)
            print(json.dumps(dict(
                ok=bool(res.success),
                joints=[float(x) for x in res.q[:6]],
                error=float(res.error)
            )))
        return  # 纯运动学计算直接返回，无任何总线 I/O

    # 模式 2：物理抓取执行
    if action != 'execute':
        raise ValueError('未知动作')
    if not payload.get('port'):
        raise ValueError('执行需要明确的机械臂串口')

    # 动态生成隔离的硬件配置文件，绑定指定串口和 100Hz 频率限制
    import yaml
    hardware = yaml.safe_load(Path(SDK, 'config/rebotarm_dm.yaml').read_text())
    hardware['channel'] = payload['port']
    hardware['rate'] = 100  # 限制独立控制进程的串口通信频率，防止总线拥塞
    hardware['urdf_path'] = str(Path(SDK, hardware['urdf_path']).resolve())
    
    fd, temporary = tempfile.mkstemp(prefix='grasp-hardware-', suffix='.yaml')
    with os.fdopen(fd, 'w') as stream:
        yaml.safe_dump(hardware, stream)
        
    arm = RebotArm(temporary)
    os.unlink(temporary)
    
    from grasp_runtime import GraspController
    arm.connect()
    controller = None

    try:
        names = arm.groups['arm'].joint_names
        deadline = time.monotonic() + 2
        
        # 步骤 1：轮询等待各关节电机实测状态就绪
        while True:
            arm.get_state()
            states = [arm._motor_map[name].get_state() for name in names]
            if all(state is not None for state in states) and arm._motor_map['gripper'].get_state() is not None:
                break
            if time.monotonic() > deadline:
                raise RuntimeError('机械臂反馈等待超时')
            time.sleep(0.02)

        if len(states) != 6 or any(state is None for state in states):
            raise RuntimeError('机械臂没有完整的实测关节反馈')

        # 步骤 2：安全一致性校验：检查当前实际关节角与规划前 seed 的偏差是否超过阈值（0.15 rad）
        if max(abs(state.pos - seed) for state, seed in zip(states, payload['seed'])) > 0.15:
            raise RuntimeError('机械臂位置已改变，请重新预演')

        # 步骤 3：初始化抓取动力学控制器
        controller = GraspController(arm, [state.pos for state in states], payload.get('gripper_torque_limit', 0.3))

        def log(text):
            print('[grasp] ' + text, file=sys.stderr, flush=True)

        x, y, z = (float(payload[key]) for key in ('x', 'y', 'z'))
        r, p, yaw = (float(payload.get(key, 0)) for key in ('roll', 'pitch', 'yaw'))
        approach = float(payload.get('approach', 0.08))

        # 步骤 4：执行标准化抓取阶段时序
        controller.start()
        log('张开夹爪')
        controller.open_gripper()
        
        log('移动到预抓取点')
        controller.move(x, y, z + approach, r, p, yaw)
        
        log('下降到抓取点')
        controller.move(x, y, z, r, p, yaw)
        
        log('限力闭合夹爪')
        held = controller.grasp()
        
        log('抬起')
        controller.move(x, y, z + approach, r, p, yaw)

        # 若成功夹到物品，演示保持 1 秒后原路放回并释放
        if held:
            time.sleep(1)
            log('放回原位置')
            controller.move(x, y, z, r, p, yaw)
            log('松开夹爪')
            controller.open_gripper()
            log('退回预抓取点')
            controller.move(x, y, z + approach, r, p, yaw)

        print(json.dumps(dict(ok=True, held=held)))

    finally:
        # 安全退出清理
        if controller:
            controller.stop()
        arm.disconnect()


if __name__ == "__main__":
    main()
