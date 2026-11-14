"""Metric RGB-D geometry; no camera, transport or motor operations."""
import hashlib
import json
import math
import cv2
import numpy as np


class SortingError(ValueError):
    pass


def array(value, shape, name):
    try:a=np.asarray(value,dtype=float)
    except (TypeError,ValueError):raise SortingError(name+' 必须是有效数值')
    if a.shape!=shape or not np.isfinite(a).all():raise SortingError(f'{name} 需要 {shape} 有限数值')
    return a


def transform(value,name='坐标变换'):
    a=array(value,(4,4),name)
    if not np.allclose(a[3],[0,0,0,1],atol=1e-6) or not np.allclose(a[:3,:3].T@a[:3,:3],np.eye(3),atol=1e-4) or abs(np.linalg.det(a[:3,:3])-1)>1e-4:
        raise SortingError(name+' 必须是单位为米的刚体变换，不能包含缩放或镜像')
    return a


def camera(value,name):
    value=value or {};K=array(value.get('K'),(3,3),name+' 内参')
    if K[0,0]<=0 or K[1,1]<=0 or not np.allclose(K[2],[0,0,1]):raise SortingError(name+' 焦距或内参格式无效')
    w,h=value.get('width'),value.get('height')
    if not all(isinstance(i,int) and not isinstance(i,bool) and 16<=i<=7680 for i in [w,h]):raise SortingError(name+' 缺少有效标定分辨率')
    d=np.asarray(value.get('distortion',[0]*5),dtype=float)
    if d.size not in (4,5,8,12,14) or not np.isfinite(d).all():raise SortingError(name+' 畸变参数无效')
    return K,d,w,h


def apply(T,points):
    return np.asarray(points)@T[:3,:3].T+T[:3,3]


def unproject(pixels,depth_m,intrinsic):
    K,d,_,_=camera(intrinsic,'深度')
    if len(np.asarray(pixels).reshape(-1,2))==0:return np.empty((0,3))
    rays=cv2.undistortPoints(np.asarray(pixels,dtype=np.float64).reshape(-1,1,2),K,d).reshape(-1,2)
    return np.column_stack((rays,np.ones(len(rays))))*np.asarray(depth_m).reshape(-1,1)


def rigid_fit(source,target):
    a=np.asarray(source,dtype=float);b=np.asarray(target,dtype=float)
    if a.shape!=b.shape or a.ndim!=2 or a.shape[1]!=3 or len(a)<6 or not np.isfinite(a).all() or not np.isfinite(b).all():raise SortingError('至少需要六对有效三维对应点（米）')
    if np.linalg.matrix_rank(a-a.mean(0),tol=.001)<3:raise SortingError('三维标定点需覆盖平面并包含至少一个不同高度的点')
    u,_,vt=np.linalg.svd((a-a.mean(0)).T@(b-b.mean(0)))
    R=vt.T@u.T
    if np.linalg.det(R)<0:vt[-1]*=-1;R=vt.T@u.T
    T=np.eye(4);T[:3,:3]=R;T[:3,3]=b.mean(0)-R@a.mean(0)
    errors=np.linalg.norm(apply(T,a)-b,axis=1)
    if np.sqrt(np.mean(errors**2))>.005 or errors.max()>.012:raise SortingError('三维标定误差过大，请检查对应点和毫米/米单位')
    return {'matrix':T.tolist(),'rms_m':float(np.sqrt(np.mean(errors**2))),'max_m':float(errors.max()),'samples':len(a),'residuals':errors.tolist()}


