"""Base placement from a shared workspace frame; never infer bases from TCP FK."""
import math


def resolve_monitor_geometry(settings):
    if settings is None:settings={}
    if not isinstance(settings,dict):raise ValueError('基座位置设置必须是对象')
    mode=settings.get('mode','coordinates')
    if mode not in ('coordinates','distance'):raise ValueError('基座位置来源必须是 coordinates 或 distance')
    vectors={}
    for side in ('left','right'):
        value=settings.get(side+'_base_mm')
        if value is None:vectors[side]=None;continue
        if not isinstance(value,list) or len(value)!=3:raise ValueError('底座坐标需要 X/Y/Z 三个毫米值')
        if any(v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1000000) for v in value):raise ValueError('底座坐标需要有限的毫米数值')
        vectors[side]=value if all(v is not None for v in value) else None
    distance=settings.get('base_center_distance_mm')
    if distance is not None and (isinstance(distance,bool) or not isinstance(distance,(int,float)) or not math.isfinite(distance) or not 0<distance<=10000):raise ValueError('实测底座中心距需要在 0～10000 mm 之间且不能为零')
    complete=mode=='coordinates' and all(vectors.values())
    if complete:
        distance=math.dist(vectors['left'],vectors['right'])
        if distance<=0:raise ValueError('左右底座坐标不能重合；各臂局部原点不能作为共同坐标')
    elif mode=='coordinates':distance=None
    return {'mode':mode,'frame_id':settings.get('frame_id','workspace'),'unit':'mm',
            'distance_mm':distance,'base_positions_mm':vectors if complete else None,
            'ready':distance is not None,'source':'shared-frame-coordinates' if complete else 'measured-distance' if distance is not None else 'unset'}
