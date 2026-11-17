#!/usr/bin/env python3
"""双臂快递分拣执行入口。默认只检查配置；--execute 后仍需在控制台确认开始。

python run_sorting.py --dry-run
python run_sorting.py --simulate
python run_sorting.py --config sorting.json --check
python run_sorting.py --config sorting.json --execute

业务与进程入口在此文件；关联的相机、ACT、安全代码和官方硬件模型随文件夹交付。
不导入 rebot_web，不需要启动原平台。物理控制仅支持 Ubuntu。
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import sys
import threading
import time
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
JOINTS = ['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_yaw','wrist_roll','gripper']
ACTION_NAMES = [side+'_'+joint+'.pos' for side in ('left','right') for joint in JOINTS]
SKILLS = ['unstack','push','flip_box','flip_bag','flatten','route_box','route_bag']


def load_settings(path):
    path = Path(path).expanduser().resolve()
    cfg = json.loads(path.read_text('utf-8'))
    if not isinstance(cfg, dict): raise ValueError('配置必须为 JSON 对象')
    cfg = copy.deepcopy(cfg)
    cfg['_root'] = str(path.parent)
    cfg.setdefault('fps',30); cfg.setdefault('control_hz',100)
    cfg.setdefault('heartbeat_s',1.); cfg.setdefault('camera_max_age_s',1.)
    cfg.setdefault('skill_seconds',8.); cfg.setdefault('max_skill_attempts',3)
    cfg.setdefault('max_parcels',10); cfg.setdefault('stable_frames',3)
    cfg.setdefault('skill_entry_tolerance_deg',8.)
    cfg.setdefault('execution_mode','complete');cfg.setdefault('max_run_s',600.)
    if cfg['execution_mode'] not in ('complete','skills'):raise ValueError('execution_mode 只能为complete或skills')
    cfg.setdefault('workspace',[.15,.15,.85,.85]); cfg.setdefault('models',{})
    cfg.setdefault('rules',{}); cfg.setdefault('device','cuda')
    inference_settings(cfg)
    cfg.setdefault('max_speed_rad_s',.15); cfg.setdefault('max_acceleration_rad_s2',.2)
    cfg.setdefault('temperature_limit_c',80.); cfg.setdefault('tracking_limit_deg',20.)
    for key in ('fps','control_hz','max_parcels','max_skill_attempts','stable_frames'):
        value=cfg[key]
        if isinstance(value,bool) or not isinstance(value,int) or value<1:raise ValueError(key+' 必须为正整数')
    if not 10<=cfg['control_hz']<=100 or not 1<=cfg['fps']<=60:raise ValueError('控制频率需在10~100Hz，策略频率需在1~60Hz')
    for key in ('heartbeat_s','camera_max_age_s','skill_seconds','max_speed_rad_s','max_acceleration_rad_s2','temperature_limit_c','tracking_limit_deg','skill_entry_tolerance_deg'):
        value=float(cfg[key])
        if not math.isfinite(value) or value<=0:raise ValueError(key+' 必须为有限正数')
    if cfg['max_speed_rad_s']>.3 or cfg['max_acceleration_rad_s2']>.4:raise ValueError('初始执行版本限制速度<=0.3rad/s、加速度<=0.4rad/s²')
    workspace=cfg['workspace']
    if len(workspace)!=4 or not all(isinstance(v,(float,int)) and math.isfinite(v) and 0<=v<=1 for v in workspace) or not(workspace[0]<workspace[2] and workspace[1]<workspace[3]):raise ValueError('workspace 必须为归一化图像矩形[x1,y1,x2,y2]')
    cameras=cfg.get('cameras',[])
    if len(cameras)!=3 or len({c['name'] for c in cameras})!=3:raise ValueError('需要明确配置三个不同名称的RGB相机')
    from rebot_sort_support.capture import camera_source
    for c in cameras:c['source']=camera_source(str(c['source']))
    if len({os.path.realpath(c['source']) for c in cameras})!=3:raise ValueError('三个相机不能指向同一设备')
    if cfg.get('top_camera') not in {c['name'] for c in cameras}:raise ValueError('top_camera 必须为已配置相机键')
    zones=cfg.get('exit_regions',{})
    if set(zones)!={'box','bag'}:raise ValueError('必须明确配置盒子和袋子的图像出料区域 exit_regions')
    for area in zones.values():
        if len(area)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) and 0<=v<=1 for v in area) or not(area[0]<area[2] and area[1]<area[3]):raise ValueError('出料区域必须为归一化图像矩形')
    ports=cfg.get('ports',{})
    if set(ports)!={'left','right'} or any(not str(p).startswith('/dev/') for p in ports.values()) or len({os.path.realpath(p) for p in ports.values()})!=2:raise ValueError('左右从臂必须为两个不同的Ubuntu设备路径')
    if cfg.get('action_names',ACTION_NAMES)!=ACTION_NAMES:raise ValueError('动作顺序必须与本交付的14维B601数据契约一致')
    return cfg


def inference_settings(cfg):
    """Shared model-only settings for the runner and its no-hardware check."""
    values = cfg.get('inference', {})
    if not isinstance(values, dict):
        raise ValueError('inference 必须为JSON对象')
    values = dict(values)
    if 'n_action_steps' in values:
        values['chunk_size'] = values['n_action_steps']
    result = {'chunk_size': 0, 'flow_matching_steps': 0, 'prefetch_ratio': .6,
              'max_age_s': 1.5, 'wait_timeout_s': 2, 'max_step_deg': 1.}
    for key, maximum in [('chunk_size', 1000), ('flow_matching_steps', 50)]:
        value = values.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f'inference.{key} 必须为0–{maximum}的整数，0沿用模型')
        result[key] = value
    return result


def resolve(cfg, value):
    path=Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path(cfg['_root'])/path).resolve()


def load_act_config(path,device):
    # A standalone interpreter has not imported LeRobot's policy factory yet.
    # Importing ACTConfig registers the 'act' choice before parsing config.json.
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
    from lerobot.configs.policies import PreTrainedConfig
    config=PreTrainedConfig.from_pretrained(path)
    if not isinstance(config,(ACTConfig,SmolVLAConfig)):raise ValueError('执行入口支持 ACT / SmolVLA 模型')
    config.device=device;config.pretrained_path=path
    return config


def model_plan(cfg, *, required=True):
    """Validate features before allocating camera buffers or opening a motor port."""
    camera_keys={c['name'] for c in cfg['cameras']}
    paths, feature_shape = {}, None
    for skill in (['complete'] if cfg['execution_mode']=='complete' else SKILLS):
        source=cfg.get('models',{}).get(skill)
        rule=cfg.get('rules',{}).get(skill)
        if rule:
            route=json.loads(resolve(cfg,rule).read_text())
            if route.get('action_names')!=ACTION_NAMES or not route.get('points'):raise ValueError(skill+' 规则轨迹缺少完整动作契约/点列')
            previous=-1.
            for point in route['points']:
                stamp=float(point['t']); action=point['action']
                if not math.isfinite(stamp) or stamp<=previous or len(action)!=14 or not all(math.isfinite(float(v)) for v in action):raise ValueError(skill+' 轨迹时间或14维角度无效')
                previous=stamp
            if route['points'][0]['t']!=0:raise ValueError('规则轨迹必须从t=0开始')
            if route.get('validated_on_hardware') is not True:raise ValueError('规则轨迹必须经过实际工作区验证，不能使用示例角度')
            continue
        if not source:
            if required and skill!='flatten':raise ValueError('尚未配置ACT模型或已验证规则轨迹：'+skill)
            continue
        p=resolve(cfg,source)
        if not (p/'config.json').exists():
            for candidate in [p/'pretrained_model',p/'checkpoints/last/pretrained_model']:
                if (candidate/'config.json').exists():p=candidate;break
        info=json.loads((p/'config.json').read_text())
        for parent in [p,*list(p.parents)[:4]]:
            ref=parent/'dataset_reference.json'
            if ref.is_file():
                reference=json.loads(ref.read_text())
                for key in ('action_names','state_names'):
                    if reference.get(key) and reference[key]!=ACTION_NAMES:raise ValueError(skill+' 数据集关节顺序与B601执行契约不一致')
                break
        if info.get('type') not in ('act','smolvla'):raise ValueError(skill+' 仅支持 ACT / SmolVLA')
        if list(info.get('output_features',{}).get('action',{}).get('shape',[]))!=[14]:raise ValueError(skill+' 动作维度必须为14')
        inputs=info.get('input_features',{})
        if list(inputs.get('observation.state',{}).get('shape',[]))!=[14]:raise ValueError(skill+' 状态维度必须为14')
        keys={k.removeprefix('observation.images.') for k in inputs if k.startswith('observation.images.')}
        if keys!=camera_keys:raise ValueError(f'{skill} 相机键不一致：模型{sorted(keys)} / 配置{sorted(camera_keys)}')
        shapes={tuple(v['shape']) for k,v in inputs.items() if k.startswith('observation.images.')}
        if len(shapes)!=1:raise ValueError('模型三路图像尺寸必须一致')
        shape=next(iter(shapes))
        if len(shape)!=3 or shape[0]!=3:raise ValueError('ACT相机必须是CHW RGB')
        if feature_shape and feature_shape!=shape:raise ValueError('技能模型间图像尺寸不同，不能共用同一观测缓存')
        feature_shape=shape
        if not (p/'model.safetensors').exists():raise ValueError('缺少模型权重：'+str(p))
        paths[skill]=str(p)
    return paths, feature_shape


def contains(box, point):
    return box[0]<=point[0]<=box[2] and box[1]<=point[1]<=box[3]


def choose_skill(scene, cfg):
    """Face-up is positively verified by a waybill code within the target parcel."""
    parcels=[p for p in scene.get('parcels',[]) if not any(contains(zone,[(p['bbox'][0]+p['bbox'][2])/2,(p['bbox'][1]+p['bbox'][3])/2]) for zone in cfg.get('exit_regions',{}).values())]
    if not parcels:return 'SCAN_WORKSPACE',None,None
    parcel=max(parcels,key=lambda p:float(p.get('score',0)))
    box=parcel['bbox']; centre=((box[0]+box[2])/2,(box[1]+box[3])/2)
    if parcel.get('stacked'):return 'SKILL_UNSTACK','unstack',parcel
    if not(contains(cfg['workspace'],box[:2]) and contains(cfg['workspace'],box[2:])):return 'SKILL_PUSH','push',parcel
    kind=parcel.get('kind')
    if kind not in ('box','bag'):return 'CLASSIFY_PRE',None,parcel
    if parcel.get('waybill'):
        return 'CLASSIFY_FINAL','route_'+kind,parcel
    if parcel.get('label_visible'):
        if kind=='bag' and (cfg['models'].get('flatten') or cfg.get('rules',{}).get('flatten')):
            return 'SCAN_BARCODE','flatten',parcel
        return 'SCAN_BARCODE',None,parcel
    return 'SKILL_FLIP_'+kind.upper(),'flip_'+kind,parcel


class Workflow:
    def __init__(self,cfg):
        self.cfg=cfg;self.skill=None;self.started=0.;self.phase='SCAN_WORKSPACE'
        self.attempts={};self.completed=0;self.stable=0;self.key=None;self.last_sequence=-1
        self.route_target=None;self.route_started=False;self.target_bbox=None
    def update(self,scene,now):
        # Repeated reads of a slow scene inference are not independent confirmations.
        if scene.get('sequence')==self.last_sequence:return None
        self.last_sequence=scene.get('sequence')
        parcels=scene.get('parcels',[])
        tracked=None
        if self.target_bbox and parcels:
            old=self.target_bbox;cx=(old[0]+old[2])/2;cy=(old[1]+old[3])/2
            candidate=min(parcels,key=lambda p:((p['bbox'][0]+p['bbox'][2])/2-cx)**2+((p['bbox'][1]+p['bbox'][3])/2-cy)**2)
            distance=math.hypot((candidate['bbox'][0]+candidate['bbox'][2])/2-cx,(candidate['bbox'][1]+candidate['bbox'][3])/2-cy)
            if distance<.2:tracked=candidate;self.target_bbox=candidate['bbox']
        selected={'parcels':[tracked]} if self.skill and tracked else scene
        phase,skill,target=choose_skill(selected,self.cfg)
        if self.skill:
            if self.skill.startswith('route_'):
                zone=self.cfg['exit_regions'][self.skill.removeprefix('route_')]
                reached=tracked and contains(zone,[(tracked['bbox'][0]+tracked['bbox'][2])/2,(tracked['bbox'][1]+tracked['bbox'][3])/2])
                if reached:
                    self.stable+=1
                    if self.stable>=self.cfg['stable_frames']:
                        self.completed+=1;self.skill=None;self.target_bbox=None;self.stable=0;self.attempts.clear()
                        return 'hold'
                else:self.stable=0
            elif tracked and skill!=self.skill:
                self.stable+=1
                if self.stable>=self.cfg['stable_frames']:
                    self.skill=None;self.stable=0;return 'hold'
            else:self.stable=0
            if now-self.started>self.cfg['skill_seconds']:
                self.skill=None;self.stable=0
                return 'hold'
            return None
        key=(skill,tuple(round(v,1) for v in target['bbox']) if target else None)
        self.stable=self.stable+1 if self.key==key else 1;self.key=key;self.phase=phase
        if skill and self.stable>=self.cfg['stable_frames']:
            attempts=self.attempts.get(skill,0)+1
            if attempts>self.cfg['max_skill_attempts']:raise RuntimeError('技能重试上限，等待人工检查：'+skill)
            self.attempts[skill]=attempts;self.skill=skill;self.started=now;self.stable=0
            self.target_bbox=target['bbox']
            if skill.startswith('route_'):self.route_target=target['bbox']
            return skill
        return None


class CompleteWorkflow:
    """One ACT handles the whole demonstration; optional YOLO verifies exits."""
    def __init__(self,cfg):
        self.cfg=cfg;self.completed=0;self.started=False;self.last_sequence=-1
        self.tracks=[];self.codes=[];self.finished_codes=set()
    def update(self,scene,now):
        if scene.get('sequence')==self.last_sequence:return None
        self.last_sequence=scene.get('sequence');self.codes=list(scene.get('codes',[]));used=set()
        for parcel in scene.get('parcels',[]):
            box=parcel['bbox'];cx=(box[0]+box[2])/2;cy=(box[1]+box[3])/2
            if parcel.get('waybill') in self.finished_codes:continue
            if any(t['done'] and now-t['last']<2 and math.hypot(t['centre'][0]-cx,t['centre'][1]-cy)<.15 for t in self.tracks):continue
            candidates=[(i,t) for i,t in enumerate(self.tracks) if i not in used and not t['done'] and now-t['last']<2]
            match=min(candidates,key=lambda pair:math.hypot(pair[1]['centre'][0]-cx,pair[1]['centre'][1]-cy),default=None)
            if match and math.hypot(match[1]['centre'][0]-cx,match[1]['centre'][1]-cy)<.2:index,track=match
            else:
                track={'centre':[cx,cy],'waybill':None,'stable':0,'done':False,'last':now};self.tracks.append(track);index=len(self.tracks)-1
            used.add(index);track['centre']=[cx,cy];track['last']=now
            if parcel.get('waybill'):track['waybill']=parcel['waybill']
            zone=self.cfg['exit_regions'].get(parcel.get('kind'))
            if zone:
                margin=.06;guard=[max(0,zone[0]-margin),max(0,zone[1]-margin),min(1,zone[2]+margin),min(1,zone[3]+margin)]
                if contains(guard,[cx,cy]) and not track['waybill']:raise RuntimeError('目标接近出料区但面单单号未验证，停止ACT并保持')
                track['stable']=track['stable']+1 if contains(zone,[cx,cy]) and track['waybill'] else 0
                if track['stable']>=self.cfg['stable_frames']:
                    track['done']=True;self.completed+=1;self.finished_codes.add(track['waybill'])
        if not self.started:self.started=True;return 'complete'
        return None


def guarded_torque(target,position,velocity,gravity,kp,kd,limit):
    values=[target,position,velocity,gravity,kp,kd,limit]
    if not all(math.isfinite(float(v)) for v in values) or limit<=0:raise ValueError('力矩控制输入无效')
    # Torque-only MIT: the FOC loop receives a bounded total torque, not an
    # additional unbounded position spring on top of a clipped feedforward.
    torque=kp*(target-position)-kd*velocity+gravity
    return max(-limit,min(limit,torque))


def control_worker(cfg, state, command, lock, requests, replies, stop):
    """The only motor owner. No cameras, YOLO, Torch or CUDA in this process."""
    import numpy as np
    import pinocchio as pin
    import yaml
    from motorbridge import Controller, Mode
    from rebot_sort_support.motor_mode import ensure_mode_checked
    from rebot_sort_support.shutdown_worker import home_arm
    from rebot_sort_support.coordinated_shutdown import parallel_shutdown
    from rebot_sort_support.shutdown_signals import finish_cleanup
    contract=json.loads((ROOT/'assets/robot_contract.json').read_text())
    hardware=yaml.safe_load((ROOT/'assets/rebotarm_dm.yaml').read_text())
    model=pin.buildModelFromUrdf(str(ROOT/'assets/DM/urdf/ReBot_Arm_DM.urdf'))
    sessions=[];model_data=[model.createData(),model.createData()]
    signal.signal(signal.SIGINT,lambda *_:requests.put('home'))
    fault=None; enabled=False; homed=False; sequence=0
    try:
        # Both complete buses are checked before enabling either arm.
        for side in ('left','right'):
            bus=Controller.from_dm_serial(cfg['ports'][side],921600);motors={}
            sessions.append((side,bus,motors))
            for j in hardware['joints']:
                motors[j['motor_id']]=bus.add_damiao_motor(j['motor_id'],j['feedback_id'],j['model'])
        def read_all():
            out=[]
            for side,bus,motors in sessions:
                for m in motors.values():m.request_feedback()
                bus.poll_feedback_once()
                for i in range(1,8):
                    s=motors[i].get_state()
                    if s is None or not all(math.isfinite(v) for v in [s.pos,s.vel,s.torq,s.t_mos,s.t_rotor]):raise RuntimeError(side+' 关节反馈缺失/无效')
                    if max(s.t_mos,s.t_rotor)>cfg['temperature_limit_c']:raise RuntimeError(side+' 电机温度超限')
                    if s.status_code not in (0,1):raise RuntimeError(side+' 电机故障状态 '+str(s.status_code))
                    out.append(s)
            return out
        deadline=time.monotonic()+3
        while True:
            try:measured=read_all();break
            except RuntimeError:
                if time.monotonic()>deadline:raise
                time.sleep(.02)
        q=np.array([s.pos for s in measured]);target=q.copy();velocity=np.zeros(14)
        directions=np.array([contract['joint_directions'][j] for _ in range(2) for j in JOINTS])
        limits=np.array(cfg['torque_limits_nm'],float)
        kp=np.array([j['MIT']['kp'] for _ in range(2) for j in hardware['joints']])
        kd=np.array([j['MIT']['kd'] for _ in range(2) for j in hardware['joints']])
        def gravity(q):
            result=np.zeros(14)
            for arm_index in range(2):
                vector=pin.neutral(model)
                for idx in range(6):
                    joint=model.joints[model.getJointId('joint'+str(idx+1))]
                    if joint.nq!=1:raise RuntimeError('官方模型关节不是单自由度')
                    vector[joint.idx_q]=q[arm_index*7+idx]
                g=pin.computeGeneralizedGravity(model,model_data[arm_index],vector)
                for idx in range(6):
                    joint=model.joints[model.getJointId('joint'+str(idx+1))]
                    result[arm_index*7+idx]=g[joint.idx_v]
            if not np.isfinite(result).all():raise RuntimeError('重力计算无效')
            return result
        tau=gravity(q)
        if np.any(np.abs(tau)>.8*limits):raise RuntimeError('配置力矩限值不足以保持当前重力负载，未使能')
        live=np.zeros(14);all_motors=[m[i] for _,_,m in sessions for i in range(1,8)]
        for idx,m in enumerate(all_motors):
            ensure_mode_checked(m,Mode.MIT)
            m.get_register_u32(10,timeout_ms=200);live[idx]=time.monotonic()
            m.send_mit(q[idx],0,0,0,float(tau[idx]))
        for m in all_motors:m.enable()
        with lock:
            command[:14]=np.degrees(q)/directions;command[14]=time.monotonic()
            state[:14]=q;state[42]=time.monotonic()
        enabled=True;replies.put({'type':'ready'})
        tick=0;last_tick=time.monotonic()
        while not stop.is_set():
            began=time.monotonic();dt=min(.05,max(.001,began-last_tick));last_tick=began
            try:
                instruction=requests.get_nowait()
                if instruction=='home':break
                if instruction=='hold' and not fault:
                    fault='上层请求安全保持';target=q.copy();velocity[:]=0
                    replies.put({'type':'fault','message':fault})
            except queue.Empty:pass
            try:
                measured=read_all();q=np.array([s.pos for s in measured]);dq=np.array([s.vel for s in measured])
                # Motorbridge's cached states have no timestamp. A rotating fresh
                # RID10 transaction establishes per-joint transport liveness.
                idx=tick%14
                if all_motors[idx].get_register_u32(10,timeout_ms=20)!=int(Mode.MIT):raise RuntimeError('MIT模式回读改变')
                live[idx]=time.monotonic()
                if np.any(began-live>.5):raise RuntimeError('关节通信看门狗超时')
                with lock:
                    sent=float(command[14]);desired=np.array(command[:14])
                    state[:14]=q;state[14:28]=dq;state[28:42]=[s.torq for s in measured];state[42]=began;state[43]=sequence
                if not fault and (sent<=0 or began-sent>cfg['heartbeat_s']):raise RuntimeError('控制心跳中断')
                if not fault and any(abs(s.torq)>limits[i] for i,s in enumerate(measured)):raise RuntimeError('实测力矩看门狗超限')
                if not fault:
                    desired=np.radians(desired*directions)
                    for i,joint in enumerate(JOINTS*2):
                        lo,hi=contract['joint_limits_deg'][joint]
                        if not math.radians(lo)-.03<=desired[i]<=math.radians(hi)+.03:raise RuntimeError('策略目标超过实际关节限位')
                    # Follow-error concerns the rate-limited target actually sent,
                    # not a distant but valid upstream gripper open/close goal.
                    if np.max(np.abs(target-q))>math.radians(cfg['tracking_limit_deg']):raise RuntimeError('实际执行目标与反馈偏差超限，保持当前姿态')
                    error=desired-target
                    want=np.sign(error)*np.minimum(cfg['max_speed_rad_s'],np.sqrt(2*cfg['max_acceleration_rad_s2']*np.abs(error)))
                    velocity+=np.clip(want-velocity,-cfg['max_acceleration_rad_s2']*dt,cfg['max_acceleration_rad_s2']*dt)
                    target+=velocity*dt
                tau=gravity(q)
                if fault:target=q.copy()
                for i,m in enumerate(all_motors):
                    value=guarded_torque(target[i],q[i],dq[i],tau[i],kp[i],kd[i],limits[i])
                    m.send_mit(q[i],0,0,0,value)
            except Exception as exc:
                if not fault:
                    fault=str(exc);target=q.copy();velocity[:]=0
                    replies.put({'type':'fault','message':fault})
                # Stop contact-position pressure immediately. Keep bounded gravity
                # and damping, rather than leaving the last nonzero torque latched.
                states=[m.get_state() for m in all_motors]
                if any(s and max(s.t_mos,s.t_rotor)>=110 for s in states):
                    for m in all_motors:m.disable()
                    replies.put({'type':'error','message':'电机达到110°C紧急停机阈值，未执行归零'})
                    return
                for i,m in enumerate(all_motors):
                    try:m.send_mit(q[i],0,0,1.,float(np.clip(tau[i],-limits[i],limits[i])))
                    except Exception:pass
                # Next successful cycle holds measurement with gravity and bounded
                # torque. Missing feedback/overheat never starts a homing motion.
            tick+=1;sequence+=1
            stop.wait(max(0,1/cfg['control_hz']-(time.monotonic()-began)))
        # Only a deliberate home/normal stop enters this verified slow trajectory.
        with finish_cleanup():
            def finish(session,group):
                side,bus,motors=session
                home_arm(bus,motors,position_mode=Mode.POS_VEL,force_mode=Mode.FORCE_POS,
                         ratio=cfg.get('gripper_torque_ratio',.12),sync=group,
                         log=lambda text,**kw:print('['+side+'] '+text,**kw))
            parallel_shutdown(sessions,finish)
            homed=True
        replies.put({'type':'homed'})
    except BaseException as exc:
        replies.put({'type':'error','message':str(exc),'homed':homed})
    finally:
        for _,bus,motors in sessions:
            for m in motors.values():
                try:
                    if homed or not enabled:m.disable()
                    m.close()
                except Exception:pass
            try:bus.close()
            except Exception:pass


def scene_worker(cfg,nodes,output,stop):
    import cv2
    import numpy as np
    if cfg.get('yolo_model'):from ultralytics import YOLO
    from rebot_sort_support.waybill_decoder import _decode_waybill_frame
    cv2.setNumThreads(1)
    try:
        detector=YOLO(str(resolve(cfg,cfg['yolo_model']))) if cfg.get('yolo_model') else None
        if detector:
            labels=set(detector.names.values())
            for kind,aliases in cfg['yolo_classes'].items():
                if not labels.intersection(aliases):raise ValueError('YOLO模型缺少配置的包裹类别：'+kind)
        top=next(n for n in nodes if n['name']==cfg['top_camera'])
        sequence=0;last=0
        while not stop.is_set():
            with top['condition']:
                top['condition'].wait_for(lambda:top['sequence'].value!=last or stop.is_set(),timeout=.5)
                if stop.is_set():return
                if not top['sequence'].value:continue
                stamp=top['stamp'].value;last=top['sequence'].value
                rgb=np.frombuffer(top['pixels'],np.uint8).reshape(top['height'],top['width'],3).copy()
            if time.monotonic()-stamp>cfg['camera_max_age_s']:continue
            bgr=cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR)
            result=detector.predict(bgr,conf=cfg.get('yolo_confidence',.6),device=cfg.get('vision_device','cpu'),verbose=False)[0] if detector else None
            scan=_decode_waybill_frame(bgr);parcels=[];width=top['width'];height=top['height']
            aliases=cfg.get('yolo_classes',{'box':['box'],'bag':['bag'],'stack':['stack','stacked']})
            for box in (result.boxes if result is not None else []):
                label=str(result.names[int(box.cls[0])]);kind=next((k for k,v in aliases.items() if label in v),None)
                if kind not in ('box','bag','stack'):continue
                x1,y1,x2,y2=[float(v) for v in box.xyxy[0].tolist()]
                decoded=[]
                for code in scan.get('codes',[]):
                    area=code.get('bbox')
                    if area and contains([x1,y1,x2,y2],[(area[0]+area[2])/2,(area[1]+area[3])/2]) and re.fullmatch(cfg.get('waybill_pattern',r'[A-Za-z0-9-]{8,32}'),str(code['text'])):decoded.append(code['text'])
                label_visible=any(b.get('kind')=='label_roi' and contains([x1,y1,x2,y2],[(b['bbox'][0]+b['bbox'][2])/2,(b['bbox'][1]+b['bbox'][3])/2]) for b in scan.get('boxes',[]))
                parcels.append({'bbox':[x1/width,y1/height,x2/width,y2/height],'kind':kind,'stacked':kind=='stack','score':float(box.conf[0]),'waybill':decoded[0] if decoded else None,'label_visible':label_visible})
            sequence+=1;message={'sequence':sequence,'stamp':stamp,'parcels':parcels,'codes':[c['text'] for c in scan.get('codes',[])],'supervision':'yolo' if detector else 'barcode_only'}
            try:output.put_nowait(message)
            except queue.Full:
                try:output.get_nowait()
                except queue.Empty:pass
                try:output.put_nowait(message)
                except queue.Full:pass
            stop.wait(.1)
    except BaseException as exc:
        try:output.put({'error':str(exc)},timeout=.1)
        except queue.Full:pass


def validate_physical(cfg):
    if sys.platform!='linux':raise ValueError('机械臂控制只允许在Ubuntu/Linux运行')
    limits=cfg.get('torque_limits_nm')
    if not isinstance(limits,list) or len(limits)!=14 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in limits):raise ValueError('必须填写经现场验证的14个关节力矩阈值 torque_limits_nm，不使用猜测的安全值')
    if cfg.get('hardware_validated') is not True:raise ValueError('核对端口、关节方向、URDF、力矩阈值后设置 hardware_validated=true')
    if cfg['execution_mode']=='skills' and not cfg.get('yolo_model'):raise ValueError('独立技能选择需要包裹YOLO模型')
    if cfg.get('yolo_model') and not resolve(cfg,cfg['yolo_model']).is_file():raise ValueError('YOLO权重文件不存在')
    paths,shape=model_plan(cfg)
    for skill in paths:
        entry=cfg.get('skill_entries',{}).get(skill)
        if not isinstance(entry,dict) or len(entry.get('min_motor_deg',[]))!=14 or len(entry.get('max_motor_deg',[]))!=14:raise ValueError('缺少技能入口实测角度范围：'+skill+'；可由训练师 export_handoff.py 从数据集导出')
        if not all(math.isfinite(float(a)) and math.isfinite(float(b)) and a<=b for a,b in zip(entry['min_motor_deg'],entry['max_motor_deg'])):raise ValueError('技能入口范围无效：'+skill)
    if not paths and not cfg.get('rules'):raise ValueError('没有可以执行的技能')
    import importlib.util
    for package in ['lerobot','torch','numpy','cv2','motorbridge','pinocchio','zxingcpp','yaml']+(['ultralytics'] if cfg.get('yolo_model') else []):
        if importlib.util.find_spec(package) is None:raise ValueError('缺少依赖 '+package+'，请安装本文件夹 requirements.txt')
    return paths,shape


def snapshot(nodes,max_age):
    import numpy as np
    observation={}
    for node in nodes:
        with node['condition']:
            if node['error'].value:raise RuntimeError(node['error'].value.decode(errors='replace'))
            if time.monotonic()-node['stamp'].value>max_age:raise RuntimeError('摄像头画面过期：'+node['name'])
            observation['observation.images.'+node['name']]=np.frombuffer(node['pixels'],np.uint8).reshape(node['height'],node['width'],3).copy()
    return observation


def dashboard(app,host,port):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            if self.headers.get('X-Rebot-Token')!=app.token:self.send_error(403);return
            length=int(self.headers.get('Content-Length','0'))
            if length>1024:self.send_error(413);return
            self.rfile.read(length)
            action=self.path.removeprefix('/control/')
            if action=='heartbeat':app.browser_at=time.monotonic()
            elif action in ('start','home'):
                try:app.commands.put_nowait(action)
                except queue.Full:self.send_error(409);return
            else:self.send_error(404);return
            self.send_response(204);self.end_headers()
        def do_GET(self):
            if self.path=='/status':
                body=json.dumps(app.status,ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
            if self.path=='/':
                cards=''.join(f'<article><h3>{html.escape(n["name"])}</h3><img src="/stream/{i}"></article>' for i,n in enumerate(app.cameras.nodes))
                body=('''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>快递分拣执行台</title>
<style>body{font:16px system-ui;margin:24px;background:#f4f6fa;color:#17243b}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}article,pre{padding:18px;background:white;border-radius:14px}img{width:100%;cursor:zoom-in}button{padding:12px 20px;border:0;border-radius:9px;margin:8px;background:#2557cf;color:white}dialog{border:0;border-radius:16px;max-width:90vw}dialog img{max-height:85vh;width:auto;max-width:85vw}pre{white-space:pre-wrap}</style>
<h1>快递分拣执行台</h1><p>三路画面共用一个相机进程。完整流程由同一个ACT执行；未配置独立包裹检测时，扫码只作监视，不自动确认出料数量。</p>
<button onclick="if(confirm('确认端口和模型已核对、人员与障碍物已离开机械臂运动范围，开始自主分拣？'))send('start')">开始分拣</button>
<button onclick="if(confirm('停止策略，双臂从当前位置慢速归零并失能。确认归零路径已清空？'))send('home')">收工归零并失能</button>
<pre id="state">正在连接执行进程…</pre><main>'''+cards+'''</main><dialog id="zoom" onclick="if(event.target===this)this.close()"><button onclick="document.querySelector('#zoom').close()">×</button><img id="large"></dialog>
<script>async function send(k){const r=await fetch('/control/'+k,{method:'POST',headers:{'X-Rebot-Token':__CONTROL_TOKEN__}});if(!r.ok)alert('请求未成功，请查看终端')}
setInterval(async()=>{try{const r=await fetch('/status',{cache:'no-store'});document.querySelector('#state').textContent=JSON.stringify(await r.json(),null,2);await send('heartbeat')}catch(e){document.querySelector('#state').textContent='连接中断，控制进程将触发心跳保护'}},250);
document.querySelectorAll('article img').forEach(i=>i.onclick=()=>{document.querySelector('#large').src=i.src;document.querySelector('#zoom').showModal()});</script>''').replace('__CONTROL_TOKEN__',json.dumps(app.token)).encode()
                self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.end_headers();self.wfile.write(body);return
            match=re.fullmatch(r'/stream/(\d+)',self.path)
            if not match or int(match[1])>=len(app.cameras.nodes):self.send_error(404);return
            import cv2
            import numpy as np
            node=app.cameras.nodes[int(match[1])]
            self.send_response(200);self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.send_header('Cache-Control','no-store');self.end_headers()
            try:
                while not app.closed.is_set():
                    with node['condition']:
                        node['condition'].wait(timeout=.05)
                        rgb=np.frombuffer(node['pixels'],np.uint8).reshape(node['height'],node['width'],3).copy()
                    valid,jpg=cv2.imencode('.jpg',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))
                    if valid:self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpg.tobytes()+b'\r\n');self.wfile.flush()
                    app.closed.wait(.1)
            except OSError:pass
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True,name='sorting-dashboard').start()
    return server


class Runtime:
    def __init__(self,cfg,paths,shape,execute,host,port):
        import numpy as np
        from rebot_sort_support.capture import CaptureService
        self.cfg=cfg;self.paths=paths;self.execute=execute;self.host=host;self.port=port
        self.token=secrets.token_urlsafe(24)
        self.ctx=mp.get_context('spawn');self.lock=self.ctx.Lock();self.state=self.ctx.RawArray('d',44);self.command=self.ctx.RawArray('d',15)
        self.control_requests=self.ctx.Queue(8);self.control_replies=self.ctx.Queue(16);self.scenes=self.ctx.Queue(2)
        self.control_stop=self.ctx.Event();self.scene_stop=self.ctx.Event();self.closed=threading.Event();self.commands=queue.Queue(8)
        self.control=self.vision=None;self.models={};self.active=None;self.scene=None;self.browser_at=0.
        self.status={'phase':'PREPARING','message':'准备相机、视觉与ACT模型；尚未连接机械臂','completed':0}
        height,width=(shape[1],shape[2]) if shape else (360,640)
        self.cameras=CaptureService(NS(size=(width,height),cameras=cfg['cameras'],capture_size=tuple(cfg.get('capture_size',[1920,1080])),fps=cfg['fps'],preview_port=0))
        self.workflow=CompleteWorkflow(cfg) if cfg['execution_mode']=='complete' else Workflow(cfg)
        self.rule=None;self.rule_started=0.;self.homing=False;self.control_started_at=0.
        contract=json.loads((ROOT/'assets/robot_contract.json').read_text())
        self.robot=NS(cameras={});self.feedback=[0.]*14
        for arm_index,side in enumerate(('left','right')):
            motors={joint:NS(get_state=lambda i=arm_index*7+j:NS(pos=self.feedback[i])) for j,joint in enumerate(JOINTS)}
            setattr(self.robot,side+'_arm',NS(config=NS(joint_directions=contract['joint_directions'],joint_limits=contract['joint_limits_deg']),motors=motors))
    def set_command(self,action=None):
        with self.lock:
            if action is not None:self.command[:14]=[float(v) for v in action]
            self.command[14]=time.monotonic()
    def start_control(self):
        if not self.execute:raise RuntimeError('当前为观察模式；需要用 --execute 启用物理执行能力')
        if self.control is not None:raise RuntimeError('控制进程已经启动；收工后请重新运行入口，不能重复使能')
        if time.monotonic()-self.browser_at>self.cfg['heartbeat_s']:raise RuntimeError('浏览器心跳尚未就绪')
        if self.scene is None or time.monotonic()-self.scene['stamp']>self.cfg['camera_max_age_s']:raise RuntimeError('场景识别尚未就绪')
        from rebot_sort_support.capture import check_owners
        check_owners(self.cfg['ports'].values())
        self.control=self.ctx.Process(target=control_worker,args=(self.cfg,self.state,self.command,self.lock,self.control_requests,self.control_replies,self.control_stop),name='sorting-CAN-owner')
        self.control.start();self.status.update(phase='CONNECTING',message='控制进程检查双臂反馈及重力负载')
    def hold(self,reason):
        if self.active:
            self.models[self.active].deactivate();self.active=None
        self.rule=None
        if self.control and self.control.is_alive() and self.state[42]>0:
            contract=json.loads((ROOT/'assets/robot_contract.json').read_text())
            directions=[contract['joint_directions'][j] for _ in range(2) for j in JOINTS]
            self.set_command([math.degrees(q)/d for q,d in zip(self.state[:14],directions)])
        self.status.update(phase='HOLD',message=reason)
    def fault(self,reason):
        first=self.status.get('phase')!='FAULT'
        self.hold(reason);self.status['phase']='FAULT'
        if first and self.control and self.control.is_alive():
            try:self.control_requests.put_nowait('hold')
            except queue.Full:pass
        # Stop refreshing the control heartbeat, so its own loop latches a measured hold.
        print('[分拣故障] '+reason,flush=True)
    def switch(self,skill):
        self.hold('等待技能交接')
        self.status.update(phase=skill.upper(),message='切换技能：'+skill)
        if skill in self.cfg.get('rules',{}):
            self.rule=json.loads(resolve(self.cfg,self.cfg['rules'][skill]).read_text());self.rule_started=time.monotonic()
            current=[math.degrees(v) for v in self.state[:14]]
            contract=json.loads((ROOT/'assets/robot_contract.json').read_text());directions=[contract['joint_directions'][j] for _ in range(2) for j in JOINTS]
            if max(abs(a*d-q) for a,d,q in zip(self.rule['points'][0]['action'],directions,current))>self.cfg['skill_entry_tolerance_deg']:raise RuntimeError('规则轨迹入口与当前姿态不同，未自动接近或回零')
        else:
            entry=self.cfg['skill_entries'][skill]
            current=[math.degrees(q) for q in self.feedback]
            tolerance=self.cfg['skill_entry_tolerance_deg']
            if any(not lo-tolerance<=q<=hi+tolerance for q,lo,hi in zip(current,entry['min_motor_deg'],entry['max_motor_deg'])):raise RuntimeError('当前姿态不在技能训练入口范围：'+skill+'；需要补采交接数据或已验证过渡轨迹，未自动回零')
            self.active=skill;policy=self.models[skill]
            policy.attach(self.robot,{},self.cfg['fps']);policy.reset()
    def run(self):
        import numpy as np
        from rebot_sort_support.async_act_runtime import AsyncACTPolicy
        features={'action':{'dtype':'float32','shape':[14],'names':ACTION_NAMES},'observation.state':{'dtype':'float32','shape':[14],'names':ACTION_NAMES}}
        server=None
        try:
            self.cameras.start()
            for n in self.cameras.nodes:features['observation.images.'+n['name']]={'dtype':'video','shape':[n['height'],n['width'],3]}
            server=dashboard(self,self.host,self.port)
            print(f'执行台：http://{self.host}:{self.port}；等待模型预热完成及网页开始确认。',flush=True)
            for skill,path in self.paths.items():
                self.status['message']='加载并预热技能：'+skill
                config=load_act_config(path,self.cfg['device'])
                policy=AsyncACTPolicy(config,features,inference_settings(self.cfg))
                self.models[skill]=policy;policy.start({})
            self.vision=self.ctx.Process(target=scene_worker,args=(self.cfg,self.cameras.nodes,self.scenes,self.scene_stop),name='sorting-YOLO-waybill')
            self.vision.start();self.status.update(phase='READY',message='相机和模型已准备；请在网页确认开始')
            signal.signal(signal.SIGINT,lambda *_:self.commands.put('home'))
            signal.signal(signal.SIGTERM,lambda *_:self.commands.put('home'))
            while not self.closed.is_set():
                began=time.monotonic()
                try:
                    action=self.commands.get_nowait()
                    if action=='start':self.start_control()
                    elif action=='home':
                        self.hold('双臂慢速收工');self.homing=True;self.status['phase']='HOMING'
                        if self.control and self.control.is_alive():self.control_requests.put('home')
                        else:break
                except queue.Empty:pass
                except Exception as exc:self.status.update(message=str(exc))
                while True:
                    try:
                        result=self.control_replies.get_nowait()
                        if result['type']=='ready':
                            self.control_started_at=time.monotonic();self.status.update(phase='SCAN_WORKSPACE',message='双臂已连接并保持实测姿态')
                        elif result['type']=='homed':self.status.update(phase='DONE',message='双臂归零失能完成');self.closed.set()
                        else:self.fault(result.get('message','控制进程故障'))
                    except queue.Empty:break
                while True:
                    try:
                        scene=self.scenes.get_nowait()
                        if 'error' in scene:self.fault('视觉进程：'+scene['error'])
                        else:self.scene=scene
                    except queue.Empty:break
                if not self.vision.is_alive():self.fault('视觉进程已退出')
                active=self.control and self.control.is_alive() and self.status['phase'] not in ('FAULT','HOMING','DONE','CONNECTING')
                if active:
                    try:
                        with self.lock:self.feedback=list(self.state[:14])
                        if began-self.browser_at>self.cfg['heartbeat_s']:raise RuntimeError('浏览器心跳超时，停止技能并保持')
                        if not self.scene or began-self.scene['stamp']>self.cfg['camera_max_age_s']:raise RuntimeError('视觉场景过期')
                        if began-self.state[42]>.5:raise RuntimeError('控制进程反馈过期')
                        event=self.workflow.update(self.scene,began)
                        if event=='hold':self.hold('重新观察包裹状态')
                        elif event:self.switch(event)
                        self.status['completed']=self.workflow.completed if self.cfg.get('yolo_model') else None
                        self.status['waybills']=self.scene.get('codes',[])
                        self.status['supervision']=self.scene.get('supervision','yolo')
                        self.status['parcels']=self.scene['parcels']
                        if began-self.control_started_at>self.cfg['max_run_s']:
                            self.fault('已达到最大演示时间，保持姿态；确认现场路径后点击收工')
                        elif self.workflow.completed>=self.cfg['max_parcels']:
                            self.fault('已达到本轮分拣数量，保持姿态；确认现场路径后点击收工')
                        elif self.active:
                            obs=snapshot(self.cameras.nodes,self.cfg['camera_max_age_s']);obs['observation.state']=np.degrees(np.array(self.feedback)).astype(np.float32)
                            policy=self.models[self.active];target=policy.predict(obs,task='快递分拣，面单朝上后出料',robot_type='bi_seeed_b601_dm_follower')
                            self.status['async_inference']={'pid':policy.process.pid if policy.process else None,
                                'waiting':policy.waiting_for_prediction,'inference_ms':round(policy.last_inference_s*1000,1),
                                'buffer_steps':policy.buffer.remaining(policy.tick+1),'reason':policy.failure_reason}
                            if policy.failed:raise RuntimeError(policy.policy_label+'执行拒绝：'+(policy.failure_reason or '未返回有效预测'))
                            self.set_command(target[0].numpy())
                        elif self.rule:
                            elapsed=began-self.rule_started;points=self.rule['points']
                            before=points[0];after=points[-1]
                            for a,b in zip(points,points[1:]):
                                if a['t']<=elapsed<=b['t']:before,after=a,b;break
                            alpha=min(1,max(0,(elapsed-before['t'])/max(1e-6,after['t']-before['t'])))
                            value=np.array(before['action'])*(1-alpha)+np.array(after['action'])*alpha
                            self.set_command(value)
                        else:self.set_command()
                    except Exception as exc:self.fault(str(exc))
                if self.control and not self.control.is_alive() and self.status['phase']!='DONE':
                    self.fault('控制进程已退出；未报告归零成功');break
                self.closed.wait(max(0,1/self.cfg['fps']-(time.monotonic()-began)))
            return 0 if self.status['phase']=='DONE' or not self.control else 1
        finally:
            self.closed.set()
            if self.control and self.control.is_alive():
                self.control_requests.put('home');self.control.join(1200)
                if self.control.is_alive():print('控制进程仍在收工/保持，未强杀；请现场检查。',flush=True)
            self.scene_stop.set()
            if self.vision:
                self.vision.join(2)
                if self.vision.is_alive():self.vision.terminate();self.vision.join(2)
            for policy in self.models.values():policy.close()
            self.cameras.close()
            if server:server.shutdown();server.server_close()
            for q in [self.control_requests,self.control_replies,self.scenes]:q.close()


def check_async(cfg,seconds=5.):
    """Exercise the actual model/IPC/buffer, without importing motor or camera I/O."""
    import numpy as np
    from rebot_sort_support.async_act_runtime import AsyncACTPolicy
    paths,shape=model_plan(cfg)
    if not paths:raise ValueError('异步检查需要一个ACT模型路径')
    path=paths.get('complete',next(iter(paths.values())))
    config=load_act_config(path,cfg['device'])
    features={'action':{'dtype':'float32','shape':[14],'names':ACTION_NAMES},'observation.state':{'dtype':'float32','shape':[14],'names':ACTION_NAMES}}
    for camera in cfg['cameras']:
        features['observation.images.'+camera['name']]={'dtype':'video','shape':[shape[1],shape[2],3]}
    client=AsyncACTPolicy(config,features,inference_settings(cfg))
    observation={k:np.zeros(v['shape'],np.uint8 if v['dtype']=='video' else np.float32) for k,v in features.items() if k.startswith('observation.')}
    contract=json.loads((ROOT/'assets/robot_contract.json').read_text());directions=np.array([contract['joint_directions'][j] for _ in range(2) for j in JOINTS])
    predictions=waits=chunks=0;first=None;max_poll=0.;latencies=[]
    try:
        client.start({});client.attach(None,{},cfg['fps']);client.reset()
        start=time.monotonic();last_request=0
        while time.monotonic()-start<seconds:
            began=time.monotonic();tick=int((began-start)*cfg['fps'])
            client._receive(began,tick)
            if client.buffer.last_request>last_request:
                chunks+=1;last_request=client.buffer.last_request;latencies.append(client.last_inference_s)
            lead=max(math.ceil(client.chunk_size*client.ratio),math.ceil(client.last_inference_s*cfg['fps'])+2)
            if client.buffer.remaining(tick)<=min(client.chunk_size,lead):
                client._submit(observation,began,tick,'快递分拣','bi_seeed_b601_dm_follower')
            value=client.buffer.take(tick)
            if value is not None:
                predictions+=1;first=first if first is not None else began-start
                # Virtual feedback only, never a robot command.
                observation['observation.state']=(value*directions).astype(np.float32)
            elif first is not None:waits+=1
            max_poll=max(max_poll,time.monotonic()-began)
            time.sleep(max(0,1/cfg['fps']-(time.monotonic()-began)))
        report={'check':'actual_model_async_only','device':cfg['device'],'model':path,
                'actuator_connected':False,'camera_opened':False,'safety_gates_exercised':False,
                'inference_pid':client.process.pid,'caller_pid':os.getpid(),'returned_chunks':chunks,
                'prediction_frames':predictions,'waiting_frames_after_first_prediction':waits,
                'first_prediction_s':round(first,3) if first is not None else None,
                'max_poll_ms':round(max_poll*1000,2),'max_inference_ms':round(max(latencies,default=0)*1000,2),
                'stale_chunks':client.buffer.stale_chunks,'passed':predictions>0}
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if not predictions:raise RuntimeError('模型未在有效时间窗口内返回可用动作块；请检查上述延迟、设备与配置')
        return 0
    finally:client.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config',type=Path,default=ROOT/'sorting.example.json')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true',help='纯配置预览，不导入驱动/模型，不打开设备')
    mode.add_argument('--simulate',action='store_true',help='使用四种合成场景检查状态选择，不打开设备')
    mode.add_argument('--check',action='store_true',help='校验实际模型、数据契约与依赖，不打开设备')
    mode.add_argument('--check-async',action='store_true',help='用随包真实模型检查异步IPC/动作缓冲；不连接机械臂或相机')
    mode.add_argument('--execute',action='store_true',help='启用物理执行能力；仍需网页二次确认开始')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--device',choices=['cpu','cuda'],help='覆盖模型推理设备，不修改配置文件')
    parser.add_argument('--seconds',type=float,default=5.,help='仅异步检查时使用，范围1~30秒')
    args=parser.parse_args(argv);cfg=load_settings(args.config)
    if args.device:cfg['device']=args.device
    if args.check_async:
        if not math.isfinite(args.seconds) or not 1<=args.seconds<=30:raise ValueError('--seconds 必须在1~30之间')
        return check_async(cfg,args.seconds)
    if args.simulate:
        print('以下为辅助视觉状态选择的合成检查；complete模式始终使用同一个ACT，不按这些标签换模型。')
        common={'bbox':[.3,.3,.6,.6],'score':.9,'kind':'box'}
        for name,parcel in [('堆叠',{**common,'stacked':True}),('面单未验证朝上',common),('盒/袋分类',{**common,'kind':'bag','waybill':'SF1234567890'}),('正常直接出料',{**common,'waybill':'123456789012'})]:
            print(name,'→',choose_skill({'parcels':[parcel]},cfg)[:2])
        return 0
    if args.dry_run or not(args.execute or args.check):
        wanted=['complete'] if cfg['execution_mode']=='complete' else SKILLS
        print(json.dumps({'config':str(args.config),'execution_mode':cfg['execution_mode'],'ports':cfg['ports'],'cameras':cfg['cameras'],'models':cfg['models'],'processes':['CAN控制/重力补偿/看门狗','单相机采集进程（三路）','可选YOLO/面单扫码','已预热ACT推理进程'],'motion_enabled':False,'missing_skills':[k for k in wanted if k!='flatten' and not(cfg['models'].get(k) or cfg['rules'].get(k))],'torque_limits_configured':isinstance(cfg.get('torque_limits_nm'),list)},ensure_ascii=False,indent=2));return 0
    paths,shape=validate_physical(cfg)
    if args.check:print('配置、依赖、14维动作及三路相机键检查通过；未打开设备。');return 0
    from rebot_sort_support.capture import check_owners
    check_owners([*cfg['ports'].values(),*[c['source'] for c in cfg['cameras']]])
    return Runtime(cfg,paths,shape,args.execute,args.host,args.port).run()


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print('[分拣未启动/执行失败] '+str(exc),file=sys.stderr);raise SystemExit(1)

