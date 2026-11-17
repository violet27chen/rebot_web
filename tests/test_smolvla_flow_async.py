"""SmolVLA native flow sampler, action horizon overrides and ready telemetry."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from backend.act_inference_process import configure_execution_steps, configure_flow_sampling


class FlowMatchingTests(unittest.TestCase):
    def test_sampling_iterations_do_not_change_model_or_execution_horizon(self):
        cfg = NS(type='smolvla', num_steps=10, chunk_size=50, n_action_steps=32)
        configure_flow_sampling(cfg, 5)
        self.assertEqual((cfg.num_steps, cfg.chunk_size, cfg.n_action_steps), (5, 50, 32))
        configure_flow_sampling(cfg, 0)
        self.assertEqual(cfg.num_steps, 5)
        with self.assertRaises(ValueError):
            configure_flow_sampling(NS(type='act'), 5)
        for value in [-1, 51, 1.5, float('nan')]:
            with self.assertRaises(ValueError):
                configure_flow_sampling(cfg, value)

    def test_flow_and_chunk_overrides_apply_to_sync_and_async_launches(self):
        from backend import main
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'config.json').write_text(json.dumps(
                {'type': 'smolvla', 'num_steps': 10, 'chunk_size': 50, 'n_action_steps': 50}))
            for asynchronous in [False, True]:
                cfg = copy.deepcopy(main.DEFAULT_CONFIG)
                cfg['eval'].update(policy_path=folder, async_inference=asynchronous,
                                   flow_matching_steps=5, async_chunk_size=32)
                with patch.object(main, 'robot_base_args', return_value=[]), \
                     patch.object(main, 'eval_policy_cameras', return_value=[]), \
                     patch.object(main, '_safe_record', return_value=['python', 'safe_record.py']):
                    command = main.build_command('eval', cfg)
                self.assertIn('--policy.num_steps=5', command)
                self.assertIn('--policy.n_action_steps=32', command)
            self.assertEqual(main.eval_action_chunk_steps({'policy_path': folder, 'async_chunk_size': 100}), 50)

    def test_explicit_n_action_steps_takes_precedence_and_can_reset_to_auto(self):
        from backend import main
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'config.json').write_text(json.dumps(
                {'type': 'smolvla', 'chunk_size': 50, 'n_action_steps': 8}))
            for asynchronous in [False, True]:
                cfg = copy.deepcopy(main.DEFAULT_CONFIG)
                cfg['eval'].update(policy_path=folder, async_inference=asynchronous,
                                   n_action_steps=16, async_chunk_size=32)
                with patch.object(main, 'robot_base_args', return_value=[]), \
                     patch.object(main, 'eval_policy_cameras', return_value=[]), \
                     patch.object(main, '_safe_record', return_value=['python', 'safe_record.py']):
                    command = main.build_command('eval', cfg)
                self.assertIn('--policy.n_action_steps=16', command)
                self.assertNotIn('--policy.n_action_steps=32', command)
            settings = dict(policy_path=folder, n_action_steps=0, async_chunk_size=32)
            self.assertEqual(main.eval_action_chunk_steps(settings), 0)
            settings['n_action_steps'] = 100
            self.assertEqual(main.eval_action_chunk_steps(settings), 50)
            for value in [-1, 1001, .5, float('nan'), 'bad']:
                settings['n_action_steps'] = value
                with self.assertRaises(main.ApiError):
                    main.eval_action_chunk_steps(settings)

    def test_actor_execution_override_preserves_chunk_size_and_native_auto(self):
        cfg = NS(chunk_size=50, n_action_steps=8)
        configure_execution_steps(cfg, 0)
        self.assertEqual(cfg.n_action_steps, 8)
        configure_execution_steps(cfg, 16)
        self.assertEqual((cfg.n_action_steps, cfg.chunk_size), (16, 50))
        configure_execution_steps(cfg, 100)
        self.assertEqual((cfg.n_action_steps, cfg.chunk_size), (50, 50))
        for value in [-1, 1001, .5, float('nan')]:
            with self.assertRaises(ValueError):
                configure_execution_steps(cfg, value)

    def test_runtime_forwards_parent_cli_override_when_actor_reloads_checkpoint(self):
        from backend.async_act_runtime import AsyncACTPolicy
        cfg = NS(type='smolvla', device='cpu', use_amp=False, num_steps=10,
                 pretrained_path='/unused', chunk_size=50, n_action_steps=16,
                 output_features={'action': NS(shape=(2,))})
        features = {'action': {'names': ['shoulder_pan.pos', 'gripper.pos'], 'shape': (2,)}}
        policy = AsyncACTPolicy(cfg, features, {'chunk_size': 0})
        self.assertEqual(policy.chunk_size, 16)
        policy = AsyncACTPolicy(cfg, features, {'n_action_steps': 8, 'chunk_size': 32})
        self.assertEqual(policy.chunk_size, 8)

    def test_default_keeps_checkpoint_sampler_and_invalid_values_are_rejected(self):
        from backend import main
        self.assertEqual(main.eval_flow_matching_steps({}), 0)
        for value in [-1, 51, .5, float('inf'), 'bad']:
            with self.assertRaises(main.ApiError):
                main.eval_flow_matching_steps({'flow_matching_steps': value})
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'config.json').write_text(json.dumps({'type': 'act'}))
            with self.assertRaisesRegex(main.ApiError, 'ACT请设0'):
                main.eval_flow_matching_steps({'flow_matching_steps': 5, 'policy_path': folder})

    def test_ready_message_updates_actual_runtime_details(self):
        from backend import main
        job = main.Job(name='SmolVLA', kind='eval', command=[], display='')
        details = dict(policy_type='smolvla', model_chunk_size=50, action_chunk_steps=32,
                       n_action_steps=32, flow_matching_steps=5, inference_pid=1234)
        with patch.object(main, 'publish_progress') as progress:
            main._record_line(job, 'REBOT_INFERENCE_READY ' + json.dumps(details))
            self.assertEqual(job.inference_details, details)
            progress.assert_called_once_with(job, 'policy-ready')
        self.assertEqual(main._job_progress(job)['inference_details'], details)

    def test_standalone_settings_forward_sampler_and_execution_horizon(self):
        from sorting_operator.run_sorting import inference_settings
        settings = inference_settings({'inference': {'chunk_size': 32, 'flow_matching_steps': 5}})
        self.assertEqual((settings['chunk_size'], settings['flow_matching_steps']), (32, 5))
        self.assertEqual(inference_settings({})['flow_matching_steps'], 0)
        self.assertEqual(inference_settings({'inference': {'n_action_steps': 16, 'chunk_size': 32}})['chunk_size'], 16)
        self.assertEqual(inference_settings({'inference': {'n_action_steps': 0, 'chunk_size': 32}})['chunk_size'], 0)
        for invalid in [None, {'chunk_size': -1}, {'chunk_size': 1.5}, {'flow_matching_steps': 51}]:
            with self.assertRaises(ValueError):
                inference_settings({'inference': invalid})


if __name__ == '__main__':
    unittest.main()
