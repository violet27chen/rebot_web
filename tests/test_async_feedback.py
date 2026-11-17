"""Reproduce slow following and stalled arms without physical device access."""
import copy
import os
import time
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock

import numpy as np

from backend.async_action_buffer import AsyncActionError, FeedbackProgressWatchdog, limit_action


def delayed_motion_actor(connection, lock, specs, model, device, amp, options, chunk):
    size = chunk or 12
    connection.send({'type': 'ready', 'pid': os.getpid(), 'chunk_size': size, 'policy_type': 'smolvla'})
    while True:
        request = connection.recv()
        if request['type'] == 'close':
            break
        time.sleep(.02)
        actions = np.zeros((size, 2), np.float32)
        actions[size // 2:, 0] = 20
        connection.send({**request, 'type': 'chunk', 'actions': actions, 'inference_s': .02})
    connection.close()


class FeedbackTests(unittest.TestCase):
    def test_slow_arm_continues_instead_of_accumulating_a_30_degree_error(self):
        measured = np.array([0.])
        previous = measured.copy()
        watchdog = FeedbackProgressWatchdog()
        names = ['right_elbow_flex.pos']
        for tick in range(240):
            status = {}
            command = limit_action([80], measured, previous, [1], max_step=2,
                                   max_lead=6, joint_names=names, diagnostics=status)
            self.assertLessEqual(abs(command[0] - measured[0]), 6.00001)
            self.assertLessEqual(abs(command[0] - previous[0]), 2.00001)
            watchdog.update(command, measured, [1], names, tick / 30)
            measured += np.clip(command - measured, -.35, .35)
            previous = command
        self.assertGreater(measured[0], 79)

    def test_blocked_arm_stays_bounded_and_still_trips_the_progress_watchdog(self):
        previous = [0.]
        watchdog = FeedbackProgressWatchdog()
        fault = None
        for tick in range(100):
            command = limit_action([80], [0], previous, [1], max_lead=6,
                                   joint_names=['left_elbow_flex.pos'])
            self.assertLessEqual(float(command[0]), 6.00001)
            try:
                watchdog.update(command, [0], [1], ['left_elbow_flex.pos'], tick / 30)
            except AsyncActionError as error:
                fault = str(error)
                break
            previous = command
        self.assertIn('跟随停滞', fault or '')

    def test_both_arm_steps_use_the_same_fraction_when_one_arm_lags(self):
        value = limit_action([80, 80], [0, 0], [5, 1], [1, 1], max_lead=6,
                             joint_names=['left_elbow_flex.pos', 'right_elbow_flex.pos'])
        np.testing.assert_allclose(value, [6, 2])

    def test_gripper_contact_does_not_freeze_the_two_arms(self):
        names = ['left_shoulder_pan.pos', 'right_gripper.pos']
        value = limit_action([80, 45], [0, 0], [0, 1], [1, -6], max_lead=6, joint_names=names)
        np.testing.assert_allclose(value, [2, 1])
        watchdog = FeedbackProgressWatchdog()
        for stamp in [0., 1., 10.]:
            watchdog.update(value, [2, 0], [1, -6], names, stamp)

    def test_leash_does_not_hide_preexisting_tracking_loss_or_remove_slew_on_retreat(self):
        with self.assertRaisesRegex(AsyncActionError, '上一命令已失跟'):
            limit_action([80], [0], [31], [1], max_lead=6)
        value = limit_action([0], [0], [12], [1], max_lead=6)
        np.testing.assert_allclose(value, [10])

    def test_encoder_direction_and_gripper_ratio_are_applied_in_motor_space(self):
        value = limit_action([45], [0], [1], [-6], max_lead=6, joint_names=['gripper.pos'])
        self.assertAlmostEqual(float(value[0]), 1.)
        with self.assertRaisesRegex(AsyncActionError, '物理限位'):
            limit_action([60], [0], [0], [-6], max_lead=6,
                         joint_names=['gripper.pos'], motor_limits=[[-270, 0]])


class LaunchSnapshotTests(unittest.TestCase):
    def test_spawn_uses_snapshot_even_if_autosave_file_has_changed(self):
        from backend import main
        cfg = copy.deepcopy(main.DEFAULT_CONFIG)
        cfg['eval'].update(async_inference=True, n_action_steps=16, async_chunk_size=32)
        proc = NS(stdin=None)
        with patch.object(main.STATE, 'job', None), patch.object(main.STATE, 'running', return_value=False), \
             patch.object(main, 'load_config', side_effect=AssertionError('must not reread autosave')), \
             patch.object(main, '_popen', return_value=proc) as popen, \
             patch.object(main, 'read_policy', return_value={'type':'smolvla','chunk_size':50,'n_action_steps':50}), \
             patch.object(main.threading.Thread, 'start'), patch.object(main.HUB, 'log'), \
             patch.object(main.HUB, 'state'), patch.object(main.TELEMETRY, 'stop_observers'), \
             patch.object(main.TELEMETRY, 'environment', return_value={}), patch.object(main, '_ARM_PROC', None):
            result = main._spawn('eval', 'snapshot test', ['python', 'safe_record.py'], config_snapshot=cfg)
            self.assertEqual(popen.call_args.args[2]['REBOT_ASYNC_ACT'], '1')
            self.assertEqual(popen.call_args.args[2]['REBOT_ASYNC_CHUNK'], '16')
            self.assertEqual(result['inference_mode'], 'async_policy')
            self.assertEqual(main.STATE.job.inference_mode, 'async_policy')

    def test_eval_request_preserves_clicked_mode_over_an_older_autosave(self):
        from backend import main
        cfg = copy.deepcopy(main.DEFAULT_CONFIG)
        cfg['eval']['async_inference'] = False
        with patch.object(main, 'load_config', return_value=cfg), patch.object(main, 'start_job', return_value={}) as start:
            main.api_eval({'eval': {'async_inference': True}})
        self.assertTrue(start.call_args.args[2]['eval']['async_inference'])
        self.assertFalse(cfg['eval']['async_inference'])


class SmolVLAAsyncTests(unittest.TestCase):
    def test_web_accepts_smolvla_without_act_only_attributes(self):
        from backend import main
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            config.write_text(json.dumps({'type': 'smolvla', 'chunk_size': 50, 'n_action_steps': 50}))
            main.validate_async_eval({'policy_path': folder, 'async_chunk_size': 0})
            config.write_text(json.dumps({'type': 'wall_x'}))
            with self.assertRaises(main.ApiError):
                main.validate_async_eval({'policy_path': folder})

    def test_record_hook_takes_over_smolvla_instead_of_calling_synchronous_factory(self):
        from backend.async_record_hook import install
        module = NS(make_policy=Mock(side_effect=AssertionError('synchronous fallback')),
                    make_pre_post_processors=Mock(side_effect=AssertionError('synchronous processor')),
                    predict_action=Mock(), record_loop=Mock())
        close = install(module)
        self.addCleanup(close)
        cfg = NS(type='smolvla', device='cpu', use_amp=False, pretrained_path='/unused',
                 output_features={'action': NS(shape=(2,))})
        features = {'action': {'names': ['shoulder_pan.pos', 'gripper.pos'], 'shape': (2,)}}
        policy = module.make_policy(cfg, ds_meta=NS(features=features))
        self.assertEqual(policy.policy_type, 'smolvla')
        with patch.object(policy, 'start') as start:
            module.make_pre_post_processors(cfg, pretrained_path='/unused')
            start.assert_called_once_with({})

    def test_prefetch_cannot_keep_restarting_the_stationary_prefix(self):
        from backend.async_act_runtime import AsyncACTPolicy
        from test_async_act import robot
        cfg = NS(type='smolvla', device='cpu', use_amp=False, pretrained_path='/unused',
                 output_features={'action': NS(shape=(2,))})
        features = {'action': {'names': ['shoulder_pan.pos', 'gripper.pos'], 'shape': (2,)},
                    'observation.state': {'dtype': 'float32', 'shape': (2,)}}
        policy = AsyncACTPolicy(cfg, features, {'chunk_size': 12}, actor_target=delayed_motion_actor)
        self.addCleanup(policy.close)
        policy.start({}, startup_timeout=15)
        policy.attach(robot(), {}, 30)
        policy.reset()
        values = []
        for _ in range(25):
            values.append(float(policy.predict({'observation.state': np.zeros(2, np.float32)})[0, 0]))
            time.sleep(1 / 30)
        self.assertFalse(policy.failed)
        self.assertGreater(max(values), 0., 'the moving suffix must eventually be executed')


if __name__ == '__main__':
    unittest.main()
