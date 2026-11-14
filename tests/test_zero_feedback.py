"""Cold bridge / delayed feedback tests. Never opens a real device."""
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from backend.zero_adjust_worker import adjust_zero,collect_feedback
from test_global_controls import Motor,Clock
import backend.main as main

class DelayedBus:
    def __init__(self,ready_after=24):self.polls=0;self.ready_after=ready_after
    def poll_feedback_once(self):self.polls+=1

class DelayedMotor(Motor):
    def __init__(self,i,bus):super().__init__(i);self.bus=bus;self.requests=0
    def request_feedback(self):self.requests+=1
    def get_state(self):return super().get_state() if self.bus.polls>=self.bus.ready_after and self.requests else None

class FeedbackTests(unittest.TestCase):
    def test_selected_shoulder_read_does_not_require_six_other_joints(self):
        motors={i:Motor(i) for i in range(1,8)}
        for i,m in motors.items():m.missing=i!=2
        clock=Clock();bus=SimpleNamespace(poll_feedback_once=lambda:None)
        r=adjust_zero(bus,motors,'read',[2],sleep=clock.sleep,clock=clock.time)
        self.assertEqual(r['selected'],[2]);self.assertEqual([j['id'] for j in r['joints']],[2])
        self.assertEqual(r['missing_joints'],[1,3,4,5,6,7]);self.assertAlmostEqual(r['joints'][0]['degrees'],math.degrees(.3))
        self.assertTrue(all(m.enables==m.disables==m.zeros==m.stores==0 and not m.commands for m in motors.values()))
    def test_cold_bridge_retries_and_drains_pending_frames(self):
        clock=Clock();bus=DelayedBus();motors={i:DelayedMotor(i,bus) for i in range(1,8)}
        r=adjust_zero(bus,motors,'read',list(range(1,8)),sleep=clock.sleep,clock=clock.time)
        self.assertEqual(len(r['joints']),7);self.assertGreater(bus.polls,24)
        self.assertGreater(motors[2].requests,1);self.assertTrue(all(not m.commands and m.enables==0 for m in motors.values()))
    def test_missing_all_joints_names_all_ids_and_never_returns_fabricated_zero(self):
        clock=Clock();motors={i:Motor(i) for i in range(1,8)}
        for m in motors.values():m.missing=True
        with self.assertRaisesRegex(RuntimeError,'CAN ID 1,2,3,4,5,6,7'):
            adjust_zero(SimpleNamespace(poll_feedback_once=lambda:None),motors,'read',list(range(1,8)),sleep=clock.sleep,clock=clock.time)
        self.assertTrue(all(m.zeros==m.enables==m.disables==0 for m in motors.values()))
    def test_releasing_or_setting_zero_still_requires_entire_arm(self):
        for action in ['release','set-zero']:
            clock=Clock();motors={i:Motor(i) for i in range(1,8)};motors[3].missing=True
            with self.assertRaisesRegex(RuntimeError,'CAN ID 3'):
                adjust_zero(SimpleNamespace(poll_feedback_once=lambda:None),motors,action,[2],sleep=clock.sleep,clock=clock.time)
            self.assertTrue(all(m.zeros==m.enables==m.disables==0 for m in motors.values()))
    def test_parameter_diagnostics_do_not_write_registers_or_modes(self):
        clock=Clock();bus=DelayedBus(100000);motors={i:DelayedMotor(i,bus) for i in range(1,8)};lines=[]
        with self.assertRaises(RuntimeError):collect_feedback(bus,motors,{2},sleep=clock.sleep,clock=clock.time,max_attempts=12,log=lambda m,**k:lines.append(m))
        self.assertTrue(any('控制模式' in x for x in lines));self.assertTrue(any('反馈ID' in x for x in lines))
        self.assertTrue(all(m.mode in (2,4) and m.zeros==m.enables==0 for m in motors.values()))
    def test_full_error_is_preserved_for_global_frontend(self):
        job=main.Job('read','zero-adjust',[],'mock');error='CAN ID 2: '+('设备回包诊断 '*80)
        import json
        main._record_line(job,'REBOT_ZERO_ERROR '+json.dumps({'ok':False,'error':error}))
        self.assertEqual(job.result['error'],error);self.assertLessEqual(len(job.last_line),180)

if __name__=='__main__':unittest.main()
