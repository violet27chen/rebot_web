"""
LeRobot 原始帧二进制无损流序列化协议 (Raw Frame Packet Serialization)
========================================================================

【模块背景与定位】
在 LeRobot 数据集录制与机械臂强化学习策略推理过程中，相机图像需要以极低延迟、零压缩损耗
传输至 LeRobot 相机共享插件。
本模块定义了版本 1 的无损 BGR 二进制协议头格式与打包函数。

【二进制包头结构规范 (HEADER: 36 字节 Little-Endian)】
- 4s: 协议幻数标识 (Magic bytes)，固定为 b'RBFR' (Rebot Frame)
- Q:  64位无符号整型帧序列号 (Sequence ID, 从 1 递增)
- d:  64位双精度浮点捕获时间戳 (Timestamp, 秒)
- I:  32位无符号整型图像宽度 (Width)
- I:  32位无符号整型图像高度 (Height)
- I:  32位无符号整型通道数 (Channels, 通常为 3 代表 BGR)
- I:  32位无符号整型有效载荷字节数 (Payload Length)
跟在包头之后的是连续的原始未压缩图像像素字节流 (Raw BGR bytes)。
"""
import struct

# 二进制帧头打包器: < 代表标准小端序 (Little-Endian)
HEADER = struct.Struct('<4sQdIIII')


def frame_packet(sequence, timestamp, frame):
    """
    将单帧 NumPy 图像数组打包为带元数据头的二进制数据包。

    Args:
        sequence (int): 当前帧的全局递增流水号。
        timestamp (float): 硬件采集完成时的 Unix 时间戳（秒）。
        frame (np.ndarray): OpenCV BGR 格式的图像数组 (H, W, C)。

    Returns:
        bytes: 包含 36 字节二进制头与连续像素字节的完整网络/共享内存数据包。
    """
    height, width, channels = frame.shape
    payload = frame.tobytes()
    return HEADER.pack(b'RBFR', sequence, timestamp, width, height, channels, len(payload)) + payload
