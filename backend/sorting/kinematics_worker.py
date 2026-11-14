"""Official DM URDF FK/IK only; no actuator or motorbridge import."""
import sys
from pathlib import Path
import numpy as np
from .ipc import worker_pipes
from .geometry import apply,transform,inside,SortingError


def main():
    sdk=Path(__file__).resolve().parents[2]/'rebot_grasp/sdk/reBotArm_control_py';sys.path.insert(0,str(sdk))
    import pinocchio as pin
    from reBotArm_control_py.kinematics.robot_model import load_robot_model
    from reBotArm_control_py.kinematics.inverse_kinematics import solve_ik,IKParams
    model=load_robot_model(str(sdk/'urdf/DM/urdf/ReBot_Arm_DM.urdf'));data=model.createData();frame=model.getFrameId('end_link')
    def pad(q):
        a=np.zeros(model.nq);a[:6]=q;return a
    def fk(q):
        pin.forwardKinematics(model,data,pad(q));pin.updateFramePlacements(model,data)
        return data.oMf[frame].translation.copy()
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
                value={'joints':result.q[:6].tolist(),'error':float(result.error)}
            elif method=='validate-path':
                c=p['calibration']
                for ratio in np.linspace(0,1,31):
                    q=(1-ratio)*np.asarray(p['from'])+ratio*np.asarray(p['to']);world=[]
                    for side,index in [('left',0),('right',7)]:
                        point=apply(np.linalg.inv(transform(c['arms'][side]['base_from_workspace'])),[fk(q[index:index+6])])[0]
                        if not inside(point,c['workspace_bounds']):raise SortingError('插补路径超出标定工作区')
                        if np.dot(c['table_plane'][:3],point)+c['table_plane'][3]<.005:raise SortingError('插补路径穿过桌面')
                        world.append(point)
                    if np.linalg.norm(world[0]-world[1])<.12:raise SortingError('双臂末端间距不足')
                value={'valid':True,'samples':31}
            else:raise SortingError('未知运动学请求')
            outgoing.send((sequence,True,value))
        except Exception as exc:outgoing.send((sequence,False,str(exc)))


if __name__=='__main__':main()
