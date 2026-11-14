"""Read, release, or explicitly persist selected B601-DM motor zero positions."""
import json
import math
from pathlib import Path
import sys
import time


class FeedbackUnavailable(RuntimeError):
    def __init__(self,message,states,missing):
        super().__init__(message);self.states=states;self.missing=missing


def feedback_rows(states):
    return [{'id':i,'degrees':math.degrees(st.pos),'disabled':st.status_code==0,
             'status_code':int(st.status_code),'reported_motor_id':getattr(st,'can_id',None),
             'feedback_can_id':getattr(st,'arbitration_id',None)} for i,st in sorted(states.items())]


def collect_feedback(bus,motors,required,*,sleep=time.sleep,clock=time.monotonic,
                     timeout_s=2.5,max_attempts=80,log=None):
    """Read-only, paced polling of actual feedback, including cold serial bridges.

    No enable, mode switch, target, zero or parameter-store operation is used to
    provoke a response. Only required joints decide whether the read succeeds.
    """
    started=clock();deadline=started+timeout_s;errors={};states={}
    for attempt in range(max_attempts):
        for i in sorted(required):
            try:
                motors[i].request_feedback()
                sleep(.002)
                bus.poll_feedback_once()
            except Exception as exc:errors[i]=str(exc)
        # poll_feedback_once may process only one pending CAN frame per call.
        for _ in range(max(2,len(motors))):
            try:bus.poll_feedback_once()
            except Exception as exc:errors['bus']=str(exc);break
        states={}
        for i,motor in motors.items():
            try:
                value=motor.get_state()
                if value is not None and math.isfinite(float(value.pos)):
                    states[i]=value
            except Exception as exc:errors[i]=str(exc)
        missing=sorted(set(required)-states.keys())
        if not missing:return states
        if log and attempt==9:
            # Parameter reads are non-mutating and help distinguish a state
            # parser/feedback-ID mismatch from an entirely silent CAN link.
            probe=missing[0]
            for rid,label in ((10,'控制模式'),(7,'反馈ID')):
                try:
                    value=motors[probe].get_register_u32(rid,timeout_ms=200)
                    log(f'[零点读取诊断] CAN {probe} {label}={value}',flush=True)
                except Exception as exc:
                    errors[probe]=f'只读寄存器 {rid} 未响应：{exc}'
                    log(f'[零点读取诊断] CAN {probe} '+errors[probe],flush=True)
        if log and attempt in (0,9,29,59):
            log('[零点读取] 等待实测反馈 · 缺少 CAN ID '+','.join(map(str,missing))+f' · 第 {attempt+1} 次',flush=True)
        if clock()>=deadline:break
        sleep(.025)
    missing=sorted(set(required)-states.keys())
    detail='；'.join(f'{i}: {errors[i]}' for i in missing if i in errors)
    if errors.get('bus'):detail+='；总线轮询：'+errors['bus']
    raise FeedbackUnavailable('所选关节反馈缺失（CAN ID '+','.join(map(str,missing))+
                       f'，已重试 {attempt+1} 次 / {clock()-started:.2f}s）'+
                       '。请检查该臂电机供电、CAN 接线 / ID 与串口映射；未发送使能、运动或设零指令'+('；'+detail if detail else ''),states,missing)


