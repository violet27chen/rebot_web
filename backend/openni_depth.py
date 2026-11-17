"""
Orbbec OpenNI 2.3 C API 精简 ctypes 底层绑定驱动 (OpenNI 2.3 Minimal C-Bindings)
===================================================================================

【模块背景与架构定位】
本驱动直接对奥比中光（Orbbec）随系统发布的官方 OpenNI 2.3 动态链接库（`libOpenNI2.so`）进行
纯 ctypes 封装，专用于 Astra / Gemini 系列深度相机的 16 位毫米流采集。

【设计原则与结构体映射】
1. 结构体对齐：严格匹配 SDK 的 `OniCTypes.h` 头文件定义（包括 VideoMode, SensorInfo,
   DeviceInfo 以及带 serialNumber 扩展字段的 Orbbec 专属布局）。
2. 只打开深度流：本驱动严格仅创建并拉取 OniSensorType=3（深度传感器流）；
   UVC 协议的 RGB 彩色摄像头部分由通用的 `CameraManager` 统一管理，二者互不干扰。
3. 严格针孔几何内参导出 (`intrinsics`)：
   通过调用原厂 `oniCoordinateConverterDepthToWorld` API 对特定像素点进行三维空间反投影，
   严格推导标准针孔相机内参矩阵 K (fx, fy, cx, cy)，并校验投影残差 (< 0.5mm)，
   确保输出标准的 OpenCV 坐标系内参 (X 向右、Y 向下、Z 向前)。
"""
import ctypes as C
import os
from pathlib import Path

import numpy as np


class VideoMode(C.Structure):
    """OpenNI 视频格式结构体 (匹配 OniVideoMode)。"""
    _fields_ = [
        ('pixel_format', C.c_int),
        ('width', C.c_int),
        ('height', C.c_int),
        ('fps', C.c_int),
    ]


class SensorInfo(C.Structure):
    """OpenNI 传感器信息结构体 (匹配 OniSensorInfo)。"""
    _fields_ = [
        ('sensor_type', C.c_int),
        ('count', C.c_int),
        ('modes', C.POINTER(VideoMode)),
    ]


class DeviceInfo(C.Structure):
    """OpenNI 设备描述结构体 (包含奥比中光特定的 serial 字段)。"""
    _fields_ = [
        ('uri', C.c_char * 256),
        ('vendor', C.c_char * 256),
        ('name', C.c_char * 256),
        ('serial', C.c_char * 256),
        ('vid', C.c_uint16),
        ('pid', C.c_uint16),
    ]


class Frame(C.Structure):
    """OpenNI 帧数据包结构体 (匹配 OniFrame)。"""
    _fields_ = [
        ('size', C.c_int),
        ('data', C.c_void_p),
        ('sensor_type', C.c_int),
        ('timestamp', C.c_uint64),
        ('index', C.c_int),
        ('width', C.c_int),
        ('height', C.c_int),
        ('mode', VideoMode),
        ('cropping', C.c_int),
        ('origin_x', C.c_int),
        ('origin_y', C.c_int),
        ('stride', C.c_int),
    ]


