"""Read, release, or explicitly persist selected B601-DM motor zero positions."""
import json
import math
from pathlib import Path
import sys
import time


def adjust_zero(bus, motors, action, selected, *, sleep=time.sleep, publish=None):
    if action not in ('read','release','set-zero') or not selected or any(i not in motors for i in selected):
        raise ValueError('无效的操作或关节选择')
    def read():
        for motor in motors.values():motor.request_feedback()
        sleep(.03);bus.poll_feedback_once()
        states={i:m.get_state() for i,m in motors.items()}
        if any(s is None or not math.isfinite(s.pos) for s in states.values()):
            raise RuntimeError('关节反馈缺失，未允许设零')
        if publish:publish([states[i].pos for i in range(1,7)],[states[i].torq for i in range(1,7)])
        return states
    states=read()
    before={i:math.degrees(s.pos) for i,s in states.items()}
    if action=='release':
        # Release the entire selected follower so manual placement is possible.
        for motor in motors.values():motor.disable()
        for _ in range(20):
            states=read()
            if all(s.status_code==0 for s in states.values()):break
        else:raise RuntimeError('未确认全部电机失能，请勿手动摆动')
    if action=='set-zero':
        if any(states[i].status_code != 0 for i in motors):
            raise RuntimeError('所选从臂尚未全部失能，请先托住机械臂并点击“释放力矩”')
        for i in selected:
            # Fresh reply, not merely a cached zero from an interrupted session.
            motors[i].get_register_u32(10,timeout_ms=1500)
        completed=[];zeroed=[]
        try:
            for i in selected:
                motors[i].set_zero_position()
                zeroed.append(i)
                for _ in range(25):
                    states=read()
                    if abs(states[i].pos)<.02:break
                else:raise RuntimeError(f'关节 {i} 设零后读数没有接近 0')
                motors[i].store_parameters()
                sleep(.1)
                motors[i].get_register_u32(10,timeout_ms=1500)
                completed.append(i)
        except Exception as error:
            raise RuntimeError(f'零点未全部完成；已执行设零 {zeroed}，已确认保存 {completed}，其余结果不确定，请核查：{error}') from error
    states=read()
    if action=='set-zero' and any(abs(states[i].pos)>=.02 for i in selected):
        raise RuntimeError('持久化后零点回读未通过，未报告成功')
    return {'ok':True,'action':action,'selected':selected,'before_deg':before,
            'joints':[{'id':i,'degrees':math.degrees(s.pos),'disabled':s.status_code==0} for i,s in states.items()]}


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
                           publish=lambda q,t:publisher.send(payload['port'],q,t))
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
        print(f'[零点调整失败] {error}',flush=True);return 1
    finally:
        for motor in motors.values():
            try:motor.close()
            except Exception:pass
        if bus:
            try:bus.close()
            except Exception:pass


if __name__=='__main__':raise SystemExit(main())
