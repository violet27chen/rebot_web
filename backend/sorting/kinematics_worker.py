"""Official DM URDF FK/IK only; no actuator or motorbridge import."""
import sys
from pathlib import Path
import numpy as np
from .ipc import worker_pipes
from .geometry import apply,transform,inside,arm_chain_min_distance,SortingError


def main():
    sdk=Path(__file__).resolve().parents[2]/'rebot_grasp/sdk/reBotArm_control_py';sys.path.insert(0,str(sdk))
    import pinocchio as pin
    from reBotArm_control_py.kinematics.robot_model import load_robot_model
    from reBotArm_control_py.kinematics.inverse_kinematics import solve_ik,IKParams
    model=load_robot_model(str(sdk/'urdf/DM/urdf/ReBot_Arm_DM.urdf'));data=model.createData();frame=model.getFrameId('end_link')
    chain_frames=[model.getFrameId(n) for n in ('base_link','link2','link3','link4','link5','link6','end_link')]
    def pad(q):
        a=np.zeros(model.nq);a[:6]=q;return a
    def fk(q):
        pin.forwardKinematics(model,data,pad(q));pin.updateFramePlacements(model,data)
        return data.oMf[frame].translation.copy()
    def fk_chain(q):
        pin.forwardKinematics(model,data,pad(q));pin.updateFramePlacements(model,data)
        return np.array([data.oMf[f].translation.copy() for f in chain_frames])
    incoming,outgoing=worker_pipes()
    while True:
        try:sequence,method,p=incoming.recv()
        except EOFError:return
        try:
            if method=='close':outgoing.send((sequence,True,True));return
            if method=='fk':value={'xyz':fk(p['joints']).tolist()}
            elif method=='ik':
                target=pin.SE3(pin.rpy.rpyToMatrix(*p['rpy']),np.asarray(p['xyz']))
                result=solve_ik(model,model.createData(),frame,target,pad(p['joints']),IKParams(),controlled_joints=6)
                if not result.success or np.linalg.norm(fk(result.q[:6])-p['xyz'])>.005:raise SortingError('官方 DM 模型 IK/回算未达到目标精度')
                if p.get('check_rot'):
                    pin.forwardKinematics(model,data,pad(result.q[:6]));pin.updateFramePlacements(model,data)
                    Rt=pin.rpy.rpyToMatrix(*p['rpy']);Ra=data.oMf[frame].rotation
                    if np.arccos(np.clip((np.trace(Ra.T@Rt)-1)/2,-1,1))>float(p.get('max_rot_rad',.1)):raise SortingError('IK 解未达到要求的爪头朝向')
                value={'joints':result.q[:6].tolist(),'error':float(result.error)}
            elif method=='validate-path':
                c=p['calibration'];min_dist=float(p.get('min_arm_distance_m',.12))
                for ratio in np.linspace(0,1,31):
                    q=(1-ratio)*np.asarray(p['from'])+ratio*np.asarray(p['to']);chains=[]
                    for side,index in [('left',0),('right',7)]:
                        T_ws_base=np.linalg.inv(transform(c['arms'][side]['base_from_workspace']))
                        chain_ws=apply(T_ws_base,fk_chain(q[index:index+6]))
                        point=chain_ws[-1]
                        if not inside(point,c['workspace_bounds']):raise SortingError('插补路径超出标定工作区')
                        for pt in chain_ws[2:]:
                            if np.dot(c['table_plane'][:3],pt)+c['table_plane'][3]<.005:raise SortingError('插补路径穿过桌面')
                        chains.append(chain_ws)
                    dist=arm_chain_min_distance(chains[0],chains[1])
                    if dist<min_dist:raise SortingError(f'双臂防撞预检未通过：双臂连杆最小间距 {dist*100:.1f}cm 小于安全间距 {min_dist*100:.0f}cm')
                value={'valid':True,'samples':31}
            else:raise SortingError('未知运动学请求')
            outgoing.send((sequence,True,value))
        except Exception as exc:outgoing.send((sequence,False,str(exc)))


if __name__=='__main__':main()
