import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import backend.main as main
from backend.training import prepare_finetune, training_mode


class FineTuneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model = self.root / 'old/checkpoints/last/pretrained_model'
        self.model.mkdir(parents=True)
        self.data = self.root / 'new-data'
        (self.data / 'meta').mkdir(parents=True)
        self.policy = {'type': 'act', 'input_features': {
            'observation.state': {'type': 'STATE', 'shape': [14]},
            'observation.images.top': {'type': 'VISUAL', 'shape': [3, 480, 640]}},
            'output_features': {'action': {'type': 'ACTION', 'shape': [14]}}}
        self.features = {'action': {'shape': [14]}, 'observation.state': {'shape': [14]},
                         'observation.images.top': {'dtype': 'video', 'shape': [480, 640, 3]}}
        (self.model / 'config.json').write_text(json.dumps(self.policy))
        (self.model / 'model.safetensors').write_bytes(b'test-only-weight-placeholder')
        self.write_features()
        self.params = dict(training_mode='finetune', policy_type='act', base_model=str(self.model),
                           dataset_dir=str(self.data), repo_id='demo/new', steps=2000,
                           learning_rate=1e-6, job_name='flip_box_finetune', output_dir=str(self.root / 'old'))
        self.config_path = self.root / 'web-config.json'
        cfg = copy.deepcopy(main.DEFAULT_CONFIG)
        cfg['train'].update(self.params)
        cfg['dual_arm']['enabled'] = False
        self.config_path.write_text(json.dumps(cfg))
        self.patch = patch.object(main, 'CONFIG_FILE', self.config_path)
        self.patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def write_features(self):
        (self.data / 'meta/info.json').write_text(json.dumps({'features': self.features}))

    def command(self, **changes):
        cfg = main.load_config()
        cfg['train'].update(changes)
        with patch.object(main, '_lerobot', return_value=['lerobot-train']):
            return main.build_command('train', cfg)

    def test_finetune_loads_weights_without_replacing_act_architecture_or_resuming_optimizer(self):
        cmd = self.command()
        self.assertIn('--policy.path=' + str(self.model.resolve()), cmd)
        self.assertIn('--dataset.root=' + str(self.data), cmd)
        self.assertIn('--steps=2000', cmd)
        self.assertIn('--policy.optimizer_lr=1e-06', cmd)
        self.assertIn('--policy.optimizer_lr_backbone=1e-06', cmd)
        self.assertFalse(any(x.startswith(('--policy.type=', '--policy.input_features=', '--policy.output_features=', '--resume=', '--config_path=')) for x in cmd))

    def test_scratch_ignores_previously_selected_finetune_weights(self):
        cmd = self.command(training_mode='scratch')
        self.assertIn('--policy.type=act', cmd)
        self.assertFalse(any(x.startswith('--policy.path=') for x in cmd))

    def test_legacy_base_model_still_means_finetune(self):
        self.assertEqual(training_mode({'base_model': str(self.model)}), 'finetune')
        self.assertEqual(training_mode({}), 'scratch')

    def test_run_or_step_directory_resolves_to_existing_policy(self):
        for source in [self.root / 'old', self.model.parent, self.model / 'config.json']:
            params = dict(self.params, base_model=str(source))
            resolved, cfg = prepare_finetune(params, self.root, self.root)
            self.assertEqual(resolved, str(self.model.resolve()))
            self.assertEqual(cfg['input_features'], self.policy['input_features'])

    def test_rejects_single_arm_state_or_action_and_camera_schema_changes(self):
        for key, value in [('action', {'shape': [7]}), ('observation.state', {'shape': [7]}),
                           ('observation.images.top', {'shape': [720, 1280, 3]})]:
            with self.subTest(key=key):
                before = copy.deepcopy(self.features)
                self.features[key] = value
                self.write_features()
                with self.assertRaisesRegex(ValueError, '微调特征不匹配'):
                    prepare_finetune(self.params, self.root, self.root)
                self.features = before
        self.features['observation.images.wrist'] = self.features.pop('observation.images.top')
        self.write_features()
        with self.assertRaisesRegex(ValueError, '相机名称或数量不一致'):
            prepare_finetune(self.params, self.root, self.root)

    def test_inspection_rejects_missing_weights_and_wrong_policy(self):
        response = self.client.post('/api/train/finetune/inspect', json=self.params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['output_features']['action']['shape'], [14])
        self.assertEqual(self.client.post('/api/train/finetune/inspect', json=dict(self.params, policy_type='diffusion')).status_code, 400)
        (self.model / 'model.safetensors').unlink()
        self.assertEqual(self.client.post('/api/train/finetune/inspect', json=self.params).status_code, 400)

    def test_new_run_preserves_old_weights_and_does_not_reuse_existing_output(self):
        before = (self.model / 'model.safetensors').read_bytes()
        with patch.object(main, 'require_env'), patch.object(main.STATE, 'running', return_value=False), \
                patch.object(main, '_lerobot', return_value=['lerobot-train']), \
                patch.object(main, '_spawn', return_value={'ok': True, 'name': 'test'}) as spawn, \
                patch.object(main, '_hf_probe') as probe:
            response = self.client.post('/api/train/start', json={'backend': 'lerobot', 'params': self.params})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertNotEqual(result['output_dir'], self.params['output_dir'])
        self.assertFalse(Path(result['output_dir']).exists())
        self.assertIn('--output_dir=' + result['output_dir'], spawn.call_args.args[2])
        self.assertEqual(spawn.call_args.kwargs['env_extra']['HF_HUB_OFFLINE'], '1')
        self.assertEqual((self.model / 'model.safetensors').read_bytes(), before)
        probe.assert_not_called()

    def test_bad_source_or_busy_job_never_spawns_training(self):
        with patch.object(main, 'require_env'), patch.object(main.STATE, 'running', return_value=False), patch.object(main, '_spawn') as spawn:
            response = self.client.post('/api/train/start', json={'backend': 'lerobot', 'params': dict(self.params, base_model='/nonexistent/model')})
            self.assertEqual(response.status_code, 400)
            spawn.assert_not_called()
        with patch.object(main, 'require_env'), patch.object(main.STATE, 'running', return_value=True), patch.object(main, '_spawn') as spawn:
            before = self.config_path.read_bytes()
            self.assertEqual(self.client.post('/api/train/start', json={'backend': 'lerobot', 'params': self.params}).status_code, 409)
            self.assertEqual(self.config_path.read_bytes(), before)
            spawn.assert_not_called()

    def test_nonfinite_or_negative_learning_rate_is_rejected(self):
        for lr in [float('nan'), float('inf'), -1, 'bad']:
            with self.subTest(lr=lr), self.assertRaises(main.ApiError):
                self.command(learning_rate=lr)


if __name__ == '__main__':
    unittest.main()
