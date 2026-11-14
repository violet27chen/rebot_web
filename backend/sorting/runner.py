"""One bounded package cycle. All hardware calls go to the independent CAN owner."""
import base64
import json
import signal
import sys
import time
import urllib.request
import cv2
import numpy as np
from .ipc import Worker
from .geometry import SortingError,inside,verify_push
from .skills import raw_observation,action_to_raw


def main():
    p=json.loads(sys.argv[1]);url=p['url'];token=p['token'];settings=p['settings'];proposal=p['proposal'];stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    def call(method,payload=None,timeout=20):
        request=urllib.request.Request(url+'/api/sorting/internal',data=json.dumps({'token':token,'method':method,'payload':payload or {}}).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as r:result=json.load(r)
        except urllib.error.HTTPError as exc:
            detail=json.loads(exc.read());raise SortingError(detail.get('error') or str(exc))
        if not result.get('ok'):raise SortingError(result.get('error','自动操作失败'))
        return result['value']
    def check():
        if stopped:raise InterruptedError('用户已停止自动操作')
    def phase(name):
        print('[分拣] '+name,flush=True);call('phase',{'phase':name})
    def state():
        value=call('state')
        if value['fault']:raise SortingError(value['fault'])
        return value
    def move(goal):
        start=np.asarray(state()['joints']);goal=np.asarray(goal)
        duration=max(1.,np.max(abs(goal-start))*1.875/settings['safety']['max_velocity_rad_s'])
        began=time.monotonic()
        while True:
            check();s=min(1.,(time.monotonic()-began)/duration);smooth=10*s**3-15*s**4+6*s**5
            call('target',{'joints':(start+(goal-start)*smooth).tolist()},timeout=3)
            if s>=1:break
            time.sleep(.04)
        deadline=time.monotonic()+5;settled=0
        while time.monotonic()<deadline:
            check();actual=state()
            settled=settled+1 if np.max(abs(np.asarray(actual['joints'])-goal))<.035 else 0
            call('target',{'joints':goal.tolist()},timeout=3)
            if settled>=4:call('hold');return
            time.sleep(.04)
        raise SortingError('实测末端关节未到达预演目标，未启动 ACT')
    def associate(scene,before,max_distance):
        matches=[t for t in scene['targets'] if t.get('valid') and t['name']==before['name'] and np.linalg.norm(np.array(t['xyz_m'])-before['xyz_m'])<max_distance]
        if len(matches)!=1:raise SortingError('目标丢失或出现多个同类包裹，已停止继续动作')
        return matches[0]
    inference=Worker(sys.executable,'backend.sorting.inference_worker',p['root'])
    def run_act(skill):
        phase('ACT_'+skill['name'])
        actual=state()
        if np.max(abs(np.asarray(actual['joints'])-skill['start_pose_rad']))>float(skill.get('start_tolerance_rad',.2)):raise SortingError('实际接近姿态未落入技能训练起始范围')
        duration=float(skill.get('duration_s',5))
        if not .1<=duration<=30:raise SortingError('ACT 单次技能时长应在 0.1～30 秒')
        end=time.monotonic()+duration
        while time.monotonic()<end:
            check();actual=state();frames=call('frames',{'camera_map':skill['camera_map']},timeout=8)
            observation={'observation.state':np.asarray(raw_observation(actual['joints'],skill['state_names']),np.float32)}
            for feature,item in frames.items():
                image=cv2.imdecode(np.frombuffer(base64.b64decode(item['png']),np.uint8),cv2.IMREAD_COLOR)
                if image is None:raise SortingError('ACT 图像解码失败')
                shape=skill['visual'][feature]['shape']
                if list(image.shape)!=[shape[1],shape[2],3]:raise SortingError('ACT 图像尺寸与训练时不同，未自动裁剪或缩放')
                observation[feature]=cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
            timestamps=[v['timestamp'] for v in frames.values()]
            if max(timestamps)-min(timestamps)>.15:raise SortingError('ACT 多相机时间偏差超过 150ms')
            values=inference.call('act',{'observation':observation,'device':settings['device'],'task':skill['task']},timeout=3)
            if time.time()-min(v['timestamp'] for v in frames.values())>.5:raise SortingError('ACT 推理对应图像已过期，未发送该动作')
            raw=action_to_raw(values,skill['action_names']);call('target',{'joints':raw},timeout=3)
            time.sleep(.04)
        call('hold')

    def cycle_mode():
        from .cycle import run_cycle
        cfg=proposal.get('cycle') or {};flip=proposal['flip_skill'];loaded=[None]
        class IO:
            def check(self):check()
            def phase(self,name):phase(name)
            def scene(self):return call('scene',timeout=60)
            def pick(self,scene,previous):
                valid=[t for t in scene['targets'] if t.get('valid')]
                if previous is None:
                    names=[t for t in valid if t['name']==proposal['target']['name']]
                    near=sorted(names,key=lambda t:np.linalg.norm(np.array(t['xyz_m'])-proposal['target']['xyz_m']))
                    return near[0] if near and np.linalg.norm(np.array(near[0]['xyz_m'])-proposal['target']['xyz_m'])<.05 else None
                near=[t for t in valid if t['name']==previous['name'] and np.linalg.norm(np.array(t['xyz_m'])-previous['xyz_m'])<.12]
                if len(near)>1:raise SortingError('出现多个同类包裹，已停止继续动作')
                return near[0] if near else None
            def plan(self,scene,target,action):return call('plan',{'scene_id':scene['scene_id'],'target_id':target['id'],'action':action},timeout=90)
            def execute(self,plan):
                for point in plan['paths']:move(point['joints'])
            def scan(self):return call('label',timeout=15)
            def act_flip(self,target):
                if loaded[0]!=flip['checkpoint']:
                    inference.call('act-load',{'path':flip['checkpoint'],'device':settings['device']},timeout=120);loaded[0]=flip['checkpoint']
                goal=flip['start_pose_rad'];call('validate',{'from':state()['joints'],'to':goal},timeout=60);move(goal);run_act(flip)
            def verify_after_push(self,target):
                phase('VERIFY_PUSH_LEFT');fresh=call('scene',timeout=60)
                near=[t for t in fresh['targets'] if t.get('valid') and t['name']==target['name'] and np.linalg.norm(np.array(t['xyz_m'])-target['xyz_m'])<float(cfg.get('left_push_m',settings['safety']['max_push_m']))+.1]
                if len(near)>1:raise SortingError('推拢后出现多个同类包裹，已停止')
                return near[0] if near else None
        result=run_cycle(IO(),cfg)
        print('[分拣结果] '+json.dumps(result,ensure_ascii=False),flush=True)
    try:
        if proposal.get('mode')=='cycle':
            phase('RECHECK_RGBD');call('init',timeout=90);cycle_mode();return
        skill=proposal['skill'];phase('PREPARE_ACT')
        inference.call('act-load',{'path':skill['checkpoint'],'device':settings['device']},timeout=120)
        check();phase('RECHECK_RGBD')
        fresh=call('scene',timeout=60);target=associate(fresh,proposal['target'],.025)
        if abs(target['height_m']-proposal['target']['height_m'])>.025:raise SortingError('包裹高度已变化，请重新预演')
        call('init',timeout=90);plan=proposal
        for attempt in range(4):
            check();phase('SKILL_PUSH' if plan['push'] else 'APPROACH_TARGET')
            for point in plan['paths']:move(point['joints'])
            if not plan['push']:break
            phase('VERIFY_PUSH');fresh=call('scene',timeout=60)
            moved=associate(fresh,target,float(settings['safety']['max_push_m'])+.07)
            result=verify_push(target,moved,skill['training_bounds'])
            print('[推拢反馈] '+json.dumps(result,ensure_ascii=False),flush=True)
            if not result['reached'] and attempt==2:raise SortingError('三次推拢后仍未到达训练区域，已停止重试')
            target=moved
            planned=call('plan',{'scene_id':fresh['scene_id'],'target_id':target['id']},timeout=90)
            plan={'paths':planned['paths'],'push':planned['push']}
        else:raise SortingError('推拢次数已达上限')
        run_act(skill)
        call('hold');phase('VERIFY_AFTER_ACT');after=call('scene',timeout=60)
        print('[分拣结果] '+json.dumps({'skill':skill['name'],'targets':after['targets'],'note':'技能时长结束，视觉结果供后续业务状态判断'},ensure_ascii=False),flush=True)
    finally:
        try:call('hold',timeout=3)
        except Exception:pass
        inference.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:print('[分拣停止] '+str(exc),flush=True);raise SystemExit(1)
