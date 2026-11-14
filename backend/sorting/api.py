"""Sorting routes are attached to functional pages; no new mode-only page."""
import copy
import json
import os
import secrets
import time
from pathlib import Path
from fastapi import APIRouter,Body,Request
from fastapi.routing import APIRoute
import cv2
import numpy as np
from .geometry import SortingError,array,validate_calibration,fingerprint,transform
from .service import SortingService
from .ipc import Worker


class SortingRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handle(request):
            try:return await original(request)
            except SortingError:raise
            except (KeyError,TypeError,ValueError,cv2.error) as exc:
                raise SortingError('参数不完整或格式无效：'+str(exc)) from exc
        return handle


def attach(app,h):
    service=SortingService(h);router=APIRouter(prefix='/api/sorting',route_class=SortingRoute)
    @app.exception_handler(SortingError)
    async def sorting_error(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'ok':False,'error':str(exc)},status_code=400)
    @router.get('/status')
    def status():
        s=service.settings();errors=[]
        try:validate_calibration(s['calibration'],('left','right'))
        except SortingError as exc:errors.append(str(exc))
        try:service.check_camera(s)
        except SortingError as exc:errors.append(str(exc))
        if not s['yolo_model']:errors.append('尚未选择 YOLO 检测模型')
        if s['safety'].get('torque_limits_nm') is None or s['safety'].get('joint_limits_rad') is None:errors.append('尚未配置 14 个电机的力矩上限和原始关节限位')
        return {'ok':True,'version':h.version(),'settings':s,'calibration_id':fingerprint(s['calibration']),
                'ready':not errors,'errors':errors,'control_active':bool(service.control and service.control.alive()),
                'session':{'job_id':service.session.get('job_id'),'phase':service.session.get('phase')} if service.session else None}
    @router.post('/capture')
    def capture():return {'ok':True,**service.capture()[2]}
    @router.post('/label/scan')
    def label_scan(p:dict=Body(default={})):return {'ok':True,**service.scan_label(p.get('source'),p.get('side'))}
    @router.post('/calibration/depth')
    def depth(p:dict=Body(...)):return {'ok':True,'intrinsics':service.adopt_depth(p['frame_id'])}
    @router.post('/calibration/auto')
    def calib_auto(p:dict=Body(default={})):return {'ok':True,**service.auto_calibrate(p.get('frame_id'))}
    @router.post('/calibration/board')
    def board(p:dict=Body(...)):
        return {'ok':True,**service.board(p['frame_id'],int(p['columns']),int(p['rows']),float(p['square_m']))}
    @router.post('/calibration/point')
    def point(p:dict=Body(...)):return {'ok':True,'point':service.add_point(p)}
    @router.post('/calibration/point/delete')
    def point_delete(p:dict=Body(...)):return {'ok':True,'points':service.delete_point(p.get('index'))}
    @router.post('/calibration/solve')
    def solve(p:dict=Body(...)):
        if p['kind']=='rgb':return {'ok':True,'fit':service.solve_rgb()}
        return {'ok':True,'fit':service.solve(p['kind'])}
    @router.post('/calibration/confirm')
    def confirm(p:dict=Body(...)):
        if p.get('confirm') is not True:raise SortingError('确认标定结果需要二次确认')
        c=copy.deepcopy(service.settings()['calibration']);kind=p.get('kind')
        if kind=='alignment':
            if not c.get('alignment_fit'):raise SortingError('请先求解 RGB-D 对齐')
            transform(c.get('rgb_from_depth'));c['alignment_verified']=True
        elif kind=='workspace':
            if not c.get('workspace_fit'):raise SortingError('请先求解工作台坐标')
            transform(c.get('workspace_from_depth'));c['workspace_verified']=True
        elif kind in ('left','right'):
            arm=(c.get('arms') or {}).get(kind) or {}
            if not arm.get('fit'):raise SortingError('请先求解对应从臂基座坐标')
            transform(arm.get('base_from_workspace'));arm['verified']=True;c['arms'][kind]=arm
        else:raise SortingError('无效的标定确认类型')
        service.save({'calibration':c});return {'ok':True,'calibration_id':fingerprint(c)}
    @router.post('/calibration/reset')
    def reset(p:dict=Body(...)):
        if p.get('confirm') is not True:raise SortingError('清空标定采样需要确认')
        service.save({'calibration':{'points':[],'board_samples':[],'alignment_verified':False,'workspace_verified':False,
                                     'arms':{'left':{'verified':False},'right':{'verified':False}}}})
        return {'ok':True}
    @router.get('/fk')
    def fk(side:str='left'):
        if side not in ('left','right'):raise SortingError('无效从臂')
        q=h.measured();i=0 if side=='left' else 7
        return {'ok':True,**service.kinematics('fk',{'joints':q[i:i+6]})}
    @router.post('/detect')
    def detect():return {'ok':True,**service.scene()}
    @router.post('/plan')
    def plan(p:dict=Body(...)):return {'ok':True,**service.plan(p['scene_id'],p['target_id'],p.get('action'))}
    @router.post('/execute')
    def execute(p:dict=Body(...)):
        if p.get('confirm') is not True:raise SortingError('执行规则推拢与 ACT 需要明确二次确认')
        with h.lock,service.lock:
            if h.running():raise SortingError('已有任务运行，请先停止')
            if service.control and service.control.alive():raise SortingError('上一动作的 CAN 进程仍保持姿态，请先收工归零并失能，再连接和预演')
            proposal=service.plans.get(p.get('plan_id'))
            if not proposal or time.monotonic()-proposal['created']>60:raise SortingError('预演已过期，请重新检测预演')
            if proposal['fingerprint']!=fingerprint(service.settings()):raise SortingError('配置或标定已变化，请重新预演')
            h.require_connected()
            service.prepare_detector()
            token=secrets.token_urlsafe(32);heartbeat=service.folder/'heartbeat'
            heartbeat.write_text('0');os.chmod(heartbeat,0o600)
            service.session={'token':token,'proposal':copy.deepcopy(proposal),'job_id':None,'heartbeat':heartbeat,
                             'phase':'PREPARE','settings':service.settings()}
            cmd=[h.python(),'-u','-m','backend.sorting.runner',json.dumps({'url':h.local_url(),'token':token,'plan_id':p['plan_id'],'settings':service.settings(),'proposal':proposal,'root':str(h.root)})]
            try:result=h.spawn('sorting-run','RGB-D 推拢与 ACT',cmd,display='RGB-D → YOLO → 标定坐标预演 → 规则推拢 → 独立 ACT 技能')
            except Exception:
                service.session=None;heartbeat.unlink(missing_ok=True);raise
            service.session['job_id']=result['job_id'];return result
    @router.post('/heartbeat')
    def heartbeat(p:dict=Body(...)):
        session=service.session
        if not session or not h.running() or session['job_id']!=p.get('job_id'):raise SortingError('自动操作会话已结束')
        temp=service.folder/('heartbeat-'+secrets.token_hex(6)+'.tmp');temp.write_text(str(time.monotonic()));temp.replace(session['heartbeat'])
        return {'ok':True}
    @router.post('/internal')
    def internal(request:Request,p:dict=Body(...)):
        session=service.session
        if request.client.host not in ('127.0.0.1','::1') or not session or not secrets.compare_digest(str(p.get('token','')),session['token']):raise SortingError('自动操作会话校验失败')
        if not h.running():raise SortingError('自动操作任务已停止')
        method=p.get('method');value=p.get('payload') or {}
        if fingerprint(service.settings())!=fingerprint(session['settings']):raise SortingError('执行期间配置发生变化，已停止继续运动')
        if method=='init':
            if service.control and service.control.alive():
                state=service.control.call('state')
                if state['fault']:raise SortingError(state['fault'])
                if np.max(abs(np.asarray(state['joints'])-session['proposal']['initial_joints']))>.035:raise SortingError('起始姿态已变化，请重新预演')
                return {'ok':True,'value':state}
            h.prepare_control()
            service.control=Worker(service.control_python(),'backend.sorting.control_worker',h.root,env=h.environment())
            safety=copy.deepcopy(service.settings()['safety']);safety.update(arms=h.arms(),heartbeat_path=str(session['heartbeat']),seed_rad=session['proposal']['initial_joints'])
            return {'ok':True,'value':service.control.call('init',safety,timeout=60)}
        if method=='scene':return {'ok':True,'value':service.scene()}
        if method=='plan':return {'ok':True,'value':service.plan(value['scene_id'],value['target_id'],value.get('action'))}
        if method=='label':return {'ok':True,'value':service.scan_label(value.get('source'),value.get('side'))}
        if method=='validate':return {'ok':True,'value':service.kinematics('validate-path',{'from':value['from'],'to':value['to'],'calibration':service.settings()['calibration']})}
        if method=='frames':
            result={}
            for feature,source in (value.get('camera_map') or session['proposal']['skill']['camera_map']).items():
                lease=h.camera(source)
                try:
                    _,frame=lease.read()
                    if time.time()-lease.captured_at>.5:raise SortingError('ACT 相机画面已过期')
                    # Binary frames stay local; HTTP uses lossless PNG, not MJPEG.
                    import cv2,base64
                    ok,encoded=cv2.imencode('.png',frame)
                    if not ok:raise SortingError('ACT 相机编码失败')
                    result[feature]={'png':base64.b64encode(encoded).decode(),'timestamp':lease.captured_at}
                finally:lease.release()
            return {'ok':True,'value':result}
        if method=='phase':
            session['phase']=str(value['phase']);h.log('[分拣] '+session['phase']);return {'ok':True,'value':True}
        if method in ('state','target','hold'):
            if not service.control or not service.control.alive():raise SortingError('独立 CAN 进程未就绪')
            return {'ok':True,'value':service.control.call(method,value,timeout=3)}
        raise SortingError('未知自动操作请求')
    app.include_router(router)
    return service
