"""Minimal bindings to the installed Orbbec OpenNI 2.3 C API (Astra family).

Structures match the SDK's OniCTypes.h, including Orbbec's serialNumber field.
This driver opens only the depth sensor; UVC RGB remains owned by CameraManager.
"""
import ctypes as C
import os
from pathlib import Path

import numpy as np


class VideoMode(C.Structure):
    _fields_ = [('pixel_format', C.c_int), ('width', C.c_int), ('height', C.c_int), ('fps', C.c_int)]


class SensorInfo(C.Structure):
    _fields_ = [('sensor_type', C.c_int), ('count', C.c_int), ('modes', C.POINTER(VideoMode))]


class DeviceInfo(C.Structure):
    _fields_ = [('uri', C.c_char*256), ('vendor', C.c_char*256), ('name', C.c_char*256),
                ('serial', C.c_char*256), ('vid', C.c_uint16), ('pid', C.c_uint16)]


class Frame(C.Structure):
    _fields_ = [('size', C.c_int), ('data', C.c_void_p), ('sensor_type', C.c_int),
                ('timestamp', C.c_uint64), ('index', C.c_int), ('width', C.c_int), ('height', C.c_int),
                ('mode', VideoMode), ('cropping', C.c_int), ('origin_x', C.c_int), ('origin_y', C.c_int), ('stride', C.c_int)]


