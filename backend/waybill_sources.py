"""
扫码相机数据源解析与确定性匹配过滤模块 (Waybill Camera Sources Resolver)
========================================================================

【模块背景与定位】
在工业视觉系统中，用户界面通常使用直观的显示名称（Display Name，如“俯视相机”、“侧方相机”）
或统一设备索引/路径（如 "/dev/v4l/by-id/..." 或数字索引 0, 1）来选择用于面单扫描的设备源。
本模块负责严格区分前端显示名称与物理相机唯一标识符，防止多相机环境下的误匹配。

【匹配保护策略】
1. target 为 None、空字符串或 'all' 时，返回全部可用相机列表。
2. 精准匹配：优先利用 `source_key` 对物理硬件设备路径/索引做规范化唯一映射。
3. 显示名称回退：若传入的是相机名称字符串，进行精确匹配；若检测到同名重名，
   抛出明确的 CameraError 提示用户，坚决不随意 fallback 到错误相机，杜绝视觉定位偏差。
"""
from .camera_manager import CameraError, source_key


def select_cameras(cameras, target='all'):
    """
    根据给定的 target 目标标识，从可用相机配置列表中筛选匹配的相机项。

    Args:
        cameras (Sequence[dict]): 系统中已配置的相机字典列表（包含 name, index_or_path 等）。
        target (str, optional): 目标相机标识符（可以是 'all'、显示名称、设备路径或索引号），默认为 'all'。

    Returns:
        list[dict]: 匹配到的相机项列表。

    Raises:
        CameraError: 当目标相机不存在、显示名称重复或匹配结果不唯一时抛出，坚决不随意切换其他相机。
    """
    target = 'all' if target is None or str(target).strip() == '' else str(target).strip()
    # 分支 1：全选模式
    if target == 'all':
        return list(cameras)

    # 分支 2：尝试通过物理设备 source_key 解析
    try:
        wanted = source_key(target)
    except CameraError as exc:
        # 若 source_key 解析失败，说明 target 可能是用户自定义的相机显示别名 (Display Name)
        names = [c for c in cameras if str(c.get('name') or '').strip() == target]
        if len(names) == 1:
            return names
        if len(names) > 1:
            raise CameraError('扫码相机名称重复，请选择唯一的设备源') from exc
        raise CameraError(f'扫码相机未配置或已移除：{target}，请重新选择相机源') from exc

    # 分支 3：基于标准化的物理 source_key 过滤匹配
    selected = [c for c in cameras if source_key(c.get('index_or_path', '')) == wanted]
    if len(selected) != 1:
        raise CameraError('扫码相机未配置或设备源不唯一，请重新选择；未切换到其他相机')
    return selected
