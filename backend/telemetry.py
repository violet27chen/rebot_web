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
        self.observers = {}; self.ports = {}; self.bindings = {}; self.binding_generation = 0; self.binding_since = 0.; self.paused = False

    def start(self, loop):
        self.loop = loop
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path)); os.chmod(self.path, 0o600); self.socket.settimeout(.25)
        self.thread = threading.Thread(target=self._receive, daemon=True, name='joint-feedback'); self.thread.start()

    def configure(self, ports):
        """Current configured port owns the side; publisher side labels are hints.

        Clear cached poses whenever roles/ports change. Duplicate aliases remain
        unbound rather than arbitrarily selecting the last side in a dict.
        """
        bindings={side:{'port':str(port),'canonical_port':os.path.realpath(port)}
                  for side,port in ports.items() if side in ('left','right') and port}
        grouped={}
        for side,binding in bindings.items():grouped.setdefault(binding['canonical_port'],[]).append(side)
        for binding in bindings.values():binding['ambiguous']=len(grouped[binding['canonical_port']])!=1
        mapping={port:sides[0] for port,sides in grouped.items() if len(sides)==1}
        with self.lock:
            if bindings==self.bindings:return False
            self.bindings=bindings;self.ports=mapping;self.latest={}
            self.binding_generation+=1;self.binding_since=time.time()
            event={'type':'bindings','bindings':dict(bindings),'binding_generation':self.binding_generation,'time':self.binding_since}
        self.publish(event);return True

    def environment(self, ports):
        self.configure(ports)
        with self.lock:mapping=dict(self.ports)
        return {'REBOT_TELEMETRY_SOCKET':str(self.path),'REBOT_TELEMETRY_KEY':self.token,
                'REBOT_TELEMETRY_PORTS':json.dumps(mapping)}

    def accept(self, packet):
        """Validate feedback, then route by the configured canonical device path."""
        packet=dict(packet)
        if not secrets.compare_digest(str(packet.pop('token','')),self.token):return False
        source=packet.get('port')
        if not isinstance(source,str) or not source or not os.path.exists(source):return False
        port=os.path.realpath(source);values=packet.get('joints');timestamp=packet.get('timestamp')
        finite=lambda q:isinstance(q,(int,float)) and not isinstance(q,bool) and math.isfinite(q)
        if not isinstance(values,list) or len(values)!=6 or not all(finite(q) for q in values):return False
        if not finite(timestamp) or abs(time.time()-timestamp)>2:return False
        if 'gripper' in packet and not finite(packet['gripper']):return False
        with self.lock:
            side=self.ports.get(port)
            if self.paused or side not in ('left','right') or timestamp<self.binding_since:return False
            previous=self.latest.get(side)
            if previous and timestamp<previous['timestamp']:return False
            # Never trust a side captured by a long-lived observer before ports
            # were reassigned. Both server and browser route by device identity.
            packet.update(side=side,port=port,binding_port=self.bindings[side]['port'],
                          binding_generation=self.binding_generation)
            self.latest[side]=packet
            event={'type':'feedback','arm':packet,'binding_generation':self.binding_generation,'server_time':time.time()}
        self.publish(event);return True

    def _receive(self):
        while not self.stop.is_set():
            try:self.accept(json.loads(self.socket.recv(8192)))
            except (OSError, ValueError, TypeError):pass

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
            return {'type':'snapshot','arms':dict(self.latest),'paused':self.paused,'layout':'parallel','bindings':dict(self.bindings),'binding_generation':self.binding_generation,'time':time.time()}

    def stop_observers(self):
        for process in list(self.observers.values()):
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        self.observers.clear()

    def observe_idle(self, ports, connected):
        self.configure(ports)
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
