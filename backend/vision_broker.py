"""The platform's sole physical RGB + depth capture process (Unix socket RPC)."""
import os
import socket
import threading
from multiprocessing.connection import Listener

from .camera_manager import CameraManager, CameraError, list_devices
from .depth_manager import DepthManager


def serve(root, path, key, ready):
    cameras = CameraManager(); depth = DepthManager(root); stopping = threading.Event()
    listener = Listener(path, family='AF_UNIX', authkey=key); os.chmod(path,0o600)
    listener._listener._socket.settimeout(.3)
    ready.send(os.getpid()); ready.close()
    def client(connection):
        lease=None
        try:
            while not stopping.is_set():
                request=connection.recv();method=request[0]
                try:
                    if method=='acquire-rgb':lease=cameras.acquire(request[1],request[2]);value=True
                    elif method=='acquire-depth':lease=depth.acquire();value=True
                    elif method=='read-rgb':
                        _,frame=lease.read();value=(lease.seq,lease.captured_at,frame)
                    elif method=='jpeg':
                        data=lease.jpeg(request[1]);value=(lease.seq,lease.session.captured_at,data)
                    elif method=='read-depth':value=lease.read(request[1])
                    elif method=='release':
                        if lease:lease.release();lease=None
                        connection.send((True,None));return
                    elif method=='rgb-status':value=dict(cameras.status(),capture_pid=os.getpid())
                    elif method=='depth-status':value=dict(depth.status(),capture_pid=os.getpid())
                    elif method=='close-rgb':cameras.close_all(request[1]);value=True
                    elif method=='close-devices':cameras.close_devices(request[1]);value=True
                    elif method=='close-depth':depth.close();value=True
                    elif method=='discover':value=list_devices()
                    elif method=='shutdown':stopping.set();value=True
                    else:raise CameraError('未知视觉服务请求')
                    connection.send((True,value))
                except Exception as exc:connection.send((False,str(exc)))
        except (EOFError,OSError):pass
        finally:
            if lease:lease.release()
            connection.close()
    try:
        while not stopping.is_set():
            try:connection=listener.accept()
            except socket.timeout:continue
            threading.Thread(target=client,args=(connection,),daemon=True,name='vision-client').start()
    finally:
        listener.close()
        try:depth.close()
        except CameraError:pass
        try:cameras.close_all()
        except CameraError:pass