def align_fit(points,pixels,rgb):
    xyz=np.asarray(points,dtype=np.float64);uv=np.asarray(pixels,dtype=np.float64)
    if xyz.ndim!=2 or uv.ndim!=2:raise SortingError('RGB-D 对齐至少需要八对对应点')
    if xyz.shape!=(len(xyz),3) or uv.shape!=(len(xyz),2) or len(xyz)<8 or not np.isfinite(xyz).all() or not np.isfinite(uv).all():raise SortingError('RGB-D 对齐至少需要八对同一物理点的深度三维点和 RGB 像素')
    K,d,_,_=camera(rgb,'RGB')
    ok,r,t,inliers=cv2.solvePnPRansac(xyz,uv,K,d,iterationsCount=200,reprojectionError=2.5,confidence=.999,flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok or inliers is None or len(inliers)<max(6,int(len(xyz)*.8)):raise SortingError('RGB-D 对应点不一致，无法建立可靠对齐')
    indices=inliers.ravel();r,t=cv2.solvePnPRefineLM(xyz[indices],uv[indices],K,d,r,t)
    T=np.eye(4);T[:3,:3]=cv2.Rodrigues(r)[0];T[:3,3]=t.ravel()
    projected=cv2.projectPoints(xyz,r,t,K,d)[0].reshape(-1,2)
    error=np.linalg.norm(projected-uv,axis=1)
    if np.any(apply(T,xyz)[:,2]<=0) or error.max()>4 or np.sqrt(np.mean(error**2))>2:raise SortingError('RGB-D 重投影误差未通过，请重新标定')
    return {'matrix':T.tolist(),'rms_px':float(np.sqrt(np.mean(error**2))),'max_px':float(error.max()),'samples':len(xyz),'residuals':error.tolist()}


def bounds(value,name='工作区'):
    a=array(value,(3,2),name)
    if np.any(a[:,1]<=a[:,0]) or np.any(a[:,1]-a[:,0]>5):raise SortingError(name+' 上下限无效（米）')
    return a


def inside(point,region,margin=0):
    b=bounds(region);p=array(point,(3,),'三维目标')
    return bool(np.all(p>=b[:,0]+margin)&np.all(p<=b[:,1]-margin))


def validate_calibration(c,arms=()):
    if not isinstance(c,dict) or c.get('mount')!='fixed':raise SortingError('当前流程需要固定俯视相机标定')
    camera(c.get('depth'),'深度');camera(c.get('rgb'),'RGB')
    transform(c.get('rgb_from_depth'),'RGB←深度')
    transform(c.get('workspace_from_depth'),'工作台←深度')
    plane=array(c.get('table_plane'),(4,),'桌面平面')
    if not np.isclose(np.linalg.norm(plane[:3]),1,atol=.001) or not np.allclose(plane[:3],[0,0,1],atol=.001):raise SortingError('工作台坐标的 Z 轴必须垂直桌面向上，平面参数为 [0,0,1,-桌面Z]')
    bounds(c.get('workspace_bounds'))
    if not c.get('alignment_verified') or not c.get('workspace_verified'):raise SortingError('请完成 RGB-D 对齐、工作区标定并确认验证结果')
    for side in arms:
        entry=(c.get('arms') or {}).get(side) or {}
        transform(entry.get('base_from_workspace'),side+' 基座←工作台')
        if not entry.get('verified'):raise SortingError(side+' 从臂坐标尚未验证')
    return c


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def locate(depth,rgb_shape,detections,c,*,min_points=25):
    validate_calibration(c)
    _,_,dw,dh=camera(c['depth'],'深度');Kr,dr,rw,rh=camera(c['rgb'],'RGB')
    if depth.shape!=(dh,dw) or tuple(rgb_shape[:2])!=(rh,rw):raise SortingError('当前图像分辨率与标定不一致，请重新标定或恢复原分辨率')
    if depth.dtype!=np.uint16:raise SortingError('定位需要原始 uint16 毫米深度，不能使用伪彩截图')
    y,x=np.nonzero(depth);z=depth[y,x].astype(float)*.001
    pixels=np.column_stack((x,y));xyz=unproject(pixels,z,c['depth'])
    rgb_xyz=apply(transform(c['rgb_from_depth']),xyz)
    keep=rgb_xyz[:,2]>.05;xyz=xyz[keep];rgb_xyz=rgb_xyz[keep]
    uv=cv2.projectPoints(rgb_xyz,np.zeros(3),np.zeros(3),Kr,dr)[0].reshape(-1,2) if len(xyz) else np.empty((0,2))
    workspace=apply(transform(c['workspace_from_depth']),xyz)
    plane=np.asarray(c['table_plane']);height=workspace@plane[:3]+plane[3]
    region=bounds(c['workspace_bounds']);foreground=(height>.003)&(height<1)&np.all(workspace>=region[:,0],axis=1)&np.all(workspace<=region[:,1],axis=1)
    output=[]
    for index,det in enumerate(detections):
        box=array(det['xyxy'],(4,),'YOLO 检测框');x1,y1,x2,y2=box
        if x2<=x1 or y2<=y1:continue
        pad=max(1,min(x2-x1,y2-y1)*.08)
        selected=foreground&(uv[:,0]>=x1+pad)&(uv[:,0]<=x2-pad)&(uv[:,1]>=y1+pad)&(uv[:,1]<=y2-pad)
        cloud=workspace[selected];h=height[selected]
        result=dict(det,id=str(index),valid=False)
        if len(cloud)<min_points:
            result['error']='有效前景深度不足或包裹被遮挡';output.append(result);continue
        # Reject isolated far surfaces instead of averaging a box and background.
        median=np.median(cloud,axis=0);distance=np.linalg.norm(cloud-median,axis=1)
        cloud=cloud[distance<=max(.025,np.percentile(distance,85))];h=h[distance<=max(.025,np.percentile(distance,85))]
        top=np.percentile(h,80);surface=cloud[h>=top-.015]
        target=np.median(surface,axis=0)
        # Choose an observed surface point, not an unobserved average in a hole.
        target=surface[np.argmin(np.linalg.norm(surface-target,axis=1))]
        result.update(valid=True,xyz_m=target.tolist(),height_m=float(np.percentile(h,90)),
                      extent_m=(np.percentile(cloud,95,axis=0)-np.percentile(cloud,5,axis=0)).tolist(),
                      points=len(cloud),calibration_id=fingerprint(c))
        output.append(result)
    return output


def verify_push(before,after,training_region):
    if before['name']!=after['name'] or not after.get('valid'):raise SortingError('推拢后没有重新确认同一类别目标')
    if abs(after['height_m']-before['height_m'])>.08:raise SortingError('推拢后形状变化过大，需要重新检测')
    return {'reached':inside(after['xyz_m'],training_region),
            'displacement_m':float(np.linalg.norm(np.asarray(after['xyz_m'])-before['xyz_m'])),
            'height_m':after['height_m']}
