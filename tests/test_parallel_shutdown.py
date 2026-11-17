"""Exercise both real shutdown paths with simulated buses; never open hardware."""
import ast
import math
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from backend.coordinated_shutdown import ShutdownGroup, ShutdownCancelled, parallel_shutdown
from backend.shutdown_worker import home_arm

PACKAGE=Path('/home/legion/rebot_lerobot/lerobot/lerobot-robot-seeed-b601/lerobot_robot_seeed_b601')
NAMES=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_yaw','wrist_roll','gripper']


class FastClock:
    def __init__(self):self.now=0.
    def sleep(self,seconds):
        self.now+=seconds
        time.sleep(.00002)  # Yield to the other independent bus worker.
    def monotonic(self):return self.now


class Motor:
    def __init__(self,position,mode):
        self.pos=position;self.mode=mode;self.status=1;self.stuck=False;self.missing=False
        self.commands=[];self.disables=0;self.closed=False;self.on_disable=lambda:None
        self.on_send=lambda:None
    def request_feedback(self):pass
    def get_state(self):
        return None if self.missing else NS(pos=self.pos,vel=0.,torq=0.,t_mos=20.,t_rotor=20.,status_code=self.status)
    def ensure_mode(self,mode,**kwargs):self.mode=mode
    def damiao_read_param_u32(self,index):return self.mode
    def send_pos_vel(self,position,speed):
        self.on_send();self.commands.append((position,speed))
        if not self.stuck:self.pos=position
    def send_force_pos(self,position,speed,ratio):self.send_pos_vel(position,speed)
    def enable(self):self.status=1
    def disable(self):self.on_disable();self.disables+=1;self.status=0
    def close(self):self.closed=True


def motors(scale=1):
    return {i:Motor(position*scale,4 if i==7 else 2)
            for i,position in enumerate([.4,-.5,-.7,.2,-.2,.3,-.8],1)}


def extract(path,classname,methods,**env):
    tree=ast.parse(path.read_text())
    source=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==classname)
    cls=ast.ClassDef(name='TestDriver',bases=[],keywords=[],decorator_list=[],
                    body=[n for n in source.body if isinstance(n,ast.FunctionDef) and n.name in methods])
    module=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
    scope={'__name__':'backend.shutdown_test','__package__':'backend','math':math,
           'logger':Mock(),'DeviceNotConnectedError':RuntimeError,'FOLLOWER_GRIPPER_MOTOR':'gripper',**env}
    exec(compile(module,str(path),'exec'),scope)
    return scope['TestDriver']


class GroupTests(unittest.TestCase):
    def test_both_operations_start_before_either_finishes(self):
        started=[threading.Event(),threading.Event()]
        def operation(side,group):
            started[side].set()
            if not started[1-side].wait(.5):raise RuntimeError('sequential shutdown')
            group.checkpoint()
        parallel_shutdown([0,1],operation)
    def test_first_hardware_error_cancels_peer_and_waits_for_cleanup(self):
        entered=threading.Event();cleaned=threading.Event()
        def operation(side,group):
            if side==0:
                entered.set()
                try:group.checkpoint()
                finally:cleaned.set()
            else:
                self.assertTrue(entered.wait(.5))
                raise ValueError('missing motor feedback')
        with self.assertRaisesRegex(ValueError,'missing motor feedback'):
            parallel_shutdown([0,1],operation)
        self.assertTrue(cleaned.is_set())
    def test_timeout_does_not_wait_forever_or_advance_a_stage(self):
        group=ShutdownGroup(2,timeout=.01)
        with self.assertRaises(TimeoutError):group.checkpoint()
        self.assertTrue(group.cancelled.is_set())
    def test_different_starting_poses_use_one_shared_slow_duration(self):
        values=[]
        def operation(distance,group):
            values.append(group.movement_duration([distance],[0.]))
        parallel_shutdown([.1,1.],operation)
        self.assertEqual(values,[9.375,9.375])


