"""No hardware: exercise the actual installed follower and SDK homing bodies."""
import ast
import math
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from backend.homing_profile import trajectory
from backend.shutdown_worker import home_arm
import backend.main as main

ROOT=Path(__file__).resolve().parents[1]
SDK=ROOT/'rebot_grasp/sdk/reBotArm_control_py/reBotArm_control_py/controllers/rebotarm_endpose_controller.py'
FOLLOWER=Path('/home/legion/rebot_lerobot/lerobot/lerobot-robot-seeed-b601/lerobot_robot_seeed_b601/seeed_b601_follower.py')


class Motor:
    def __init__(self,q):self.pos=q;self.commands=[];self.stuck=False;self.status=1;self.mode=2
    def request_feedback(self):pass
    def get_state(self):return SimpleNamespace(pos=self.pos,vel=0.,torq=.1,t_mos=25.,t_rotor=25.,status_code=self.status)
    def send_pos_vel(self,q,v):
        self.commands.append((q,v))
        if not self.stuck:self.pos=q
    def send_force_pos(self,q,v,*_):self.send_pos_vel(q,v)
    def ensure_mode(self,mode,**_):self.mode=mode
    def damiao_read_param_u32(self,_):return self.mode
    def enable(self):self.status=1
    def disable(self):self.status=0


class Clock:
    def __init__(self):self.now=0.
    def sleep(self,t):self.now+=t
    def monotonic(self):return self.now


