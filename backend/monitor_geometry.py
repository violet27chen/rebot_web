"""
双臂空间基座几何位姿与相对中心距解析模块 (Dual Arm Base Geometry Resolver)
=============================================================================

【模块背景与定位】
在双臂协作监控与 3D 可视化中，左右两台机械臂物理安装在同一台工作台上。
为了在三维空间中准确呈现两臂的相对位姿，必须建立统一的工作台全局坐标系。

【核心安全设计准则】
- 严禁通过工具中心点（TCP）的正向运动学（FK）反推基座位置，因为两臂各自的局部坐标系原点不同。
- 支持两种工业配置模式：
  1. 'coordinates': 显式提供左臂与右臂在统一世界/工作台坐标系下的 [X, Y, Z] 毫米三维坐标；
  2. 'distance': 仅提供用激光测距仪或卡尺实测的两臂底座中心直线物理间距 (毫米)。
- 严格做物理边界有效性检验：间距必须在 (0, 10000] mm 之间，且左右臂坐标绝对不能重合。
"""
import math


def resolve_monitor_geometry(settings):
    """
    校验并解析双臂底座几何坐标与相对间距配置。

    Args:
        settings (dict): 前端或配置文件传入的几何设置字典。

    Returns:
        dict: 结构化解析结果，包含 mode, frame_id, unit, distance_mm,
              base_positions_mm, ready, source 等字段。

    Raises:
        ValueError: 当配置格式不正确、坐标非有限数字、距离超出 [0, 10000] mm 或左右底座重合时抛出。
    """
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError('基座位置设置必须是对象')

    mode = settings.get('mode', 'coordinates')
    if mode not in ('coordinates', 'distance'):
        raise ValueError('基座位置来源必须是 coordinates 或 distance')

    vectors = {}
    # 步骤 1：解析并校验左右臂的三维物理坐标向量
    for side in ('left', 'right'):
        value = settings.get(side + '_base_mm')
        if value is None:
            vectors[side] = None
            continue
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError('底座坐标需要 X/Y/Z 三个毫米值')
        if any(
            v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1000000)
            for v in value
        ):
            raise ValueError('底座坐标需要有限的毫米数值')
        vectors[side] = value if all(v is not None for v in value) else None

    # 步骤 2：解析并校验实测中心物理距离
    distance = settings.get('base_center_distance_mm')
    if distance is not None and (
        isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or not (0 < distance <= 10000)
    ):
        raise ValueError('实测底座中心距需要在 0～10000 mm 之间且不能为零')

    # 步骤 3：坐标完整性判定与距离计算
    complete = mode == 'coordinates' and all(vectors.values())
    if complete:
        distance = math.dist(vectors['left'], vectors['right'])
        if distance <= 0:
            raise ValueError('左右底座坐标不能重合；各臂局部原点不能作为共同坐标')
    elif mode == 'coordinates':
        distance = None

    return {
        'mode': mode,
        'frame_id': settings.get('frame_id', 'workspace'),
        'unit': 'mm',
        'distance_mm': distance,
        'base_positions_mm': vectors if complete else None,
        'ready': distance is not None,
        'source': 'shared-frame-coordinates' if complete else 'measured-distance' if distance is not None else 'unset'
    }
