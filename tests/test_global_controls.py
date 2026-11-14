"""Hardware-free checks for global interaction, lifecycle and explicit homing."""
import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import backend.main as main
from backend.dual_probe import names_from_registry
from backend.shutdown_worker import home_arm
from backend.zero_adjust_worker import adjust_zero
from backend.motor_mode import ensure_mode_checked


class Clock:
    def __init__(self):self.now=0.
    def sleep(self,delta):self.now+=delta
    def time(self):return self.now


class Motor:
    def __init__(self,index):
        self.index=index;self.pos=.15*index;self.mode=4 if index==7 else 2
        self.status=0;self.missing=False;self.stuck=False;self.bad_mode=False
        self.enables=0;self.disables=0;self.commands=[];self.temperature=25
        self.zeros=0;self.stores=0
    def request_feedback(self):pass
    def get_state(self):
        if self.missing:return None
        return SimpleNamespace(pos=self.pos,vel=0.,torq=0.,t_mos=self.temperature,
                               t_rotor=25.,status_code=self.status)
    def ensure_mode(self,mode,**kwargs):self.mode=int(mode)
    def damiao_read_param_u32(self,register):
        if self.bad_mode:raise TimeoutError('No live register response')
        return self.mode
    def get_register_u32(self,register,**kw):return self.damiao_read_param_u32(register)
    def set_zero_position(self):self.zeros+=1;self.pos=0
    def store_parameters(self):self.stores+=1
    def enable(self):self.enables+=1;self.status=1
    def disable(self):self.disables+=1;self.status=0
    def send_pos_vel(self,pos,speed):
        self.commands.append((pos,speed))
        if not self.stuck:self.pos=pos
    def send_force_pos(self,pos,speed,ratio):self.send_pos_vel(pos,speed)


class HomingTests(unittest.TestCase):
    def setUp(self):
        self.motors={i:Motor(i) for i in range(1,8)}
        self.clock=Clock();self.bus=SimpleNamespace(poll_feedback_once=lambda:None)
    def home(self):
        return home_arm(self.bus,self.motors,position_mode=2,force_mode=4,
                        sleep=self.clock.sleep,clock=self.clock.time,log=lambda *a,**k:None)
    def test_seeds_measured_positions_then_homes_and_confirms_disabled(self):
        starts={i:m.pos for i,m in self.motors.items()};self.home()
        for i,m in self.motors.items():
            self.assertEqual(m.commands[0][0],starts[i])
            self.assertEqual(m.pos,0);self.assertEqual(m.status,0)
            self.assertEqual(m.enables,1);self.assertGreaterEqual(m.disables,1)
            self.assertTrue(all(speed<=.5 for _,speed in m.commands))
    def test_missing_feedback_never_enables_or_disables(self):
        self.motors[3].missing=True
        with self.assertRaisesRegex(RuntimeError,'没有实测反馈'):self.home()
        self.assertTrue(all(m.enables==m.disables==0 for m in self.motors.values()))
    def test_register_timeout_never_enables_or_reports_success(self):
        self.motors[2].bad_mode=True
        with self.assertRaises(TimeoutError):self.home()
        self.assertTrue(all(m.enables==m.disables==0 for m in self.motors.values()))
    def test_stalled_joint_does_not_disable_an_unhomed_arm(self):
        self.motors[4].stuck=True
        with self.assertRaisesRegex(RuntimeError,'归零未到位'):self.home()
        self.assertTrue(all(m.disables==0 for m in self.motors.values()))
    def test_gripper_obstruction_cannot_be_treated_as_zero(self):
        self.motors[7].stuck=True
        with self.assertRaisesRegex(RuntimeError,'限力夹爪归零未到位'):self.home()
        self.assertTrue(all(m.disables==0 for m in self.motors.values()))
    def test_overtemperature_does_not_start_homing(self):
        self.motors[5].temperature=90
        with self.assertRaisesRegex(RuntimeError,'80°C'):self.home()
        self.assertTrue(all(not m.commands for m in self.motors.values()))
    def test_motor_fault_is_not_a_valid_home_feedback(self):
        self.motors[3].status=8
        with self.assertRaisesRegex(RuntimeError,'故障状态'):self.home()
    def test_incomplete_hardware_mapping_rejected(self):
        del self.motors[7]
        with self.assertRaises(ValueError):self.home()
    def test_selected_zero_is_verified_persisted_and_never_enabled(self):
        result=adjust_zero(self.bus,self.motors,'set-zero',[7],sleep=self.clock.sleep)
        self.assertTrue(result['ok']);self.assertEqual(self.motors[7].zeros,1);self.assertEqual(self.motors[7].stores,1)
        self.assertTrue(all(m.enables==0 for m in self.motors.values()))
        self.assertTrue(all(m.zeros==0 for i,m in self.motors.items() if i!=7))
    def test_enabled_arm_cannot_be_rezeroed(self):
        self.motors[2].status=1
        with self.assertRaisesRegex(RuntimeError,'尚未全部失能'):adjust_zero(self.bus,self.motors,'set-zero',[7],sleep=self.clock.sleep)
        self.assertTrue(all(m.zeros==0 for m in self.motors.values()))
    def test_zero_flash_failure_is_not_success(self):
        def fail():raise TimeoutError('flash failed')
        self.motors[7].store_parameters=fail
        with self.assertRaisesRegex(RuntimeError,'未全部完成'):adjust_zero(self.bus,self.motors,'set-zero',[7],sleep=self.clock.sleep)
    def test_lost_write_ack_requires_live_matching_mode(self):
        motor=Motor(1)
        def fail(*a,**kw):raise RuntimeError('ensure_mode failed: register 10 write ack not received within 50ms')
        motor.ensure_mode=fail
        ensure_mode_checked(motor,2,sleep=self.clock.sleep)
        motor.mode=4
        with self.assertRaisesRegex(RuntimeError,'未允许使能'):ensure_mode_checked(motor,2,sleep=self.clock.sleep)
        self.assertEqual(motor.enables,0)


class GlobalControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'config.json'
        cfg=copy.deepcopy(main.DEFAULT_CONFIG);cfg['follower']['port']='/dev/qa-single'
        cfg['dual_arm']['left']['follower_port']='/dev/qa-left'
        cfg['dual_arm']['right']['follower_port']='/dev/qa-right'
        self.cfg=cfg;self.path.write_text(json.dumps(cfg))
        self.config=patch.object(main,'CONFIG_FILE',self.path);self.config.start()
        self.job=main.STATE.job;main.STATE.job=None
        self.flags=(main.ENV.connected,main.ENV.dual_connected,dict(main.ENV.dual_ports),main.DEVICES_RELEASED)
        main.DEVICES_RELEASED=False;self.client=TestClient(main.app)
        self.zero_jobs=dict(main._ZERO_JOBS)
    def tearDown(self):
        main.STATE.job=self.job
        main.ENV.connected,main.ENV.dual_connected,main.ENV.dual_ports,main.DEVICES_RELEASED=self.flags
        self.config.stop();self.temp.cleanup()
        main._ZERO_JOBS.clear();main._ZERO_JOBS.update(self.zero_jobs)
    def new_job(self):
        job=main.Job('calibration','calibrate-left-follower',[],'fake')
        job.stdin_live=True;job.proc=SimpleNamespace(stdin=io.BytesIO())
        main.STATE.job=job
        return job
    def test_old_modal_cannot_write_to_new_job(self):
        job=self.new_job()
        response=self.client.post('/api/job/input',json={'text':'c','job_id':'old-job'})
        self.assertEqual(response.status_code,409);self.assertEqual(job.proc.stdin.getvalue(),b'')
    def test_prompt_can_be_answered_once_even_with_duplicate_requests(self):
        job=self.new_job();job.awaiting_input=True;job.prompt_id=3
        data={'text':'','job_id':job.job_id,'prompt_id':3}
        with patch.object(main.HUB,'publish'):
            self.assertEqual(self.client.post('/api/job/input',json=data).status_code,200)
            self.assertEqual(self.client.post('/api/job/input',json=data).status_code,409)
        self.assertEqual(job.proc.stdin.getvalue(),b'\n')
    def test_identical_successive_prompts_have_distinct_ids(self):
        job=self.new_job()
        with patch.object(main.HUB,'publish'):
            main._note_prompt(job,'Press ENTER when ready:')
            first=job.prompt_id
            main.write_job_input('\n',job.job_id,first)
            main._note_prompt(job,'Press ENTER when ready:')
        self.assertEqual(job.prompt_id,first+1)
        self.assertEqual(main._job_progress(job)['job_id'],job.job_id)
    def test_y_n_prompt_without_enter_is_detected(self):
        job=self.new_job()
        with patch.object(main.HUB,'publish'):main._note_prompt(job,'Continue? [Y/n]')
        self.assertTrue(job.awaiting_input)
    def test_duplicate_finishers_publish_one_completion(self):
        job=self.new_job()
        with patch.object(main.HUB,'log') as log,patch.object(main.HUB,'state') as state,patch.object(main,'_log_failure_hints'):
            with ThreadPoolExecutor(8) as pool:
                results=list(pool.map(lambda _:main._finish_job_once(job,0),range(16)))
        self.assertEqual(sum(results),1);log.assert_called_once();state.assert_called_once()
        self.assertFalse(job.stdin_live);self.assertTrue(job.finished.is_set())
    def test_real_pipe_pump_and_watcher_close_once_without_bad_fd(self):
        job=main.Job('pipe test','test',[],'no hardware')
        job.proc=subprocess.Popen([sys.executable,'-u','-c',"print('ordinary output')"],
                                  stdout=subprocess.PIPE,stdin=subprocess.PIPE,stderr=subprocess.STDOUT)
        main.STATE.job=job
        with patch.object(main.HUB,'log') as log,patch.object(main.HUB,'state'),patch.object(main,'_log_failure_hints'):
            watcher=threading.Thread(target=main._watch_job_exit,args=(job,));watcher.start()
            main._pump_stdout(job);watcher.join(timeout=3)
        lines=[str(c.args[0]) for c in log.call_args_list]
        self.assertEqual(sum('结束，退出码' in line for line in lines),1)
        self.assertFalse(any('Bad file descriptor' in line for line in lines))
        self.assertTrue(job.proc.stdout.closed);self.assertTrue(job.proc.stdin.closed)
    def test_retired_pages_redirect_without_rendering_their_controls(self):
        for page in ('grasp','arm'):
            response=self.client.get('/p/'+page,follow_redirects=False)
            self.assertEqual(response.status_code,307)
            self.assertEqual(response.headers['location'],'/p/home')
    def test_home_selects_only_followers_and_respects_mode(self):
        self.assertEqual([a['port'] for a in main._home_targets(self.cfg)],['/dev/qa-single'])
        self.cfg['dual_arm']['enabled']=True
        self.assertEqual([a['port'] for a in main._home_targets(self.cfg)],['/dev/qa-left','/dev/qa-right'])
    def test_duplicate_follower_port_is_rejected(self):
        self.cfg['dual_arm']['enabled']=True
        self.cfg['dual_arm']['right']['follower_port']='/dev/qa-left'
        with self.assertRaises(main.ApiError):main._home_targets(self.cfg)
    def test_active_job_cannot_be_interrupted_by_home_button(self):
        with patch.object(main.STATE,'running',return_value=True),patch.object(main,'_spawn') as spawn:
            response=self.client.post('/api/devices/home-and-disable',json={'confirm':True})
        self.assertEqual(response.status_code,409);spawn.assert_not_called()
    def test_released_devices_cannot_be_reconnected_by_home(self):
        main.DEVICES_RELEASED=True
        with patch.object(main,'_spawn') as spawn:
            response=self.client.post('/api/devices/home-and-disable',json={'confirm':True})
        self.assertEqual(response.status_code,409);spawn.assert_not_called()
    def test_home_uses_job_process_and_invalidates_connection_state(self):
        with patch.object(main,'require_env'),patch.object(main.TELEMETRY,'stop_observers'),\
             patch.object(main,'_ARM_PROC',None),patch('backend.device_release.owners',return_value=[]),\
             patch.object(main,'_spawn',return_value={'ok':True,'name':'收工归零并失能'}) as spawn,\
             patch.object(main.HUB,'publish'):
            response=self.client.post('/api/devices/home-and-disable',json={'confirm':True})
        self.assertEqual(response.status_code,200,response.text)
        args=spawn.call_args.args;self.assertEqual(args[0],'home-and-disable')
        self.assertIn('shutdown_worker.py',args[2][-2]);self.assertNotIn('leader',args[2][-1])
        self.assertFalse(main.ENV.connected)
    def test_probe_understands_draccus_public_registry(self):
        fake=SimpleNamespace(get_known_choices=lambda:{'bi_actual_follower':object})
        self.assertIn('bi_actual_follower',names_from_registry(fake))
    def test_home_and_zero_write_require_explicit_confirmation(self):
        with patch.object(main,'_spawn') as spawn:
            self.assertEqual(self.client.post('/api/devices/home-and-disable',json={}).status_code,400)
            self.assertEqual(self.client.post('/api/devices/zero/start',json={'side':'left','action':'set-zero','joints':[7]}).status_code,400)
        spawn.assert_not_called()
    def test_passed_gripper_check_does_not_produce_false_failure_hint(self):
        job=main.Job('test','test',[],'test');job.exit_code=1
        job.tail.extend(['gripper zero-detect: -0.03 within [-270,0], passed','ensure_mode failed: register 10 write ack not received within 50ms'])
        with patch.object(main.HUB,'log') as log:main._log_failure_hints(job)
        lines=[str(c.args[0]) for c in log.call_args_list]
        self.assertFalse(any('夹爪零点或反馈校验未通过' in x for x in lines))
        self.assertTrue(any('寄存器 10 通信确认失败' in x for x in lines))
    def test_zero_capabilities_are_available_without_opening_devices(self):
        with patch.object(main,'_popen') as popen,patch.object(main.TELEMETRY,'stop_observers') as stop:
            response=self.client.get('/api/devices/zero/capabilities')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['api_version'],1)
        self.assertEqual(set(response.json()['actions']),{'read','release','set-zero'})
        self.assertTrue(response.json()['worker_available']);popen.assert_not_called();stop.assert_not_called()
    def test_zero_http_start_to_result_uses_a_real_safe_process(self):
        result={'ok':True,'side':'left','port':'/dev/qa-single','timestamp':time.time(),
                'joints':[{'id':i,'degrees':i/10,'disabled':True} for i in range(1,8)]}
        code='print('+repr('REBOT_ZERO_RESULT '+json.dumps(result))+', flush=True)'
        def safe_process(*args,**kwargs):
            return subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        spawned=None
        try:
            with patch.object(main,'require_env'),patch.object(main.TELEMETRY,'stop_observers'),\
                 patch.object(main,'_ARM_PROC',None),patch('backend.device_release.owners',return_value=[]),\
                 patch.object(main,'_popen',side_effect=safe_process),patch.object(main.HUB,'publish'):
                start=self.client.post('/api/devices/zero/start',json={'action':'read','side':'left','joints':[7]})
                self.assertEqual(start.status_code,200,start.text)
                job_id=start.json()['job_id'];self.assertTrue(job_id)
                spawned=main._ZERO_JOBS[job_id]
                deadline=time.monotonic()+6
                while time.monotonic()<deadline:
                    response=self.client.get('/api/devices/zero/result',params={'job_id':job_id})
                    self.assertEqual(response.status_code,200,response.text)
                    if response.json()['finished']:break
                    time.sleep(.03)
                else:self.fail('safe zero fixture did not finish')
                data=response.json();self.assertEqual(data['exit_code'],0);self.assertEqual(data['result'],result)
                self.assertTrue(all(x['disabled'] for x in data['result']['joints']))
        finally:
            if spawned:
                if spawned.proc.poll() is None:spawned.proc.kill();spawned.proc.wait()
                spawned.thread.join(timeout=3)


if __name__=='__main__':unittest.main()
