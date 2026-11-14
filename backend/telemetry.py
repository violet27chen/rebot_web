"""Push measured left/right feedback to every page through one WebSocket feed."""
import asyncio
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class Telemetry:
    def __init__(self, root):
        self.root = Path(root); self.token = secrets.token_hex(24)
        self.directory = Path(tempfile.mkdtemp(prefix='rebot-feedback-'))
        self.path = self.directory/'feedback.sock'
        self.socket = None; self.thread = None; self.stop = threading.Event()
        self.lock = threading.RLock(); self.latest = {}; self.subscribers = set(); self.loop = None
        self.observers = {}; self.ports = {}; self.paused = False

    def start(self, loop):
        self.loop = loop
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path)); os.chmod(self.path, 0o600); self.socket.settimeout(.25)
        self.thread = threading.Thread(target=self._receive, daemon=True, name='joint-feedback'); self.thread.start()

    def environment(self, ports):
        self.ports = {os.path.realpath(port):side for side,port in ports.items() if port}
        return {'REBOT_TELEMETRY_SOCKET':str(self.path),'REBOT_TELEMETRY_KEY':self.token,
                'REBOT_TELEMETRY_PORTS':json.dumps(self.ports)}

    def _receive(self):
        while not self.stop.is_set():
            try:
                packet = json.loads(self.socket.recv(8192))
                if not secrets.compare_digest(str(packet.pop('token','')),self.token): continue
                side, port = packet.get('side'), packet.get('port')
                if self.paused or self.ports.get(port) != side or side not in ('left','right') or not os.path.exists(port): continue
                values = packet.get('joints')
                if not isinstance(values,list) or len(values)!=6 or not all(isinstance(q,(int,float)) and math.isfinite(q) for q in values): continue
                if 'gripper' in packet and (not isinstance(packet['gripper'],(int,float)) or not math.isfinite(packet['gripper'])): continue
                if abs(time.time()-packet.get('timestamp',0))>2: continue
                with self.lock: self.latest[side] = packet
                self.publish({'type':'feedback','arm':packet})
            except (OSError, ValueError, TypeError): pass

    def publish(self, packet):
        if not self.loop: return
        def deliver():
            for queue in tuple(self.subscribers):
                if queue.full():
                    try: queue.get_nowait()
                    except asyncio.QueueEmpty: pass
                try: queue.put_nowait(packet)
                except asyncio.QueueFull: pass
        try: self.loop.call_soon_threadsafe(deliver)
        except RuntimeError: pass

    def subscribe(self):
        queue = asyncio.Queue(maxsize=2); self.subscribers.add(queue); return queue

    def snapshot(self):
        with self.lock:
            return {'type':'snapshot','arms':dict(self.latest),'paused':self.paused,'layout':'parallel', 'time':time.time()}

    def stop_observers(self):
        for process in list(self.observers.values()):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        self.observers.clear()

    def observe_idle(self, ports, connected):
        if self.paused or not connected: self.stop_observers(); return
        self.environment(ports)
        wanted = set(ports.values())
        for port, process in list(self.observers.items()):
            if port not in wanted or process.poll() is not None:
                if process.poll() is None: process.terminate(); process.wait(timeout=2)
                self.observers.pop(port,None)
        hardware = self.root/'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml'
        if not hardware.exists(): return
        for side,port in ports.items():
            if not port or not os.path.exists(port) or port in self.observers: continue
            # Respect every existing owner, including the manual control daemon.
            result = subprocess.run(['fuser',port],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2)
            if result.returncode == 0: continue
            env = os.environ.copy(); env.update(self.environment(ports))
            self.observers[port] = subprocess.Popen([sys.executable,str(self.root/'backend/feedback_observer.py'),
                '--port',port,'--hardware',str(hardware)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def close(self):
        self.stop.set(); self.stop_observers()
        if self.socket: self.socket.close()
        try: self.path.unlink(); self.directory.rmdir()
        except OSError: pass