class ExplicitHomeTests(unittest.TestCase):
    def prepare(self):
        pair=[motors(),motors(.4)]
        verified=[]
        def disable_check():
            self.assertTrue(all(abs(m.pos)<.035 for arm in pair for m in arm.values()),
                            'neither arm may disable before both are at zero')
            verified.append(True)
        for arm in pair:
            for motor in arm.values():motor.on_disable=disable_check
        return pair,verified
    def finish(self,pair):
        def operation(item,group):
            clock=FastClock()
            home_arm(NS(poll_feedback_once=lambda:None),item,position_mode=2,force_mode=4,
                     sleep=clock.sleep,clock=clock.monotonic,log=lambda *a,**k:None,sync=group)
        parallel_shutdown(pair,operation)
    def test_both_start_motion_concurrently_and_disable_after_both_arrive(self):
        pair,verified=self.prepare();started=[threading.Event(),threading.Event()]
        for side,arm in enumerate(pair):
            def first(side=side):
                started[side].set()
                self.assertTrue(started[1-side].wait(.5))
            arm[1].on_send=first
        self.finish(pair)
        self.assertEqual(len(verified),14)
        self.assertTrue(all(m.status==0 for arm in pair for m in arm.values()))
    def test_preflight_failure_prevents_motion_on_both_arms(self):
        pair,_=self.prepare();pair[1][2].missing=True
        with self.assertRaisesRegex(RuntimeError,'没有实测反馈'):self.finish(pair)
        self.assertTrue(all(not m.commands and not m.disables for arm in pair for m in arm.values()))
    def test_obstruction_stops_peer_without_disabling_either_arm(self):
        pair,_=self.prepare();pair[1][3].stuck=True
        with self.assertRaisesRegex(RuntimeError,'归零未到位'):self.finish(pair)
        self.assertTrue(all(not m.disables for arm in pair for m in arm.values()))


@unittest.skipUnless((PACKAGE/'bi_seeed_b601_dm_follower.py').exists(),'editable B601 plugin not installed')
class InstalledDriverTests(unittest.TestCase):
    def pair(self):
        arms=[]
        for side,scale in [('left',1),('right',.4)]:
            C=extract(PACKAGE/'seeed_b601_follower.py','SeeedB601FollowerBase',{'safe_zero','disconnect'},time=FastClock())
            arm=C();arm.is_connected=True;arm._connection_ready=True;arm._in_safe_zero=False
            arm._skip_safe_zero_on_disconnect=False;arm._emergency_disable_requested=False
            arm.motor_type='dm';arm.cameras={};arm.bus=NS(poll_feedback_once=lambda:None,close=Mock())
            arm.motors=dict(zip(NAMES,motors(scale).values()))
            arm.config=NS(motor_can_ids={n:(i,i+16) for i,n in enumerate(NAMES,1)},port='/dev/qa-'+side,
                          gripper_park_deg=0.,force_pos_torque_ration=.12,disable_torque_on_disconnect=True,
                          hold_gripper_on_disconnect=False)
            arms.append(arm)
        C=extract(PACKAGE/'bi_seeed_b601_dm_follower.py','BiSeeedB601DMFollower',{'disconnect'})
        robot=C();robot.left_arm,robot.right_arm=arms
        return robot,arms
    def test_native_disconnect_uses_concurrent_motion_and_joint_disable_gate(self):
        robot,arms=self.pair();started=[threading.Event(),threading.Event()]
        for side,arm in enumerate(arms):
            def first(side=side):
                started[side].set();self.assertTrue(started[1-side].wait(.5))
            arm.motors['shoulder_pan'].on_send=first
            def disable():self.assertTrue(all(abs(m.pos)<.035 for a in arms for m in a.motors.values()))
            for motor in arm.motors.values():motor.on_disable=disable
        robot.disconnect()
        self.assertTrue(all(m.disables==1 and m.closed for arm in arms for m in arm.motors.values()))
        self.assertTrue(all(arm.bus is None and not hasattr(arm,'_coordinated_shutdown') for arm in arms))
    def test_native_swallowed_homing_error_cancels_peer_and_reports_failure(self):
        robot,arms=self.pair();arms[1].motors['elbow_flex'].stuck=True
        with self.assertRaises(ShutdownCancelled):robot.disconnect()
        self.assertTrue(all(not m.disables and m.closed for arm in arms for m in arm.motors.values()))
        self.assertTrue(all(arm.bus is None for arm in arms))
    def test_partial_failed_connection_only_cleans_transport_without_deadlock(self):
        robot,arms=self.pair()
        for arm in arms:
            arm._skip_safe_zero_on_disconnect=True;arm._connection_ready=False;arm.is_connected=False
        robot.disconnect()
        self.assertTrue(all(not m.commands and m.closed for arm in arms for m in arm.motors.values()))
    def test_single_remaining_bus_can_finish_without_a_missing_peer(self):
        robot,arms=self.pair();arms[1].bus=None;arms[1].is_connected=False
        robot.disconnect()
        self.assertTrue(all(m.disables==1 for m in arms[0].motors.values()))


if __name__=='__main__':unittest.main()
