"""Web-side leases. Only the child vision process can open capture devices."""
import multiprocessing as mp
import os
import secrets
import shutil
import tempfile
import threading
from pathlib import Path
from multiprocessing.connection import Client

from .camera_manager import CameraError
from .depth_manager import depth_present
from .vision_broker import serve


class RPCLease:
    def __init__(self, connection, depth=False):
        self.connection=connection;self.depth=depth;self.seq=0;self.captured_at=0
        self.released=False;self.lock=threading.RLock();self.session=self;self.error=''
    def request(self,*args):
        with self.lock:
            if self.released:raise CameraError('视觉连接已释放')
            try:
                self.connection.send(args)
                if not self.connection.poll(15):raise CameraError('视觉服务等待超时')
                valid,value=self.connection.recv()
                if not valid:raise CameraError(value)
                return value
            except (OSError,EOFError) as exc:
                self.error='共享视觉进程已断开，请重新连接画面';raise CameraError(self.error) from exc
    def read(self,fresh=False):
        if self.depth:return self.request('read-depth',fresh)
        self.seq,self.captured_at,frame=self.request('read-rgb');return True,frame
    def jpeg(self,fresh=False):
        self.seq,self.captured_at,jpeg=self.request('jpeg',fresh);return jpeg
    def release(self):
        with self.lock:
            if self.released:return
            self.released=True
            try:self.connection.send(('release',))
            except (OSError,EOFError):pass
            self.connection.close()


class VisionProcess:
    def __init__(self,root):
        self.root=str(root);self.context=mp.get_context('spawn');self.lock=threading.RLock()
        self.process=None;self.folder=None;self.path=None;self.key=secrets.token_bytes(32);self.paused=False
        self.rgb=RGBCameras(self);self.depth=DepthCamera(self)
    def alive(self):return bool(self.process and self.process.is_alive())
    def start(self):
        with self.lock:
            if self.paused:raise CameraError('设备已断开，请先恢复监视或重新连接设备')
            if self.alive():return
            if self.process:self.process.join(timeout=.1)
            if self.folder:shutil.rmtree(self.folder,ignore_errors=True)
            self.folder=tempfile.mkdtemp(prefix='rebot-vision-');os.chmod(self.folder,0o700)
            self.path=str(Path(self.folder)/'capture.sock')
            receive,send=self.context.Pipe(duplex=False)
            self.process=self.context.Process(target=serve,args=(self.root,self.path,self.key,send),name='rebot-vision',daemon=True)
            self.process.start();send.close()
            try:
                if not receive.poll(10):raise CameraError('共享视觉进程启动超时')
                receive.recv()
            except (EOFError,OSError) as exc:raise CameraError('共享视觉进程启动失败') from exc
            finally:receive.close()
    def connect(self,start=True):
        if start:self.start()
        if not self.alive():raise CameraError('共享视觉进程尚未启动')
        try:return Client(self.path,family='AF_UNIX',authkey=self.key)
        except (OSError,EOFError) as exc:raise CameraError('共享视觉进程连接已断开') from exc
    def request(self,*args,start=True):
        connection=self.connect(start)
        try:
            connection.send(args)
            if not connection.poll(2 if args[0].endswith('status') else 15):raise CameraError('共享视觉进程响应超时')
            valid,value=connection.recv()
            if not valid:raise CameraError(value)
            return value
        except (EOFError,OSError) as exc:raise CameraError('共享视觉进程已退出') from exc
        finally:connection.close()
    def acquire(self,method,*args,depth=False):
        connection=self.connect()
        lease=RPCLease(connection,depth)
        try:lease.request(method,*args);return lease
        except BaseException:lease.release();raise
    def discover(self):return self.request('discover')
    def stop(self):
        with self.lock:
            if self.alive():
                try:self.request('shutdown',start=False)
                except CameraError:pass
                self.process.join(timeout=3)
                if self.process.is_alive():self.process.terminate();self.process.join(timeout=2)
                if self.process.is_alive():self.process.kill();self.process.join(timeout=2)
            if self.folder:shutil.rmtree(self.folder,ignore_errors=True)


class RGBCameras:
    def __init__(self,vision):self.vision=vision
    def acquire(self,source,config=None):return self.vision.acquire('acquire-rgb',source,config or {})
    def close_all(self,exclusive=False):
        if self.vision.alive():return self.vision.request('close-rgb',exclusive,start=False)
    def close_devices(self,sources):
        if sources and self.vision.alive():return self.vision.request('close-devices',list(sources),start=False)
    def status(self):
        if self.vision.alive():
            try:return self.vision.request('rgb-status',start=False)
            except CameraError:pass
        return {'sessions':[],'exclusive':False,'capture_pid':None}


class DepthCamera:
    def __init__(self,vision):self.vision=vision
    def acquire(self):return self.vision.acquire('acquire-depth',depth=True)
    def close(self):
        if self.vision.alive():return self.vision.request('close-depth',start=False)
    def status(self):
        if self.vision.alive():
            try:return self.vision.request('depth-status',start=False)
            except CameraError:pass
        return {'available':depth_present(),'active':False,'clients':0,'profile':None,'age_ms':None,'error':'','capture_pid':None}
