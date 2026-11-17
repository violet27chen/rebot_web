"""Exercise scheduling and real process IPC with simulated feedback, no devices."""
import math
import os
import time
import unittest
from multiprocessing import shared_memory
from types import SimpleNamespace as NS
import numpy as np
from backend.async_action_buffer import ActionBuffer, AsyncActionError, limit_action
from backend.async_act_runtime import AsyncACTPolicy, hold_feedback


def delayed_actor(connection, lock, specs, path, device, use_amp, options, chunk):
    connection.send({'type':'ready', 'pid':os.getpid(), 'chunk_size':chunk})
    while True:
        message = connection.recv()
        if message['type'] == 'close': break
        # Read the same shared-memory observation transport as the GPU actor.
        with lock:
            memory = shared_memory.SharedMemory(name=specs['observation.state']['memory'])
            state = np.ndarray((2,), np.float32, buffer=memory.buf).copy()
            memory.close()
        time.sleep(options.get('delay', .08))
        connection.send({**message, 'type':'chunk', 'inference_s':.08,
                         'actions':np.tile(state + .1, (chunk, 1))})
    connection.close()


def robot():
    return NS(config=NS(joint_directions={'shoulder_pan':-1, 'gripper':-6}),
              cameras={}, motors={key:NS(get_state=lambda:NS(pos=0.)) for key in ('shoulder_pan','gripper')})


class BufferTests(unittest.TestCase):
    def setUp(self): self.buffer = ActionBuffer(2, 30)
    def chunk(self, start=0, request=1, value=1, **extra):
        return dict(generation=self.buffer.generation, request_id=request,
                    start_tick=start, observed_at=10, actions=np.full((10,2), value,dtype=np.float32), **extra)
    def test_latency_drops_past_actions_and_overlap_is_time_aligned(self):
        b=self.buffer
        self.assertTrue(b.put(self.chunk(),10.1,3))
        self.assertEqual(b.remaining(3),7)
        np.testing.assert_allclose(b.take(3),[1,1])
        self.assertTrue(b.put(self.chunk(start=2,request=2,value=2),10.2,4))
        np.testing.assert_allclose(b.take(4),[1.2,1.2])
        self.assertIsNone(b.take(4))
        self.assertEqual(b.remaining(5),7)
    def test_previous_episode_stale_and_duplicate_chunks_are_rejected(self):
        old=self.chunk();self.buffer.reset()
        self.assertFalse(self.buffer.put(old,10.1,0))
        new=self.chunk();self.assertFalse(self.buffer.put(new,12,0))
        self.assertTrue(self.buffer.put(new,10.1,0))
        self.assertFalse(self.buffer.put(new,10.2,0))
        self.assertEqual(self.buffer.stale_chunks,1)
    def test_invalid_predictions_and_jump_are_rejected(self):
        bad=self.chunk();bad['actions'][0,0]=float('nan')
        with self.assertRaises(AsyncActionError):self.buffer.put(bad,10.1,0)
        with self.assertRaises(AsyncActionError):limit_action([50],[0],[40],[-1])
        with self.assertRaises(AsyncActionError):limit_action([0],[0],[0],[0])
    def test_slew_limits_apply_in_motor_space_including_gripper(self):
        result=limit_action([10,4],[0,0],[0,0],[-1,-6])
        np.testing.assert_allclose(result,[2,1/3],atol=1e-6)
    def test_raw_feedback_is_inverted_and_dual_ports_are_not_swapped(self):
        left,right=robot(),robot()
        left.motors['shoulder_pan'].get_state=lambda:NS(pos=math.radians(12))
        right.motors['gripper'].get_state=lambda:NS(pos=math.radians(-60))
        measured,dirs=hold_feedback(NS(left_arm=left,right_arm=right),['left_shoulder_pan.pos','right_gripper.pos'])
        np.testing.assert_allclose(measured,[-12,10]);np.testing.assert_allclose(dirs,[-1,-6])
        right.motors['gripper'].get_state=lambda:None
        with self.assertRaises(AsyncActionError):hold_feedback(right,['gripper.pos'])


class ProcessTests(unittest.TestCase):
    def policy(self, delay=.08, timeout=1):
        config=NS(device='cpu',use_amp=False,pretrained_path='/unused',output_features={'action':NS(shape=(2,))})
        features={'action':{'names':['shoulder_pan.pos','gripper.pos'],'shape':(2,)},
                  'observation.state':{'dtype':'float32','shape':(2,)}}
        policy=AsyncACTPolicy(config,features,{'chunk_size':12,'wait_timeout_s':timeout},actor_target=delayed_actor)
        self.addCleanup(policy.close)
        policy.start({'delay':delay},startup_timeout=15)
        policy.attach(robot(),{'exit_early':False,'stop_recording':False},30);policy.reset()
        return policy
    def test_inference_runs_separately_and_control_does_not_wait_for_chunk(self):
        import torch  # Exclude first torch import from scheduling measurements.
        p=self.policy();names=[m.name for m in p.memories]
        self.assertNotEqual(p.process.pid,os.getpid())
        waits=[];durations=[];actions=[]
        for _ in range(25):
            start=time.monotonic();actions.append(p.predict({'observation.state':np.zeros(2,np.float32)}))
            durations.append(time.monotonic()-start);waits.append(p.waiting_for_prediction);time.sleep(1/30)
        self.assertFalse(p.failed)
        self.assertTrue(any(waits[:5]));self.assertTrue(any(not w for w in waits[5:]))
        self.assertFalse(any(waits[10:]),'prefetch must keep the buffer populated after startup')
        self.assertLess(max(durations),.07,'80ms model computation must not block control')
        self.assertGreater(p.sequence,2)
        p.close()
        for name in names:
            with self.assertRaises(FileNotFoundError):shared_memory.SharedMemory(name=name)
    def test_timeout_requests_normal_stop_without_sending_zero(self):
        p=self.policy(delay=1,timeout=.1)
        # Keep a nonzero command so a failure cannot be confused with zeroing.
        p.robot.motors['shoulder_pan'].get_state=lambda:NS(pos=math.radians(-5))
        p.predict({'observation.state':np.zeros(2,np.float32)});time.sleep(.12)
        result=p.predict({'observation.state':np.zeros(2,np.float32)})
        self.assertTrue(p.failed);self.assertTrue(p.events['stop_recording'])
        self.assertEqual(result[0,0],5)


if __name__=='__main__':unittest.main()