class OpenNIDepth:
    """
    OpenNI 2.3 深度传感器封装类。
    提供设备枚举、深度流启动、16位毫米深度帧读取与针孔内参自动标定解析功能。
    """

    def __init__(self, sdk_path, width=640, height=480, fps=30):
        """
        加载 OpenNI 动态库并配置 C 导出函数签名。

        Args:
            sdk_path (str or Path): 包含 libOpenNI2.so 的 OpenNI Redist 路径。
            width (int, optional): 期望分辨率宽，默认 640。
            height (int, optional): 期望分辨率高，默认 480。
            fps (int, optional): 期望帧率，默认 30。
        """
        self.path = Path(sdk_path).resolve()
        os.environ['OPENNI2_REDIST'] = str(self.path)
        self.lib = C.CDLL(str(self.path / 'libOpenNI2.so'))

        signatures = {
            'oniInitialize': ([C.c_int], C.c_int),
            'oniShutdown': ([], None),
            'oniGetExtendedError': ([], C.c_char_p),
            'oniGetDeviceList': ([C.POINTER(C.POINTER(DeviceInfo)), C.POINTER(C.c_int)], C.c_int),
            'oniReleaseDeviceList': ([C.POINTER(DeviceInfo)], C.c_int),
            'oniDeviceOpen': ([C.c_char_p, C.POINTER(C.c_void_p)], C.c_int),
            'oniDeviceClose': ([C.c_void_p], C.c_int),
            'oniDeviceGetSensorInfo': ([C.c_void_p, C.c_int], C.POINTER(SensorInfo)),
            'oniDeviceCreateStream': ([C.c_void_p, C.c_int, C.POINTER(C.c_void_p)], C.c_int),
            'oniStreamSetProperty': ([C.c_void_p, C.c_int, C.c_void_p, C.c_int], C.c_int),
            'oniStreamStart': ([C.c_void_p], C.c_int),
            'oniStreamStop': ([C.c_void_p], None),
            'oniStreamDestroy': ([C.c_void_p], None),
            'oniWaitForAnyStream': ([C.POINTER(C.c_void_p), C.c_int, C.POINTER(C.c_int), C.c_int], C.c_int),
            'oniStreamReadFrame': ([C.c_void_p, C.POINTER(C.POINTER(Frame))], C.c_int),
            'oniFrameRelease': ([C.POINTER(Frame)], None),
            'oniCoordinateConverterDepthToWorld': ([
                C.c_void_p, C.c_float, C.c_float, C.c_float,
                C.POINTER(C.c_float), C.POINTER(C.c_float), C.POINTER(C.c_float)
            ], C.c_int),
        }
        for name, (args, result) in signatures.items():
            if name == 'oniCoordinateConverterDepthToWorld' and not hasattr(self.lib, name):
                continue
            function = getattr(self.lib, name)
            function.argtypes = args
            function.restype = result

        self.device, self.stream = C.c_void_p(), C.c_void_p()
        self.initialized = False
        self.requested = (width, height, fps)
        self.metadata = {}

    def check(self, result, operation):
        """校验 OpenNI C API 返回码，非 0 时提取扩展错误详情并抛出异常。"""
        if result:
            error = (self.lib.oniGetExtendedError() or b'').decode(errors='replace')
            raise RuntimeError(f'OpenNI {operation} 失败 ({result})：{error}')

    def open(self):
        """
        初始化 OpenNI 运行时、打开深度硬件设备、选择最接近的 16 位毫米流格式并启动流。

        Returns:
            dict: 包含深度相机元数据与内参的字典。
        """
        self.check(self.lib.oniInitialize(2003), '初始化')
        self.initialized = True
        devices, count = C.POINTER(DeviceInfo)(), C.c_int()
        self.check(self.lib.oniGetDeviceList(C.byref(devices), C.byref(count)), '枚举设备')
        try:
            found = [DeviceInfo.from_buffer_copy(devices[i]) for i in range(count.value) if devices[i].vid == 0x2bc5]
            if len(found) != 1:
                raise RuntimeError('未发现 Orbbec OpenNI 深度设备' if not found else '发现多个深度设备，请指定设备后再读取')
            info = found[0]
        finally:
            self.lib.oniReleaseDeviceList(devices)

        self.check(self.lib.oniDeviceOpen(info.uri, C.byref(self.device)), '打开深度设备')
        # 获取类型为 3 (OniSensorType = ONI_SENSOR_DEPTH) 的传感器描述
        sensor = self.lib.oniDeviceGetSensorInfo(self.device, 3)
        if not sensor:
            raise RuntimeError('该设备没有深度传感器')

        modes = [VideoMode.from_buffer_copy(sensor.contents.modes[i]) for i in range(sensor.contents.count)]
        # pixel_format 100 对应 ONI_PIXEL_FORMAT_DEPTH_1_MM (16位毫米深度图)
        supported = [m for m in modes if m.pixel_format == 100]
        if not supported:
            raise RuntimeError('该设备未提供毫米单位的 16 位深度流')

        w, h, fps = self.requested
        mode = min(supported, key=lambda m: (abs(m.width - w) + abs(m.height - h), abs(m.fps - fps)))

        self.check(self.lib.oniDeviceCreateStream(self.device, 3, C.byref(self.stream)), '创建深度流')
        self.check(self.lib.oniStreamSetProperty(self.stream, 3, C.byref(mode), C.sizeof(mode)), '设置深度格式')
        mirror = C.c_int(0)
        self.lib.oniStreamSetProperty(self.stream, 7, C.byref(mirror), C.sizeof(mirror))
        self.check(self.lib.oniStreamStart(self.stream), '启动深度流')

        self.metadata = {
            'name': info.name.decode(errors='replace'),
            'uri': info.uri.decode(errors='replace'),
            'width': mode.width,
            'height': mode.height,
            'fps': mode.fps,
            'unit': 'mm',
            'format': 'uint16',
            'aligned_to_rgb': False,
            'driver': 'Orbbec OpenNI 2.3'
        }
        try:
            self.metadata['intrinsics'] = self.intrinsics(mode.width, mode.height)
        except Exception as exc:
            self.metadata['intrinsics_error'] = str(exc)
        return self.metadata

    def intrinsics(self, width, height):
        """
        基于 SDK 的原厂坐标反投影 API 严格推导标准针孔相机内参矩阵 K。
        校验图像中心点与对角点的投影误差，确保满足 OpenCV 针孔投影模型。

        Args:
            width (int): 图像宽。
            height (int): 图像高。

        Returns:
            dict: 包含 K 矩阵 (3x3), 畸变系数 distortion, 分辨率与坐标系规范的字典。
        """
        def point(u, v):
            x, y, z = C.c_float(), C.c_float(), C.c_float()
            self.check(
                self.lib.oniCoordinateConverterDepthToWorld(self.stream, u, v, 1000.0, C.byref(x), C.byref(y), C.byref(z)),
                '深度坐标转换'
            )
            return np.array([x.value, y.value, z.value])

        p = point(0, 0)
        px = point(width - 1, 0)
        py = point(0, height - 1)
        dx = (px[0] - p[0]) / (width - 1)
        dy = (py[1] - p[1]) / (height - 1)
        if abs(dx) < 1e-6 or abs(dy) < 1e-6:
            raise RuntimeError('SDK 深度几何无效')

        fx, fy = 1000 / abs(dx), 1000 / abs(dy)
        cx, cy = -p[0] / dx, -p[1] / dy

        # 校验采样点的针孔投影误差
        for u, v in [(width / 2, height / 2), (width - 1, height - 1)]:
            q = point(u, v) * [np.sign(dx), np.sign(dy), 1]
            expected = np.array([(u - cx) * 1000 / fx, (v - cy) * 1000 / fy, 1000])
            if np.linalg.norm(q - expected) > 0.5:
                raise RuntimeError('SDK 投影不符合当前支持的针孔模型，请导入标定参数')

        return {
            'K': [[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
            'distortion': [0.0] * 5,
            'width': width,
            'height': height,
            'source': 'openni-coordinate-converter',
            'frame': 'x-right,y-down,z-forward'
        }

    def read(self, timeout_ms=1000):
        """
        阻塞等待并读取下一帧 16 位无符号毫米整型深度图像。

        Args:
            timeout_ms (int, optional): 等待超时时间（毫秒），默认 1000。

        Returns:
            tuple[np.ndarray, int, int]: (深度矩阵 np.uint16, 帧序号, 硬件时间戳)。

        Raises:
            RuntimeError: 当等待超时或数据格式不匹配时抛出。
        """
        streams = (C.c_void_p * 1)(self.stream.value)
        ready = C.c_int()
        self.check(self.lib.oniWaitForAnyStream(streams, 1, C.byref(ready), timeout_ms), '等待深度帧')
        pointer = C.POINTER(Frame)()
        self.check(self.lib.oniStreamReadFrame(self.stream, C.byref(pointer)), '读取深度帧')
        try:
            f = pointer.contents
            if f.mode.pixel_format != 100 or f.size < f.height * f.stride or f.stride < f.width * 2:
                raise RuntimeError('深度帧尺寸、步长或毫米格式不匹配')
            depth = np.frombuffer(
                C.string_at(f.data, f.size),
                dtype=np.uint16,
                count=f.height * (f.stride // 2)
            ).reshape(f.height, f.stride // 2)[:, :f.width].copy()
            return depth, f.index, f.timestamp
        finally:
            self.lib.oniFrameRelease(pointer)

    def close(self):
        """释放流、关闭设备并反初始化 OpenNI 动态库。"""
        if self.stream:
            self.lib.oniStreamStop(self.stream)
            self.lib.oniStreamDestroy(self.stream)
            self.stream = C.c_void_p()
        if self.device:
            self.lib.oniDeviceClose(self.device)
            self.device = C.c_void_p()
        if self.initialized:
            self.lib.oniShutdown()
            self.initialized = False