def extract(path,classname,methods,namespace):
    tree=ast.parse(path.read_text());original=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==classname)
    body=[n for n in original.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in methods]
    cls=ast.ClassDef(name='ControllerUnderTest',bases=[],keywords=[],body=body,decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
    env={'__name__':'backend.slow_home_test','__package__':'backend','np':np,'math':math,'DeviceNotConnectedError':RuntimeError,'logger':SimpleNamespace(info=lambda *a:None),**namespace}
    exec(compile(module,str(path),'exec'),env);return env['ControllerUnderTest']


class ProfileTests(unittest.TestCase):
    def test_long_return_is_bounded_in_speed_acceleration_and_starts_at_measurement(self):
        values=list(trajectory([2.,-3.8],[0,0]));dt=values[0][0];q=np.asarray([p for _,p in values]);v=np.diff(q,axis=0)/dt;a=np.diff(v,axis=0)/dt
        np.testing.assert_equal(q[0],[2.,-3.8]);np.testing.assert_allclose(q[-1],0,atol=1e-12)
        self.assertLessEqual(abs(v).max(),.15+1e-6);self.assertLessEqual(abs(a).max(),.20+1e-6)
        self.assertGreater(dt*len(q),45.)
    def test_short_return_has_a_soft_start_and_end(self):
        points=list(trajectory([.02],[0]));self.assertGreaterEqual(sum(dt for dt,_ in points),2.)
        self.assertLess(abs(points[1][1][0]-.02),.00001)
    def test_backend_homing_never_emits_a_fast_native_target(self):
        clock=Clock();motors={i:Motor(q) for i,q in enumerate([1,-.8,-1.2,.4,-.5,.3,-3.8],1)}
        home_arm(SimpleNamespace(poll_feedback_once=lambda:None),motors,position_mode=2,force_mode=4,sleep=clock.sleep,clock=clock.monotonic,log=lambda *a,**k:None)
        for motor in motors.values():
            self.assertLessEqual(max(v for q,v in motor.commands),.15)
            self.assertLessEqual(max(abs(b[0]-a[0]) for a,b in zip(motor.commands,motor.commands[1:])),.00501)
            self.assertEqual(motor.pos,0);self.assertEqual(motor.status,0)


@unittest.skipUnless(FOLLOWER.exists(),'standalone editable driver not installed')
class FollowerTests(unittest.TestCase):
    def make(self):
        clock=Clock();C=extract(FOLLOWER,'SeeedB601FollowerBase',{'safe_zero'},{'time':clock});obj=C()
        obj.is_connected=True;obj._in_safe_zero=False;obj.motor_type='dm';obj.cameras={}
        obj.bus=SimpleNamespace(poll_feedback_once=lambda:None)
        names=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_yaw','wrist_roll','gripper']
        obj.motors={name:Motor(q) for name,q in zip(names,[1,-.8,-1.2,.4,-.5,.3,-3.8])}
        obj.config=SimpleNamespace(motor_can_ids={name:(i,i+16) for i,name in enumerate(names,1)},gripper_park_deg=0.,force_pos_torque_ration=.12)
        return obj,clock
    def test_installed_dm_follower_does_not_reuse_runtime_32rad_speed_or_half_second_gripper(self):
        arm,clock=self.make();arm.safe_zero(exit_on_complete=False)
        self.assertFalse(arm._safe_zero_incomplete);self.assertGreater(clock.now,45)
        for motor in arm.motors.values():
            self.assertLessEqual(max(v for q,v in motor.commands),.15)
            self.assertLessEqual(max(abs(b[0]-a[0]) for a,b in zip(motor.commands,motor.commands[1:])),.00301)
            self.assertEqual(motor.pos,0)
    def test_obstruction_never_sends_final_zero_jump_or_claims_finished(self):
        arm,_=self.make();arm.motors['elbow_flex'].stuck=True
        with self.assertRaisesRegex(RuntimeError,'following error'):arm.safe_zero(exit_on_complete=False)
        self.assertTrue(arm._safe_zero_incomplete);self.assertFalse(arm._in_safe_zero)
        motor=arm.motors['elbow_flex'];self.assertEqual(motor.commands[-1][0],-1.2)


class SdkTests(unittest.TestCase):
    def make(self):
        clock=Clock();C=extract(SDK,'RebotArmEndPose',{'safe_home','_home_measurement','start'},{'time':clock});obj=C()
        obj._running=True;obj._n=6;obj._has_gripper=True;obj._send_thread=None;obj._stop_send=threading.Event();obj._vlim_override=None
        obj._q_target=np.zeros(6);obj._qd_target=np.zeros(6);obj._gripper_target=0.
        names=['j'+str(i) for i in range(7)];motors={n:Motor(q) for n,q in zip(names,[1,-.8,-1.2,.4,-.5,.3,-3.8])}
        obj._arm_group=SimpleNamespace(joint_names=names[:6]);obj._gripper_group=SimpleNamespace(joint_names=names[6:])
        obj.rebotarm=SimpleNamespace(_motor_map=motors,get_state=lambda:None)
        def feedback(t):
            for name,q in zip(names,list(obj._q_target)+[obj._gripper_target]):motors[name].send_pos_vel(q,.15)
            clock.now+=t
        clock.sleep=feedback;return obj,motors,clock
    def test_sdk_slowly_returns_all_joints_from_measured_pose(self):
        ctl,motors,clock=self.make();ctl.safe_home()
        self.assertGreater(clock.now,45.)
        for m in motors.values():
            self.assertLessEqual(max(abs(b[0]-a[0]) for a,b in zip(m.commands,m.commands[1:])),.00301)
            self.assertEqual(m.pos,0.)
    def test_timeout_or_tracking_error_does_not_overwrite_target_with_zero(self):
        ctl,motors,_=self.make();motors['j2'].stuck=True
        with self.assertRaisesRegex(RuntimeError,'following error'):ctl.safe_home()
        self.assertEqual(ctl._q_target[2],-1.2);self.assertEqual(motors['j2'].pos,-1.2)
    def test_sdk_start_seeds_measured_targets_before_first_enable(self):
        ctl,motors,_=self.make();ctl._running=False;ctl._arm_control_mode='posvel';events=[]
        expected=[m.pos for m in motors.values()]
        ctl.rebotarm.connect=lambda:None;ctl._loop_cb=lambda *a:None
        ctl.rebotarm.start_control_loop=lambda callback:events.append('loop')
        ctl._arm_group.mode_pos_vel=lambda:True
        ctl._arm_group.send_pos_vel=lambda q,**kw:events.append(('seed',list(q)))
        ctl._arm_group.enable=lambda:events.append('enable')
        ctl._gripper_group.mode_mit=lambda:True
        ctl._gripper_group.send_mit=lambda q:events.append(('gripper',list(q)))
        ctl._gripper_group.enable=lambda:events.append('gripper-enable')
        ctl.start()
        self.assertEqual(events[0],('seed',expected[:6]))
        self.assertLess(events.index(('gripper',expected[6:])),events.index('enable'))
        np.testing.assert_equal(ctl._q_target,expected[:6]);self.assertEqual(ctl._gripper_target,expected[6])
    def test_unconfirmed_control_mode_cannot_enable(self):
        ctl,_,_=self.make();ctl._running=False;ctl._arm_control_mode='posvel'
        ctl.rebotarm.connect=lambda:None;ctl._arm_group.mode_pos_vel=lambda:False
        ctl._arm_group.enable=Mock()
        with self.assertRaisesRegex(RuntimeError,'mode not confirmed'):ctl.start()
        ctl._arm_group.enable.assert_not_called()
    def test_no_current_feedback_cannot_start_a_home(self):
        ctl,motors,_=self.make();motors['j1'].get_state=lambda:None
        with self.assertRaisesRegex(RuntimeError,'feedback'):ctl.safe_home()
        self.assertTrue(all(not m.commands for m in motors.values()))


class StopTests(unittest.TestCase):
    def test_repeated_normal_stop_sends_one_signal_and_does_not_block_status_lock(self):
        job=main.Job(name='qa',kind='teleoperate',command=[],display='qa');job.proc=SimpleNamespace(poll=lambda:None)
        gate=threading.Event()
        def terminate(*a,**k):gate.wait(1)
        with patch.object(main.STATE,'job',job),patch.object(main,'_terminate',side_effect=terminate) as stop:
            began=time.monotonic();main.stop_all();main.stop_all()
            self.assertLess(time.monotonic()-began,.1)
            self.assertTrue(job.stopping);self.assertEqual(stop.call_count,1)
            self.assertTrue(main.STATE.lock.acquire(timeout=.05));main.STATE.lock.release();gate.set()


if __name__=='__main__':unittest.main()