class OpenNIDepth:
    def __init__(self, sdk_path, width=640, height=480, fps=30):
        self.path = Path(sdk_path).resolve()
        os.environ['OPENNI2_REDIST'] = str(self.path)
        self.lib = C.CDLL(str(self.path/'libOpenNI2.so'))
        signatures = {
            'oniInitialize': ([C.c_int], C.c_int), 'oniShutdown': ([], None),
            'oniGetExtendedError': ([], C.c_char_p),
            'oniGetDeviceList': ([C.POINTER(C.POINTER(DeviceInfo)), C.POINTER(C.c_int)], C.c_int),
            'oniReleaseDeviceList': ([C.POINTER(DeviceInfo)], C.c_int),
            'oniDeviceOpen': ([C.c_char_p, C.POINTER(C.c_void_p)], C.c_int),
            'oniDeviceClose': ([C.c_void_p], C.c_int),
            'oniDeviceGetSensorInfo': ([C.c_void_p, C.c_int], C.POINTER(SensorInfo)),
            'oniDeviceCreateStream': ([C.c_void_p, C.c_int, C.POINTER(C.c_void_p)], C.c_int),
            'oniStreamSetProperty': ([C.c_void_p, C.c_int, C.c_void_p, C.c_int], C.c_int),
            'oniStreamStart': ([C.c_void_p], C.c_int), 'oniStreamStop': ([C.c_void_p], None),
            'oniStreamDestroy': ([C.c_void_p], None),
            'oniWaitForAnyStream': ([C.POINTER(C.c_void_p), C.c_int, C.POINTER(C.c_int), C.c_int], C.c_int),
            'oniStreamReadFrame': ([C.c_void_p, C.POINTER(C.POINTER(Frame))], C.c_int),
            'oniFrameRelease': ([C.POINTER(Frame)], None),
            'oniCoordinateConverterDepthToWorld': ([C.c_void_p,C.c_float,C.c_float,C.c_float,
                C.POINTER(C.c_float),C.POINTER(C.c_float),C.POINTER(C.c_float)],C.c_int),
        }
        for name, (args, result) in signatures.items():
            if name=='oniCoordinateConverterDepthToWorld' and not hasattr(self.lib,name):continue
            function = getattr(self.lib, name); function.argtypes = args; function.restype = result
        self.device, self.stream = C.c_void_p(), C.c_void_p()
        self.initialized = False
        self.requested = (width, height, fps)
        self.metadata = {}

    def check(self, result, operation):
        if result:
            error = (self.lib.oniGetExtendedError() or b'').decode(errors='replace')
            raise RuntimeError(f'OpenNI {operation} 失败 ({result})：{error}')

    def open(self):
        self.check(self.lib.oniInitialize(2003), '初始化'); self.initialized = True
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
        sensor = self.lib.oniDeviceGetSensorInfo(self.device, 3)
        if not sensor:
            raise RuntimeError('该设备没有深度传感器')
        modes = [VideoMode.from_buffer_copy(sensor.contents.modes[i]) for i in range(sensor.contents.count)]
        supported = [m for m in modes if m.pixel_format == 100]
        if not supported:
            raise RuntimeError('该设备未提供毫米单位的 16 位深度流')
        w, h, fps = self.requested
        mode = min(supported, key=lambda m: (abs(m.width-w)+abs(m.height-h), abs(m.fps-fps)))
        self.check(self.lib.oniDeviceCreateStream(self.device, 3, C.byref(self.stream)), '创建深度流')
        self.check(self.lib.oniStreamSetProperty(self.stream, 3, C.byref(mode), C.sizeof(mode)), '设置深度格式')
        mirror = C.c_int(0)
        self.lib.oniStreamSetProperty(self.stream, 7, C.byref(mirror), C.sizeof(mirror))
        self.check(self.lib.oniStreamStart(self.stream), '启动深度流')
        self.metadata = {'name': info.name.decode(errors='replace'), 'uri': info.uri.decode(errors='replace'),
                         'width': mode.width, 'height': mode.height, 'fps': mode.fps,
                         'unit': 'mm', 'format': 'uint16', 'aligned_to_rgb': False,
                         'driver': 'Orbbec OpenNI 2.3'}
        try:self.metadata['intrinsics']=self.intrinsics(mode.width,mode.height)
        except Exception as exc:self.metadata['intrinsics_error']=str(exc)
        return self.metadata

    def intrinsics(self,width,height):
        """Derive the exact current-mode pinhole mapping from the vendor API.

        OpenNI world Y may point upwards; our stored camera frame is x-right,
        y-down, z-forward, matching OpenCV. No RGB alignment is inferred here.
        """
        def point(u,v):
            x,y,z=C.c_float(),C.c_float(),C.c_float()
            self.check(self.lib.oniCoordinateConverterDepthToWorld(self.stream,u,v,1000.,C.byref(x),C.byref(y),C.byref(z)),'深度坐标转换')
            return np.array([x.value,y.value,z.value])
        p=point(0,0);px=point(width-1,0);py=point(0,height-1)
        dx=(px[0]-p[0])/(width-1);dy=(py[1]-p[1])/(height-1)
        if abs(dx)<1e-6 or abs(dy)<1e-6:raise RuntimeError('SDK 深度几何无效')
        fx,fy=1000/abs(dx),1000/abs(dy);cx,cy=-p[0]/dx,-p[1]/dy
        for u,v in [(width/2,height/2),(width-1,height-1)]:
            q=point(u,v)*[np.sign(dx),np.sign(dy),1]
            expected=np.array([(u-cx)*1000/fx,(v-cy)*1000/fy,1000])
            if np.linalg.norm(q-expected)>.5:raise RuntimeError('SDK 投影不符合当前支持的针孔模型，请导入标定参数')
        return {'K':[[fx,0,cx],[0,fy,cy],[0,0,1]],'distortion':[0.]*5,
                'width':width,'height':height,'source':'openni-coordinate-converter','frame':'x-right,y-down,z-forward'}

    def read(self, timeout_ms=1000):
        streams = (C.c_void_p*1)(self.stream.value); ready = C.c_int()
        self.check(self.lib.oniWaitForAnyStream(streams, 1, C.byref(ready), timeout_ms), '等待深度帧')
        pointer = C.POINTER(Frame)()
        self.check(self.lib.oniStreamReadFrame(self.stream, C.byref(pointer)), '读取深度帧')
        try:
            f = pointer.contents
            if f.mode.pixel_format != 100 or f.size < f.height*f.stride or f.stride < f.width*2:
                raise RuntimeError('深度帧尺寸、步长或毫米格式不匹配')
            depth = np.frombuffer(C.string_at(f.data, f.size), dtype=np.uint16,
                                  count=f.height*(f.stride//2)).reshape(f.height, f.stride//2)[:, :f.width].copy()
            return depth, f.index, f.timestamp
        finally:
            self.lib.oniFrameRelease(pointer)

    def close(self):
        if self.stream:
            self.lib.oniStreamStop(self.stream); self.lib.oniStreamDestroy(self.stream); self.stream = C.c_void_p()
        if self.device:
            self.lib.oniDeviceClose(self.device); self.device = C.c_void_p()
        if self.initialized:
            self.lib.oniShutdown(); self.initialized = False
