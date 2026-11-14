"""Threaded OpenNI capture, hosted inside the ONE platform vision process."""
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from .camera_manager import CameraError
from .openni_depth import OpenNIDepth


def depth_present():
    for vendor in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        try:
            if vendor.read_text().strip()=='2bc5' and vendor.with_name('idProduct').read_text().strip() in ('060f','0401','0403'):return True
        except OSError:pass
    return False


class DepthLease:
    def __init__(self,manager):self.manager=manager;self.generation=manager.generation;self.sequence=0;self.released=False
    def read(self,fresh=False):
        m=self.manager
        with m.condition:
            ready=m.condition.wait_for(lambda:m.error or m.closed or m.generation!=self.generation or (m.meta and m.meta['sequence']>(self.sequence if fresh else 0)),12)
            if m.error:raise CameraError(m.error)
            if not ready or m.closed or m.generation!=self.generation:raise CameraError('深度流已暂停或等待超时')
            if time.time()-m.meta['timestamp']>2:raise CameraError('深度图像已过期，请重新连接相机')
            self.sequence=m.meta['sequence'];return dict(m.meta),m.jpeg,m.png
    def release(self):
        m=self.manager
        with m.condition:
            if self.released:return
            self.released=True
            if self.generation==m.generation:m.refs=max(0,m.refs-1);m.last_release=time.monotonic()


class DepthManager:
    def __init__(self,root):
        self.root=Path(root);self.condition=threading.Condition(threading.RLock());self.thread=None
        self.stopping=threading.Event();self.generation=0;self.closed=True;self.refs=0;self.last_release=0
        self.meta=self.jpeg=self.png=None;self.error=''
    def acquire(self):
        with self.condition:
            if self.thread and self.thread.is_alive() and self.closed:raise CameraError('深度连接正在释放，请稍后重试')
            if not self.thread or not self.thread.is_alive():
                if not depth_present():raise CameraError('未检测到 Orbbec 深度模块，请检查 USB 连接')
                self.generation+=1;self.refs=0;self.closed=False;self.stopping.clear();self.error=''
                self.meta=self.jpeg=self.png=None
                self.thread=threading.Thread(target=self._capture,args=(self.generation,),daemon=True,name='openni-depth');self.thread.start()
            self.refs+=1;return DepthLease(self)
    def _capture(self,generation):
        camera=OpenNIDepth(self.root/'vendor/openni2')
        try:
            metadata=camera.open();counter=0;published=0
            while not self.stopping.is_set():
                depth,index,device_time=camera.read(1000);now=time.time()
                with self.condition:
                    if not self.refs and time.monotonic()-self.last_release>2:self.closed=True;break
                if now-published<.065:continue
                published=now;counter+=1
                value=np.clip((depth.astype(np.float32)-200)*255/3800,0,255).astype(np.uint8)
                view=cv2.applyColorMap(value,cv2.COLORMAP_TURBO);view[depth==0]=0;valid=depth[depth>0]
                ok_png,png=cv2.imencode('.png',depth,[cv2.IMWRITE_PNG_COMPRESSION,1]);ok_jpg,jpeg=cv2.imencode('.jpg',view,[cv2.IMWRITE_JPEG_QUALITY,85])
                if not ok_png or not ok_jpg:raise CameraError('深度图编码失败')
                meta=dict(metadata,sequence=counter,frame_index=index,timestamp=now,device_timestamp=device_time,
                    color_range_mm=[200,4000],valid_ratio=float((depth>0).mean()),minimum_mm=int(valid.min()) if valid.size else None,
                    maximum_mm=int(valid.max()) if valid.size else None,capture_pid=os.getpid())
                with self.condition:
                    if self.generation!=generation:break
                    self.meta,self.jpeg,self.png=meta,jpeg.tobytes(),png.tobytes();self.condition.notify_all()
        except Exception as exc:
            with self.condition:self.error=str(exc);self.condition.notify_all()
        finally:
            camera.close()
            with self.condition:
                if self.generation==generation:self.closed=True
                self.condition.notify_all()
    def close(self):
        with self.condition:self.closed=True;self.refs=0;self.generation+=1;self.stopping.set();self.condition.notify_all()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
            if self.thread.is_alive():raise CameraError('深度 SDK 尚未释放连接')
    def status(self):
        with self.condition:return dict(available=depth_present(),active=bool(self.thread and self.thread.is_alive() and not self.closed),
            clients=self.refs,error=self.error,profile=self.meta,age_ms=round((time.time()-self.meta['timestamp'])*1000) if self.meta else None)
