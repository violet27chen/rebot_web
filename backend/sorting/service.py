"""Read-only camera snapshots, calibration, YOLO and immutable motion proposals."""
import base64
import copy
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
import cv2
import numpy as np
from .geometry import SortingError,camera,unproject,align_fit,rigid_fit,validate_calibration,locate,fingerprint,transform,apply,inside,bounds,array
from .skills import route,inspect_skill,NAMES
from .pushleft import push_left_poses,gripper_down_ok,left_direction
from .ipc import Worker

DEFAULTS={'camera_source':'','yolo_model':'','device':'cuda','confidence':.6,'control_python':'',
          'calibration':{'mount':'fixed','arms':{}},'skills':[],
          'safety':{'torque_limits_nm':None,'joint_limits_rad':None,'max_velocity_rad_s':.25,
                    'max_delta_rad':.15,'clearance_m':.10,'max_push_m':.12}}


class SortingService:
    def __init__(self,hooks):
        self.h=hooks;self.lock=threading.RLock();self.frames={};self.scenes={};self.plans={}
        self.detector=None;self.detector_key=None;self.kin=None;self.control=None;self.session=None
        self.folder=Path(tempfile.mkdtemp(prefix='rebot-sorting-'));os.chmod(self.folder,0o700)

    def settings(self):
        value=copy.deepcopy(DEFAULTS)
        for k,v in (self.h.load_config().get('sorting') or {}).items():
            if isinstance(v,dict) and isinstance(value.get(k),dict):value[k].update(v)
            else:value[k]=v
        return value

    def save(self,patch):
        with self.h.lock:self.h.save_config({'sorting':patch})

    def check_camera(self,s):
        c=s['calibration']
        if c.get('camera_source')!=str(s['camera_source']):raise SortingError('俯视 RGB 来源与标定不一致，请重新采样标定')

    def control_python(self):
        local=self.h.root/'.venv-sorting/bin/python'
        return self.settings().get('control_python') or (str(local) if local.is_file() else self.h.python())

    def prepare_detector(self):
        s=self.settings();path=Path(s['yolo_model']).expanduser()
        if not path.is_absolute():path=self.h.root/path
        if not path.is_file() or path.suffix!='.pt':raise SortingError('请先选择本地训练完成的 YOLO .pt 检测模型')
        key=(str(path),path.stat().st_mtime_ns,s['device'])
        with self.lock:
            if self.detector and not self.detector.alive():self.detector.close();self.detector=None
            if key!=self.detector_key:
                if self.detector:self.detector.close()
                self.detector=Worker(self.h.python(),'backend.sorting.inference_worker',self.h.root)
                self.detector.call('yolo-load',{'path':str(path),'device':s['device']},timeout=120)
                self.detector_key=key

    @staticmethod
    def remember(storage,value,limit=8):
        key=secrets.token_urlsafe(18);storage[key]=value
        while len(storage)>limit:storage.pop(next(iter(storage)))
        return key

    def capture(self):
        s=self.settings();source=s.get('camera_source')
        if source in (None,''):raise SortingError('请在摄像头页选择固定俯视 RGB 来源')
        rgb=self.h.camera(source);depth=self.h.depth()
        try:
            for _ in range(3):
                _,image=rgb.read();meta,view,png=depth.read()
                rgb_time=rgb.captured_at
                if abs(meta['timestamp']-rgb_time)<=.15 and time.time()-min(meta['timestamp'],rgb_time)<.6:break
            else:raise SortingError('RGB 与深度时间不匹配或帧已过期，请检查共享视觉服务')
            raw=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_UNCHANGED)
            value={'rgb':image,'depth':raw,'meta':meta,'timestamp':min(meta['timestamp'],rgb_time),
                   'source':str(source),'rgb_timestamp':rgb_time,'created':time.monotonic()}
            with self.lock:key=self.remember(self.frames,value)
            _,jpeg=cv2.imencode('.jpg',image)
            return key,value,{'frame_id':key,'rgb':'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode(),
                              'depth':'data:image/jpeg;base64,'+base64.b64encode(view).decode(),
                              'meta':meta,'rgb_width':image.shape[1],'rgb_height':image.shape[0]}
        finally:rgb.release();depth.release()

    def scan_label(self,source=None):
        from .label import scan
        s=self.settings();source=source or s.get('label_camera') or s.get('camera_source')
        if source in (None,''):raise SortingError('请选择用于读取单号的相机')
        lease=self.h.camera(source)
        try:
            _,image=lease.read()
            if time.time()-lease.captured_at>.5:raise SortingError('单号相机画面已过期')
        finally:lease.release()
        result=scan(image);result['source']=str(source)
        if result['status']=='ok':result['destination']=self._destination(result['codes'],s)
        return result

    @staticmethod
    def _destination(codes,s):
        from .label import destination
        try:return destination(codes,s.get('label_rules') or [])
        except SortingError as exc:return {'error':str(exc)}

    def frame(self,key):
        value=self.frames.get(key)
        if not value or time.monotonic()-value['created']>300:raise SortingError('标定画面已过期，请重新冻结采样')
        if value['source']!=str(self.settings()['camera_source']):raise SortingError('相机来源已变化，请重新采样')
        return value

    def adopt_depth(self,key):
        frame=self.frame(key);intrinsic=frame['meta'].get('intrinsics')
        if not intrinsic:raise SortingError('SDK 未返回当前模式深度内参，请检查 OpenNI 几何信息')
        camera(intrinsic,'深度');self.save({'calibration':{'mount':'fixed','camera_source':str(frame['source']),'depth_uri':frame['meta'].get('uri'),'depth':intrinsic,'alignment_verified':False,'workspace_verified':False,'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return intrinsic

    def board(self,key,columns,rows,square):
        frame=self.frame(key)
        if not 3<=columns<=15 or not 3<=rows<=15 or not .005<=square<=.1:raise SortingError('棋盘内角点数量或方格边长无效')
        gray=cv2.cvtColor(frame['rgb'],cv2.COLOR_BGR2GRAY)
        ok,corners=cv2.findChessboardCorners(gray,(columns,rows))
        if not ok:raise SortingError('未找到完整棋盘内角点，请调整位置、角度或光照后重新采样')
        corners=cv2.cornerSubPix(gray,corners,(5,5),(-1,-1),(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_MAX_ITER,30,.01))
        s=self.settings();c=s['calibration'];samples=list(c.get('board_samples') or [])
        value={'image_points':corners.reshape(-1,2).tolist(),'columns':columns,'rows':rows,'square_m':square,
               'width':gray.shape[1],'height':gray.shape[0],'frame_id':key}
        if any(p.get('frame_id')==key for p in samples):raise SortingError('该画面已经采样，请移动棋盘后重新冻结')
        samples.append(value);self.save({'calibration':{'board_samples':samples[-30:],'alignment_verified':False}})
        return {'samples':len(samples)}

    def solve_rgb(self):
        samples=self.settings()['calibration'].get('board_samples') or []
        if len(samples)<8:raise SortingError('至少需要八张不同位置/角度的棋盘画面')
        first=samples[0];signature=lambda p:[p[k] for k in ['columns','rows','square_m','width','height']]
        if any(signature(p)!=signature(first) for p in samples):raise SortingError('棋盘尺寸或相机分辨率发生变化，请清空后重新采样')
        means=np.array([np.mean(p['image_points'],axis=0) for p in samples])
        if np.linalg.norm(means.std(0))<25:raise SortingError('采样过于集中，请让棋盘覆盖画面不同位置')
        object_points=np.zeros((first['columns']*first['rows'],3),np.float32)
        object_points[:,:2]=np.mgrid[:first['columns'],:first['rows']].T.reshape(-1,2)*first['square_m']
        error,K,d,_,_=cv2.calibrateCamera([object_points]*len(samples),[np.asarray(p['image_points'],np.float32) for p in samples],(first['width'],first['height']),None,None)
        if not np.isfinite(error) or error>1.5:raise SortingError('RGB 内参重投影误差过大，请重新采样')
        value={'K':K.tolist(),'distortion':d.ravel().tolist(),'width':first['width'],'height':first['height'],'rms_px':error}
        self.save({'calibration':{'rgb':value,'alignment_verified':False}});return value

    def add_point(self,p):
        f=self.frame(p['frame_id']);c=self.settings()['calibration'];uv=array(p.get('depth_uv'),(2,),'深度像素')
        x,y=np.rint(uv).astype(int);depth=f['depth']
        if not 1<=x<depth.shape[1]-1 or not 1<=y<depth.shape[0]-1:raise SortingError('深度采样点超出图像')
        patch=depth[y-1:y+2,x-1:x+2];valid=patch[patch>0]
        if len(valid)<5 or np.ptp(valid)>.02*1000:raise SortingError('该点附近深度空洞或边缘不稳定，请选择平整表面')
        xyz=unproject([uv],[float(np.median(valid))*.001],c.get('depth'))[0]
        point={'depth_xyz':xyz.tolist(),'depth_uv':uv.tolist(),'frame_id':p['frame_id']}
        if p.get('rgb_uv') is not None:point['rgb_uv']=array(p['rgb_uv'],(2,),'RGB 像素').tolist()
        if p.get('workspace_xyz') is not None:point['workspace_xyz']=array(p['workspace_xyz'],(3,),'工作台坐标（米）').tolist()
        if p.get('base_xyz') is not None:
            side=p.get('side')
            if side not in ('left','right'):raise SortingError('请选择左右从臂')
            point.update(side=side,base_xyz=array(p['base_xyz'],(3,),'从臂基座坐标（米）').tolist())
        points=list(c.get('points') or []);points.append(point)
        self.save({'calibration':{'points':points[-100:],'alignment_verified':False,'workspace_verified':False,'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return point

    @staticmethod
    def _tag(fit,idx):
        r=fit['residuals'];fit['point_indices']=idx;fit['worst_index']=idx[int(np.argmax(r))];return fit

    def delete_point(self,index):
        c=self.settings()['calibration'];points=list(c.get('points') or [])
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(points):raise SortingError('对应点序号无效')
        points.pop(index)
        self.save({'calibration':{'points':points,'alignment_verified':False,'workspace_verified':False,'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return len(points)

    def solve(self,kind):
        c=self.settings()['calibration'];points=c.get('points') or []
        if kind=='alignment':
            idx=[i for i,p in enumerate(points) if 'rgb_uv' in p];pairs=[points[i] for i in idx]
            fit=self._tag(align_fit([p['depth_xyz'] for p in pairs],[p['rgb_uv'] for p in pairs],c.get('rgb')),idx)
            self.save({'calibration':{'rgb_from_depth':fit['matrix'],'alignment_fit':fit,'alignment_verified':False}})
        elif kind=='workspace':
            idx=[i for i,p in enumerate(points) if 'workspace_xyz' in p];pairs=[points[i] for i in idx]
            fit=self._tag(rigid_fit([p['depth_xyz'] for p in pairs],[p['workspace_xyz'] for p in pairs]),idx)
            self.save({'calibration':{'workspace_from_depth':fit['matrix'],'workspace_fit':fit,'workspace_verified':False}})
        elif kind in ('left','right'):
            idx=[i for i,p in enumerate(points) if p.get('side')==kind and 'workspace_xyz' in p and 'base_xyz' in p];pairs=[points[i] for i in idx]
            fit=self._tag(rigid_fit([p['workspace_xyz'] for p in pairs],[p['base_xyz'] for p in pairs]),idx)
            self.save({'calibration':{'arms':{kind:{'base_from_workspace':fit['matrix'],'fit':fit,'verified':False}}}})
        else:raise SortingError('未知标定求解类型')
        return fit

    def scene(self):
        s=self.settings();self.check_camera(s);validate_calibration(s['calibration']);self.prepare_detector()
        key,frame,preview=self.capture()
        if frame['meta'].get('uri')!=s['calibration'].get('depth_uri'):raise SortingError('深度设备与标定不一致，请重新标定')
        detections=self.detector.call('detect',{'frame':frame['rgb'],'confidence':s['confidence'],'device':s['device']},timeout=10)
        if time.time()-frame['timestamp']>1.:raise SortingError('检测耗时过长，图像已过期，请检查推理设备')
        packages=locate(frame['depth'],frame['rgb'].shape,detections,s['calibration'])
        for package in packages:
            try:package['skill']=route(package,s['skills'])['name']
            except SortingError as exc:package['route_error']=str(exc)
        scene=dict(frame_id=key,targets=packages,timestamp=frame['timestamp'],fingerprint=fingerprint(s))
        with self.lock:ident=self.remember(self.scenes,scene)
        return dict(preview,scene_id=ident,targets=packages,timestamp=frame['timestamp'])

    def kinematics(self,method,payload):
        with self.lock:
            if not self.kin or not self.kin.alive():self.kin=Worker(self.control_python(),'backend.sorting.kinematics_worker',self.h.root)
            return self.kin.call(method,payload,timeout=30)

    def plan(self,scene_id,target_id,action=None):
        if action in ('inspect','push_left','cycle'):return self._plan_cycle(scene_id,target_id,action)
        s=self.settings();scene=self.scenes.get(scene_id)
        if not scene or time.time()-scene['timestamp']>20 or scene['fingerprint']!=fingerprint(s):raise SortingError('检测结果已过期或配置变化，请重新检测')
        target=next((t for t in scene['targets'] if t['id']==str(target_id) and t.get('valid')),None)
        if not target:raise SortingError('请选择有有效深度的目标')
        c=s['calibration'];self.check_camera(s);validate_calibration(c,('left','right'))
        entry=inspect_skill(route(target,s['skills']),self.h.root,fingerprint(c))
        if entry.get('arm') not in ('left','right'):raise SortingError('技能需指定主动从臂')
        measured=self.h.measured();q=array(measured,(14,),'双臂实测角度')
        limits=array(s['safety'].get('joint_limits_rad'),(14,2),'原始电机关节限位（弧度）')
        torque=array(s['safety'].get('torque_limits_nm'),(14,),'电机安全力矩上限（Nm）')
        if np.any(torque<=0):raise SortingError('每个电机安全力矩上限必须为正数')
        if np.any(limits[:,0]>=limits[:,1]) or np.any(q<limits[:,0]) or np.any(q>limits[:,1]):raise SortingError('实测姿态或配置关节限位无效')
        velocity=float(s['safety']['max_velocity_rad_s']);delta=float(s['safety']['max_delta_rad'])
        if not 0<velocity<=.5 or not 0<delta<=.3:raise SortingError('初期执行速度上限 0.5 rad/s，单步差值上限 0.3 rad')
        clearance=float(s['safety']['clearance_m'])
        if not .05<=clearance<=.3:raise SortingError('预接近净空应在 0.05～0.3 米')
        primary=entry['arm'];other='right' if primary=='left' else 'left';i=0 if primary=='left' else 7
        package=np.asarray(target['xyz_m']);world_to_base=transform(c['arms'][primary]['base_from_workspace'])
        poses=[];is_push=not inside(package,entry['training_bounds'])
        if is_push:
            region=bounds(entry['training_bounds']);goal=package.copy();goal[:2]=np.clip(package[:2],region[:2,0]+.03,region[:2,1]-.03)
            vector=goal-package;vector[2]=0;distance=float(np.linalg.norm(vector))
            if distance<.005:raise SortingError('目标高度不在训练范围，水平推拢无法解决')
            direction=vector/distance;travel=min(distance,float(s['safety']['max_push_m']))
            if not 0<travel<=.2:raise SortingError('单次规则推拢距离不能超过 0.2 米')
            extent=np.asarray(target['extent_m']);radius=float(np.linalg.norm(extent[:2]))/2+.04
            contact=package-direction*radius;contact[2]-=target['height_m']/2
            guard=package+np.array([-direction[1],direction[0],0])*radius;guard[2]=contact[2]
            poses=[(other,guard+np.array([0,0,clearance])),(other,guard),
                   (primary,contact+np.array([0,0,clearance])),(primary,contact),
                   (primary,contact+direction*travel),(primary,contact+direction*travel+[0,0,clearance]),
                   (other,guard+[0,0,clearance])]
        else:poses=[(primary,package+np.array([0,0,clearance]))]
        paths=[];current=q.copy()
        for side,xyz in poses:
            if not inside(xyz,c['workspace_bounds']):raise SortingError('规划接近点或围挡点超出已标定工作区')
            arm=c['arms'][side];base=apply(transform(arm['base_from_workspace']),[xyz])[0];index=0 if side=='left' else 7
            rpy=array((entry.get('approach_rpy') or {}).get(side),(3,),side+' 标定接近姿态')
            ik=self.kinematics('ik',{'joints':current[index:index+6].tolist(),'xyz':base.tolist(),'rpy':rpy.tolist()})
            next_q=current.copy();next_q[index:index+6]=ik['joints']
            if np.any(next_q<limits[:,0]) or np.any(next_q>limits[:,1]):raise SortingError('IK 结果超过已确认的电机关节限位')
            # Dense joint interpolation validates the actual commanded TCP path.
            self.kinematics('validate-path',{'from':current.tolist(),'to':next_q.tolist(),'calibration':c})
            paths.append({'joints':next_q.tolist(),'side':side,'xyz_workspace':np.asarray(xyz).tolist()});current=next_q
        if not is_push and np.max(np.abs(current-np.asarray(entry['start_pose_rad'])))>float(entry.get('start_tolerance_rad',.2)):
            raise SortingError('到达包裹上方后的姿态不在该 ACT 技能训练起始范围，请补充相应示教')
        plan={'target':target,'skill':entry,'paths':paths,'push':is_push,'initial_joints':q.tolist(),
              'fingerprint':fingerprint(s),'created':time.monotonic(),'scene_id':scene_id}
        with self.lock:ident=self.remember(self.plans,plan,4)
        return {'plan_id':ident,'push':is_push,'paths':paths,'target':target,'skill':entry['name'],'expires_in':60}

    def _pick_arm(self,package,c,cyc):
        if cyc.get('arm') in ('left','right'):return cyc['arm']
        best=None
        for side in ('left','right'):
            base=np.linalg.inv(transform(c['arms'][side]['base_from_workspace']))[:3,3]
            d=float(np.linalg.norm((np.asarray(package)-base)[:2]))
            if best is None or d<best[0]:best=(d,side)
        return best[1]

    def _plan_cycle(self,scene_id,target_id,action):
        """悬停看面单（inspect）、向左推走（push_left）、整套流程预演（cycle）。不执行任何运动。"""
        s=self.settings();scene=self.scenes.get(scene_id)
        if not scene or time.time()-scene['timestamp']>20 or scene['fingerprint']!=fingerprint(s):raise SortingError('检测结果已过期或配置变化，请重新检测')
        target=next((t for t in scene['targets'] if t['id']==str(target_id) and t.get('valid')),None)
        if not target:raise SortingError('请选择有有效深度的目标')
        c=s['calibration'];self.check_camera(s);validate_calibration(c,('left','right'))
        cyc=s.get('cycle') or {};q=array(self.h.measured(),(14,),'双臂实测角度')
        limits=array(s['safety'].get('joint_limits_rad'),(14,2),'原始电机关节限位（弧度）')
        torque=array(s['safety'].get('torque_limits_nm'),(14,),'电机安全力矩上限（Nm）')
        if np.any(torque<=0):raise SortingError('每个电机安全力矩上限必须为正数')
        if np.any(limits[:,0]>=limits[:,1]) or np.any(q<limits[:,0]) or np.any(q>limits[:,1]):raise SortingError('实测姿态或配置关节限位无效')
        clearance=float(s['safety']['clearance_m'])
        if not .05<=clearance<=.3:raise SortingError('预接近净空应在 0.05～0.3 米')
        package=np.asarray(target['xyz_m']);primary=self._pick_arm(package,c,cyc);other='right' if primary=='left' else 'left'
        rpys={side:array((cyc.get('approach_rpy') or {}).get(side),(3,),side+' 标定接近姿态') for side in ('left','right')}
        for side in ('left','right'):
            if not gripper_down_ok(rpys[side],cyc.get('tool_axis',[0,0,1]),c['arms'][side]['base_from_workspace'],float(cyc.get('max_tilt_rad',.35))):
                raise SortingError(side+' 接近姿态没有让爪头朝下，已拒绝（检查 approach_rpy 与 tool_axis）')
        if action=='push_left':
            travel=float(cyc.get('left_push_m',s['safety']['max_push_m']))
            poses=push_left_poses(package,target['extent_m'],target['height_m'],cyc.get('left_direction'),travel,clearance,primary,other)
        else:poses=[(primary,package+np.array([0,0,clearance]))]
        paths=[];current=q.copy()
        for side,xyz in poses:
            if not inside(xyz,c['workspace_bounds']):raise SortingError('规划点超出已标定工作区')
            base=apply(transform(c['arms'][side]['base_from_workspace']),[xyz])[0];index=0 if side=='left' else 7
            ik=self.kinematics('ik',{'joints':current[index:index+6].tolist(),'xyz':base.tolist(),'rpy':rpys[side].tolist(),'check_rot':True})
            next_q=current.copy();next_q[index:index+6]=ik['joints']
            if np.any(next_q<limits[:,0]) or np.any(next_q>limits[:,1]):raise SortingError('IK 结果超过已确认的电机关节限位')
            self.kinematics('validate-path',{'from':current.tolist(),'to':next_q.tolist(),'calibration':c})
            paths.append({'joints':next_q.tolist(),'side':side,'xyz_workspace':np.asarray(xyz).tolist()});current=next_q
        plan={'target':target,'paths':paths,'push':False,'action':action,'initial_joints':q.tolist(),
              'fingerprint':fingerprint(s),'created':time.monotonic(),'scene_id':scene_id}
        if action=='cycle':
            flips={e['class_name']:inspect_skill(e,self.h.root,fingerprint(c)) for e in s['skills'] if e.get('role')=='flip' and e.get('enabled',True)}
            if target['name'] not in flips:raise SortingError('该类别没有登记翻面技能（role=flip）')
            plan.update(mode='cycle',flip_skills=flips,cycle=cyc,skill=flips[target['name']])
        with self.lock:ident=self.remember(self.plans,plan,4)
        return {'plan_id':ident,'action':action,'paths':paths,'target':target,'expires_in':60}

    def close(self):
        for worker in (self.detector,self.kin,self.control):
            if worker:worker.close()


def validate_settings(value):
    """Allow incomplete setup to autosave, reject malformed control boundaries."""
    if not isinstance(value,dict):raise SortingError('定位与技能配置需要对象')
    for name in ('calibration','safety'):
        if name in value and not isinstance(value[name],dict):raise SortingError(name+' 配置需要对象')
    if 'skills' in value and (not isinstance(value['skills'],list) or any(not isinstance(x,dict) for x in value['skills'])):raise SortingError('技能清单必须是对象数组')
    if 'confidence' in value and not .1<=float(value['confidence'])<=.99:raise SortingError('置信度应在 0.1～0.99')
    for name in ('camera_source','control_python','yolo_model','device'):
        if name in value and not isinstance(value[name],str):raise SortingError(name+' 需要字符串')
    if 'label_rules' in value:
        rules=value['label_rules']
        if not isinstance(rules,list) or any(not isinstance(r,dict) or not isinstance(r.get('pattern'),str) or not isinstance(r.get('destination'),str) for r in rules):raise SortingError('单号规则需要 {pattern,destination} 数组')
        import re
        for r in rules:
            try:re.compile(r['pattern'])
            except re.error:raise SortingError('单号规则正则无效：'+r['pattern'])
    if 'cycle' in value:
        cyc=value['cycle']
        if not isinstance(cyc,dict):raise SortingError('cycle 配置需要对象')
        if cyc.get('left_direction') is not None:left_direction(cyc['left_direction'])
        if cyc.get('arm') not in (None,'','auto','left','right'):raise SortingError('cycle.arm 只能是 auto/left/right')
        if cyc.get('left_push_m') is not None and not .005<=float(cyc['left_push_m'])<=.2:raise SortingError('left_push_m 应在 0.005～0.2 米')
        if cyc.get('min_left_push_m') is not None and not .01<=float(cyc['min_left_push_m'])<=.5:raise SortingError('min_left_push_m 应在 0.01～0.5 米')
        if cyc.get('max_packages') is not None and not 1<=int(cyc['max_packages'])<=500:raise SortingError('max_packages 应在 1～500')
        if cyc.get('max_flips') is not None and not 0<=int(cyc['max_flips'])<=4:raise SortingError('max_flips 应在 0～4')
    if 'label_camera' in value and not isinstance(value['label_camera'],str):raise SortingError('label_camera 需要字符串')
    safe=value.get('safety') or {}
    for name,low,high in [('max_velocity_rad_s',.001,.5),('max_delta_rad',.001,.3),('clearance_m',.05,.3),('max_push_m',.005,.2)]:
        if name in safe and not low<=float(safe[name])<=high:raise SortingError(name+' 超出初期控制范围')
    if safe.get('torque_limits_nm') is not None:
        if np.any(array(safe['torque_limits_nm'],(14,),'电机力矩上限')<=0):raise SortingError('电机力矩上限需要 14 个正数')
    if safe.get('joint_limits_rad') is not None:
        limits=array(safe['joint_limits_rad'],(14,2),'关节限位')
        if np.any(limits[:,0]>=limits[:,1]):raise SortingError('关节限位下限必须小于上限')
    c=value.get('calibration') or {}
    if c.get('workspace_bounds') is not None:bounds(c['workspace_bounds'])
    if c.get('table_plane') is not None:
        plane=array(c['table_plane'],(4,),'桌面平面')
        if not np.allclose(plane[:3],[0,0,1],atol=.001):raise SortingError('工作台 Z 轴须向上，平面法向为 [0,0,1]')
    # Explicitly reject NaN/Infinity even in optional/imported metadata.
    try:fingerprint(value)
    except (TypeError,ValueError):raise SortingError('配置不能包含 NaN、Infinity 或非 JSON 值')
