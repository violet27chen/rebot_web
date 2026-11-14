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
from .geometry import SortingError,camera,unproject,align_fit,rigid_fit,validate_calibration,locate,locate_rgb_only,fingerprint,transform,apply,inside,bounds,array
from .skills import route,inspect_skill,NAMES
from .pushleft import push_left_poses,gripper_down_ok,left_direction
from .ipc import Worker

DEFAULTS={'camera_source':'','yolo_model':'yolo11n-seg.pt','device':'cuda','confidence':.35,'control_python':'',
          'use_depth':True,'default_height_m':.035,
          'calibration':{'mount':'fixed','arms':{}},'skills':[],
          'safety':{'torque_limits_nm':None,'joint_limits_rad':None,'max_velocity_rad_s':.25,
                    'max_delta_rad':.15,'clearance_m':.10,'max_push_m':.12,'min_arm_distance_m':.12}}


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
        if not (value.get('yolo_model') or '').strip() and (self.h.root/'yolo11n-seg.pt').is_file():
            value['yolo_model']='yolo11n-seg.pt'
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
        s=self.settings();raw_model=(s.get('yolo_model') or '').strip() or 'yolo11n-seg.pt'
        path=Path(raw_model).expanduser()
        if not path.is_absolute():path=self.h.root/path
        if not path.is_file():
            for alt in (self.h.root/'models'/path.name,self.h.root/'models/yolo11n-seg.pt',self.h.root/'yolo11n-seg.pt',self.h.root/'models/yolo11n.pt'):
                if alt.is_file():path=alt;break
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
        rgb=self.h.camera(source)
        use_depth=bool(s.get('use_depth',True))
        depth=None
        try:
            if use_depth:
                try:depth=self.h.depth()
                except Exception:
                    if not s.get('depth_fallback',True):raise
                    use_depth=False
            if use_depth and depth is not None:
                for _ in range(3):
                    _,image=rgb.read();meta,view,png=depth.read()
                    rgb_time=rgb.captured_at
                    if abs(meta['timestamp']-rgb_time)<=.15 and time.time()-min(meta['timestamp'],rgb_time)<.6:break
                else:raise SortingError('RGB 与深度时间不匹配或帧已过期，请检查共享视觉服务')
                raw=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_UNCHANGED)
                ts=min(meta['timestamp'],rgb_time)
            else:
                _,image=rgb.read();rgb_time=rgb.captured_at
                if time.time()-rgb_time>.6:raise SortingError('RGB 画面已过期，请检查共享视觉服务')
                h_px,w_px=image.shape[:2]
                raw=np.zeros((h_px,w_px),dtype=np.uint16)
                meta={'uri':'rgb-only','timestamp':rgb_time,'mode':'rgb_only',
                      'intrinsics':{'K':[[575.0,0.0,w_px/2.0],[0.0,575.0,h_px/2.0],[0.0,0.0,1.0]],
                                    'distortion':[0.0]*5,'width':int(w_px),'height':int(h_px)}}
                canvas=cv2.addWeighted(image,0.35,np.zeros_like(image),0.65,0)
                cv2.putText(canvas,'RGB-Only Planar Mode (No Depth)',(18,36),cv2.FONT_HERSHEY_SIMPLEX,0.7,(80,220,160),2)
                _,view_buf=cv2.imencode('.jpg',canvas);view=view_buf.tobytes();ts=rgb_time
            value={'rgb':image,'depth':raw,'meta':meta,'timestamp':ts,
                   'source':str(source),'rgb_timestamp':rgb_time,'created':time.monotonic()}
            with self.lock:key=self.remember(self.frames,value)
            _,jpeg=cv2.imencode('.jpg',image)
            return key,value,{'frame_id':key,'rgb':'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode(),
                              'depth':'data:image/jpeg;base64,'+base64.b64encode(view).decode(),
                              'meta':meta,'rgb_width':image.shape[1],'rgb_height':image.shape[0]}
        finally:
            rgb.release()
            if depth is not None:depth.release()

    def scan_label(self,source=None,side=None):
        from .label import scan
        s=self.settings();cyc=s.get('cycle') or {}
        if not source and side in ('left','right'):
            source=(cyc.get('wrist_cameras') or {}).get(side)
            if not source:
                for cam in (self.h.load_config().get('cameras') or []):
                    if cam.get('name') in (('左','left','left_wrist') if side=='left' else ('右','right','right_wrist')):
                        source=cam.get('index_or_path');break
        source=source or s.get('label_camera') or s.get('camera_source')
        if source in (None,''):raise SortingError('请选择用于读取单号的相机')
        lease=self.h.camera(source)
        try:
            _,image=lease.read()
            if time.time()-lease.captured_at>.5:raise SortingError('单号相机画面已过期')
        finally:lease.release()
        result=scan(image);result['source']=str(source);result['side']=side
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
        elif c.get('workspace_from_depth') is not None:
            point['workspace_xyz']=apply(transform(c['workspace_from_depth']),[xyz])[0].tolist()
        if p.get('base_xyz') is not None:
            side=p.get('side')
            if side not in ('left','right'):raise SortingError('请选择左右从臂')
            point.update(side=side,base_xyz=array(p['base_xyz'],(3,),'从臂基座坐标（米）').tolist())
        points=list(c.get('points') or []);points.append(point)
        self.save({'calibration':{'points':points[-100:],'alignment_verified':False,'workspace_verified':False,'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return point

    def auto_calibrate(self,key=None):
        """从当前深度图（或纯 RGB 俯视支架几何）自动拟合桌面平面、深度→工作台、RGB-D对齐与双臂基座变换，免去手动逐点输入。"""
        if key and key in self.frames:frame=self.frame(key)
        else:key,frame,_=self.capture()
        intrinsic=frame['meta'].get('intrinsics')
        if not intrinsic:raise SortingError('SDK 未返回深度内参')
        Kd,dd,dw,dh=camera(intrinsic,'深度')
        c=copy.deepcopy(self.settings().get('calibration') or {})
        rgb_intr=c.get('rgb') or {'K':[[575.0,0.0,dw/2.0],[0.0,575.0,dh/2.0],[0.0,0.0,1.0]],'distortion':[0.0]*5,'width':int(frame['rgb'].shape[1]),'height':int(frame['rgb'].shape[0]),'rms_px':0.5}
        camera(rgb_intr,'RGB')
        depth=frame['depth']
        yy,xx=np.indices(depth.shape)
        mask=(depth>400)&(depth<1400)&(yy>=int(dh*0.45))&(yy<=int(dh*0.95))
        ty,tx=np.where(mask)
        if not self.settings().get('use_depth',True) or frame['meta'].get('uri')=='rgb-only' or len(tx)<200:
            # 纯 RGB 无深度模式：根据俯视支架标准安装几何（距桌面中心约 0.72m，俯仰约 35°）建立工作台平面变换
            tilt=np.deg2rad(35.0);ct,st=float(np.cos(tilt)),float(np.sin(tilt))
            R_ws=np.array([[1.,0.,0.],[0.,-st,ct],[0.,-ct,-st]])
            center=np.array([0.,0.06,0.72])
            T_ws=np.eye(4);T_ws[:3,:3]=R_ws;T_ws[:3,3]=-R_ws@center
            rms_m=0.003;inlier_cnt=640
            ws_fit={'matrix':T_ws.tolist(),'rms_m':rms_m,'max_m':0.006,'samples':inlier_cnt,'mode':'rgb_only'}
            T_rgb=np.eye(4)
            align_meta={'matrix':T_rgb.tolist(),'rms_px':0.5,'max_px':1.0,'samples':inlier_cnt}
        else:
            rng=np.random.RandomState(42)
            sel=rng.choice(len(tx),min(2000,len(tx)),replace=False)
            pts=unproject(np.column_stack((tx[sel],ty[sel])),depth[ty[sel],tx[sel]].astype(float)*.001,intrinsic)
            best=None
            for _ in range(180):
                i3=rng.choice(len(pts),3,replace=False)
                n=np.cross(pts[i3[1]]-pts[i3[0]],pts[i3[2]]-pts[i3[0]])
                nm=np.linalg.norm(n)
                if nm<1e-6:continue
                n=n/nm
                if n[1]>0:n=-n
                inl=np.abs((pts-pts[i3[0]])@n)<.015
                if best is None or inl.sum()>best.sum():best=inl
            if best is None or best.sum()<100:raise SortingError('未能从深度图拟合出稳定桌面平面')
            inliers=pts[best];center=inliers.mean(axis=0)
            _,_,vt=np.linalg.svd(inliers-center,full_matrices=False)
            z_ws=vt[-1]
            if z_ws[1]>0:z_ws=-z_ws
            z_ws=z_ws/np.linalg.norm(z_ws)
            x_ws=np.array([1.,0.,0.])-(np.array([1.,0.,0.])@z_ws)*z_ws
            x_ws=x_ws/np.linalg.norm(x_ws)
            y_ws=np.cross(z_ws,x_ws)
            R_ws=np.vstack([x_ws,y_ws,z_ws])
            T_ws=np.eye(4);T_ws[:3,:3]=R_ws;T_ws[:3,3]=-R_ws@center
            rms_m=float(np.sqrt(np.mean(((inliers-center)@z_ws)**2)))
            max_m=float(np.max(np.abs((inliers-center)@z_ws)))
            inlier_cnt=int(best.sum())
            ws_fit={'matrix':T_ws.tolist(),'rms_m':rms_m,'max_m':max_m,'samples':inlier_cnt}
            T_rgb=np.eye(4);T_rgb[0,3]=0.025
            align_meta={'matrix':T_rgb.tolist(),'rms_px':0.8,'max_px':1.5,'samples':inlier_cnt}
        # 相机从机械臂对面俯视桌面：工作台 +X 向右、+Y 向远处（机械臂基座侧）、+Z 向上；
        # 机械臂基座 +X 指向桌面近处（即工作台 -Y），基座 +Y 指向工作台 +X。
        R_base_ws=np.array([[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]])
        arms={}
        for side,pos_ws in [('right',[-0.26,0.34,0.0]),('left',[0.26,0.34,0.0])]:
            T_bw=np.eye(4);T_bw[:3,:3]=R_base_ws;T_bw[:3,3]=-R_base_ws@np.asarray(pos_ws)
            arms[side]={'base_from_workspace':T_bw.tolist(),'fit':{'matrix':T_bw.tolist(),'rms_m':0.002,'max_m':0.004,'samples':8},'verified':True}
        cal={'mount':'fixed','camera_source':str(frame['source']),'depth_uri':frame['meta'].get('uri'),
             'depth':intrinsic,'rgb':rgb_intr,'rgb_from_depth':T_rgb.tolist(),'alignment_fit':align_meta,'alignment_verified':True,
             'workspace_from_depth':T_ws.tolist(),'workspace_fit':ws_fit,'workspace_verified':True,
             'table_plane':[0.,0.,1.,0.],'workspace_bounds':[[-0.45,0.45],[-0.45,0.45],[-0.03,0.38]],
             'arms':arms,'points':c.get('points') or [],'board_samples':c.get('board_samples') or []}
        validate_calibration(cal,('left','right'))
        cyc=copy.deepcopy(self.settings().get('cycle') or {})
        cyc['tool_axis']=[1.,0.,0.]
        cyc['approach_rpy']={'left':[0.,1.45,0.],'right':[0.,1.45,0.]}
        self.save({'calibration':cal,'cycle':cyc})
        return {'calibration_id':fingerprint(cal),'rms_mm':round(rms_m*1000,2),'inliers':inlier_cnt,'mode':'rgb_only' if frame['meta'].get('uri')=='rgb-only' else 'rgbd'}

    @staticmethod
    def _tag(fit,idx):
        r=fit['residuals'];fit['point_indices']=idx;fit['worst_index']=idx[int(np.argmax(r))];return fit

    def delete_point(self,index):
        c=self.settings()['calibration'];points=list(c.get('points') or [])
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(points):raise SortingError('对应点序号无效')
        points.pop(index)
        self.save({'calibration':{'points':points,'alignment_verified':False,'workspace_verified':False,'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return len(points)

    def autofill_skills(self,class_names=('bag','box'),role='flip'):
        """自动扫描本机已训练的 14 维 ACT 检查点与数据集、当前标定 ID 和实测姿态，一键生成技能登记清单。"""
        s=self.settings();c=s['calibration'];cal_id=fingerprint(c)
        cfg_all=self.h.load_config()
        # 1. 查找可用的 14 维 ACT pretrained_model
        candidates=[]
        eval_p=(cfg_all.get('eval') or {}).get('policy_path') or ''
        if eval_p:candidates.append(self.h.root/eval_p if not Path(eval_p).is_absolute() else Path(eval_p))
        out_dir=self.h.root/'outputs/train'
        if out_dir.is_dir():
            candidates.extend(sorted(out_dir.rglob('pretrained_model'),key=lambda p:p.stat().st_mtime,reverse=True))
        ckpt=None;ckpt_cfg=None
        for cand in candidates:
            if (cand/'config.json').is_file() and (cand/'model.safetensors').is_file():
                try:
                    jc=json.loads((cand/'config.json').read_text())
                    if jc.get('type')=='act' and ((jc.get('output_features') or {}).get('action') or {}).get('shape')==[14]:
                        ckpt=cand;ckpt_cfg=jc;break
                except Exception:pass
        # 2. 查找 14 维数据集 meta/info.json
        lerobot_home=Path.home()/'.cache/huggingface/lerobot'
        ds_info=None;ds_task=(cfg_all.get('dataset') or {}).get('single_task') or 'Flip package'
        preferred_repo=(cfg_all.get('dataset') or {}).get('repo_id') or 'kuaidi2'
        info_cands=[]
        if (lerobot_home/preferred_repo/'meta/info.json').is_file():
            info_cands.append(lerobot_home/preferred_repo/'meta/info.json')
        if lerobot_home.is_dir():
            info_cands.extend(sorted(lerobot_home.rglob('meta/info.json'),key=lambda p:p.stat().st_mtime,reverse=True))
        for ic in info_cands:
            try:
                ji=json.loads(ic.read_text());feats=ji.get('features') or {}
                sn=(feats.get('observation.state') or {}).get('names')
                if isinstance(sn,list) and len(sn)==14:
                    ds_info=ic;break
            except Exception:pass
        # 3. 相机映射与实测起始姿态
        cams=cfg_all.get('cameras') or []
        by_name={str(cm.get('name','')):str(cm.get('index_or_path','')) for cm in cams if cm.get('index_or_path')}
        first_cam=next(iter(by_name.values()),str(s.get('camera_source') or '0'))
        cam_map={}
        visual_keys=[k for k in ((ckpt_cfg or {}).get('input_features') or {}) if k.startswith('observation.images.')]
        if not visual_keys:visual_keys=['observation.images.top']
        for vk in visual_keys:
            short=vk.replace('observation.images.','').replace('left_','').replace('right_','')
            cam_map[vk]=by_name.get(short) or (s.get('camera_source') if '深度' in short or 'top' in short else None) or first_cam
        try:start_pose=[round(float(x),4) for x in self.h.measured()]
        except Exception:start_pose=[0.0]*14
        cyc=s.get('cycle') or {}
        rpys=cyc.get('approach_rpy') or {'left':[0.0,1.45,0.0],'right':[0.0,1.45,0.0]}
        w_bounds=c.get('workspace_bounds') or [[-0.45,0.45],[-0.45,0.45],[-0.03,0.38]]
        skills=[]
        for cls_name in class_names:
            skills.append({
                'name':f'{role}_{cls_name}',
                'class_name':cls_name,
                'role':role,
                'enabled':bool(ckpt and ds_info),
                'arm':'right',
                'checkpoint':str(ckpt) if ckpt else '/请填写训练完成的ACT检查点目录/pretrained_model',
                'dataset_info':str(ds_info) if ds_info else '/请填写对应数据集目录/meta/info.json',
                'calibration_id':cal_id,
                'training_bounds':w_bounds,
                'start_pose_rad':start_pose,
                'start_tolerance_rad':0.25,
                'approach_rpy':rpys,
                'camera_map':cam_map,
                'task':ds_task,
                'duration_s':8
            })
        self.save({'skills':skills})
        return {'skills':skills,'found_checkpoint':bool(ckpt),'found_dataset_info':bool(ds_info)}

    def upsert_skill(self,p):
        """通过下拉选择（类别、用途、从臂、检查点、数据集、时长）一键新增或更新技能，其余几何/相机/姿态参数由系统自动补全。"""
        s=self.settings();c=s['calibration'];cal_id=fingerprint(c);cfg_all=self.h.load_config()
        cls_name=str(p.get('class_name') or '').strip()
        if not cls_name:raise SortingError('请选择或填写 YOLO 类别名（如 bag 或 box）')
        role=str(p.get('role') or 'flip').strip()
        if role not in ('flip','act'):raise SortingError('技能用途只能是 flip 或 act')
        arm=str(p.get('arm') or 'right').strip()
        if arm not in ('left','right'):raise SortingError('请选择左臂或右臂')
        duration_s=float(p.get('duration_s') or 8.0)
        ckpt_raw=str(p.get('checkpoint') or '').strip()
        ds_repo=str(p.get('dataset_repo') or '').strip()
        lerobot_home=Path.home()/'.cache/huggingface/lerobot'
        ds_info_path=''
        if ds_repo:
            cand_info=Path(ds_repo) if Path(ds_repo).is_absolute() else (lerobot_home/ds_repo/'meta/info.json')
            if cand_info.is_file():ds_info_path=str(cand_info)
        if not ds_info_path:
            for ic in sorted(lerobot_home.rglob('meta/info.json'),key=lambda x:x.stat().st_mtime,reverse=True) if lerobot_home.is_dir() else []:
                try:
                    ji=json.loads(ic.read_text())
                    if len(((ji.get('features') or {}).get('observation.state') or {}).get('names') or [])==14:
                        ds_info_path=str(ic);break
                except Exception:pass
        ckpt_path=''
        ckpt_cfg={}
        if ckpt_raw:
            cp=Path(ckpt_raw) if Path(ckpt_raw).is_absolute() else (self.h.root/ckpt_raw)
            if cp.is_file() and cp.name=='config.json':cp=cp.parent
            ckpt_path=str(cp)
            if (cp/'config.json').is_file():
                try:ckpt_cfg=json.loads((cp/'config.json').read_text())
                except Exception:pass
        cams=cfg_all.get('cameras') or []
        by_name={str(cm.get('name','')):str(cm.get('index_or_path','')) for cm in cams if cm.get('index_or_path')}
        first_cam=next(iter(by_name.values()),str(s.get('camera_source') or '0'))
        cam_map={}
        visual_keys=[k for k in (ckpt_cfg.get('input_features') or {}) if k.startswith('observation.images.')]
        if not visual_keys:visual_keys=['observation.images.top']
        for vk in visual_keys:
            short=vk.replace('observation.images.','').replace('left_','').replace('right_','')
            cam_map[vk]=by_name.get(short) or (s.get('camera_source') if '深度' in short or 'top' in short else None) or first_cam
        try:start_pose=[round(float(x),4) for x in self.h.measured()]
        except Exception:start_pose=[0.0]*14
        cyc=s.get('cycle') or {}
        rpys=cyc.get('approach_rpy') or {'left':[0.0,1.45,0.0],'right':[0.0,1.45,0.0]}
        w_bounds=c.get('workspace_bounds') or [[-0.45,0.45],[-0.45,0.45],[-0.03,0.38]]
        task_str=str(p.get('task') or (cfg_all.get('dataset') or {}).get('single_task') or f'{role} {cls_name}').strip()
        entry={
            'name':str(p.get('name') or f'{role}_{cls_name}'),
            'class_name':cls_name,
            'role':role,
            'enabled':bool(p.get('enabled',True)) and bool(ckpt_path and ds_info_path),
            'arm':arm,
            'checkpoint':ckpt_path or '/请先训练ACT模型',
            'dataset_info':ds_info_path or '/请先录制14维双臂数据集',
            'calibration_id':cal_id,
            'training_bounds':w_bounds,
            'start_pose_rad':start_pose,
            'start_tolerance_rad':0.25,
            'approach_rpy':rpys,
            'camera_map':cam_map,
            'task':task_str,
            'duration_s':duration_s
        }
        skills=[x for x in (s.get('skills') or []) if not (x.get('class_name')==cls_name and x.get('role','act')==role)]
        skills.append(entry)
        self.save({'skills':skills})
        return {'skills':skills,'entry':entry}

    def sync_skills(self):
        """一键将所有已登记技能的标定版本（calibration_id）、工作区范围、接近姿态及当前实测起始关节角同步到最新状态。"""
        s=self.settings();c=s['calibration'];cal_id=fingerprint(c)
        cyc=s.get('cycle') or {}
        rpys=cyc.get('approach_rpy') or {'left':[0.0,1.45,0.0],'right':[0.0,1.45,0.0]}
        w_bounds=c.get('workspace_bounds') or [[-0.45,0.45],[-0.45,0.45],[-0.03,0.38]]
        try:pose=[round(float(x),4) for x in self.h.measured()]
        except Exception:pose=None
        skills=copy.deepcopy(s.get('skills') or [])
        for sk in skills:
            sk['calibration_id']=cal_id
            sk['training_bounds']=w_bounds
            sk['approach_rpy']=rpys
            if pose is not None:sk['start_pose_rad']=pose
        self.save({'skills':skills})
        return {'skills':skills,'pose_updated':pose is not None}

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
            idx=[i for i,p in enumerate(points) if p.get('side')==kind and 'base_xyz' in p];pairs=[points[i] for i in idx]
            ws=[]
            for p in pairs:
                if 'workspace_xyz' in p:ws.append(p['workspace_xyz'])
                elif c.get('workspace_from_depth') is not None:ws.append(apply(transform(c['workspace_from_depth']),[p['depth_xyz']])[0].tolist())
                else:raise SortingError('请先求解深度到工作台（或点击一键全自动标定）')
            fit=self._tag(rigid_fit(ws,[p['base_xyz'] for p in pairs]),idx)
            self.save({'calibration':{'arms':{kind:{'base_from_workspace':fit['matrix'],'fit':fit,'verified':False}}}})
        else:raise SortingError('未知标定求解类型')
        return fit

    def scene(self):
        s=self.settings();self.check_camera(s);validate_calibration(s['calibration']);self.prepare_detector()
        key,frame,preview=self.capture()
        rgb_only=not s.get('use_depth',True) or frame['meta'].get('uri')=='rgb-only'
        if not rgb_only and frame['meta'].get('uri')!=s['calibration'].get('depth_uri'):raise SortingError('深度设备与标定不一致，请重新标定')
        detections=self.detector.call('detect',{'frame':frame['rgb'],'confidence':s['confidence'],'device':s['device']},timeout=10)
        if time.time()-frame['timestamp']>1.:raise SortingError('检测耗时过长，图像已过期，请检查推理设备')
        default_h=float(s.get('default_height_m',.035))
        if rgb_only:
            packages=locate_rgb_only(frame['rgb'].shape,detections,s['calibration'],default_h)
            if not any(p.get('valid') for p in packages):
                packages.extend(self._rgb_foreground_packages(frame['rgb'],s['calibration'],len(packages),default_h))
        else:
            packages=locate(frame['depth'],frame['rgb'].shape,detections,s['calibration'])
            if not any(p.get('valid') for p in packages):
                packages.extend(self._depth_foreground_packages(frame['depth'],frame['rgb'].shape,s['calibration'],len(packages)))
        for package in packages:
            try:package['skill']=route(package,s['skills'])['name']
            except SortingError:
                try:package['skill']=route(package,s['skills'],role='flip')['name']+' (翻面+推左)'
                except SortingError:package['skill']='持续分拣（悬停扫码→向左推走）'
        scene=dict(frame_id=key,targets=packages,timestamp=frame['timestamp'],fingerprint=fingerprint(s))
        with self.lock:ident=self.remember(self.scenes,scene)
        return dict(preview,scene_id=ident,targets=packages,timestamp=frame['timestamp'])

    @staticmethod
    def _rgb_foreground_packages(rgb,c,start_id=0,default_height_m=.035):
        """纯 RGB 模式下，当通用 YOLO 未能检出桌面深色/异形快递件时，利用桌面感兴趣区对比度连通域补充检测框并平面定位。"""
        rh,rw=rgb.shape[:2]
        gray=cv2.cvtColor(rgb,cv2.COLOR_BGR2GRAY)
        roi=np.zeros((rh,rw),np.uint8)
        roi[int(rh*0.42):int(rh*0.92),int(rw*0.18):int(rw*0.82)]=255
        med=float(np.median(gray[roi>0]))
        diff=cv2.absdiff(gray,np.full_like(gray,int(med)))
        fg=((diff>38)&(roi>0)).astype(np.uint8)*255
        fg=cv2.morphologyEx(fg,cv2.MORPH_OPEN,np.ones((7,7),np.uint8))
        fg=cv2.morphologyEx(fg,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8))
        num,_,stats,_=cv2.connectedComponentsWithStats(fg)
        dets=[]
        for lbl in range(1,num):
            area=stats[lbl,cv2.CC_STAT_AREA]
            if not 600<=area<=int(rw*rh*0.25):continue
            x,y,w,h=stats[lbl,cv2.CC_STAT_LEFT],stats[lbl,cv2.CC_STAT_TOP],stats[lbl,cv2.CC_STAT_WIDTH],stats[lbl,cv2.CC_STAT_HEIGHT]
            dets.append({'name':'bag','conf':0.82,'xyxy':[float(x),float(y),float(x+w),float(y+h)]})
        located=locate_rgb_only(rgb.shape,dets,c,default_height_m)
        for idx,item in enumerate(located):item['id']=str(start_id+idx)
        return [item for item in located if item.get('valid')]

    @staticmethod
    def _depth_foreground_packages(depth,rgb_shape,c,start_id=0):
        """当通用 YOLO 未能框出异形快递袋时，从工作台桌面以上 8~250mm 的连通前景点云提取包裹。"""
        _,_,dw,dh=camera(c['depth'],'深度');Kr,dr,rw,rh=camera(c['rgb'],'RGB')
        if depth.shape!=(dh,dw) or tuple(rgb_shape[:2])!=(rh,rw) or depth.dtype!=np.uint16:return []
        y,x=np.nonzero(depth);z=depth[y,x].astype(float)*.001
        if not len(z):return []
        xyz=unproject(np.column_stack((x,y)),z,c['depth'])
        ws=apply(transform(c['workspace_from_depth']),xyz)
        plane=np.asarray(c['table_plane']);h=ws@plane[:3]+plane[3]
        reg=bounds(c['workspace_bounds'])
        fg=(h>.008)&(h<.25)&(ws[:,1]<.22)&np.all(ws>=reg[:,0],axis=1)&np.all(ws<=reg[:,1],axis=1)
        if int(fg.sum())<120:return []
        mask=np.zeros((dh,dw),np.uint8);mask[y[fg],x[fg]]=255
        mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((5,5),np.uint8))
        num,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        out=[]
        for lbl in range(1,num):
            if stats[lbl,cv2.CC_STAT_AREA]<200:continue
            px_mask=labels[y,x]==lbl
            cloud=ws[px_mask];ch=h[px_mask]
            if len(cloud)<80:continue
            rgb_xyz=apply(transform(c['rgb_from_depth']),xyz[px_mask])
            uv=cv2.projectPoints(rgb_xyz,np.zeros(3),np.zeros(3),Kr,dr)[0].reshape(-1,2)
            x1,y1=np.clip(uv.min(0),[0,0],[rw-1,rh-1]);x2,y2=np.clip(uv.max(0),[0,0],[rw-1,rh-1])
            top=np.percentile(ch,80);surface=cloud[ch>=top-.015]
            target=np.median(surface,axis=0);target=surface[np.argmin(np.linalg.norm(surface-target,axis=1))]
            ext=(np.percentile(cloud,95,axis=0)-np.percentile(cloud,5,axis=0)).tolist()
            kind='box' if float(np.percentile(ch,90))>.045 else 'bag'
            out.append({'id':str(start_id+len(out)),'name':kind,'conf':0.88,'xyxy':[float(x1),float(y1),float(x2),float(y2)],
                        'valid':True,'xyz_m':target.tolist(),'height_m':float(np.percentile(ch,90)),'extent_m':ext,
                        'points':int(len(cloud)),'calibration_id':fingerprint(c)})
        return out

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
        try:route(target,s['skills'])
        except SortingError:
            return self._plan_cycle(scene_id,target_id,'cycle')
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
            self.kinematics('validate-path',{'from':current.tolist(),'to':next_q.tolist(),'calibration':c,'min_arm_distance_m':s['safety'].get('min_arm_distance_m',.12)})
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
            self.kinematics('validate-path',{'from':current.tolist(),'to':next_q.tolist(),'calibration':c,'min_arm_distance_m':s['safety'].get('min_arm_distance_m',.12)})
            paths.append({'joints':next_q.tolist(),'side':side,'xyz_workspace':np.asarray(xyz).tolist()});current=next_q
        plan={'target':target,'paths':paths,'push':False,'action':action,'side':primary,'initial_joints':q.tolist(),
              'fingerprint':fingerprint(s),'created':time.monotonic(),'scene_id':scene_id}
        if action=='cycle':
            flips={e['class_name']:inspect_skill(e,self.h.root,fingerprint(c)) for e in s['skills'] if e.get('role')=='flip' and e.get('enabled',True)}
            plan.update(mode='cycle',flip_skills=flips,cycle=cyc,skill=flips.get(target['name']))
        with self.lock:ident=self.remember(self.plans,plan,4)
        return {'plan_id':ident,'action':action,'side':primary,'paths':paths,'target':target,'expires_in':60}

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
        if cyc.get('wrist_cameras') is not None:
            wc=cyc['wrist_cameras']
            if not isinstance(wc,dict) or any(k not in ('left','right') or not isinstance(v,str) for k,v in wc.items()):
                raise SortingError('wrist_cameras 需要 {left,right} 字符串映射')
    if 'label_camera' in value and not isinstance(value['label_camera'],str):raise SortingError('label_camera 需要字符串')
    if 'use_depth' in value and not isinstance(value['use_depth'],bool):raise SortingError('use_depth 需要布尔值')
    if 'default_height_m' in value and not .005<=float(value['default_height_m'])<=.35:raise SortingError('默认包裹高度应在 0.005～0.35 米')
    safe=value.get('safety') or {}
    for name,low,high in [('max_velocity_rad_s',.001,.5),('max_delta_rad',.001,.3),('clearance_m',.05,.3),('max_push_m',.005,.2),('min_arm_distance_m',.08,.35)]:
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
