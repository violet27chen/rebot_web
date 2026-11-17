"""
数据集录制选项解析与图像分辨率规范化配置模块 (Recording Settings Resolver)
==============================================================================

【模块背景与定位】
在构建具身智能离线数据集时，相机的硬件采集物理分辨率（例如 1080P 或 720P）
与最终神经网络模型输入所保存的视频分辨率（如 640x360 或 1280x720）需要解耦。
同时，当用户选择“续录（Resume）”已有数据集时，为了保证强化学习与行为克隆（ACT）
训练的一致性，必须严格保持原有数据集的帧率（FPS）、视频编码格式（h264/hevc/av1）
以及图像尺寸完全一致，绝对禁止混合不同编码或分辨率拼接数据集。

【核心功能】
1. `recording_options`: 确定最终录制的视频编码器与目标帧率。若续录，自动解析
   原数据集 `meta/info.json` 的编码与 FPS；
2. `recording_cameras`: 规范化各相机的目标保存分辨率，标记 `_resize_to_config=True`，
   并在续录时校验相机特征名称与张量维度（H, W, C）。
"""
import copy
import json
from pathlib import Path

# 支持的标准化录制保存分辨率规格
SIZES = {
    'source': None,           # 保持相机原始物理分辨率，不缩放
    '640x360': (640, 360),    # 标准 16:9 低显存轻量级格式
    '1280x720': (1280, 720),  # 标准 720P 高清格式
}


def recording_options(cfg, dataset_root):
    """
    解析并校验数据集录制所使用的视频编码格式与目标采样帧率。

    Args:
        cfg (dict): 平台主配置字典（包含 dataset 配置与 cameras 配置）。
        dataset_root (str or Path): 本地数据集存储根目录。

    Returns:
        tuple[str, int]: (视频编码器名称, 录制帧率 FPS)。

    Raises:
        ValueError: 续录时原配置丢失、编码不统一或帧率超出 [1, 120] 区间时抛出。
    """
    settings = cfg['dataset']
    codec = settings.get('vcodec') or 'h264'
    # 取数据集配置帧率与所有相机物理最高帧率的最小值，保证不超频
    fps = min([int(settings.get('fps') or 30)] + [int(c.get('fps') or 30) for c in cfg.get('cameras', [])])

    # 若为续录模式，严格继承原数据集的编码器与帧率
    if settings.get('resume'):
        path = Path(dataset_root) / str(settings['repo_id']) / 'meta/info.json'
        try:
            info = json.loads(path.read_text('utf-8'))
            fps = int(info['fps'])
            codecs = {
                f.get('info', {}).get('video.codec')
                for f in info['features'].values()
                if f.get('dtype') == 'video'
            }
            if len(codecs) != 1:
                raise ValueError('原视频编码缺失或不一致')
            codec = {'av1': 'libsvtav1', 'h264': 'h264', 'hevc': 'hevc'}[codecs.pop()]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('续录需要原数据集的帧率和视频编码，不能混合编码拼接；请检查 meta/info.json') from exc

    if codec not in ('h264', 'hevc', 'libsvtav1') or not (1 <= fps <= 120):
        raise ValueError('采集编码或帧率无效')
    return codec, fps


def recording_cameras(cfg, dataset_root):
    """
    处理并生成参与录制的相机配置列表，根据配置添加尺寸裁剪/缩放指示。

    Args:
        cfg (dict): 平台配置字典。
        dataset_root (str or Path): 数据集根目录。

    Returns:
        list[dict]: 调整后的相机配置副本列表。

    Raises:
        ValueError: 分辨率模式非法或续录时相机特征不匹配。
    """
    settings = cfg['dataset']
    size = settings.get('video_resolution', '640x360')
    if size not in SIZES:
        raise ValueError('采集分辨率请选择 640×360、1280×720 或设备原始尺寸')

    existing = None
    if settings.get('resume'):
        path = Path(dataset_root) / str(settings['repo_id']) / 'meta/info.json'
        try:
            existing = json.loads(path.read_text('utf-8'))['features']
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError('续录需要读取原数据集 meta/info.json，不能改变图像尺寸') from exc

    result = copy.deepcopy(cfg.get('cameras') or [])
    dual = (cfg.get('dual_arm') or {}).get('enabled')

    for camera in result:
        target = SIZES[size]
        # 续录时强制严格比对历史特征维度
        if existing is not None:
            prefix = 'left_' if dual and (cfg.get('dual_arm') or {}).get('robot_type') == 'bi_seeed_b601_dm_follower' else ''
            name = 'observation.images.' + prefix + camera['name']
            feature = existing.get(name)
            if not feature or len(feature.get('shape', [])) != 3:
                raise ValueError(f'续录数据集没有匹配的相机特征：{name}')
            height, width, _ = feature['shape']
            target = (width, height)

        # 若目标分辨率与物理相机捕获参数不一致，标记 _resize_to_config
        if target and (camera.get('width'), camera.get('height')) != target:
            camera.update(width=target[0], height=target[1], _resize_to_config=True)

    return result
