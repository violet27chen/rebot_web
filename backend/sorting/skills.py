"""Strict skill registry: class routing does not invent spatial generalization."""
import json
from pathlib import Path
from .geometry import SortingError,bounds,inside,array

JOINTS=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_yaw','wrist_roll','gripper']
NAMES=[side+'_'+name+'.pos' for side in ('left','right') for name in JOINTS]
DIRECTIONS=[-1.,-1.,1.,1.,1.,-1.,-6.]*2


def inspect_skill(entry,root,calibration_id):
    if not entry.get('name') or not entry.get('class_name'):raise SortingError('技能需要独立名称和 YOLO 类别')
    path=Path(str(entry.get('checkpoint',''))).expanduser()
    if not path.is_absolute():path=Path(root)/path
    if not (path/'config.json').is_file() or not (path/'model.safetensors').is_file():raise SortingError('ACT 检查点不存在或尚未训练完成')
    try:config=json.loads((path/'config.json').read_text())
    except (OSError,ValueError):raise SortingError('ACT config.json 无法读取')
    if config.get('type')!='act' or (config.get('output_features',{}).get('action') or {}).get('shape')!=[14]:raise SortingError('技能必须是独立训练的双臂 14 维 ACT')
    meta=Path(str(entry.get('dataset_info',''))).expanduser()
    if not meta.is_absolute():meta=Path(root)/meta
    if not meta.is_file():raise SortingError('请选择该技能训练数据的 meta/info.json，以核对动作顺序')
    try:info=json.loads(meta.read_text())
    except (OSError,ValueError):raise SortingError('训练数据 meta/info.json 无法读取')
    features=info.get('features') or {}
    state_names=(features.get('observation.state') or {}).get('names');action_names=(features.get('action') or {}).get('names')
    if not isinstance(state_names,list) or not isinstance(action_names,list) or set(state_names)!=set(NAMES) or set(action_names)!=set(NAMES) or len(state_names)!=14 or len(action_names)!=14:raise SortingError('训练数据的状态/动作关节名称与当前双臂设备不匹配')
    state_feature=(config.get('input_features') or {}).get('observation.state') or {}
    if state_feature.get('shape')!=[14]:raise SortingError('ACT 状态输入必须是 14 维')
    if entry.get('calibration_id')!=calibration_id:raise SortingError('技能登记的标定版本与当前工作区不同，请核对训练坐标')
    bounds(entry.get('training_bounds'),'该技能训练区域')
    if not .01<=float(entry.get('start_tolerance_rad',.2))<=.3:raise SortingError('技能起始容差应在 0.01～0.3 rad')
    if not .1<=float(entry.get('duration_s',5))<=30:raise SortingError('技能时长应在 0.1～30 秒')
    pose=array(entry.get('start_pose_rad'),(14,),'技能实测起始姿态（原始电机弧度）')
    cameras=entry.get('camera_map') or {}
    visual={k:f for k,f in config.get('input_features',{}).items() if k.startswith('observation.images.')}
    if not visual or set(cameras)!=set(visual):raise SortingError('需要为 ACT 每一个训练相机特征明确选择对应设备')
    for k,v in visual.items():
        if len(v.get('shape',[]))!=3 or v['shape'][0]!=3:raise SortingError('当前执行器只支持训练时的 RGB 相机输入')
    if not isinstance(entry.get('task'),str) or not entry['task'].strip():raise SortingError('请填写该技能训练时的任务描述')
    return dict(entry,checkpoint=str(path),state_names=state_names,action_names=action_names,visual=visual)


def route(target,skills,role='act'):
    matches=[entry for entry in skills if entry.get('class_name')==target.get('name') and entry.get('enabled',True) and entry.get('role','act')==role]
    if len(matches)!=1:raise SortingError('检测类别需要唯一的 ACT 技能映射；未匹配或重复时不会选择任意模型')
    return matches[0]


def action_to_raw(action,order):
    values=array(action,(14,),'ACT 动作')
    by_name=dict(zip(order,values));return [float(by_name[name]*direction*3.141592653589793/180) for name,direction in zip(NAMES,DIRECTIONS)]


def raw_observation(raw,order):
    values=dict(zip(NAMES,[float(v)*180/3.141592653589793 for v in raw]))
    return [values[name] for name in order]