def adjust_zero(bus,motors,action,selected,*,sleep=time.sleep,publish=None,clock=time.monotonic,log=None):
    if action not in ('read','release','set-zero') or not selected or any(isinstance(i,bool) or i not in motors for i in selected):
        raise ValueError('无效的操作或关节选择')
    # Reading one joint must not depend on an unrelated motor being online.
    # Mutation still requires the entire follower to be observed and disabled.
    required=set(selected) if action=='read' else set(motors)
    def read():
        states=collect_feedback(bus,motors,required,sleep=sleep,clock=clock,log=log)
        if publish and all(i in states for i in range(1,7)):
            try:publish([states[i].pos for i in range(1,7)],[states[i].torq for i in range(1,7)])
            except Exception:pass
        return states
    states=read();before={i:math.degrees(st.pos) for i,st in states.items()}
    if action=='release':
        for motor in motors.values():motor.disable()
        for _ in range(20):
            states=read()
            if all(st.status_code==0 for st in states.values()):break
        else:raise RuntimeError('未确认全部电机失能，请勿手动摆动')
    if action=='set-zero':
        if any(states[i].status_code!=0 for i in motors):
            raise RuntimeError('所选从臂尚未全部失能，请先托住机械臂并点击“释放力矩”')
        for i in selected:motors[i].get_register_u32(10,timeout_ms=1500)
        completed=[];zeroed=[]
        try:
            for i in selected:
                motors[i].set_zero_position();zeroed.append(i)
                for _ in range(25):
                    states=read()
                    if abs(states[i].pos)<.02:break
                else:raise RuntimeError(f'关节 {i} 设零后读数没有接近 0')
                motors[i].store_parameters();sleep(.1)
                motors[i].get_register_u32(10,timeout_ms=1500);completed.append(i)
        except Exception as error:
            raise RuntimeError(f'零点未全部完成；已执行设零 {zeroed}，已确认保存 {completed}，其余结果不确定，请核查：{error}') from error
    states=read()
    if action=='set-zero' and any(abs(states[i].pos)>=.02 for i in selected):
        raise RuntimeError('持久化后零点回读未通过，未报告成功')
    return {'ok':True,'action':action,'selected':selected,'before_deg':before,
            'missing_joints':sorted(set(motors)-states.keys()),
            'joints':feedback_rows(states)}


def main():
    from motorbridge import Controller
    from telemetry_client import FeedbackPublisher
    import yaml
    payload=json.loads(sys.argv[1]);bus=None;motors={}
    try:
        hardware=yaml.safe_load((Path(__file__).resolve().parents[1]/'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml').read_text())
        bus=Controller.from_dm_serial(payload['port'],921600)
        for j in hardware['joints']:
            motors[int(j['motor_id'])]=bus.add_damiao_motor(j['motor_id'],j['feedback_id'],j['model'])
        publisher=FeedbackPublisher()
        result=adjust_zero(bus,motors,payload['action'],payload['joints'],
                           publish=lambda q,t:publisher.send(payload['port'],q,t,gripper=motors[7].get_state().pos if motors[7].get_state() else None),log=print)
        if payload['action']=='set-zero' and set(payload['joints'])==set(range(1,8)):
            # Match the official calibration metadata, without connect()/enable().
            from lerobot_robot_seeed_b601.seeed_b601_dm_follower import SeeedB601DMFollower
            from lerobot_robot_seeed_b601.config_seeed_b601_dm_follower import SeeedB601DMFollowerConfig
            from lerobot.motors import MotorCalibration
            robot=SeeedB601DMFollower(SeeedB601DMFollowerConfig(port=payload['port'],id=payload['id'],can_adapter='damiao'))
            robot.calibration={name:MotorCalibration(id=ids[0],drive_mode=0,homing_offset=0,range_min=-90,range_max=90)
                               for name,ids in robot.config.motor_can_ids.items()}
            if robot.calibration_fpath.exists():
                import shutil
                shutil.copy2(robot.calibration_fpath,str(robot.calibration_fpath)+'.before-zero-'+str(time.time_ns()))
            robot._save_calibration()
        result.update(side=payload['side'],port=payload['port'],timestamp=time.time())
        print('REBOT_ZERO_RESULT '+json.dumps(result,ensure_ascii=False),flush=True)
        return 0
    except Exception as error:
        detail={'ok':False,'error':str(error),'side':payload.get('side'),'port':payload.get('port'),'timestamp':time.time()}
        if isinstance(error,FeedbackUnavailable):detail.update(joints=feedback_rows(error.states),missing_joints=error.missing)
        print('REBOT_ZERO_ERROR '+json.dumps(detail,ensure_ascii=False),flush=True)
        print(f'[零点调整失败] {error}',flush=True);return 1
    finally:
        for motor in motors.values():
            try:motor.close()
            except Exception:pass
        if bus:
            try:bus.close()
            except Exception:pass


if __name__=='__main__':raise SystemExit(main())
