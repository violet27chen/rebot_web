#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉抓取用的 IK/FK worker（在 rebotarm 环境里运行）。

用法:
    python grasp_worker.py fk '{"joints":[0,0,0,0,0,0]}'
    python grasp_worker.py ik '{"x":0.30,"y":0.0,"z":0.12,"roll":0,"pitch":0.7,"yaw":0}'

输出单行 JSON:
    fk    -> {"ok":true,"joints":[...],"xyz":[x,y,z]}
    ik    -> {"ok":true,"joints":[...],"error":0.0001}
"""
from __future__ import annotations

import json
import sys
import time
import tempfile
import os
from pathlib import Path

SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

import numpy as np  # noqa: E402
from reBotArm_control_py.actuator import RebotArm  # noqa: E402
from reBotArm_control_py.kinematics.robot_model import (  # noqa: E402
    load_robot_model, get_end_effector_frame_id, pad_q_for_model,
)
from reBotArm_control_py.kinematics.forward_kinematics import compute_fk  # noqa: E402
from reBotArm_control_py.kinematics.inverse_kinematics import (  # noqa: E402
    solve_ik, IKParams, pos_rot_to_se3,
)


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "state"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    model = load_robot_model()
    frame = get_end_effector_frame_id(model)
    if action in ('fk', 'ik'):
        q = np.asarray(payload.get('joints', [0.0]*6), dtype=float)
        if len(q)!=6 or not np.isfinite(q).all(): raise ValueError('无效的六维关节角')
        q_pad = pad_q_for_model(model, q, 6)
        if action == 'fk':
            import pinocchio as pin
            pos, rot, _ = compute_fk(model, q_pad)
            print(json.dumps(dict(ok=True, joints=q.tolist(), xyz=pos.tolist(), rpy=pin.rpy.matrixToRpy(rot).tolist())))
        else:
            target = pos_rot_to_se3(np.array([payload['x'],payload['y'],payload['z']]),
                roll=float(payload.get('roll',0)),pitch=float(payload.get('pitch',0)),yaw=float(payload.get('yaw',0)))
            res = solve_ik(model,model.createData(),frame,target,q_pad,IKParams(),controlled_joints=6)
            print(json.dumps(dict(ok=bool(res.success),joints=[float(x) for x in res.q[:6]],error=float(res.error))))
        return  # Pure FK / IK: no serial open, enable, disable or motion.
    if action != 'execute': raise ValueError('未知动作')
    if not payload.get('port'): raise ValueError('执行需要明确的机械臂串口')
    import yaml
    hardware = yaml.safe_load(Path(SDK, 'config/rebotarm_dm.yaml').read_text())
    hardware['channel'] = payload['port']
    hardware['rate'] = 100  # Independent control process; bound serial bus utilization.
    hardware['urdf_path'] = str(Path(SDK, hardware['urdf_path']).resolve())
    fd, temporary = tempfile.mkstemp(prefix='grasp-hardware-',suffix='.yaml')
    with os.fdopen(fd,'w') as stream: yaml.safe_dump(hardware, stream)
    arm = RebotArm(temporary)
    os.unlink(temporary)
    from grasp_runtime import GraspController
    arm.connect()
    controller = None
    try:
        names = arm.groups['arm'].joint_names
        deadline=time.monotonic()+2
        while True:
            arm.get_state()
            states=[arm._motor_map[name].get_state() for name in names]
            if all(state is not None for state in states) and arm._motor_map['gripper'].get_state() is not None:break
            if time.monotonic()>deadline:raise RuntimeError('机械臂反馈等待超时')
            time.sleep(.02)
        if len(states)!=6 or any(state is None for state in states):
            raise RuntimeError('机械臂没有完整的实测关节反馈')
        if max(abs(state.pos-seed) for state,seed in zip(states,payload['seed']))>.15:
            raise RuntimeError('机械臂位置已改变，请重新预演')
        controller = GraspController(arm,[state.pos for state in states],payload.get('gripper_torque_limit',.3))
        def log(text):print('[grasp] '+text,file=sys.stderr,flush=True)
        x,y,z = (float(payload[key]) for key in ('x','y','z'))
        r,p,yaw = (float(payload.get(key,0)) for key in ('roll','pitch','yaw'))
        approach = float(payload.get('approach',.08))
        controller.start()
        log('张开夹爪');controller.open_gripper()
        log('移动到预抓取点');controller.move(x,y,z+approach,r,p,yaw)
        log('下降到抓取点');controller.move(x,y,z,r,p,yaw)
        log('限力闭合夹爪');held=controller.grasp()
        log('抬起');controller.move(x,y,z+approach,r,p,yaw)
        if held:
            time.sleep(1)
            log('放回原位置');controller.move(x,y,z,r,p,yaw)
            log('松开夹爪');controller.open_gripper()
            log('退回预抓取点');controller.move(x,y,z+approach,r,p,yaw)
        print(json.dumps(dict(ok=True,held=held)))
    finally:
        if controller:controller.stop()
        arm.disconnect()


if __name__ == "__main__":
    main()
