"""Private inherited pipes: model inference and CAN never share a Python GIL."""
import os
import subprocess
import threading
import time
from .geometry import SortingError
from multiprocessing.connection import Connection


class Worker:
    def __init__(self,python,module,root,env=None):
        read_child,write_parent=os.pipe();read_parent,write_child=os.pipe()
        try:
            self.process=subprocess.Popen([python,'-u','-m',module,str(read_child),str(write_child)],
                cwd=str(root),pass_fds=(read_child,write_child),stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,env=env)
        except BaseException:
            for fd in (read_child,write_parent,read_parent,write_child):os.close(fd)
            raise
        os.close(read_child);os.close(write_child)
        self.send=Connection(write_parent,readable=False);self.receive=Connection(read_parent,writable=False)
        self.lock=threading.Lock()
        self.sequence=0
    def alive(self):return self.process.poll() is None
    def call(self,method,payload=None,timeout=15):
        with self.lock:
            if not self.alive():raise SortingError('独立工作进程已退出，请检查所选 Python 环境和运行日志')
            self.sequence+=1;sequence=self.sequence
            self.send.send((sequence,method,payload or {}));deadline=time.monotonic()+timeout
            while True:
                if not self.receive.poll(max(0,deadline-time.monotonic())):raise SortingError('工作进程响应超时；不会自动重发命令')
                reply,ok,value=self.receive.recv()
                if reply==sequence:break
            if not ok:raise SortingError(str(value))
            return value
    def close(self):
        try:
            if self.alive():self.call('close',timeout=3)
        except Exception:pass
        if self.alive():
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        self.send.close();self.receive.close()


def worker_pipes():
    import sys
    return Connection(int(sys.argv[1]),writable=False),Connection(int(sys.argv[2]),readable=False)
