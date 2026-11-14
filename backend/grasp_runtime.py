"""Use the installed official trajectory controller with bounded gripper torque.

Imported only for an explicitly confirmed physical grasp. FK/IK never imports
this runtime or opens a transport. Stopping does not call the SDK's homing end().
"""
import time
import numpy as np
from reBotArm_control_py.controllers import RebotArmEndPose
from telemetry_client import FeedbackPublisher


def clipped_gripper_torque(target, position, velocity, limit=.3):
    return float(np.clip(4.0*(target-position)-.15*velocity, -limit, limit))


class GraspController(RebotArmEndPose):
    def __init__(self, arm, joints, torque_limit=.3):
        super().__init__(arm, arm_control_mode='mit', use_gravity_ff=True)
        self._has_gripper = False  # Replace the SDK's unbounded gripper PD below.
        self._q_target[:] = joints
        self.gripper = arm._motor_map['gripper']
        state = self.gripper.get_state()
        if state is None: raise RuntimeError('夹爪没有实测反馈')
        self.gripper_target = state.pos
        self.limit = float(torque_limit)
        if not .05 <= self.limit <= .5: raise ValueError('夹爪力矩限制需要在 0.05～0.5 Nm 之间')
        self.error = None; self.publisher = FeedbackPublisher(); self.last_publish = 0

    def start(self):
        self.rebotarm.groups['gripper'].mode_mit()
        super().start()

    def _loop_cb(self, arm, dt):
        try:
            arm.get_state()  # Request measured feedback within this one control loop.
            motors = [arm._motor_map[name] for name in self._arm_group.joint_names]
            states = [motor.get_state() for motor in motors]
            grip = self.gripper.get_state()
            if grip is None or any(s is None for s in states): raise RuntimeError('关节反馈缺失')
            if any(s.t_mos > 80 for s in states+[grip]): raise RuntimeError('电机温度超过 80°C，已停止抓取')
            super()._loop_cb(arm,dt)  # Official URDF-based gravity feedforward and trajectory.
            torque = clipped_gripper_torque(self.gripper_target,grip.pos,grip.vel,self.limit)
            self.gripper.send_mit(grip.pos,0,0,0,torque)
            if time.monotonic()-self.last_publish >= 1/30:
                self.last_publish = time.monotonic()
                self.publisher.send(arm._channel,[s.pos for s in states],[s.torq for s in states])
        except Exception as exc:
            self.error = exc; self._stop_send.set(); arm._running = False
            arm.disable_all()

    def check(self):
        if self.error: raise RuntimeError(str(self.error))

    def move(self,x,y,z,roll,pitch,yaw,duration=2):
        self.check()
        if not self.move_to_traj(x,y,z,roll,pitch,yaw,duration=duration): raise RuntimeError('轨迹求解失败')
        deadline=time.monotonic()+duration+8
        while self._moving:
            self.check()
            if time.monotonic()>deadline: raise TimeoutError('轨迹执行超时')
            time.sleep(.02)
        self.check()
        time.sleep(.2)

    def open_gripper(self):
        # Same measured opening used by the existing follower-control page.
        self.gripper_target = -3.05
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            self.check(); state=self.gripper.get_state()
            if state and abs(state.pos-self.gripper_target)<.12:return
            time.sleep(.02)
        raise TimeoutError('夹爪未能张开到目标，已停止抓取')

    def grasp(self):
        self.gripper_target=0; deadline=time.monotonic()+4; stable=0
        while time.monotonic()<deadline:
            self.check(); state=self.gripper.get_state()
            if state:
                if abs(state.pos)<.1:return False
                stable=stable+1 if abs(state.vel)<.05 and abs(state.torq)>.05 else 0
                if stable>=10:return True
            time.sleep(.03)
        raise TimeoutError('夹爪闭合反馈不明确，已停止抓取')

    def stop(self):
        self._stop_send.set()
        if self._send_thread:self._send_thread.join(timeout=2)
        self.rebotarm.stop_control_loop()
        self._running=False
