"""The sole CAN owner for an armed sorting session; independent of vision/ACT.

On lost web/command heartbeat targets freeze at measured positions. The process
stays alive in bounded gravity-compensated hold until explicit handoff/shutdown.
"""
import math
import sys
import time
from pathlib import Path
import numpy as np
from .ipc import worker_pipes
from .geometry import array,SortingError
from ..motor_mode import ensure_mode_checked


def bounded_target(current,target,limits,max_delta):
    q=array(target,(len(current),),'关节目标');bounds=array(limits,(len(current),2),'关节限位')
    if not np.isfinite(max_delta) or not 0<max_delta<=.3 or np.any(bounds[:,0]>=bounds[:,1]):raise SortingError('关节安全边界无效')
    if np.any(q<bounds[:,0]) or np.any(q>bounds[:,1]):raise SortingError('目标越过关节限位，未裁剪或执行')
    if np.max(np.abs(q-current))>max_delta:raise SortingError('ACT 动作跳变超过允许范围')
    return q


def main():
    incoming,outgoing=worker_pipes();root=Path(__file__).resolve().parents[2]
    sdk=root/'rebot_grasp/sdk/reBotArm_control_py';sys.path.insert(0,str(sdk))
    from motorbridge import Controller,Mode
    import yaml
    import pinocchio as pin
    from reBotArm_control_py.kinematics.robot_model import load_robot_model
    from reBotArm_control_py.kinematics.inverse_kinematics import solve_ik,IKParams
    from backend.telemetry_client import FeedbackPublisher
    model=load_robot_model(str(sdk/'urdf/DM/urdf/ReBot_Arm_DM.urdf'));data=model.createData();frame=model.getFrameId('end_link')
    hardware=yaml.safe_load((sdk/'config/rebotarm_dm.yaml').read_text());sessions=[];cfg=None
    initialized=False
    target=None;command_at=web_at=time.monotonic();fault='';armed=False;publisher=FeedbackPublisher();last_publish=0
    register_index=0;last_register=0;last_states=None;closing=False
    def states():
        result=[]
        for _,bus,motors in sessions:
            for motor in motors:motor.request_feedback()
            bus.poll_feedback_once();result.extend(m.get_state() for m in motors)
        if any(s is None for s in result):raise RuntimeError('CAN 实测反馈缺失')
        if any(not all(math.isfinite(getattr(s,k)) for k in ['pos','vel','torq','t_mos']) or s.status_code not in (0,1) for s in result):raise RuntimeError('CAN 反馈或故障状态无效')
        return result
    def model_q(q):
        full=np.zeros(model.nq);full[:6]=q[:6];return full
    def fk(q):
        pin.forwardKinematics(model,data,model_q(q));pin.updateFramePlacements(model,data)
        return data.oMf[frame].translation.tolist()
    def snapshot():
        return {'joints':[s.pos for s in last_states],'torques':[s.torq for s in last_states],
                'fault':fault,'held':not armed,'timestamp':time.time(),
                'tcp':[fk([s.pos for s in last_states][i:i+7]) for i in range(0,len(last_states),7)]}
    try:
        while True:
            began=time.monotonic()
            if cfg is not None:
                try:
                    try:web_at=float(Path(cfg['heartbeat_path']).read_text())
                    except (OSError,ValueError):web_at=0
                    last_states=states();q=np.array([s.pos for s in last_states]);velocity=np.array([s.vel for s in last_states])
                    reason=''
                    if max(s.t_mos for s in last_states)>80:reason='电机温度超过 80°C'
                    if np.any(np.abs([s.torq for s in last_states])>np.asarray(cfg['torque_limits_nm'])*1.1):reason='实测力矩看门狗触发'
                    if began-web_at>1.5:reason='浏览器心跳中断，已冻结目标'
                    if armed and began-command_at>.5:reason='推理/轨迹指令过期，已冻结目标'
                    if reason and not fault:fault=reason;armed=False;target=q.copy()
                    # Fresh register transactions detect a silent feedback channel;
                    # cached MotorState alone has no timestamp in motorbridge 0.5.
                    if began-last_register>.05:
                        all_motors=[m for _,_,ms in sessions for m in ms]
                        if all_motors[register_index].get_register_u32(10,timeout_ms=50)!=int(Mode.MIT):raise RuntimeError('CAN 控制模式发生改变')
                        register_index=(register_index+1)%len(all_motors);last_register=began
                    kp=np.array(cfg['kp']);kd=np.array(cfg['kd']);tau=kp*(target-q)-kd*velocity
                    for i in range(0,len(q),7):tau[i:i+6]+=pin.computeGeneralizedGravity(model,model.createData(),model_q(q[i:i+7]))[:6]
                    tau=np.clip(tau,-np.array(cfg['torque_limits_nm']),np.array(cfg['torque_limits_nm']))
                    for motor,t in zip([m for _,_,ms in sessions for m in ms],tau):motor.send_mit(0,0,0,0,float(t))
                    if began-last_publish>1/30:
                        last_publish=began
                        for index,(side,_,_) in enumerate(sessions):publisher.send(cfg['arms'][index]['port'],q[index*7:index*7+6].tolist(),[s.torq for s in last_states[index*7:index*7+6]],gripper=float(q[index*7+6]))
                except Exception as exc:
                    fault=str(exc);armed=False
                    if last_states:target=np.array([s.pos for s in last_states])
                    # Failed communication cannot be concealed by cached zeroes.
            if incoming.poll(0 if cfg else .2):
                try:sequence,method,p=incoming.recv()
                except EOFError:sequence,method,p=0,'close',{}
                try:
                    if method=='init':
                        if cfg is not None:raise RuntimeError('CAN 已初始化')
                        size=7*len(p['arms'])
                        p.setdefault('kp',[float(j['MIT']['kp']) for j in hardware['joints']]*len(p['arms']))
                        p.setdefault('kd',[float(j['MIT']['kd']) for j in hardware['joints']]*len(p['arms']))
                        for key in ('kp','kd','torque_limits_nm'):array(p.get(key),(size,),key)
                        if np.any(np.asarray(p['torque_limits_nm'])<=0):raise RuntimeError('请设置每个电机经确认的安全力矩上限')
                        array(p.get('joint_limits_rad'),(size,2),'关节限位')
                        for arm in p['arms']:
                            bus=Controller.from_dm_serial(arm['port'],921600);motors=[];sessions.append((arm['side'],bus,motors))
                            for j in hardware['joints']:motors.append(bus.add_damiao_motor(j['motor_id'],j['feedback_id'],j['model']))
                        for attempt in range(30):
                            try:last_states=states();break
                            except RuntimeError:
                                if attempt==29:raise
                                time.sleep(.03)
                        target=np.array([s.pos for s in last_states])
                        if np.max(abs(target-array(p['seed_rad'],(size,),'预演起始姿态')))>.1:raise RuntimeError('机械臂姿态已改变，请重新预演')
                        cfg=p
                        # Init targets are measured, gravity is computed before enable.
                        tau=np.zeros(size)
                        for i in range(0,size,7):tau[i:i+6]=pin.computeGeneralizedGravity(model,model.createData(),model_q(target[i:i+7]))[:6]
                        if np.any(abs(tau)>np.asarray(cfg['torque_limits_nm'])):raise RuntimeError('静态重力补偿超过已确认的力矩上限')
                        if time.monotonic()-float(Path(cfg['heartbeat_path']).read_text())>1.5:raise RuntimeError('初始化前没有有效浏览器心跳，未允许使能')
                        if np.any(target<np.asarray(cfg['joint_limits_rad'])[:,0]) or np.any(target>np.asarray(cfg['joint_limits_rad'])[:,1]):raise RuntimeError('初始化实测姿态超出限位')
                        for _,bus,motors in sessions:
                            for motor in motors:ensure_mode_checked(motor,Mode.MIT)
                        for m,t in zip([m for _,_,ms in sessions for m in ms],tau):m.send_mit(0,0,0,0,float(t));m.set_can_timeout_ms(500);m.enable()
                        initialized=True;web_at=command_at=time.monotonic();value=snapshot()
                    elif method=='heartbeat':web_at=time.monotonic();value=True
                    elif method=='state':value=snapshot()
                    elif method=='hold':
                        armed=False;target=np.array([s.pos for s in last_states]);value=snapshot()
                    elif method=='target':
                        if fault:raise RuntimeError(fault+'；请停止并检查后重新初始化')
                        if time.monotonic()-web_at>1.5:raise RuntimeError('缺少实时浏览器心跳')
                        q=np.array([s.pos for s in last_states]);next_q=bounded_target(q,p['joints'],cfg['joint_limits_rad'],cfg['max_delta_rad'])
                        if np.max(abs(next_q-target))>cfg['max_velocity_rad_s']*max(.02,time.monotonic()-command_at)*1.5:raise RuntimeError('目标速度超过限制')
                        target=next_q;command_at=time.monotonic();armed=True;value=True
                    elif method=='ik':
                        i=0 if p['side']=='left' else 7;seed=model_q([s.pos for s in last_states][i:i+7])
                        T=pin.SE3(pin.rpy.rpyToMatrix(*p['rpy']),np.asarray(p['xyz']))
                        solved=solve_ik(model,model.createData(),frame,T,seed,IKParams(),controlled_joints=6)
                        if not solved.success:raise RuntimeError('官方 DM 模型 IK 未收敛')
                        value={'joints':solved.q[:6].tolist(),'error':float(solved.error)}
                    elif method=='close':
                        closing=True;value=True
                    else:raise RuntimeError('未知 CAN 请求')
                    outgoing.send((sequence,True,value))
                    if closing:break
                except Exception as exc:
                    outgoing.send((sequence,False,str(exc)))
                    if method=='init':break
            time.sleep(max(0,.02-(time.monotonic()-began)))
    finally:
        # Explicit handoff: seed the firmware position loop at the measured pose.
        # No homing or zero rewrite occurs while retiring the CAN owner.
        for _,bus,motors in sessions:
            for motor in motors:
                try:
                    state=motor.get_state()
                    if initialized and state is not None:
                        ensure_mode_checked(motor,Mode.POS_VEL)
                        motor.send_pos_vel(state.pos,.1);motor.set_can_timeout_ms(600000)
                except Exception:pass
                try:motor.close()
                except Exception:pass
            try:bus.close()
            except Exception:pass


if __name__=='__main__':main()
