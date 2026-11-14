"""向左推走与爪头朝下的几何：纯函数。工作台 Z 向上，“左”由配置的水平单位向量定义。"""
import numpy as np
from .geometry import SortingError, array, transform


def left_direction(value):
    v = array(value if value is not None else [-1, 0, 0], (3,), '向左方向')
    v = v.copy();v[2] = 0
    n = float(np.linalg.norm(v))
    if n < 1e-6:
        raise SortingError('向左方向必须有水平分量')
    return v / n


def rpy_matrix(rpy):
    r, p, y = array(rpy, (3,), '接近姿态')
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([[cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr],
                     [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr],
                     [-sp, cp*sr, cp*cr]])


def gripper_down_ok(rpy, tool_axis, base_from_workspace, max_tilt_rad=.35):
    """接近姿态下工具轴（基座系）换算到工作台系后，与竖直向下的夹角不超过上限。"""
    axis = array(tool_axis, (3,), '工具轴');axis = axis / np.linalg.norm(axis)
    R_base_from_ws = transform(base_from_workspace)[:3, :3]
    in_workspace = R_base_from_ws.T @ (rpy_matrix(rpy) @ axis)
    cos = float(np.clip(in_workspace @ np.array([0, 0, -1.]), -1, 1))
    return bool(np.arccos(cos) <= max_tilt_rad)


def push_left_poses(package, extent, height, direction, travel, clearance, primary, other):
    """与规则推拢同一几何：另一臂在侧面围挡，主臂从右侧接触后向左推。"""
    package = np.asarray(package, float);d = left_direction(direction)
    if not 0 < travel <= .2:
        raise SortingError('单次向左推拢距离应在 0～0.2 米')
    radius = float(np.linalg.norm(np.asarray(extent)[:2])) / 2 + .04
    contact = package - d * radius;contact[2] -= height / 2
    guard = package + np.array([-d[1], d[0], 0]) * radius;guard[2] = contact[2]
    up = np.array([0, 0, clearance])
    return [(other, guard + up), (other, guard), (primary, contact + up), (primary, contact),
            (primary, contact + d * travel), (primary, contact + d * travel + up), (other, guard + up)]


def verify_left(before_xyz, after, direction, min_m):
    """after 为 None 表示已不在工作区（被推出）。否则要求沿“左”位移不小于 min_m。"""
    if after is None:
        return {'reached': True, 'removed': True, 'left_m': None}
    delta = np.asarray(after['xyz_m']) - np.asarray(before_xyz)
    moved = float(delta @ left_direction(direction))
    return {'reached': moved >= min_m, 'removed': False, 'left_m': moved}
