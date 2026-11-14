"""Best-effort feedback publisher; never sends commands to hardware."""
import json
import math
import os
import socket
import time


class FeedbackPublisher:
    def __init__(self):
        self.path = os.environ.get('REBOT_TELEMETRY_SOCKET', '')
        self.token = os.environ.get('REBOT_TELEMETRY_KEY', '')
        self.mapping = json.loads(os.environ.get('REBOT_TELEMETRY_PORTS', '{}'))
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) if self.path else None
        if self.socket: self.socket.setblocking(False)

    def send(self, port, joints, torque=None, gripper=None):
        if not self.socket or len(joints) < 6 or not all(math.isfinite(float(q)) for q in joints):
            return
        side = self.mapping.get(os.path.realpath(port))
        if not side: return
        packet = {'token':self.token, 'side':side, 'port':os.path.realpath(port),
                  'timestamp':time.time(), 'joints':list(joints[:6]), 'torque':list(torque or []), 'unit':'rad'}
        if gripper is not None and math.isfinite(float(gripper)): packet['gripper'] = float(gripper)
        try: self.socket.sendto(json.dumps(packet).encode(), self.path)
        except (OSError, ValueError): pass
