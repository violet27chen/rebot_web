"""Shared camera-only worker extracted from the standalone collector."""
from __future__ import annotations
import html
import multiprocessing as mp
import os
import queue
import re
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def camera_source(value):
    if re.fullmatch(r'\d+', value):
        return '/dev/video'+str(int(value))
    if re.fullmatch(r'/dev/video\d+', value) or value.startswith('/dev/v4l/'):
        return value
    raise ValueError('相机必须为非负编号、/dev/videoN 或 /dev/v4l/ 下的设备路径')

def check_owners(paths):
    wanted = {os.path.realpath(p) for p in paths}
    owners = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid(): continue
        try:
            for fd in (entry/'fd').iterdir():
                try:
                    link = os.readlink(fd)
                    if link.startswith('/dev/') and os.path.realpath(link) in wanted:
                        owners.append((entry.name, link))
                except OSError: pass
        except (OSError, PermissionError): pass
    if owners:
        raise RuntimeError('设备正在被其他进程占用，先结束原任务并断开设备，未杀死任何进程：'+str(sorted(set(owners))))

def capture_worker(nodes, capture_size, fps, stop, ready, commands, port, owner_pid):
    """Only this process opens cameras; stream preview reuses the same buffers."""
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    def capture(node):
        cap = None
        try:
            cap = cv2.VideoCapture(node['source'], cv2.CAP_V4L2)
            if not cap.isOpened(): raise RuntimeError('无法打开 '+node['source'])
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, capture_size[0]);cap.set(cv2.CAP_PROP_FRAME_HEIGHT, capture_size[1])
            cap.set(cv2.CAP_PROP_FPS, fps);cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            actual = cap.get(cv2.CAP_PROP_FPS)
            if actual > 0 and actual + 1 < fps: raise RuntimeError(f'实际 FPS {actual:g} 低于请求值 {fps}，请降低 --fps')
            target = np.frombuffer(node['pixels'], np.uint8).reshape(node['height'],node['width'],3)
            while not stop.is_set():
                valid, frame = cap.read()
                if not valid: raise RuntimeError('相机未返回图像：'+node['source'])
                frame = cv2.resize(frame,(node['width'],node['height']),interpolation=cv2.INTER_AREA)
                frame = cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
                with node['condition']:
                    target[:] = frame;node['stamp'].value = time.monotonic();node['sequence'].value += 1
                    node['condition'].notify_all()
        except Exception as error:
            with node['condition']:
                node['error'].value = str(error).encode('utf-8')[:500];node['condition'].notify_all()
            print('[相机失败]',node['name'],error,flush=True)
        finally:
            if cap is not None:cap.release()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            action = self.path.removeprefix('/control/')
            if action not in ('n','r','q') or not ready.is_set():self.send_error(409,'Collector is not ready');return
            try:commands.put_nowait(action)
            except queue.Full:self.send_error(409,'Command queue is busy');return
            self.send_response(204);self.end_headers()
        def do_GET(self):
            if self.path == '/':
                cards = ''.join('<article><h3>'+html.escape(n['name'])+'</h3><img src="/stream/'+str(i)+'"></article>' for i,n in enumerate(nodes))
                body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>双臂数据采集 · 实时画面</title>'
                        '<style>body{font:16px system-ui;background:#f4f6fa;margin:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}article{background:white;padding:16px;border-radius:12px}img{width:100%;height:auto}button{padding:10px;margin:6px}p{color:#465166}</style>'
                        '<h1>双臂技能采集 · 实时画面</h1><p>图像独立连续刷新。采集状态及保存进度见终端。左/右设备配置请先在终端核实。</p>'
                        '<button onclick="send(\'n\')">下一阶段</button><button onclick="send(\'r\')">重录本集</button><button onclick="if(confirm(\'保存当前集并结束，随后慢速收工归零？\'))send(\'q\')">保存并结束</button><span id="status"></span><main>'+cards+'</main>'
                        '<script>async function send(k){let r=await fetch("/control/"+k,{method:"POST"});document.querySelector("#status").textContent=r.ok?"请求已发送，等待采集进程处理":"采集尚未就绪或请求失败"}</script>').encode()
                self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.end_headers();self.wfile.write(body);return
            try:
                match=re.fullmatch(r'/stream/(\d+)',self.path)
                if not match:raise ValueError
                node = nodes[int(match[1])]
            except (ValueError,IndexError):self.send_error(404);return
            self.send_response(200);self.send_header('Cache-Control','no-store');self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers()
            last = 0
            try:
                while not stop.is_set():
                    with node['condition']:
                        node['condition'].wait_for(lambda:node['sequence'].value != last or node['error'].value or stop.is_set(),timeout=1)
                        if node['error'].value:break
                        if not node['sequence'].value:continue
                        last = node['sequence'].value
                        frame = np.frombuffer(node['pixels'],np.uint8).reshape(node['height'],node['width'],3).copy()
                    valid, jpg = cv2.imencode('.jpg',cv2.cvtColor(frame,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,75])
                    if valid:self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpg.tobytes()+b'\r\n');self.wfile.flush()
                    stop.wait(1/15)
            except (OSError,ConnectionError):pass
    server = None
    try:
        if port:
            server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
            threading.Thread(target=server.serve_forever,daemon=True).start()
        for node in nodes:threading.Thread(target=capture,args=(node,),daemon=True).start()
        while not stop.wait(.2):
            if os.getppid() != owner_pid:break
    except Exception as error:
        for node in nodes:
            with node['condition']:node['error'].value=str(error).encode()[:500];node['condition'].notify_all()
    finally:
        stop.set()
        if server:server.shutdown();server.server_close()

class CaptureService:
    def __init__(self,args):
        self.args=args;self.ctx=mp.get_context('spawn');self.stop=self.ctx.Event();self.ready=self.ctx.Event();self.commands=self.ctx.Queue(16)
        width,height=args.size
        self.nodes=[{**c,'width':width,'height':height,'pixels':self.ctx.RawArray('B',width*height*3),
                     'stamp':self.ctx.RawValue('d',0),'sequence':self.ctx.RawValue('Q',0),'error':self.ctx.RawArray('c',512),'condition':self.ctx.Condition()} for c in args.cameras]
        self.process=None
    def start(self):
        self.process=self.ctx.Process(target=capture_worker,args=(self.nodes,self.args.capture_size,self.args.fps,self.stop,self.ready,self.commands,self.args.preview_port,os.getpid()),name='collector-cameras')
        self.process.start();deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            for n in self.nodes:
                if n['error'].value:raise RuntimeError(n['error'].value.decode(errors='replace'))
            if not self.process.is_alive():raise RuntimeError('相机进程提前退出')
            if all(n['sequence'].value for n in self.nodes):return
            time.sleep(.05)
        raise TimeoutError('相机预检超时，尚未连接任何机械臂')
    def close(self):
        self.ready.clear();self.stop.set()
        if self.process:
            self.process.join(2)
            if self.process.is_alive():self.process.terminate();self.process.join(1)
            if self.process.is_alive():self.process.kill();self.process.join(1)
        self.commands.close();self.commands.join_thread()
